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

核心问题：**Lightsail CO 比同规格 EC2 c7i 便宜约 36%，但性能和功能上差距有多大？什么时候该选 Lightsail，什么时候必须上 EC2？** 这篇文章通过实测数据回答。

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
| 2XLarge | 8 | 16 GB | 640 GB SSD | 7 TB | $168 |
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
| 2XLarge | c7i.2xlarge | 8 | 16 GB |
| 4XLarge | c7i.4xlarge | 16 | 32 GB |
| 12XLarge | c7i.12xlarge | 48 | 96 GB |

### Lightsail 月费 vs EC2 成本

| 规格 | Lightsail 月费 | EC2 On-Demand 月费 | 含存储+传输 EC2 等效 | Lightsail 省 |
|------|-----------|-------------|--------------|-------------|
| 2C/4G | $42 | $65.15 + EBS + 传输 | ~$85 | **51%** |
| 4C/8G | $84 | $130.31 + EBS + 传输 | ~$165 | **49%** |
| 8C/16G | $168 | $260.61 + EBS + 传输 | ~$315 | **47%** |
| 16C/32G | $336 | $521.22 + EBS + 传输 | ~$600 | **44%** |
| 48C/96G | $1,126 | $1,563.66 + EBS + 传输 | ~$1,670 | **33%** |

!!! tip "EC2 等效成本计算"
    EC2 月费 = On-Demand 时价 × 730h + gp3 EBS ($0.08/GB·月 × 320GB = $25.60) + 数据传输 ($0.09/GB × 出站量) + 弹性 IP ($3.60/月)。Lightsail 这些全包。

!!! info "EC2 还有更便宜的选项"
    EC2 1 年 Standard RI (No Upfront) 可省约 34%，3 年 RI 可省约 56%，Savings Plans 类似。Spot 实例更是可省 60-90%。Lightsail 没有这些灵活的购买选项。

## 动手实践

### Step 1: 创建 Lightsail Compute-Optimized 实例

```bash
aws lightsail create-instances \
  --instance-names "ls-co-test" \
  --availability-zone "us-east-1a" \
  --blueprint-id "ubuntu_24_04" \
  --bundle-id "c_xlarge_1_0" \
  --region us-east-1
```

设置防火墙（仅允许你的 IP 访问 SSH）：

```bash
MY_IP=$(curl -s ifconfig.me)
aws lightsail put-instance-public-ports \
  --region us-east-1 \
  --instance-name ls-co-test \
  --port-infos "[{\"fromPort\":22,\"toPort\":22,\"protocol\":\"tcp\",\"cidrs\":[\"${MY_IP}/32\"]}]"
```

### Step 2: 创建 EC2 c7i.xlarge 对照实例

```bash
# 获取最新 Ubuntu 24.04 AMI
AMI_ID=$(aws ec2 describe-images --region us-east-1 \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
             "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)

# 创建安全组（仅限你的 IP）
MY_IP=$(curl -s ifconfig.me)
SG_ID=$(aws ec2 create-security-group \
  --group-name "lightsail-test-sg" \
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
  --key-name "lightsail-test-key" \
  --region us-east-1 \
  --query "KeyMaterial" --output text > lightsail-test-key.pem
chmod 600 lightsail-test-key.pem

# 启动实例
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type c7i.xlarge \
  --key-name "lightsail-test-key" \
  --security-group-ids "$SG_ID" \
  --region us-east-1 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ec2-c7i-test}]' \
  --query "Instances[0].InstanceId" --output text)

echo "EC2 Instance: $INSTANCE_ID"
```

### Step 3: 安装测试工具

等待两台实例 running 后，SSH 进入分别安装：

```bash
sudo apt-get update -qq
sudo apt-get install -y sysbench fio stress-ng
```

### Step 4: 硬件信息对比

分别在两台实例上运行 `lscpu`：

**Lightsail Compute-Optimized XLarge:**
```
Model name: Intel(R) Xeon(R) Platinum 8124M CPU @ 3.00GHz
CPU(s): 4
Thread(s) per core: 2
```

**EC2 c7i.xlarge:**
```
Model name: Intel(R) Xeon(R) Platinum 8488C
CPU(s): 4
Thread(s) per core: 2
```

!!! warning "底层硬件代差：Skylake vs Sapphire Rapids"
    Lightsail CO 使用的是 2017 年的 Intel Xeon 8124M（Skylake 架构），而 EC2 c7i 使用 2023 年的 Xeon 8488C（Sapphire Rapids）。6 年的 CPU 代差直接影响了后续所有性能测试结果。**这是 Lightsail 官方文档中未记录的信息**——你无法在创建前知道底层 CPU 型号。

