# EC2 C8in/C8ib 实测：Granite Rapids CPU/内存/加密性能深度解析与选型决策

!!! info "Lab 信息"
    - **难度**: ⭐⭐⭐ 高级（架构师选型决策，需 EC2 / benchmark 基础）
    - **预估时间**: 60 分钟
    - **预估费用**: 约 $5（含清理）
    - **Region**: us-east-1（us-east-1c AZ）
    - **最后验证**: 2026-04-18

## 背景

AWS 在 2026-04-16 宣布 EC2 **C8in** 和 **C8ib** 正式 GA，基于定制第六代 Intel Xeon Scalable（Granite Rapids）处理器 + 第六代 AWS Nitro 卡。公告中最抢眼的数字是：

- C8in：最大 **600 Gbps** 网络带宽 — "enhanced networking EC2 实例中最高"
- C8ib：最大 **300 Gbps** EBS 带宽 — "非加速计算实例中最高"
- 相对 C6in **最高 43% 性能提升**

对架构师而言真正的问题不是"新机型来了"，而是：

1. "43%" 是什么负载下的数字？我的工作负载能吃到多少？
2. 同代的 C8i、C8in、C8ib 底层是同一颗 CPU 吗？怎么选？
3. 上一代 C6in / C7i 什么场景下仍是性价比选项？

本文用 Batch A（CPU / 内存 / 加密）的实测数据回答前两个问题，并结合 AWS 官方文档规格给出选型决策树。**网络和 EBS 部分为规格分析，非实测**（详见最后一章的诚实披露）。

## 前置条件

- AWS 账号，Standard On-Demand vCPU 配额 ≥ 128（测 4 台 8xlarge）
- 目标 Region 有 C8in/C8ib 可用性（us-east-1, us-west-2, ap-northeast-1 for C8in；us-east-1, us-west-2 for C8ib）
- 同一个 placement group（cluster 策略）下可同时 launch 所有对比实例
- SSH key pair + Security Group 仅允许你的出口 IP SSH（**严禁 0.0.0.0/0**）

## 核心概念

### 实例家族对照一览

| 家族 | 处理器 | Nitro 代 | 典型定位 | 最大网络 | 最大 EBS |
|---|---|---|---|---|---|
| C6in | Intel Ice Lake | v4 | 上一代网络密集型 | 200 Gbps (32xl) | 80 Gbps (32xl) |
| C7i | Intel Sapphire Rapids | v4 | 上一代通用 Intel | 50 Gbps (48xl) | 40 Gbps (48xl) |
| C8i | Granite Rapids | **v6** | 同代通用 Intel | 100 Gbps (96xl) | 80 Gbps (96xl) |
| **C8in** | Granite Rapids | **v6** | 同代**网络密集型** | **600 Gbps (96xl)** | 120 Gbps (96xl) |
| **C8ib** | Granite Rapids | **v6** | 同代**存储密集型** | 400 Gbps (96xl) | **300 Gbps (96xl)** |

（数据来源：AWS 官方 Compute Optimized instances 规格表）

### 关键观察：C8in 和 C8i 共用同一颗 CPU

本次实测（`dmidecode`/`/proc/cpuinfo`）显示 c8in.8xlarge 和 c8i.8xlarge 的 CPU 完全相同：

```
Intel(R) Xeon(R) 6975P-C @ 2.00GHz
```

AWS 官方文档只公开架构名 "Granite Rapids"，未标注具体 SKU。**SKU 号 "6975P-C" 是通过 dmidecode 读到的 SMBIOS 层信息，不是官方公布的规格**，可能随 AWS 替换底层硬件变更。

这个发现重要之处在于：**选 C8in 而不是 C8i 的收益全部在网络 I/O，不在 CPU**。如果你的工作负载网络 < 40 Gbps，C8i 就足够了。

### 配置带宽权重（C8i 有，C8in/C8ib 没有）

AWS 官方文档明确说明：`C8a, C8g, C8gd, C8i, C8id, C8i-flex` 支持 configurable bandwidth weighting（网络和 EBS 之间动态调配）。**C8in 和 C8ib 不在此列** — 它们的网络/EBS 带宽是固定比例，这是设计取向：C8in 把硬件预算压到网络侧，C8ib 压到 EBS 侧，不再让你切换。

## 动手实践

### Step 1: 基础设施准备

us-east-1c AZ 下建 VPC + 子网 + placement group cluster + SG，SG 仅放行你自己的 SSH IP，内部全通（ICMP+TCP+UDP self-reference）。

