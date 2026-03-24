---
description: "Deploy Zeroboot on EC2 nested virtualization and benchmark sub-millisecond microVM sandbox startup for AI agent code execution."
---
# 在 EC2 嵌套虚拟化上部署 Zeroboot：亚毫秒级 VM Sandbox Benchmark 实测

!!! info "Lab 信息"
    - **难度**: ⭐⭐⭐ 高级
    - **预估时间**: 60 分钟
    - **预估费用**: ~$0.19（c8i.xlarge × 1hr）
    - **Region**: ap-southeast-1
    - **最后验证**: 2026-03-23

## 背景

### AI Agent 为什么需要快速 VM Sandbox

AI Agent 需要频繁执行用户生成的代码。安全性要求每次执行都在隔离环境中进行，但传统方案启动太慢：

| 方案 | 启动延迟 (p50) | 每 Sandbox 内存 | 隔离级别 |
|------|---------------|-----------------|---------|
| E2B | ~150ms | ~128MB | microVM |
| microsandbox | ~200ms | ~50MB | microVM |
| Daytona | ~27ms | ~50MB | Container |
| **zeroboot** | **0.79ms** | **~265KB** | **KVM VM** |

当 Agent 需要在一次对话中并行执行数十个代码片段时，150ms 的启动延迟会严重拖慢响应速度。zeroboot 把这个数字压到了亚毫秒级。

### zeroboot 是什么