### Step 5: CPU 基准测试 (sysbench)

**单线程测试：**
```bash
sysbench cpu --cpu-max-prime=20000 --threads=1 run
```

**4 线程测试：**
```bash
sysbench cpu --cpu-max-prime=20000 --threads=4 run
```

**实测结果：**

| 测试 | Lightsail CO XL | EC2 c7i.xlarge | EC2 优势 |
|------|-----------------|----------------|----------|
| CPU 单线程 (events/s) | 450.60 | 1,224.87 | **+172%** |
| CPU 4线程 (events/s) | 1,406.65 | 2,523.54 | **+79%** |

EC2 c7i 的单线程性能是 Lightsail CO 的 **2.7 倍**。即便多线程场景，EC2 也快了近一倍。

### Step 6: 内存带宽测试 (sysbench)

```bash
sysbench memory --memory-total-size=10G --threads=4 run
```

| | Lightsail CO | EC2 c7i | 差异 |
|---|---|---|---|
| 带宽 (MiB/s) | 10,061 | 8,561 | Lightsail +18% |

!!! info "内存带宽 Lightsail 略高"
    这可能与 Skylake 的内存控制器特性或测试负载特性有关。实际应用中两者差异不大。

### Step 7: 磁盘 IO 测试

**顺序写（dd）：**
```bash
dd if=/dev/zero of=/tmp/testfile bs=1M count=1024 oflag=direct
```

**随机 IO（fio）：**
```bash
# 随机读
fio --name=randread --ioengine=libaio --direct=1 --bs=4k \
    --size=512M --numjobs=4 --runtime=30 --time_based \
    --rw=randread --group_reporting

# 随机写
fio --name=randwrite --ioengine=libaio --direct=1 --bs=4k \
    --size=512M --numjobs=4 --runtime=30 --time_based \
    --rw=randwrite --group_reporting
```

| 测试 | Lightsail CO | EC2 c7i (gp3) | 差异 |
|------|-------------|---------------|------|
| 顺序写 | 123 MB/s | 149 MB/s | EC2 +21% |
| 随机读 IOPS | 3,000 | 3,098 | 基本一致 |
| 随机写 IOPS | 2,921 | 3,027 | 基本一致 |

两者的随机 IO 性能几乎相同，都在 3,000 IOPS 左右——这恰好是 EC2 gp3 卷的 baseline。说明 Lightsail SSD 的底层也是 gp3 级别存储。

!!! tip "EC2 可以升级存储性能"
    EC2 支持 gp3 (最高 16,000 IOPS)、io2 (最高 64,000 IOPS)、io2 Block Express (最高 256,000 IOPS)。Lightsail 只有内置的 SSD 存储，无法升级 IO 性能。

### Step 8: 持续负载测试（边界测试）

验证 Lightsail CO 在持续满载下是否有性能降级：

```bash
stress-ng --cpu 4 --cpu-method matrixprod --timeout 300 --metrics-brief
```

| | Lightsail CO | EC2 c7i |
|---|---|---|
| bogo ops/s (60s) | 3,645.70 | 4,524.41 |
| 持续 5 分钟 | ✅ 无降速 | ✅ 无降速 |
| 性能差距 | | +24% |

**Lightsail CO 确实提供专用 CPU**——持续满载 5 分钟无任何 throttle，这与 General Purpose 的 burst 模型有本质区别。EC2 c7i 同样稳定，但凭借新一代 CPU 持续领先 24%。

## 测试结果汇总

| # | 测试项 | Lightsail CO XL | EC2 c7i.xlarge | 差异 | 备注 |
|---|--------|-----------------|----------------|------|------|
| 1 | CPU 单线程 | 450.60 events/s | 1,224.87 events/s | EC2 +172% | CPU 代差 |
| 2 | CPU 多线程 | 1,406.65 events/s | 2,523.54 events/s | EC2 +79% | CPU 代差 |
| 3 | 内存带宽 | 10,061 MiB/s | 8,561 MiB/s | LS +18% | |
| 4 | 顺序写 | 123 MB/s | 149 MB/s | EC2 +21% | |
| 5 | 随机读 IOPS | 3,000 | 3,098 | 一致 | gp3 baseline |
| 6 | 随机写 IOPS | 2,921 | 3,027 | 一致 | gp3 baseline |
| 7 | 持续负载 | 3,645 bogo/s | 4,524 bogo/s | EC2 +24% | 无 throttle |
| 8 | CPU 型号 | Xeon 8124M | Xeon 8488C | 6 年代差 | 官方未记录 |