```bash
PROFILE=weichaol-testenv2-awswhatsnewtest
REGION=us-east-1
AZ=us-east-1c

# 1. VPC + Subnet + IGW + Route Table
aws ec2 create-vpc --profile $PROFILE --region $REGION \
    --cidr-block 10.200.0.0/16 \
    --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=c8-bench-vpc}]'

# 2. Security Group —— 只放 SSH from my IP
MY_IP=$(curl -4 -s ifconfig.me)
aws ec2 create-security-group --profile $PROFILE --region $REGION \
    --group-name c8-bench-sg --vpc-id <VPC_ID> --description "C8 benchmark SG"

aws ec2 authorize-security-group-ingress --profile $PROFILE --region $REGION \
    --group-id <SG_ID> --protocol tcp --port 22 --cidr ${MY_IP}/32

# 集群内自引用（placement group 内全通，非 0.0.0.0/0）
aws ec2 authorize-security-group-ingress --profile $PROFILE --region $REGION \
    --group-id <SG_ID> --protocol all --source-group <SG_ID>

# 3. Placement Group cluster
aws ec2 create-placement-group --profile $PROFILE --region $REGION \
    --group-name c8-benchmark-pg --strategy cluster
```

### Step 2: 批量 launch 4 机型

同 placement group、同 subnet、同 AMI（Amazon Linux 2023 最新版），**每台加 TTL trap 自毁钩子**（见踩坑 1）。

```bash
# 4 种机型：c8in.8xl / c6in.8xl / c8i.8xl / c7i.8xl
for TYPE in c8in.8xlarge c6in.8xlarge c8i.8xlarge c7i.8xlarge; do
  aws ec2 run-instances --profile $PROFILE --region $REGION \
    --image-id ami-098e39bafa7e7303d \
    --instance-type $TYPE \
    --subnet-id <SUBNET_ID> \
    --security-group-ids <SG_ID> \
    --placement "GroupName=c8-benchmark-pg" \
    --key-name ec2-benchmark-2026-04 \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Task,Value=c8in-c8ib},{Key=Name,Value=$TYPE}]" \
    --count 1
done
```

### Step 3: 识别 CPU

每台机器上跑：

```bash
lscpu | head -20
sudo dmidecode -t 4 | grep -E "Version|Core Count|Thread Count"
cat /proc/cpuinfo | grep "model name" | head -1
```

实测输出（c8in.8xlarge）：

```
Model name:           Intel(R) Xeon(R) 6975P-C
CPU(s):               32
Thread(s) per core:   2
Core(s) per socket:   16
L3 cache:             108 MiB
```

c8i.8xlarge 得到完全一致的 `Intel(R) Xeon(R) 6975P-C`；c6in.8xlarge 显示 `Intel(R) Xeon(R) Platinum 8375C @ 2.90GHz`（Ice Lake）；c7i.8xlarge 显示 `Intel(R) Xeon(R) Platinum 8488C`（Sapphire Rapids）。

### Step 4: CPU 基准 — stress-ng matrix / prime

AL2023 原生仓库有 `stress-ng`，直接 `sudo dnf install stress-ng`。

> **踩坑 2** — 原计划用 sysbench，但 AL2023 官方 repo 无此包，EPEL 在 AL2023 装不干净（`nothing provides redhat-release >= 9`）。见"踩坑记录"。

```bash
# matrix（SIMD/浮点密集），1 thread + 全线程各 3 次
for T in 1 $(nproc); do
  for i in 1 2 3; do
    stress-ng --matrix $T --metrics-brief --timeout 180s --yaml /tmp/mat_${T}_${i}.yml
  done
done

# prime（整数密集），全线程
for i in 1 2 3; do
  stress-ng --prime $(nproc) --metrics-brief --timeout 180s --yaml /tmp/prime_${i}.yml
done
```

### Step 5: 加密吞吐 — OpenSSL

```bash
# AES-256-GCM 单线程
openssl speed -evp aes-256-gcm 2>&1 | tee aes_run1.txt

# SHA-256 多线程（-multi N）
openssl speed -multi $(nproc) sha256 2>&1 | tee sha256_multi_run1.txt
```

（每台机器跑 3 轮取中位数）

### Step 6: 内存带宽 — STREAM

```bash
# 编译 STREAM，OMP 版本
wget http://www.cs.virginia.edu/stream/FTP/Code/stream.c
gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=100000000 -DNTIMES=20 stream.c -o stream

# 跑 3 次
for i in 1 2 3; do OMP_NUM_THREADS=$(nproc) ./stream > stream_run${i}.txt; done
```

