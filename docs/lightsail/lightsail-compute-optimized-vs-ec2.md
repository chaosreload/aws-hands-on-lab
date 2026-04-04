# Amazon Lightsail Compute-Optimized 实测：与 EC2 c7i 的性能、成本、功能全面对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-04

## 背景

Amazon Lightsail 一直以"简单、可预测的定价"吸引中小型工作负载用户。但之前的 General Purpose 实例受 burst 模型限制——CPU 使用超过 baseline 就会消耗 burst 额度，持续高负载下性能不可预期。

2026 年 4 月，AWS 为 Lightsail 新增了 **Compute-Optimized** 实例类型，提供最高 72 vCPUs 的专用 CPU 算力，明确面向批处理、视频编码、游戏服务器等计算密集型场景。这让 Lightsail 首次有了和 EC2 C 系列正面竞争的资格。

核心问题：**Lightsail CO 比同规格 EC2 c7i 便宜约 36%，但性能和功能上差距有多大？什么时候该选 Lightsail，什么时候必须上 EC2？** 这篇文章通过 **8 vCPU / 16 GB（2XLarge）** 规格的实测数据回答。

## 前置条件

- AWS 账号
- AWS CLI v2 已配置
- 能通过 SSH 连接实例

## 核心概念

### Lightsail Compute-Optimized 规格一览（Linux, IPv4）

| 规格 | vCPU | 内存 | 存储 | 数据传输 | 月费 |
|------|------|------|------|----------|------|
| Large | 2 | 4 GB | 160 GB SSD | 5 TB | $42 |
| XLarge | 4 | 8 GB | 320 GB SSD | 6 TB | $84 |
| **2XLarge** | **8** | **16 GB** | **640 GB SSD** | **7 TB** | **$168** |
| 4XLarge | 16 | 32 GB | 1,280 GB SSD | 8 TB | $336 |
| 9XLarge | 36 | 72 GB | 1,280 GB SSD | 9 TB | $844 |
| 12XLarge | 48 | 96 GB | 1,280 GB SSD | 10 TB | $1,126 |
| 18XLarge | 72 | 144 GB | 1,280 GB SSD | 10 TB | $1,688 |

### 与 EC2 c7i 的规格对应

Lightsail CO 的 vCPU/内存比为 1:2（每 vCPU 配 2 GB 内存），和 EC2 c7i 系列一致。对应关系：

| Lightsail CO | EC2 c7i | vCPU | 内存 |
|-------------|---------|------|------|
| Large | c7i.large | 2 | 4 GB |
| XLarge | c7i.xlarge | 4 | 8 GB |
| **2XLarge** | **c7i.2xlarge** | **8** | **16 GB** |
| 4XLarge | c7i.4xlarge | 16 | 32 GB |
| 12XLarge | c7i.12xlarge | 48 | 96 GB |

### 真实 TCO 对比：不只是实例费

很多人只比较实例单价，但 Lightsail 月费包含了存储、数据传输和静态 IP，而 EC2 需要单独购买。以 2XLarge 规格为例：

| 成本项 | Lightsail CO 2XL | EC2 c7i.2xlarge |
|--------|-----------------|-----------------|
| 实例费 | 包含 | $260.61/月 (On-Demand) |
| 存储 640GB SSD | 包含 | gp3 $51.20/月 |
| 数据传输 7TB 出站 | 包含 | ~$630/月 ($0.09/GB) |
| 静态 IP | 包含 | $3.60/月 (EIP) |
| **月度总计** | **$168** | **$945.41** |

!!! tip "EC2 传输费是关键变量"
    上表假设用满 7TB 出站传输。如果你的应用出站流量很小（<100GB），EC2 传输费可以忽略，总成本降至 ~$315。**Lightsail 在高出站流量场景下优势巨大。**

**EC2 降价选项：**