## 踩坑记录

!!! warning "踩坑 1: Lightsail CO 底层 CPU 是 Skylake 旧芯片"
    Lightsail 官方文档完全没有记录实例使用的 CPU 型号。实测发现 Compute-Optimized 使用的是 2017 年的 Intel Xeon Platinum 8124M（Skylake），而同价位段的 EC2 c7i 已经用上 2023 年的 Sapphire Rapids。这导致单线程性能差距高达 172%。
    
    **影响**：对 CPU 性能敏感的工作负载（如单线程批处理、编译），选择 Lightsail CO 相当于用上一代硬件。

!!! warning "踩坑 2: Lightsail VPC Peering 仅限默认 VPC"
    Lightsail 的 VPC Peering 功能只能连接到同 Region 的**默认 VPC**。如果你的 AWS 资源在自定义 VPC 中，无法直接与 Lightsail 实例通信。已查文档确认。
    
    **影响**：需要和 RDS、ElastiCache 等 VPC 内服务交互的架构，Lightsail 会增加额外的网络复杂度。

!!! info "发现: Lightsail SSD 性能 ≈ EC2 gp3 baseline"
    两者的随机 IO 都稳定在 3,000 IOPS 左右，说明 Lightsail 内置 SSD 的性能等级与 EC2 gp3 默认配置（3,000 IOPS / 125 MB/s）一致。但 EC2 用户可以按需升级到 io2 获得更高 IO 性能，Lightsail 无此选项。

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

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lightsail CO XL | $84/月 ($0.115/hr) | ~1 hr | $0.12 |
| EC2 c7i.xlarge | $0.1785/hr | ~1 hr | $0.18 |
| EBS gp3 (默认 8GB) | $0.08/GB·月 | 1 hr | < $0.01 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
# 1. 删除 Lightsail 实例
aws lightsail delete-instance \
  --instance-name "ls-co-test" \
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
  --key-name "lightsail-test-key" \
  --region us-east-1

rm -f lightsail-test-key.pem
```

!!! danger "务必清理"
    Lightsail 按小时计费（最高不超过月费），EC2 按秒计费。Lab 完成后请立即执行清理。

## 结论与建议

### 性能 vs 成本权衡

Lightsail CO 比等规格 EC2 c7i On-Demand **便宜约 36-51%**（含存储和传输），但 CPU 性能低 **24-172%**。这意味着：

- **每花 1 美元获得的 CPU 性能**：EC2 c7i 仍然更优（特别是单线程场景）
- **如果你的工作负载对延迟不敏感**：Lightsail CO 的性价比可以接受
- **如果你需要最高性能**：EC2 c7i 是唯一选择

### 选型建议

| 场景 | 推荐 | 理由 |
|------|------|------|
| 个人项目/小团队 Web 服务 | ✅ Lightsail CO | 便宜、简单、包含传输和存储 |
| WordPress/电商站点 | ✅ Lightsail CO | 管理简单，一键部署 |
| 批处理/数据分析 | ⚠️ 看规模 | 小规模用 Lightsail 省钱；大规模用 EC2 + Spot |
| 视频编码 | ❌ EC2 | 需要最高单线程性能 |
| 游戏服务器 | ⚠️ 看需求 | 小型用 Lightsail；要求低延迟用 EC2 + Placement Group |
| 需要 Auto Scaling | ❌ EC2 | Lightsail 不支持 ASG |
| 需要 VPC 精细控制 | ❌ EC2 | Lightsail VPC 功能极有限 |
| 多服务架构（微服务） | ❌ EC2 | 需要 IAM Role、VPC、ALB 等完整生态 |
| 预算固定、不想管基础设施 | ✅ Lightsail CO | 月费可预测，管理最简单 |

### 一句话总结

> **Lightsail Compute-Optimized 是 "便宜但不快" 的选择：用旧款 CPU 换来低价和简单管理。适合预算敏感、对性能要求不极致的计算密集型工作负载。需要最强性能或企业级功能（ASG、IAM、VPC），EC2 c7i 仍是正解。**

## 参考链接

- [AWS What's New: Lightsail Compute-Optimized Instances](https://aws.amazon.com/about-aws/whats-new/2026/04/lightsail-compute-optimized-instances/)
- [Lightsail Pricing](https://aws.amazon.com/lightsail/pricing/)
- [Lightsail Instance Bundles](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
- [Lightsail CPU Baseline Performance](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)
- [Lightsail VPC Peering](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-how-to-set-up-vpc-peering-with-aws-resources.html)
- [EC2 c7i Instance Type](https://aws.amazon.com/ec2/instance-types/c7i/)
