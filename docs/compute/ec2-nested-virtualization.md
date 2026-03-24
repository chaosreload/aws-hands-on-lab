---
description: "Test EC2 nested virtualization with KVM and Firecracker microVMs on virtual instances — 54x cost reduction vs bare metal."
---
# EC2 嵌套虚拟化实测：在虚拟实例上运行 KVM 和 Firecracker

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1（所有商业区域可用）
    - **最后验证**: 2026-03-23

## 背景

在 EC2 上运行虚拟机一直是个痛点：**以前只有 bare metal 实例才支持 KVM/Hyper-V**。想在 EC2 上跑嵌套虚拟化（nested VM、Firecracker microVM、Android 模拟器、WSL2），你得租一台 `m5.metal`（$4.608/hr）——对开发测试来说太贵了。

2026 年 2 月，AWS 宣布 **虚拟 EC2 实例支持嵌套虚拟化**。现在一台 `c8i.large`（$0.085/hr）就能跑 KVM，**成本降低约 54 倍**。

本文验证三件事：

1. 嵌套虚拟化的启用和 `/dev/kvm` 可用性
2. KVM API 是否完全可用
3. **能否在嵌套虚拟化实例上运行 Firecracker microVM**（亮点）

## 前置条件

- AWS 账号（需要 EC2 启动/修改权限）
- AWS CLI **v2.34+**（旧版本不支持 `NestedVirtualization` 参数）
- SSH 密钥对

## 核心概念

### 架构：三层虚拟化

```
L0  │  AWS 物理服务器 + Nitro Hypervisor
────┼──────────────────────────────────────
L1  │  你的 EC2 实例（运行 KVM/Hyper-V）
────┼──────────────────────────────────────
L2  │  嵌套 VM / Firecracker microVM
```

Nitro System 将 **Intel VT-x 扩展**透传到 EC2 实例，让 L1 层可以运行 hypervisor。

### 支持范围

| 项目 | 支持情况 |
|------|---------|
| **实例类型** | C8i, M8i, R8i（均为 Intel 第 8 代） |
| **Hypervisor** | KVM, Hyper-V |
| **区域** | 所有商业区域 |
| **额外费用** | 无 |
| **AMD/Graviton** | 暂不支持 |

!!! warning "性能注意"
    AWS 官方建议：性能敏感或有严格延迟要求的工作负载，仍推荐使用 bare metal 实例。嵌套虚拟化存在 L1→L2 的额外性能开销。

## 动手实践

### Step 1: 准备环境

创建密钥对和安全组：

```bash
# 创建密钥对
aws ec2 create-key-pair \
  --key-name nested-virt-test \
  --region us-east-1 \
  --query 'KeyMaterial' --output text > ~/.ssh/nested-virt-test.pem
chmod 600 ~/.ssh/nested-virt-test.pem

# 创建安全组（允许 SSH）
SG_ID=$(aws ec2 create-security-group \
  --group-name nested-virt-sg \
  --description 'SG for nested virtualization test' \
  --region us-east-1 \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 --cidr 0.0.0.0/0 \
  --region us-east-1
```

### Step 2: 启动实例（启用嵌套虚拟化）

关键参数是 `--cpu-options "NestedVirtualization=enabled"`：

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI_ID=$(aws ec2 describe-images \
  --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
  --region us-east-1 --output text)

# 启动实例（启用嵌套虚拟化）
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type c8i.large \
  --key-name nested-virt-test \
  --security-group-ids $SG_ID \
  --cpu-options "NestedVirtualization=enabled" \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance ID: $INSTANCE_ID"

# 等待实例运行
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-east-1

# 获取公网 IP
PUBLIC_IP=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --region us-east-1 \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo "Public IP: $PUBLIC_IP"
```

### Step 3: 验证嵌套虚拟化

SSH 连接后验证 `/dev/kvm` 和 CPU 虚拟化标志：

```bash
ssh -i ~/.ssh/nested-virt-test.pem ec2-user@$PUBLIC_IP
```

在实例内执行：

```bash
# 检查 /dev/kvm
ls -la /dev/kvm

# 检查 VT-x CPU 标志
grep -c vmx /proc/cpuinfo

# 查看虚拟化支持
lscpu | grep -i virtual