[zeroboot](https://github.com/zerobootdev/zeroboot) 是一个基于 Firecracker 快照的 VM fork 引擎。核心思路：

1. **一次性创建 Template**：启动 Firecracker VM → 预加载 Python/numpy 等依赖 → 拍快照（内存镜像 + CPU 状态）
2. **每次请求 fork**：用 `mmap(MAP_PRIVATE)` 对快照内存做 Copy-on-Write 映射 → 创建新 KVM VM → 恢复 CPU 状态 → 从快照断点处继续执行

关键在于 `MAP_PRIVATE | MAP_NORESERVE`：256MB 的 guest 内存映射瞬间完成（< 1µs），实际物理内存只在 guest 写入时按需分配（CoW page fault），所以每个 sandbox 的 RSS 只有 ~265KB。

### 为什么在 EC2 嵌套虚拟化上跑

zeroboot 需要 KVM（`/dev/kvm`），传统做法是租用 bare metal 实例。但成本差距巨大：

| 实例类型 | 规格 | 按需价格 | 适用场景 |
|---------|------|---------|---------|
| c7i.metal-24xl | 96 vCPU, 192GB | **$4.608/hr** | 生产环境 |
| c8i.xlarge | 4 vCPU, 8GB | **$0.192/hr** | 开发测试/Benchmark |

**成本差 24 倍**。EC2 从 2024 年开始支持[嵌套虚拟化](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html)（Intel 实例），让我们可以在普通虚拟实例上运行 KVM。本文验证：嵌套虚拟化下 zeroboot 的性能是否依然成立。

## 前置条件

- AWS 账号（需要 EC2 启动权限）
- AWS CLI v2 已配置
- SSH 客户端
- 基本 Linux 和虚拟化概念

## 核心概念

### zeroboot 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│  Template 创建（一次性，~15s）                               │
│                                                             │
│  Firecracker VM 启动 → Python import numpy → 拍快照         │
│       ↓                        ↓                            │
│  snapshot/mem (256MB)    snapshot/vmstate (CPU 状态)         │
│       ↓                                                     │
│  载入 memfd（匿名内存文件，CoW 源）                          │
└─────────────────────────────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Fork A     │  │   Fork B     │  │   Fork C     │
│  (~0.8ms)    │  │  (~0.8ms)    │  │  (~0.8ms)    │
│              │  │              │  │              │
│ mmap CoW     │  │ mmap CoW     │  │ mmap CoW     │
│ 共享快照内存 │  │ 共享快照内存  │  │ 共享快照内存  │
│ 写时复制隔离 │  │ 写时复制隔离  │  │ 写时复制隔离  │
│ ~265KB RSS   │  │ ~265KB RSS   │  │ ~265KB RSS   │
└──────────────┘  └──────────────┘  └──────────────┘
     各 fork 之间内存完全隔离（KVM 硬件级）
```

**每次 fork 的详细流程**（耗时 ~0.8ms）：

1. `KVM_CREATE_VM` — 创建新虚拟机（~2µs）
2. 恢复 IOAPIC redirect table — 中断路由
3. `mmap(MAP_PRIVATE, MAP_NORESERVE, fd=memfd)` — CoW 内存映射（< 1µs）
4. `set_user_memory_region` — 注册为 guest 物理内存
5. 恢复 CPU 状态（**严格顺序**）：`CPUID → sregs → XCRS → XSAVE → regs → LAPIC → MSRs → MP_STATE`
6. vCPU 从快照断点处恢复执行

### 嵌套虚拟化架构

在 EC2 上运行 zeroboot 涉及多层虚拟化：

```
L0: AWS Nitro Hypervisor（硬件层）
  └─ L1: EC2 实例（c8i.xlarge, NestedVirtualization=enabled）
       └─ L2: KVM / Firecracker（Template VM + fork VMs）
            └─ L3: zeroboot fork（guest 执行用户代码）
```

嵌套虚拟化带来的挑战：L1 的 KVM 对 L2 VM 的 CPUID 和扩展寄存器（XCR0）有限制，这是本文踩坑的主要来源。

## 动手实践

### Step 1: 启动 EC2 嵌套虚拟化实例

首先创建 Key Pair 和安全组：

```bash
# 创建 Key Pair
aws ec2 create-key-pair \
  --key-name zeroboot-benchmark-key \
  --key-type ed25519 \
  --query 'KeyMaterial' \
  --output text \
  --region ap-southeast-1 > /tmp/zeroboot-benchmark-key.pem

chmod 400 /tmp/zeroboot-benchmark-key.pem

# 创建安全组（允许 SSH）
SG_ID=$(aws ec2 create-security-group \
  --group-name zeroboot-benchmark-sg \
  --description "Zeroboot benchmark - SSH only" \
  --region ap-southeast-1 \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 \
  --cidr 0.0.0.0/0 \
  --region ap-southeast-1
```

启动实例，**关键参数是 `--cpu-options NestedVirtualization=enabled`**：

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0659642169bf1b4b2 \
  --instance-type c8i.xlarge \
  --key-name zeroboot-benchmark-key \
  --security-group-ids $SG_ID \
  --cpu-options NestedVirtualization=enabled \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=zeroboot-benchmark}]' \
  --region ap-southeast-1 \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance ID: $INSTANCE_ID"

# 等待实例运行
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region ap-southeast-1

# 获取 Public IP
PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --region ap-southeast-1 \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo "Public IP: $PUBLIC_IP"
```

!!! warning "常见错误：忘记启用嵌套虚拟化"
    如果没有加 `--cpu-options NestedVirtualization=enabled`，`/dev/kvm` 不会出现。这个参数**只能在启动时设置**，无法后续修改——只能 terminate 重新启动。

SSH 连接并确认 KVM 可用：

```bash
ssh -i /tmp/zeroboot-benchmark-key.pem ubuntu@$PUBLIC_IP

# 确认 /dev/kvm 存在
ls -la /dev/kvm
# crw-rw---- 1 root kvm 10, 232 Mar 23 ... /dev/kvm
```

### Step 2: 安装依赖

```bash
# 更新系统并安装构建工具
sudo apt update && sudo apt install -y \
  build-essential \
  git \
  pkg-config \
  libssl-dev \
  curl

# 安装 Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source $HOME/.cargo/env

# 确认版本
rustc --version
# rustc 1.94.0

# 下载 Firecracker v1.12.0（用于 Template 创建）
ARCH=$(uname -m)
curl -L -o firecracker-v1.12.0-${ARCH}.tgz \
  https://github.com/firecracker-microvm/firecracker/releases/download/v1.12.0/firecracker-v1.12.0-${ARCH}.tgz

tar -xzf firecracker-v1.12.0-${ARCH}.tgz
sudo mv release-v1.12.0-${ARCH}/firecracker-v1.12.0-${ARCH} /usr/local/bin/firecracker
firecracker --version
# Firecracker v1.12.0
```

### Step 3: 构建 zeroboot

```bash
# 克隆仓库
git clone https://github.com/zerobootdev/zeroboot
cd zeroboot

# 编译（Release 模式，~24 秒）
cargo build --release
```

**准备 kernel 和 rootfs**（这一步容易出错，详细记录）：

```bash
# 下载 Firecracker CI 提供的 kernel
# vmlinux-6.1.155 (43MB, 从 Firecracker CI v1.15 artifacts 获取)
curl -L -o vmlinux-6.1.155 \
  https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64/vmlinux-6.1.155

# 创建 rootfs（Ubuntu 24.04 base + Python 3.12）
# 方法：从 Ubuntu cloud image 创建 ext4 rootfs
truncate -s 2G rootfs.ext4
mkfs.ext4 rootfs.ext4

sudo mkdir -p /mnt/rootfs
sudo mount rootfs.ext4 /mnt/rootfs

# 使用 debootstrap 安装最小 Ubuntu 系统
sudo apt install -y debootstrap
sudo debootstrap --arch=amd64 noble /mnt/rootfs http://archive.ubuntu.com/ubuntu

# 安装 Python 和常用库
sudo chroot /mnt/rootfs bash -c "
  apt update && apt install -y python3 python3-pip python3-numpy
"

# 编译并安装 guest agent（zeroboot 的 init 进程）
gcc -static -o /tmp/init guest/init.c
sudo cp /tmp/init /mnt/rootfs/init.py

sudo umount /mnt/rootfs
```

!!! tip "关于 rootfs"
    zeroboot 的 guest agent 是一个 C 语言静态编译的 PID 1 进程，监听 `/dev/ttyS0`（serial port）接收代码并执行。它被放在 rootfs 的 `/init.py` 路径（由 kernel boot_args 中的 `init=` 参数指定）。

### Step 4: 创建 Template

```bash
# 创建工作目录
mkdir -p workdir

# 创建 Template（启动 Firecracker VM → 预加载 → 拍快照）
# ⚠️ 关键：boot_args 中必须添加 clearcpuid 禁用 AMX
sudo ./target/release/zeroboot template \
  --kernel vmlinux-6.1.155 \
  --rootfs rootfs.ext4 \
  --workdir ./workdir \
  --boot-args "console=ttyS0 reboot=k panic=1 pci=off clearcpuid=amx_tile,amx_bf16,amx_int8"
```

输出示例：

```
[INFO] Starting Firecracker VM for template...
[INFO] Waiting for guest to initialize (10s)...
[INFO] Taking snapshot...
[INFO] Template created in 13.89s (boot: 8.90s + snapshot)
[INFO] Snapshot state: 22843 bytes
[INFO] Snapshot memory: 512 MiB
[INFO] Loading memory into memfd...
[INFO] Template ready at ./workdir
```

!!! danger "嵌套虚拟化关键修复：禁用 AMX"
    在 EC2 嵌套虚拟化环境下，**必须**在 `boot_args` 中添加 `clearcpuid=amx_tile,amx_bf16,amx_int8`。原因：Firecracker 快照会捕获 AMX 状态（XCR0=0x602e7），但 L1 KVM 的 `KVM_SET_CPUID2` 不允许为 L2 VM 设置含 AMX 的 CPUID。禁用后 XCR0=0x2e7（仍含 AVX-512），与嵌套 KVM 兼容。详见下方踩坑记录。

### Step 5: 运行 Benchmark

zeroboot 内置了完整的 benchmark 工具，包含 5 个测试阶段：

```bash
sudo ./target/release/zeroboot bench ./workdir
```

**Phase 1: Pure mmap CoW**

测试 `mmap(MAP_PRIVATE)` 在 memfd 上的 CoW 性能，10000 次迭代。这是 fork 的内存映射基础操作。

**Phase 2: Full fork（KVM + CoW + CPU restore）**

完整的 VM fork 流程：创建 KVM VM → CoW 内存映射 → 恢复所有 CPU 状态。1000 次迭代，测量端到端 fork 延迟。

**Phase 3: Fork + exec**

Fork 一个 VM 并通过 serial 发送 `echo hello` 命令，等待执行完成。100 次迭代，测量从 fork 到获得输出的全链路延迟。

**Phase 4: Concurrent forks**

分别创建 10、100、1000 个并发 fork，测量总耗时和内存占用。验证大规模并发场景的可行性。

**Phase 5: Memory isolation**

验证 CoW 隔离：Fork A 向特定内存地址写入 `0xDEADBEEF_CAFEBABE`，Fork B 读取同一地址，应读到快照原始值而非 Fork A 写入的值。

## 测试结果

### Phase 1: Pure mmap CoW（10000 次）

| 指标 | 延迟 |
|------|------|
| P50 | 0.7 µs |
| P95 | 1.4 µs |
| P99 | 2.2 µs |

亚微秒级的 CoW 映射，说明底层 mmap 性能在嵌套虚拟化下无损——因为 mmap 是 L1 host kernel 操作，不涉及 L2 KVM。

### Phase 2: Full fork — KVM + CoW + CPU restore（1000 次）

| 指标 | 延迟 |
|------|------|
| Min | 0.502 ms |
| **P50** | **0.699 ms** |
| P95 | 0.943 ms |
| **P99** | **1.064 ms** |
| Max | 1.219 ms |

**核心指标**：P50 = 0.699ms，P99 = 1.064ms。亚毫秒级 VM spawn 在嵌套虚拟化下成立。

### Phase 3: Fork + exec echo hello（100 次，100% 成功率）

| 指标 | 延迟 |
|------|------|
| P50 | 6.309 ms |
| P95 | 6.536 ms |
| P99 | 6.790 ms |

从 fork 到执行完命令并拿到输出，~6ms。延迟主要来自 serial I/O 和 guest 内代码解析执行。

### Phase 4: Concurrent forks

| 并发数 | 总耗时 | 每 fork 耗时 | 总 RSS | 每 fork 内存 |
|--------|--------|-------------|--------|-------------|
| 10 | 8.9ms | 894.7µs | 3.9MB | 395.2KB |
| 100 | 80.2ms | 802.1µs | 9.9MB | 101.4KB |
| **1000** | **1094.4ms** | **1094.4µs** | **70.7MB** | **72.4KB** |

1000 个并发 VM，总共只占 70.7MB 内存。每个 sandbox 仅 72.4KB——这就是 CoW + `MAP_NORESERVE` 的威力。

### Phase 5: Memory Isolation

✅ Fork B 无法读取 Fork A 写入的 `0xDEADBEEF_CAFEBABE`，双向隔离验证通过。

### 与官方数据对比

| 指标 | zeroboot 官方 | 本次实测（EC2 嵌套虚拟化） | 差异 |
|------|-------------|--------------------------|------|
| Spawn P50 | 0.79ms | **0.699ms** | **-11.5%（更快）** |
| Spawn P99 | 1.74ms | **1.064ms** | **-38.9%（更快）** |
| 每 sandbox 内存 | ~265KB | **72.4KB**（1000 并发） | **-72.7%（更省）** |
| Fork + exec | ~8ms | **6.31ms** | **-21.1%（更快）** |
| 1000 并发总耗时 | 815ms | **1094.4ms** | **+34.3%（略慢）** |

#### 分析

**单次 fork 性能优于官方**，可能原因：

1. **CPUID 处理差异**：我们使用 merged CPUID（snapshot ∩ host 交集），减少了 KVM 的验证开销
2. **CPU 代际优势**：c8i 使用 Intel Sapphire Rapids（第 4 代 Xeon），VT-x 优化更成熟
3. **AMX 禁用**：减少了 XSAVE 区域大小，内存占用更低（从 ~265KB 降到 72.4KB）

**1000 并发略慢**（+34.3%），原因：

1. 嵌套虚拟化下 `KVM_CREATE_VM` 有额外的 VMX context 创建开销
2. 1000 个 L2 VM 的 VMCS 管理比 L1 更复杂
3. 但 overhead 在可接受范围，且单次 fork（P50 < 1ms）完全不受影响

## 踩坑记录

### 1. XCR0/AMX 不兼容 → clearcpuid 禁用

!!! warning "问题"
    Firecracker 快照捕获的 XCR0=0x602e7（含 AMX_TILE/AMX_BF16/AMX_INT8），但 EC2 嵌套虚拟化的 L1 KVM 不支持为 L2 VM 设置含 AMX 的 CPUID。`KVM_SET_CPUID2` 返回 `EINVAL`。

**根因**：Intel AMX 是较新的 CPU 扩展，嵌套虚拟化的 KVM 尚未完全支持其 passthrough。

**解决方案**：在 kernel `boot_args` 中添加 `clearcpuid=amx_tile,amx_bf16,amx_int8`，让 guest kernel 启动时不探测 AMX 特性。快照的 XCR0 变为 0x2e7（仍含 SSE、AVX、AVX-512），与 L2 KVM 兼容。

### 2. vmstate 解析器偏移量 → 模式匹配定位

!!! warning "问题"
    zeroboot 需要解析 Firecracker 的 vmstate 二进制文件提取 CPU 寄存器状态。但 vmstate 中各 section 是 variable-length（取决于 Firecracker 版本、kernel、boot_args），硬编码偏移量不可靠。

**解决方案**：使用**锚点模式匹配**：

- **CPU 寄存器区域**：搜索 `CR0=0x80050033`（Linux kernel 标准 CR0 值）作为锚点
- **LAPIC**：搜索 `version=0x50014` + `spurious=0x1FF` 模式
- **IOAPIC**：搜索 `0xFEC00000`（标准 IOAPIC base address）

这种方式比硬编码偏移量更鲁棒，能适应不同 Firecracker 版本和配置。

### 3. KVM_SET_CPUID2 受限 → CPUID 交集

!!! warning "问题"
    嵌套虚拟化下，L1 KVM 不允许为 L2 VM 设置任意 CPUID。直接使用快照中的 CPUID 会导致 `KVM_SET_CPUID2` 失败。

**解决方案**：将 snapshot CPUID 与 host-supported CPUID 取交集（AND 各 feature bits）。这保证了：

- guest 看到的 CPU 特性都是 L2 KVM 真正支持的
- 不会因为 CPUID 声明的特性与实际不符导致 `SIGILL`
- numpy 等在快照中已完成 SIMD detection 的库不受影响（因为交集后的特性集是子集）

### 4. LAPIC 偏移错误 → 模式匹配定位

!!! warning "问题"
    vmstate 中 LAPIC 和 IOAPIC 之间有 variable-length sections。最初尝试用 IOAPIC 的偏移量线性推算 LAPIC 位置，结果偏差 24 字节。错误的 LAPIC 状态导致 guest 中断系统失效，vCPU 死循环（无法响应 timer interrupt）。

**表现**：Fork 后 guest 无响应，`KVM_RUN` 永远不返回。

**解决方案**：放弃线性推算，改用独立的模式匹配（LAPIC version + spurious vector 特征值）直接定位 LAPIC 数据。每个设备的偏移量独立计算，互不依赖。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| c8i.xlarge（EC2 On-Demand） | $0.192/hr | ~1 hr | $0.19 |
| EBS gp3 30GB | $0.08/GB-month | ~1 hr | ~$0.003 |
| 数据传输 | - | 极少量 | ~$0 |
| **合计** | | | **~$0.19** |

对比：如果用 bare metal 实例（c7i.metal-24xl, $4.608/hr），同样 1 小时的测试成本是 **$4.61**，贵了 24 倍。嵌套虚拟化让 benchmark 验证的成本可以忽略不计。

## 清理资源

```bash
# 终止 EC2 实例
aws ec2 terminate-instances \
  --instance-ids $INSTANCE_ID \
  --region ap-southeast-1

# 等待实例终止
aws ec2 wait instance-terminated \
  --instance-ids $INSTANCE_ID \
  --region ap-southeast-1

# 删除安全组
aws ec2 delete-security-group \
  --group-id $SG_ID \
  --region ap-southeast-1

# 删除 Key Pair
aws ec2 delete-key-pair \
  --key-name zeroboot-benchmark-key \
  --region ap-southeast-1

# 清理本地临时文件
rm -f /tmp/zeroboot-benchmark-key.pem
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。c8i.xlarge 按需价格 $0.192/hr，忘记关机一天就是 $4.61。

## 结论与建议

### 核心结论

**zeroboot 在 EC2 嵌套虚拟化环境下性能验证通过**。亚毫秒级 VM spawn（P50 = 0.699ms）在嵌套虚拟化的次优条件下依然成立，部分指标甚至优于官方数据。

### 适用场景

- **AI Agent 代码执行**：需要高频、低延迟、强隔离的代码 sandbox
- **CI/CD 测试隔离**：每个测试用例在独立 VM 中运行，互不影响
- **安全沙箱**：KVM 硬件级隔离，比 container 更强的安全边界

### 嵌套虚拟化 vs Bare Metal 建议

| 场景 | 推荐 | 理由 |
|------|------|------|
| 开发测试/Benchmark | 嵌套虚拟化（c8i.xlarge） | 成本低 24 倍，性能足够 |
| 生产环境（低并发） | 嵌套虚拟化 | 单次 fork 性能无损 |
| 生产环境（高并发 >1000） | Bare Metal | 大规模并发 overhead 更低 |

### 注意事项

1. 嵌套虚拟化需要 `clearcpuid` 禁用 AMX，这意味着 guest 内无法使用 AMX 指令加速（但 AVX-512 仍可用）
2. vmstate 解析依赖 Firecracker 版本，升级 Firecracker 后可能需要重新验证
3. CPUID 交集处理是嵌套虚拟化特有的要求，bare metal 上不需要

## 参考链接

- [zeroboot GitHub](https://github.com/zerobootdev/zeroboot)
- [zeroboot 架构文档](https://github.com/zerobootdev/zeroboot/blob/main/docs/ARCHITECTURE.md)
- [EC2 嵌套虚拟化文档](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nested-virtualization.html)
- [Firecracker Snapshotting](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/)
- [E2B（对标竞品）](https://e2b.dev)
- [Linux mmap(2) — MAP_PRIVATE CoW 语义](https://man7.org/linux/man-pages/man2/mmap.2.html)