## 测试结果

4 机型、7 项测试、每项 3 次取中位数、stddev 均 < 1.6%。

| # | 测试 | c8in.8xl | c6in.8xl | c8i.8xl | c7i.8xl | c8in vs c6in | c8in vs c8i | c8in vs c7i |
|---|---|---|---|---|---|---|---|---|
| 1 | stress-ng matrix, 1T (bogo/s) | **6,040** | 3,906 | 6,011 | 4,665 | **+54.6%** | +0.5% | +29.5% |
| 2 | stress-ng matrix, 32T (bogo/s) | **116,397** | 77,491 | 115,698 | 102,723 | **+50.2%** | +0.6% | +13.3% |
| 3 | stress-ng prime, 32T (bogo/s) | **37,531** | 32,840 | 37,512 | 32,946 | **+14.3%** | +0.05% | +13.9% |
| 4 | OpenSSL AES-256-GCM, 1T (KB/s) | **13.2M** | 8.86M | 13.2M | 12.3M | **+48.9%** | +0.14% | +7.7% |
| 5 | OpenSSL SHA-256, 32T (KB/s) | **39.2M** | 29.3M | 39.2M | 35.1M | **+33.8%** | +0.11% | +11.6% |
| 6 | STREAM Triad (MB/s) | **179,172** | 119,077 | 171,444 | 134,556 | **+50.5%** | +4.5% | +33.2% |
| 7 | STREAM Copy (MB/s) | **178,742** | 119,315 | 171,103 | 121,225 | **+49.8%** | +4.5% | +47.5% |