| EC2 购买方式 | 实例月费 | + 存储 + 7TB传输 | vs Lightsail |
|-------------|---------|-----------------|-------------|
| On-Demand | $260.61 | $945.41 | LS 省 82% |
| 1yr RI (No Upfront) | ~$161.58 (38%↓) | ~$846.38 | LS 省 80% |
| 1yr Savings Plans | ~$161.58 | ~$846.38 | LS 省 80% |
| 3yr RI (All Upfront) | ~$109.50 (58%↓) | ~$794.30 | LS 省 79% |
| Spot (估) | ~$78–104 (60-70%↓) | ~$763–789 | LS 省 78–79% |

!!! warning "没有 7TB 出站的话"
    如果出站流量 <100GB，EC2 On-Demand 总成本约 $315，1yr RI 约 $216，3yr RI 约 $164。此时 **EC2 3yr RI 与 Lightsail 成本相当**，但性能高 45-164%。

## 动手实践

### Step 1: 创建 Lightsail Compute-Optimized 2XLarge 实例

```bash
aws lightsail create-instances \
  --instance-names "ls-co-2xl-test" \
  --availability-zone "us-east-1a" \
  --blueprint-id "amazon_linux_2023" \
  --bundle-id "c_2xlarge_1_0" \
  --region us-east-1
```

设置防火墙（仅允许你的 IP 访问 SSH）：

```bash
MY_IP=$(curl -s ifconfig.me)
aws lightsail put-instance-public-ports \
  --region us-east-1 \
  --instance-name ls-co-2xl-test \
  --port-infos "[{\"fromPort\":22,\"toPort\":22,\"protocol\":\"tcp\",\"cidrs\":[\"${MY_IP}/32\"]}]"
```

### Step 2: 创建 EC2 c7i.2xlarge 对照实例

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI_ID=$(aws ec2 describe-images --region us-east-1 \
  --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023.*-x86_64" \
             "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)

# 创建安全组（仅限你的 IP）
MY_IP=$(curl -s ifconfig.me)
SG_ID=$(aws ec2 create-security-group \
  --group-name "ls-co-2xl-test-sg" \
  --description "SSH only" \
  --region us-east-1 \
  --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" \
  --protocol tcp --port 22 \
  --cidr "${MY_IP}/32" \
  --region us-east-1

# 创建密钥对
aws ec2 create-key-pair \
  --key-name "ls-co-2xl-test-key" \
  --region us-east-1 \
  --query "KeyMaterial" --output text > ls-co-2xl-test-key.pem
chmod 600 ls-co-2xl-test-key.pem

# 启动实例
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type c7i.2xlarge \
  --key-name "ls-co-2xl-test-key" \
  --security-group-ids "$SG_ID" \
  --region us-east-1 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ec2-c7i-2xl-test}]' \
  --query "Instances[0].InstanceId" --output text)