# 查看 KVM 内核模块
lsmod | grep kvm
```

预期输出：

```
crw-rw-rw-. 1 root kvm 10, 232 Mar 23 03:49 /dev/kvm
4
Virtualization:                          VT-x
Virtualization type:                     full
kvm_intel             323584  0
kvm                  1384448  1 kvm_intel
```

### Step 4: KVM API 功能验证

编译运行一个 KVM API 测试程序，验证 VM 和 vCPU 创建：

```bash
cat > /tmp/kvm_test.c << 'EOF'
#include <fcntl.h>
#include <linux/kvm.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main() {
    int kvm_fd = open("/dev/kvm", O_RDWR);
    if (kvm_fd < 0) { perror("open /dev/kvm"); return 1; }

    int api_ver = ioctl(kvm_fd, KVM_GET_API_VERSION, 0);
    printf("KVM API version: %d\n", api_ver);

    int vm_fd = ioctl(kvm_fd, KVM_CREATE_VM, 0);
    if (vm_fd < 0) { perror("KVM_CREATE_VM"); return 1; }
    printf("VM created successfully (fd=%d)\n", vm_fd);

    int vcpu_fd = ioctl(vm_fd, KVM_CREATE_VCPU, 0);
    if (vcpu_fd < 0) { perror("KVM_CREATE_VCPU"); return 1; }
    printf("vCPU created successfully (fd=%d)\n", vcpu_fd);

    close(vcpu_fd); close(vm_fd); close(kvm_fd);
    printf("KVM API fully functional!\n");
    return 0;
}
EOF

sudo dnf install -y gcc
gcc -o /tmp/kvm_test /tmp/kvm_test.c
sudo /tmp/kvm_test
```

预期输出：

```
KVM API version: 12
VM created successfully (fd=4)
vCPU created successfully (fd=5)
KVM API fully functional!
```

### Step 5: 运行 Firecracker microVM（亮点）

这是最激动人心的测试——在嵌套虚拟化实例上运行 Firecracker microVM：

```bash
# 下载 Firecracker
FC_VERSION="v1.12.0"
curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-x86_64.tgz" \
  -o /tmp/firecracker.tgz
cd /tmp && tar xzf firecracker.tgz
sudo cp release-${FC_VERSION}-x86_64/firecracker-${FC_VERSION}-x86_64 /usr/local/bin/firecracker
sudo chmod +x /usr/local/bin/firecracker

# 下载测试用内核和 rootfs
curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin" \
  -o /tmp/vmlinux
curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/rootfs/bionic.rootfs.ext4" \
  -o /tmp/rootfs.ext4

# 设置 KVM 权限
sudo chmod 666 /dev/kvm

# 创建 Firecracker 配置
cat > /tmp/fc-config.json << 'EOF'
{
  "boot-source": {
    "kernel_image_path": "/tmp/vmlinux",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"
  },
  "drives": [{
    "drive_id": "rootfs",
    "path_on_host": "/tmp/rootfs.ext4",
    "is_root_device": true,
    "is_read_only": false
  }],
  "machine-config": {
    "vcpu_count": 1,
    "mem_size_mib": 256
  }
}
EOF

# 启动 Firecracker microVM
firecracker --api-sock /tmp/firecracker.socket \
  --config-file /tmp/fc-config.json
```

你会看到 Linux 内核启动日志，最终出现登录提示——**microVM 在嵌套虚拟化实例上完全正常运行**！

按 `Ctrl+C` 退出 Firecracker。

### 对比：不启用嵌套虚拟化

作为对照，启动一台相同配置但不启用嵌套虚拟化的实例：

```bash
# 启动实例（不启用嵌套虚拟化）
aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type c8i.large \
  --key-name nested-virt-test \
  --security-group-ids $SG_ID \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text
```

SSH 连接后验证：

```bash
ls -la /dev/kvm          # → No such file or directory
grep -c vmx /proc/cpuinfo  # → 0
lsmod | grep kvm            # → (空)
```

**完全相同的实例类型和 AMI，唯一差异是嵌套虚拟化开关。**

### 在已有实例上启用嵌套虚拟化

如果你已经有一台运行中的 C8i/M8i/R8i 实例，可以后续启用嵌套虚拟化：

```bash
# 先停止实例
aws ec2 stop-instances --instance-ids $INSTANCE_ID --region us-east-1
aws ec2 wait instance-stopped --instance-ids $INSTANCE_ID --region us-east-1

# 启用嵌套虚拟化
aws ec2 modify-instance-cpu-options \
  --instance-id $INSTANCE_ID \
  --nested-virtualization enabled \
  --region us-east-1