原始 3 次数据和 YAML 输出见 [evidence 目录](https://github.com/chaosreload/aws-hands-on-lab/tree/main/evidence/ec2-c8in-c8ib-benchmark)。

## 关键发现

### 发现 1: "43%" 是 up-to，SIMD 才超，整数远不到

公告原话是 "**up to** 43% higher performance compared to previous generation C6in instances"，但未指定 benchmark 工具或工作负载。实测结果：

- **SIMD / 浮点（matrix）**：**+50.2~54.6%** — 超过 43%
- **内存带宽（STREAM Triad/Copy）**：**+49.8~50.5%**
- **AES-256-GCM 加密**：**+48.9%**
- **SHA-256 多线程**：+33.8%
- **整数 prime 计算**：**仅 +14.3%** — 远低于 43%

!!! warning "选型前必须知道"
    "43%" 是营销数字上限。如果你的负载主要是整数循环 + 分支判断（典型 Java/Python 业务逻辑），单靠 CPU 升级到 C8 系列只能拿到 +14% 左右。SIMD 向量化、内存密集、加密密集的负载才能完整享受 Granite Rapids 的收益。

### 发现 2: C8in 与 C8i 的 CPU 完全相同

c8in.8xl 和 c8i.8xl 在全部 7 项测试中差异 < 1%（matrix 1T 差 0.5%，prime 差 0.05%，AES 差 0.14%），STREAM 上 c8in 略高 4.5%（可能是同等 memory channel 下 Nitro v6 的 DMA 差异或测试扰动）。

**这直接决定选型逻辑**：

| 场景 | 推荐 |
|---|---|
| 网络 I/O < 40 Gbps | C8i（同 CPU，网络仅到 40G 的规格更便宜） |
| 网络 I/O 40-100 Gbps | C8in 或 C8i.96xl（看规格是否够用） |
| 网络 I/O > 100 Gbps | **C8in 唯一选择** |
| EBS > 80 Gbps | **C8ib 唯一选择**（C8in 最大 120G 够用则 C8in 也行） |

### 发现 3: C7i 在 AES 上接近 C8in，C6in 明显落后

AES-256-GCM 单线程：c7i=12.3 M KB/s，c8in=13.2 M KB/s — 仅差 7.7%。c6in=8.86 M KB/s — 比 c8in 低 49%。

**对成本敏感的纯加密工作负载（VPN 网关、TLS 终结、磁盘加密），C7i 仍是性价比之选**。从 C7i 升到 C8i 只买到 +7.7% 的 AES 性能，不值每小时多付的费用 — 除非同时看中内存带宽（Triad 差 +33%）或 SIMD 密集计算。

### 发现 4: 内存带宽 +50% 对数据库/分析最关键

STREAM Triad：c8in 179 GB/s vs c6in 119 GB/s = **+50.5%**。Granite Rapids 用上了更宽的内存通道（+ 可能的 MR-DIMM），对以下负载意义重大：

- OLAP 数据库扫描（DuckDB、ClickHouse、Redshift 本地处理）
- 实时分析（Spark、Flink 大 state）
- In-memory cache（Redis 大 key、memcached）
- ML 推理（权重从内存加载的延迟敏感场景）

C7i 只有 134 GB/s，c8in 多出 45 GB/s；如果你的瓶颈在 memory bandwidth，这是换代能白捡的性能。

## 网络与 EBS 规格分析（未实测，基于 AWS 官方文档）

!!! danger "诚实披露"
    原计划中的 Batch B（网络 iperf3）/ Batch C（EFA NCCL）/ Batch D（EBS fio）因项目优先级调整和预算控制**未执行**。以下网络/EBS 分析全部基于 AWS 官方 [Compute Optimized instances specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html) 文档规格，**不是实测数据**。读者如需实测数据请自行跑 iperf3/fio 或参考 AWS Blog 的官方 benchmark。

### C8in 网络规格阶梯

| 规格 | 网络带宽 | EFA | 网卡数 |
|---|---|---|---|
| 8xlarge | 50 Gbps | ✗ | 1 |
| 16xlarge | 100 Gbps | ✗ | 1 |
| 24xlarge | 150 Gbps | ✗ | 1 |
| 32xlarge | 200 Gbps | ✗ | 1 |
| 48xlarge | **300 Gbps** | ✓ | 1 |
| **96xlarge** | **600 Gbps** | ✓ | 2（需 ≥2 ENI 分布到不同网卡才能拿到 600 Gbps） |

**关键约束**：要拿到 600 Gbps 必须**把 ENI 分摊到 2 张不同网卡**。应用层要用 bonding / ECMP / 多进程多 socket 才能跑满。单个 TCP 连接 + 单 ENI 最多 300 Gbps 左右。

### C8ib EBS 规格阶梯

| 规格 | EBS 带宽 | 最大 IOPS (16K) | 最大吞吐 (128K, MB/s) |
|---|---|---|---|
| 8xlarge | 25 Gbps | 120,000 | 3,125 |
| 24xlarge | 75 Gbps | 360,000 | 9,375 |
| 48xlarge | 150 Gbps | 720,000 | 18,750 |
| **96xlarge** | **300 Gbps** | **1,440,000** | **37,500** |

**对比参考**：c8in.96xl EBS 只有 120 Gbps，c8i.96xl 只有 80 Gbps。c8ib.96xl 是同代中 **EBS 带宽最高 2.5x、IOPS 最高 2x** 的选择。

### C8i vs C8in/C8ib 的"配置带宽权重"差异

AWS 官方文档特别说明：C8i 支持 `configure-bandwidth-weighting`（在网络和 EBS 之间动态调配带宽权重）。**C8in 和 C8ib 不支持** — 它们是硬件固定的网络/EBS 偏向设计。

- **C8i**：灵活，工作负载混合时可调整
- **C8in**：硬件锁定网络侧优先
- **C8ib**：硬件锁定 EBS 侧优先

## 选型决策树

```
你的主要瓶颈在哪？
│
├── CPU / 内存（通用计算）
│   ├── 网络 I/O < 40 Gbps → C8i ✅（最便宜，CPU 同 C8in）
│   ├── 需要 Java 17 / AVX-512 vec → C8i 或 C8in（任选，CPU 同）
│   └── 整数密集（Web/业务逻辑） → C7i ⚡（只比 C8 慢 14%，但便宜）
│
├── 网络 I/O（分布式训练/HPC/网关）
│   ├── 40-100 Gbps → C8i.96xl 或 C8in.16-24xl
│   ├── 100-300 Gbps → C8in.32xl-48xl + EFA
│   └── > 300 Gbps → C8in.96xl + EFA + 双 ENI ✅
│
├── EBS I/O（OLTP/分析数据库/文件系统）
│   ├── < 80 Gbps EBS → C8i ✅（+ configure-bandwidth-weighting）
│   ├── 80-150 Gbps EBS → C8ib.24-48xl ✅
│   └── > 150 Gbps EBS 或 > 720K IOPS → C8ib.96xl ✅（唯一选项）
│
└── 成本敏感、不追 peak 性能
    ├── 上一代网络密集型 → C6in（仍在售，便宜）
    ├── 上一代通用 Intel → C7i ⚡（AES 接近 C8，便宜）
    └── ARM 友好 → C8g / C8gn（本文未覆盖）
```

## 踩坑记录

!!! warning "踩坑 1: Launch 脚本没有 TTL 自毁钩子 → 空转 6 天"
    **事故经过**：本项目 Batch B（网络测试 4 台 24xlarge）在 2026-04-18 launch 后，测试脚本因环境问题未真正执行，且 launch 脚本没有任何自毁机制。实例从 4/18 空转到 4/24 被发现，合计烧掉约 $1,300+。
    
    **铁律修正**（已写入内部工作流）：
    
    1. 每个 launch 脚本必须内置 TTL trap：
       ```bash
       INSTANCE_IDS="i-xxx i-yyy"
       # trap 到脚本任何退出路径（正常 / Ctrl-C / 异常）
       trap 'aws ec2 terminate-instances --instance-ids $INSTANCE_IDS' EXIT INT TERM
       # 或硬超时：
       timeout 7200 bash test.sh || aws ec2 terminate-instances --instance-ids $INSTANCE_IDS
       ```
    2. Launch 时同步创建 CloudWatch Alarm：基于 tag + 存活时间超阈值触发 Lambda terminate。
    3. 任务结束必须 `describe-instances --filters "Name=tag:Task,Values=<slug>"` 验证无 running 残留才算完成。
    
    这是"绝不 0.0.0.0/0 SG"之后的第二条铁律：**绝不 launch 没有自毁机制的 benchmark 集群**。

!!! warning "踩坑 2: Amazon Linux 2023 没有 sysbench 包"
    **触发**：计划用 `sysbench cpu --threads=X --cpu-max-prime=Y` 做标准 CPU 基准，但 AL2023 官方 repo **没有 sysbench 包**。
    
    **尝试过的路径**：
    - 启用 EPEL → `nothing provides redhat-release >= 9`，AL2023 不兼容 EPEL-9
    - Percona 的 RHEL9 repo → 依赖链还是断
    - 从源码编译 → 时间成本过高
    
    **解决**：换用 AL2023 原生仓库有的 `stress-ng`（`--matrix`、`--prime` method）+ OpenSSL speed + STREAM。stress-ng 的 bogo ops/s 和 sysbench events/s 单位不同但都支持对比。
    
    **建议**：AL2023 上 CPU benchmark 首选 `stress-ng`，不要强上 sysbench。如果你非要 sysbench，用 Ubuntu 22.04 AMI（官方 repo 有）。

!!! info "观察：c8in 与 c8i 的 `/proc/cpuinfo` 有细微差异"
    两者 CPU model 都是 `Intel(R) Xeon(R) 6975P-C`，但 c8in 的 CPU flags 多出 `arch_perfmon`、`bus_lock_detect`、`monitor` 几个。这是同一颗芯片在 BIOS/microcode 层的暴露差异，不代表硬件差异 — 测试结果也证实 CPU 性能 <1% 差别。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|---|---|---|---|
| c8in.8xlarge On-Demand | ~$1.60/h | 0.78 h | $1.25 |
| c6in.8xlarge On-Demand | $1.454/h | 0.78 h | $1.14 |
| c8i.8xlarge On-Demand | ~$1.40/h | 0.78 h | $1.10 |
| c7i.8xlarge On-Demand | $1.36/h | 0.78 h | $1.06 |
| EBS gp3 root volume × 4 | 微量 | 1 h | ~$0.05 |
| 公网流量（下载 stream.c 等）| 微量 | | ~$0.01 |
| **合计（Batch A 实测）** | | **~47 min** | **~$4.60** |

Batch B/C/D 未执行对应费用为 $0（但事故额外产生 ~$1,300，已吸取教训）。

## 清理资源

```bash
# 1. 终止所有 benchmark 实例
INSTANCE_IDS=$(aws ec2 describe-instances --profile $PROFILE --region $REGION \
    --filters "Name=tag:Task,Values=c8in-c8ib" "Name=instance-state-name,Values=running,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
aws ec2 terminate-instances --profile $PROFILE --region $REGION --instance-ids $INSTANCE_IDS

# 2. 等 terminated 后先查 ENI 残留
aws ec2 describe-network-interfaces --profile $PROFILE --region $REGION \
    --filters "Name=group-id,Values=<SG_ID>"
# 如果有 attached ENI，先删它们

# 3. 删 SG → Placement Group → Subnet → Route Table → IGW → VPC
aws ec2 delete-security-group --profile $PROFILE --region $REGION --group-id <SG_ID>
aws ec2 delete-placement-group --profile $PROFILE --region $REGION --group-name c8-benchmark-pg
aws ec2 detach-internet-gateway --profile $PROFILE --region $REGION --internet-gateway-id <IGW_ID> --vpc-id <VPC_ID>
aws ec2 delete-internet-gateway --profile $PROFILE --region $REGION --internet-gateway-id <IGW_ID>
aws ec2 delete-subnet --profile $PROFILE --region $REGION --subnet-id <SUBNET_ID>
aws ec2 delete-route-table --profile $PROFILE --region $REGION --route-table-id <RTB_ID>
aws ec2 delete-vpc --profile $PROFILE --region $REGION --vpc-id <VPC_ID>

# 4. 删 key pair（可选，本地的 PEM 也删掉）
aws ec2 delete-key-pair --profile $PROFILE --region $REGION --key-name ec2-benchmark-2026-04
```

!!! danger "务必清理"
    Benchmark 机型即使 idle 也按小时计费，c8in.96xl 约 $20/h，c8ib.96xl 约 $25/h。完成后 **第一时间 terminate**，并用 tag filter 二次确认无残留。参考"踩坑 1"。

## 结论与建议

### 架构师选型三句话

1. **想要 Granite Rapids CPU？C8i 就够了**。C8in 和 C8i 是同一颗 CPU，C8in 多出来的钱全买网络带宽。
2. **想要 >100 Gbps 网络？只能选 C8in**；想要 >150 Gbps EBS？只能选 C8ib。这两个是唯一选项。
3. **"43%" 是 up-to 数字**。SIMD/内存/AES 能超 50%；整数密集只有 +14%。先 profile 你的负载再报预算。

### 升级/迁移建议

- ✅ **新项目**：直接用 C8i（通用）/ C8in（网络）/ C8ib（存储），跳过 C7。
- ⚠️ **现 C6in 大规模集群**：分批迁移，优先迁 SIMD/内存密集负载（收益最大）；整数密集的业务逻辑机优先级较低。
- ⚡ **现 C7i 集群**：仅在以下条件值得迁到 C8i/C8in —— (a) 瓶颈在内存带宽 (b) 瓶颈在 SIMD vector 计算 (c) 需要 > 50 Gbps 网络。纯 web/API 服务器不急迁。
- ❌ **C6in.32xlarge / metal**：已达 200 Gbps，如果你选它是为网络，C8in 同等规格起跳就是 200 Gbps，升级价值在 Nitro v6 / CPU 代差而非网络。

### 如果你从没用过这些

直接从 C6in 跳到 C8in 的客户可以期待：

- CPU 类负载：+14%~+55%，视向量化/内存依赖程度
- AES-GCM / TLS 终结：**+49%**，几乎翻一半
- 内存带宽：**+50%**，数据库/分析显著
- 网络上限：从 200 Gbps → 600 Gbps（3x）
- EBS 上限（若升 C8ib）：80 Gbps → 300 Gbps（3.75x）
- 新增能力：ENA Express、EFA 在更多规格可用、Nitro v6

### 我们没做的（以免误导）

- **iperf3 实测 600 Gbps** — 未做，基于官方规格
- **NCCL/OSU MPI on EFA** — 未做，基于官方规格
- **fio 实测 300 Gbps EBS / 1.44M IOPS** — 未做，基于官方规格
- **Redis/MySQL 应用层 benchmark** — 未做
- **Spot / Savings Plan 实际折扣定价** — 以 On-Demand 为基准

欢迎读者接力这些测试，我们愿意引用。

## 附录：原始测试脚本和数据

- 测试脚本（bash）：`content/evidence/ec2-c8in-c8ib-benchmark/scripts/` — 含 launch / bench / summary
- 原始 YAML/文本数据（12 个文件/机型 × 4 机型）：`content/evidence/ec2-c8in-c8ib-benchmark/metrics/batchA/`
- 资源清单与 CloudTrail 证据：同目录 `resources.md`

## 参考链接

- [Amazon EC2 C8in/C8ib GA 公告（What's New）](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-ec2-c8in-c8ib-instances-ga/)
- [EC2 Compute Optimized instances specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html)
- [EC2 C8i instance page](https://aws.amazon.com/ec2/instance-types/c8i/)
- [Configurable bandwidth weighting preferences](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-bandwidth-weighting.html)
- [EC2 Nitro System](https://aws.amazon.com/ec2/nitro/)