echo "EC2 Instance: $INSTANCE_ID"
```

### Step 3: 安装测试工具

等待两台实例 running 后，SSH 进入分别安装：

```bash
# Amazon Linux 2023 需要从源码编译 sysbench
sudo yum install -y fio stress-ng gcc make automake libtool openssl-devel
cd /tmp
curl -sL https://github.com/akopytov/sysbench/archive/refs/tags/1.0.20.tar.gz | tar xz
cd sysbench-1.0.20
./autogen.sh && ./configure --without-mysql && make -j$(nproc) && sudo make install
```

### Step 4: 硬件信息对比

分别在两台实例上运行 `lscpu`：

**Lightsail Compute-Optimized 2XLarge:**
```
Model name: Intel(R) Xeon(R) Platinum 8275CL CPU @ 3.00GHz
CPU(s): 8
Thread(s) per core: 2
```

**EC2 c7i.2xlarge:**
```
Model name: Intel(R) Xeon(R) Platinum 8488C
CPU(s): 8
Thread(s) per core: 2
```

!!! warning "底层硬件代差：Cascade Lake vs Sapphire Rapids"
    Lightsail CO 2XLarge 使用的是 2019 年的 Intel Xeon 8275CL（Cascade Lake 架构），而 EC2 c7i 使用 2023 年的 Xeon 8488C（Sapphire Rapids）。4 年的 CPU 代差直接影响了性能表现。

    **有趣的是**：之前测试 XLarge 规格时，Lightsail 使用的是更老的 Xeon 8124M（Skylake, 2017）。**不同规格的 Lightsail CO 可能分配到不同代的 CPU**，官方文档未记录此信息。

### Step 5: CPU 基准测试 (sysbench)

每项测试跑 3 次取平均值。

**单线程测试：**
```bash
sysbench cpu --cpu-max-prime=20000 --threads=1 run
```

**8 线程测试：**
```bash
sysbench cpu --cpu-max-prime=20000 --threads=8 run
```

**实测结果（3 次平均值）：**

| 测试 | Lightsail CO 2XL | EC2 c7i.2xlarge | EC2 优势 |
|------|-----------------|-----------------|----------|
| CPU 单线程 (events/s) | 464.47 | 1,227.56 | **+164%** |
| CPU 8线程 (events/s) | 3,144.21 | 5,101.05 | **+62%** |

EC2 c7i 的单线程性能是 Lightsail CO 的 **2.6 倍**。多线程场景下 EC2 也快了 62%。

### Step 6: 内存带宽测试 (sysbench)

```bash
sysbench memory --memory-block-size=1M --memory-total-size=100G run
```

| | Lightsail CO 2XL | EC2 c7i.2xlarge | 差异 |
|---|---|---|---|
| 带宽 (MiB/s) | 21,075 | 32,742 | EC2 **+55%** |

!!! info "内存带宽差异翻转"
    在 XLarge 测试中 Lightsail 内存带宽略高于 EC2。升级到 2XLarge 后，EC2 凭借 Sapphire Rapids 更宽的内存通道反超 55%。这说明在内存密集型工作负载（如大型缓存、内存数据库）上，EC2 优势会随规格增大而更明显。

### Step 7: 磁盘 IO 测试

**顺序写（dd）：**
```bash
dd if=/dev/zero of=/home/ec2-user/test bs=1M count=1024 oflag=direct
```

**随机 IO（fio）：**
```bash
# 随机读
fio --name=randread --ioengine=libaio --direct=1 --bs=4k \
    --size=1G --numjobs=4 --runtime=60 --time_based \
    --rw=randread --group_reporting

# 随机写
fio --name=randwrite --ioengine=libaio --direct=1 --bs=4k \
    --size=1G --numjobs=4 --runtime=60 --time_based \
    --rw=randwrite --group_reporting