# 重新启动
aws ec2 start-instances --instance-ids $INSTANCE_ID --region us-east-1
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-east-1
```

## 测试结果

### 嵌套虚拟化开关对比

| 检查项 | 启用嵌套虚拟化 | 未启用嵌套虚拟化 |
|--------|--------------|----------------|
| `/dev/kvm` | ✅ 存在 (crw-rw-rw-) | ❌ 不存在 |
| vmx CPU flag | ✅ 4 个 | ❌ 0 个 |
| `lscpu` Virtualization | VT-x | (无) |
| kvm 内核模块 | ✅ kvm_intel + kvm | ❌ 未加载 |
| KVM API (CREATE_VM) | ✅ 成功 | ❌ 无法打开 /dev/kvm |

### KVM API 测试结果

| 操作 | 结果 |
|------|------|
| KVM_GET_API_VERSION | 12 |
| KVM_CREATE_VM | ✅ 成功 |
| KVM_CREATE_VCPU | ✅ 成功 |
| KVM_GET_SUPPORTED_CPUID | 61 个条目 |

### Firecracker microVM 测试结果

| 项目 | 结果 |
|------|------|
| Firecracker 版本 | v1.12.0 |
| microVM 内核 | Linux 4.14.174 |
| Guest OS | Ubuntu 18.04.5 LTS |
| CPU 检测 | Intel Xeon (family 0x6, model 0xad) |
| Hypervisor 检测 | KVM |
| systemd 初始化 | ✅ 完成 |
| SSH server | ✅ 启动 |
| 总体结果 | **✅ 完全正常运行** |

### 不支持的实例类型

| 尝试 | 结果 |
|------|------|
| m7i.large + NestedVirtualization=enabled | `InvalidParameterCombination` 错误 |
| 错误信息 | "The specified instance type does not support Nested Virtualization" |

## 踩坑记录

!!! warning "AWS CLI 版本要求"
    AWS CLI v2.33 **不支持** `NestedVirtualization` 参数（会报 `Unknown parameter in CpuOptions`）。必须升级到 **v2.34+** 才能使用。（实测发现，官方未记录）

!!! warning "Amazon Linux 2023 无 QEMU 系统包"
    AL2023 的 dnf 仓库中没有 `qemu-kvm` 或 `qemu-system-x86` 包，只有 `qemu-img`。如果需要完整的 QEMU/KVM 环境，建议使用 Ubuntu AMI 或自行编译。对于 Firecracker 场景，这不是问题——Firecracker 是独立二进制文件。

!!! note "Firecracker 测试资源下载"
    Firecracker 官方的 quickstart 测试内核和 rootfs 托管在 S3 上。URL 格式随版本可能变化，建议参考 [Firecracker quickstart 文档](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| c8i.large（启用嵌套虚拟化） | $0.085/hr | ~1 hr | $0.085 |
| c8i.large（对照组） | $0.085/hr | ~0.5 hr | $0.043 |
| 数据传输 | - | 最小 | ~$0 |
| **合计** | | | **< $0.15** |

## 清理资源

```bash
# 终止所有测试实例
aws ec2 terminate-instances \
  --instance-ids <instance-id-1> <instance-id-2> \
  --region us-east-1

# 等待实例终止
aws ec2 wait instance-terminated \
  --instance-ids <instance-id-1> <instance-id-2> \
  --region us-east-1

# 删除安全组
aws ec2 delete-security-group --group-id <sg-id> --region us-east-1

# 删除密钥对
aws ec2 delete-key-pair --key-name nested-virt-test --region us-east-1

# 本地清理
rm -f ~/.ssh/nested-virt-test.pem
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。c8i.large 按小时计费。

## 结论与建议

### 关键发现

1. **嵌套虚拟化开箱即用** — 只需一个启动参数，无需额外配置。`/dev/kvm` 自动可用，kvm_intel 模块自动加载。
2. **Firecracker 完全兼容** — 在嵌套虚拟化实例上运行 Firecracker microVM，内核启动、systemd 初始化、服务启动全部正常。这对 Firecracker-based 项目（如轻量级 sandbox runtime）意义重大。
3. **成本大幅降低** — 从 bare metal（~$4.6/hr）到 c8i.large（$0.085/hr），降幅约 **54x**。开发测试环境终于可以用得起 KVM 了。
4. **已有实例可后续启用** — stop → modify → start 三步操作，无需重新创建实例。

### 适用场景

| 场景 | 推荐 |
|------|------|
| 开发测试（Firecracker/QEMU/模拟器） | ✅ 嵌套虚拟化 |
| CI/CD 流水线中的 VM 测试 | ✅ 嵌套虚拟化 |
| Android 模拟器 / WSL2 | ✅ 嵌套虚拟化 |
| 生产环境性能敏感工作负载 | ⚠️ 建议 bare metal |
| 需要 AMD/Graviton 平台 | ❌ 暂不支持 |

### 当前限制

- 仅 C8i/M8i/R8i 实例类型（均为 Intel 平台）
- 暂无 AMD 或 Graviton 支持
- 性能敏感场景仍建议 bare metal
- AWS CLI 需要 v2.34+ 版本

## 参考链接

- [EC2 嵌套虚拟化文档](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-nested-virtualization-on-virtual/)
- [Firecracker GitHub](https://github.com/firecracker-microvm/firecracker)
- [EC2 实例类型](https://aws.amazon.com/ec2/instance-types/)