```

| 测试 | Lightsail CO 2XL | EC2 c7i.2xlarge (gp3) | 差异 |
|------|-----------------|----------------------|------|
| 顺序写 | 137 MB/s | 149 MB/s | EC2 +9% |
| 随机读 IOPS | 3,049 | 3,048 | 一致 |
| 随机写 IOPS | 3,018 | 3,048 | 一致 |

两者的随机 IO 性能几乎相同，都在 3,000 IOPS 左右——这恰好是 EC2 gp3 卷的 baseline。说明 Lightsail SSD 的底层也是 gp3 级别存储。

!!! tip "EC2 可以升级存储性能"
    EC2 支持 gp3 (最高 16,000 IOPS)、io2 (最高 64,000 IOPS)、io2 Block Express (最高 256,000 IOPS)。Lightsail 只有内置的 SSD 存储，无法升级 IO 性能。

### Step 8: 持续负载测试（边界测试）

验证 Lightsail CO 在持续满载下是否有性能降级：

```bash
stress-ng --matrix 8 --timeout 120s --metrics-brief
```

| 指标 | Lightsail CO 2XL | EC2 c7i.2xlarge | 差异 |
|------|-----------------|-----------------|------|
| sub matrix ops/s | 261,241 | 379,335 | EC2 **+45%** |
| square matrix ops/s | 362.92 | 513.16 | EC2 +41% |
| trans matrix ops/s | 49,195 | 73,524 | EC2 +49% |
| 持续 2 分钟 | ✅ 无降速 | ✅ 无降速 | |

**Lightsail CO 确实提供专用 CPU**——持续满载 2 分钟无任何 throttle，这与 General Purpose 的 burst 模型有本质区别。EC2 c7i 同样稳定，凭借新一代 CPU 全面领先 41-49%。

## 测试结果汇总

| # | 测试项 | Lightsail CO 2XL | EC2 c7i.2xlarge | EC2 优势 | 备注 |
|---|--------|-----------------|-----------------|----------|------|
| 1 | CPU 型号 | Xeon 8275CL (2019) | Xeon 8488C (2023) | 4 年代差 | 官方未记录 |
| 2 | CPU 单线程 | 464 events/s | 1,228 events/s | **+164%** | 代差影响最大 |
| 3 | CPU 8线程 | 3,144 events/s | 5,101 events/s | **+62%** | 多线程差距缩小 |
| 4 | 内存带宽 | 21,075 MiB/s | 32,742 MiB/s | **+55%** | Sapphire Rapids 内存更强 |
| 5 | 顺序写 | 137 MB/s | 149 MB/s | +9% | 都是 gp3 级别 |
| 6 | 随机读 IOPS | 3,049 | 3,048 | 0% | gp3 baseline |
| 7 | 随机写 IOPS | 3,018 | 3,048 | +1% | gp3 baseline |
| 8 | 持续负载 | 261,241 ops/s | 379,335 ops/s | **+45%** | 无 throttle |

## 适用场景对比：谁该选 Lightsail，谁该选 EC2？

性能数据只是决策的一个维度。**选型取决于你的具体场景**：

### ✅ Lightsail CO 的甜区

**1. 高出站流量的应用（Lightsail 绝对优势）**

Lightsail CO 2XL 包含 **7TB 月度出站传输**。如果你的应用有大量出站流量（如 CDN 源站、媒体分发、API 服务），EC2 的传输费（$0.09/GB）会非常昂贵。7TB 出站在 EC2 上需要额外 ~$630/月。

- 视频流/直播源站
- 大文件下载服务
- 高流量 API Gateway 后端
- 游戏资源分发服务器

**2. 预算可预测的小团队/创业公司**

$168/月，一切包含，没有意外账单。对于预算严格且不想操心 AWS 成本优化的团队，这是最大的价值。

- 种子轮创业公司的后端服务
- 外包项目的固定报价环境
- 个人开发者的 Side Project

**3. 快速启动、不需要复杂基础设施**

Lightsail 省去了 VPC 规划、IAM Role 配置、安全组管理的复杂度。5 分钟内就能跑起一个实例。

- PoC / Demo 环境
- Hackathon 项目
- 临时数据处理任务

**4. 不追求极致性能的 CPU 密集任务**

虽然比 EC2 c7i 慢 45-164%，但对于很多工作负载来说，**够用就行**：

- CI/CD Runner（构建时间长 45% 但省 82% 费用）
- 视频转码（非实时场景，多花点时间换省钱）
- 定期批处理（夜间报表、数据 ETL）
- 小型 ML 推理服务（延迟不敏感）

**5. 单实例架构**

如果你的应用只需要一台服务器（不需要 Auto Scaling），Lightsail 的简单管理模型是优势而非限制。

- WordPress / Ghost 博客
- 小型电商站点
- 企业内部工具

### ❌ 应该选 EC2 c7i 的场景

**1. 性能敏感的生产工作负载**

单线程快 164%、多线程快 62% 的差距在生产环境中意义重大。每一毫秒延迟都影响用户体验和收入的场景必须用 EC2。

- 在线交易系统
- 实时数据分析
- 游戏服务器（低延迟要求）
- 高频 API 服务

**2. 需要弹性伸缩**

Lightsail 不支持 Auto Scaling Group。流量波动大的应用需要 EC2 + ASG。

- 电商大促
- 媒体/社交平台
- SaaS 应用

**3. 需要 AWS 完整生态**

Lightsail 是一个"围墙花园"——与 AWS 其他服务的集成有限。

- 需要 VPC 内访问 RDS/ElastiCache → EC2
- 需要 IAM Instance Profile → EC2
- 需要 Systems Manager → EC2
- 需要 CloudWatch 详细监控 → EC2
- 需要自定义网络架构（Transit Gateway、PrivateLink）→ EC2

**4. 可以用 RI/Spot 大幅降成本**

如果出站流量不大（<100GB/月），EC2 + 3yr RI 的成本与 Lightsail 相当，但性能高 45-164%。

- 长期运行的后台服务 → EC2 + RI
- 容错批处理 → EC2 + Spot（省 60-90%）
- 无状态 Worker → EC2 + Spot Fleet

**5. 需要高性能存储**

Lightsail SSD 约等于 gp3 baseline（3,000 IOPS）。需要更高 IO 的场景必须用 EC2。

- 数据库服务器 → EC2 + io2
- 日志分析平台 → EC2 + gp3 (调高 IOPS)

### 📊 决策流程图

```
你的应用月出站流量 > 1TB？
├── 是 → Lightsail CO 可能更省钱 → 对性能要求高吗？
│   ├── 是 → 计算 EC2 总成本（含传输费），对比后决定
│   └── 否 → ✅ Lightsail CO
└── 否 → EC2 大概率更优 → 需要弹性伸缩/VPC/IAM 吗？
    ├── 是 → ✅ EC2
    └── 否 → 对比 Lightsail $168 vs EC2+gp3 ~$315 (OD) / ~$216 (1yr RI)
        ├── 预算紧 → ✅ Lightsail CO
        └── 性能优先 → ✅ EC2
```

## 功能对比

| 维度 | Lightsail CO | EC2 c7i |
|------|-------------|---------|
| **定价模型** | 月费包含存储+传输 | 按需计费，各组件分开 |
| **购买选项** | 仅 On-Demand | On-Demand / RI / SP / Spot |
| **VPC** | Lightsail 专属 VPC，Peering 限默认 VPC | 完全自定义 VPC |
| **IAM** | Lightsail 级别策略 | 完整 IAM（Instance Profile、Role） |
| **Auto Scaling** | ❌ 不支持 | ✅ ASG + Target Tracking |
| **Placement Group** | ❌ 不支持 | ✅ Cluster / Spread / Partition |
| **存储选择** | 固定 SSD（≈gp3 baseline） | gp3 / io2 / st1 / sc1 |
| **监控** | 基础指标（CPU/网络/磁盘） | CloudWatch 详细指标 + 自定义 |
| **Systems Manager** | ❌ | ✅ SSM Agent / Session Manager |
| **静态 IP** | ✅ 包含 | 需单独购买 EIP ($3.60/月) |
| **DNS 管理** | ✅ 内置 | 需用 Route 53 |
| **管理控制台** | 简化界面 | 完整 EC2 Console |
| **SSH 访问** | 浏览器一键 SSH | 需自行配置 |
| **底层 CPU** | 旧款（Cascade Lake/Skylake） | 当代（Sapphire Rapids） |

## 踩坑记录

!!! warning "踩坑 1: Lightsail CO 底层 CPU 是旧款芯片，且不同规格可能不同"
    Lightsail 官方文档完全没有记录实例使用的 CPU 型号。实测发现：
    
    - **2XLarge**: Intel Xeon 8275CL（Cascade Lake, 2019）
    - **XLarge**: Intel Xeon 8124M（Skylake, 2017）
    
    而对标的 EC2 c7i 统一使用 Xeon 8488C（Sapphire Rapids, 2023）。**你在创建实例前无法知道会分配到哪代 CPU。**

!!! warning "踩坑 2: Lightsail VPC Peering 仅限默认 VPC"
    Lightsail 的 VPC Peering 功能只能连接到同 Region 的**默认 VPC**。如果你的 AWS 资源在自定义 VPC 中，无法直接与 Lightsail 实例通信。已查文档确认。
    
    **影响**：需要和 RDS、ElastiCache 等 VPC 内服务交互的架构，Lightsail 会增加额外的网络复杂度。

!!! info "发现: Lightsail SSD 性能 ≈ EC2 gp3 baseline"
    两者的随机 IO 都稳定在 3,000 IOPS 左右，说明 Lightsail 内置 SSD 的性能等级与 EC2 gp3 默认配置（3,000 IOPS / 125 MB/s）一致。但 EC2 用户可以按需升级到 io2 获得更高 IO 性能，Lightsail 无此选项。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lightsail CO 2XL | $168/月 ($0.23/hr) | ~1 hr | $0.23 |
| EC2 c7i.2xlarge | $0.357/hr | ~1 hr | $0.36 |
| EBS gp3 (默认 8GB) | $0.08/GB·月 | 1 hr | < $0.01 |
| **合计** | | | **< $1.00** |

## 清理资源

```bash
# 1. 删除 Lightsail 实例
aws lightsail delete-instance \
  --instance-name "ls-co-2xl-test" \
  --region us-east-1

# 2. 终止 EC2 实例
aws ec2 terminate-instances \
  --instance-ids "$INSTANCE_ID" \
  --region us-east-1

# 等待实例完全终止
aws ec2 wait instance-terminated \
  --instance-ids "$INSTANCE_ID" \
  --region us-east-1

# 3. 删除安全组
aws ec2 delete-security-group \
  --group-id "$SG_ID" \
  --region us-east-1

# 4. 删除密钥对
aws ec2 delete-key-pair \
  --key-name "ls-co-2xl-test-key" \
  --region us-east-1

rm -f ls-co-2xl-test-key.pem
```

!!! danger "务必清理"
    Lightsail 按小时计费（最高不超过月费），EC2 按秒计费。Lab 完成后请立即执行清理。

## 结论与建议

### 性能 vs 成本的真相

简单说"Lightsail 便宜但慢"是不够的。**真正的对比取决于你的出站流量**：

| 场景 | 月出站流量 | Lightsail CO 2XL | EC2 c7i.2xlarge (OD) | 赢家 |
|------|-----------|-----------------|---------------------|------|
| 高流量 | 7 TB | $168 | $945 | **Lightsail 省 82%** |
| 中流量 | 1 TB | $168 | $405 | **Lightsail 省 59%** |
| 低流量 | 100 GB | $168 | $324 | **Lightsail 省 48%** |
| 极低流量 | ~0 | $168 | $315 | **Lightsail 省 47%** |
| 低流量 + 3yr RI | ~0 | $168 | ~$164 | **EC2 便宜 2%，性能+45%** |

### 一句话总结

> **Lightsail Compute-Optimized 不是"穷人版 EC2"——它是面向出站流量大、预算可预测、不需要复杂 AWS 生态的计算密集型工作负载的合理选择。月出站流量超过 1TB 时，Lightsail 的成本优势远超其性能劣势。但如果你需要最强性能、弹性伸缩或 AWS 完整生态，EC2 c7i 仍是正解。**

## 参考链接

- [AWS What's New: Lightsail Compute-Optimized Instances](https://aws.amazon.com/about-aws/whats-new/2026/04/lightsail-compute-optimized-instances/)
- [Lightsail Pricing](https://aws.amazon.com/lightsail/pricing/)
- [Lightsail Instance Bundles](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
- [Lightsail CPU Baseline Performance](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)
- [Lightsail VPC Peering](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-how-to-set-up-vpc-peering-with-aws-resources.html)
- [EC2 c7i Instance Type](https://aws.amazon.com/ec2/instance-types/c7i/)
