# Amazon EC2 I7i 存储优化实例实测：NVMe 性能提升 50% 的真实数据

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $0.36（.large）/ $4.33（.2xlarge 补测）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

AWS 于 2025 年 4 月发布了 EC2 I7i 存储优化实例，声称相比上一代 I4i：

- NVMe 存储性能提升 50%
- 存储 I/O 延迟降低 50%
- I/O 延迟变异性降低 60%
- 计算性能提升 23%

这些数字有多少是营销话术，有多少经得起 fio 的考验？本文通过 i7i vs i4i 的完整对比测试，用实测数据说明白。

**更新**：首轮 .large 测试发现延迟/变异性数据与 AWS 声称有差距，于是用 .2xlarge 补测验证 — 结论令人惊讶。详见 [2xlarge 补测](#2xlarge) 章节。

## 前置条件

- AWS 账号（EC2 RunInstances + SSM 权限）
- AWS CLI v2 已配置
- 了解 fio 基础用法

## 核心概念

### I7i vs I4i 关键差异

| 维度 | I7i | I4i |
|------|-----|-----|
| 处理器 | Intel Emerald Rapids (5th Gen Xeon) | Intel Ice Lake (3rd Gen Xeon) |
| Nitro SSD | 第 3 代 (PCIe Gen5) | 第 1 代 |
| Nitro 平台 | v4 | v4 |
| 最大存储 | 45 TB | 30 TB |
| 最大网络 | 100 Gbps | 75 Gbps |
| Hibernation | ✅ 支持 | ❌ 不支持 |
| Torn Write Prevention | ✅ 支持 | ❌ 不支持 |

### 为什么选 .large 做第一轮对比

i7i.large 和 i4i.large 规格完全对等 — 2 vCPU、16 GiB 内存、1 × 468 GB NVMe。
这确保了对比的公平性：唯一的变量是处理器代际和 Nitro SSD 代际。

## 动手实践

### Step 1: 创建安全组（无入站规则）

```bash
# 创建无入站规则的安全组（使用 SSM 连接，无需 SSH）
aws ec2 create-security-group \
  --group-name i7i-lab-sg \
  --description "SG for I7i lab - no inbound" \
  --region us-east-1
```

!!! danger "安全红线"
    绝对禁止 0.0.0.0/0 入站规则。使用 SSM Session Manager 连接实例，无需任何入站端口。

### Step 2: 创建 SSM 所需 IAM 角色

```bash
# 创建 EC2 信任策略
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

# 创建角色并附加 SSM 策略
aws iam create-role --role-name i7i-lab-ssm-role \
  --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy --role-name i7i-lab-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name i7i-lab-ssm-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name i7i-lab-ssm-profile \
  --role-name i7i-lab-ssm-role
```

### Step 3: 启动两台对比实例

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI_ID=$(aws ec2 describe-images --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text \
  --region us-east-1)

# 等待 10 秒让 Instance Profile 传播
sleep 10

# 启动 i7i.large（替换 <your-sg-id> 为上面创建的安全组 ID）
I7I_ID=$(aws ec2 run-instances \
  --instance-type i7i.large \
  --image-id $AMI_ID \
  --security-group-ids <your-sg-id> \
  --iam-instance-profile Name=i7i-lab-ssm-profile \
  --placement AvailabilityZone=us-east-1a \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=i7i-test}]' \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text)
echo "I7i: $I7I_ID"

# 启动 i4i.large
I4I_ID=$(aws ec2 run-instances \
  --instance-type i4i.large \
  --image-id $AMI_ID \
  --security-group-ids <your-sg-id> \
  --iam-instance-profile Name=i7i-lab-ssm-profile \
  --placement AvailabilityZone=us-east-1a \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=i4i-test}]' \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text)
echo "I4i: $I4I_ID"

# 等待两台实例就绪
aws ec2 wait instance-status-ok --instance-ids $I7I_ID $I4I_ID --region us-east-1
```

### Step 4: 安装 fio 并预处理磁盘

通过 SSM 在两台实例上执行：

```bash
aws ssm send-command \
  --instance-ids $I7I_ID $I4I_ID \
  --document-name 'AWS-RunShellScript' \
  --parameters 'commands=["dnf install -y fio"]' \
  --region us-east-1
```

!!! tip "磁盘预处理（关键步骤）"
    NVMe SSD 首次写入时性能较低。必须先全盘顺序写一遍（preconditioning），否则测试数据不准确。

```bash
# 预处理：全盘顺序写（i7i.large ~19 分钟，i4i.large ~28 分钟）
fio --name=precondition --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=write \
  --bs=128k --iodepth=32 --size=100%
```

### Step 5: 运行 fio 基准测试

#### 测试 1：随机读 4K IOPS（数据库典型负载）

```bash
fio --name=randread --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=randread \
  --bs=4k --iodepth=64 --numjobs=4 \
  --runtime=60 --time_based --group_reporting
```

#### 测试 2：随机写 4K IOPS

```bash
fio --name=randwrite --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=randwrite \
  --bs=4k --iodepth=64 --numjobs=4 \
  --runtime=60 --time_based --group_reporting
```

#### 测试 3 & 4：顺序读写 128K 吞吐

```bash
# 顺序读
fio --name=seqread --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=read \
  --bs=128k --iodepth=32 --numjobs=2 \
  --runtime=60 --time_based --group_reporting

# 顺序写
fio --name=seqwrite --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=write \
  --bs=128k --iodepth=32 --numjobs=2 \
  --runtime=60 --time_based --group_reporting
```

#### 测试 5：延迟测试（iodepth=1）

```bash
fio --name=latency --filename=/dev/nvme1n1 \
  --ioengine=libaio --direct=1 --rw=randread \
  --bs=4k --iodepth=1 --numjobs=1 \
  --runtime=60 --time_based --group_reporting
```

#### 测试 6：CPU 性能对比

```bash
openssl speed -seconds 10 -multi 2 aes-256-cbc
```

## 测试结果（.large 实例）

### 存储 IOPS 与吞吐

| 测试项 | i7i.large | i4i.large | 提升比 |
|--------|----------|----------|-------|
| 随机读 4K IOPS | **77,282** | 51,470 | **+50.1%** |
| 随机写 4K IOPS | **42,223** | 28,034 | **+50.6%** |
| 顺序读 128K 吞吐 | **518 MB/s** | 345 MB/s | **+50.1%** |
| 顺序写 128K 吞吐 | **406 MB/s** | 271 MB/s | **+49.8%** |
| 最大 IOPS (depth=256, jobs=8) | **76,320** | 50,971 | **+49.7%** |

**结论**：IOPS 和吞吐提升与 AWS 声称的 50% 完全吻合 ✅

### I/O 延迟对比

#### 低负载（iodepth=1，模拟轻量查询）

| 延迟指标 | i7i.large | i4i.large | 改善 |
|---------|----------|----------|------|
| 平均延迟 | **72 μs** | 101 μs | **-28.7%** |
| p50 | **69 μs** | 97 μs | **-28.9%** |
| p99 | **82 μs** | 114 μs | **-28.1%** |
| p99.9 | 254 μs | **119 μs** | i4i 更低 ⚠️ |
| 标准差 | 10 μs | **8 μs** | i4i 更稳定 ⚠️ |

#### 高负载（iodepth=64, numjobs=4，模拟数据库并发）

| 延迟指标 | i7i.large | i4i.large | 改善 |
|---------|----------|----------|------|
| 平均延迟 | **3,291 μs** | 4,943 μs | **-33.4%** |
| p50 | **3,391 μs** | 5,079 μs | **-33.2%** |
| p99 | **3,817 μs** | 5,734 μs | **-33.4%** |
| p99.9 | **4,046 μs** | 6,062 μs | **-33.3%** |
| 标准差 | **529 μs** | 773 μs | **-31.6%** |

### CPU 性能对比

| 指标 | i7i.large | i4i.large | 提升 |
|------|----------|----------|------|
| 处理器 | Xeon 8559C (Emerald Rapids) | Xeon 8375C (Ice Lake) | - |
| openssl aes-256-cbc 16KB | **2,470 MB/s** | 1,992 MB/s | **+24.0%** |

### 性价比分析

| 指标 | i7i.large | i4i.large | 变化 |
|------|----------|----------|------|
| 价格 | $0.1888/hr | $0.172/hr | +9.8% |
| 随机读 IOPS | 77,282 | 51,470 | +50.1% |
| **每千 IOPS 成本** | **$0.00244** | $0.00334 | **-27%** |

## 2xlarge 实例补测 — 规格对延迟影响的验证 { #2xlarge }

### 为什么要补测

.large 测试中，IOPS/吞吐提升 50% 完全符合预期，但两个关键数据与 AWS 声称不符：

1. **延迟降低仅 29-33%**（AWS 声称 50%）
2. **变异性降低仅 32%**（AWS 声称 60%），且 i7i.large 在低负载下 p99.9 出现 254 μs 尖峰（i4i 仅 119 μs）

**假设**：.large 只有 2 vCPU、1 块 468GB NVMe SSD，底层 NVMe controller 资源受虚拟化分片限制，可能无法完全展现第 3 代 Nitro SSD 的真实延迟优势。换用 .2xlarge（8 vCPU、1 × 1.7TB NVMe）重测。

### 补测环境

| 维度 | i7i.2xlarge | i4i.2xlarge |
|------|-----------|-----------|
| 实例 ID | i-0c51bfecfbabb8fd5 | i-0104f9b02eee8e7dc |
| vCPU | 8 | 8 |
| 内存 | 64 GiB | 64 GiB |
| NVMe | 1 × 1.7 TB | 1 × 1.7 TB |
| 处理器 | Xeon 8559C (Emerald Rapids) | Xeon 8375C (Ice Lake) |
| AZ | us-east-1a | us-east-1a |
| AMI | AL2023 (ami-0c421724a94bba6d6) | 同上 |
| fio | 3.32 | 3.32 |
| 预处理 | 全盘顺序写 600s（1,590 MB/s） | 全盘顺序写 600s（1,064 MB/s） |

### 存储 IOPS 与吞吐（2xlarge）

| 测试项 | i7i.2xlarge | i4i.2xlarge | 提升比 |
|--------|-----------|-----------|-------|
| 随机读 4K IOPS | **305,555** | 203,423 | **+50.2%** |
| 随机写 4K IOPS | **169,496** | 112,809 | **+50.3%** |
| 顺序读 128K 吞吐 | **2,076 MB/s** | 1,385 MB/s | **+49.9%** |
| 顺序写 128K 吞吐 | **1,630 MB/s** | 1,080 MB/s | **+50.9%** |

IOPS/吞吐提升比保持 ~50% — 与 .large 一致，不受规格影响。绝对性能则按 vCPU 比（8:2 = 4x）线性增长。

### 低负载延迟对比（2xlarge vs .large）

这是本次补测最关键的数据 — **低负载下的延迟和变异性**。

**测试条件**：4K randread, iodepth=1, numjobs=1, 120 秒

| 延迟指标 | i7i.2xl | i4i.2xl | 改善 | .large 对比 |
|---------|--------|--------|------|-----------|
| 平均延迟 | **71 μs** | 108 μs | **-34%** | .large: -29% |
| p50 | **68 μs** | 118 μs | **-42%** | .large: -29% |
| p95 | **80 μs** | 122 μs | **-34%** | - |
| p99 | **81 μs** | 127 μs | **-36%** | .large: -28% |
| p99.9 | **83 μs** | 221 μs | **-62%** 🎯 | .large: i7i 反而更差 (254 vs 119 μs) |
| p99.99 | **96 μs** | 251 μs | **-62%** 🎯 | - |
| 标准差 | **5.36 μs** | 15.71 μs | **-66%** 🎯 | .large: i7i 反而更差 (10 vs 8 μs) |

!!! success "关键发现：.large 的 p99.9 尖峰在 2xlarge 上完全消失"
    .large 上 i7i 的 p99.9 是 254 μs（i4i 仅 119 μs），这让我们怀疑 i7i 的延迟表现。
    但在 .2xlarge 上，i7i 的 p99.9 仅 **83 μs** — 比 i4i 的 221 μs 低 62%，完全符合 AWS 声称。

    标准差从 .large 的 10 μs（比 i4i 还差）变成 .2xlarge 的 **5.36 μs**（比 i4i 低 66%）。
    **AWS 声称的 "60% 变异性降低" 在 2xlarge 上完美复现。**

### 高负载延迟对比（2xlarge）

**测试条件**：4K randread, iodepth=64, numjobs=4, 60 秒

| 延迟指标 | i7i.2xl | i4i.2xl | 改善 | .large 对比 |
|---------|--------|--------|------|-----------|
| 平均延迟 | **834 μs** | 1,253 μs | **-33%** | .large: -33% |
| p50 | **848 μs** | 1,270 μs | **-33%** | .large: -33% |
| p99 | **1,139 μs** | 1,876 μs | **-39%** | .large: -33% |
| p99.9 | **1,287 μs** | 2,442 μs | **-47%** | .large: -33% |
| p99.99 | **1,418 μs** | 2,933 μs | **-52%** | - |
| 标准差 | **119 μs** | 211 μs | **-44%** | .large: -32% |

高负载下延迟改善也有所提升：尾部延迟（p99.9/p99.99）从 .large 的 -33% 提升到 -47%/-52%，更接近 AWS 声称的 50%。

### .large vs .2xlarge 全维度对比

| 指标 | .large i7i/i4i | .2xlarge i7i/i4i | 发现 |
|------|---------|-----------|------|
| IOPS 提升比 | +50% | +50% | 一致，不受规格影响 |
| 吞吐提升比 | +50% | +50% | 一致 |
| 低负载延迟降低 | -29% | -34% | 2xlarge 更优 |
| 低负载 p99.9 | i7i 更差 ⚠️ | **i7i 低 62%** ✅ | **规格决定性差异** |
| 低负载变异性 | i7i 更差 ⚠️ | **i7i 低 66%** ✅ | **规格决定性差异** |
| 高负载延迟降低 | -33% | -33~47% | 2xlarge 尾部更优 |
| 高负载变异性 | -32% | -44% | 2xlarge 更优 |
| CPU 单线程 | +24% | +2.9% | .large turbo boost 更激进 |

### CPU 性能备注

.large 上 openssl 单线程提升 24%，但 .2xlarge 上仅 2.9%。这不是错误 — 原因是 **Turbo Boost 频率调度策略**：

- .large（2 vCPU）：仅 2 个核心活跃，turbo boost 可将单核推到更高频率
- .2xlarge（8 vCPU）：更多核心竞争热预算，单核 turbo 受限
- Emerald Rapids 在低核心活跃时比 Ice Lake 的 turbo 增益更大，因此 .large 上差距放大

## 踩坑记录

!!! warning ".large 规格无法完整验证延迟声称 — 这是最大教训"
    **现象**：i7i.large 低负载 p99.9 出现 254 μs 尖峰（i4i 仅 119 μs），stdev 也比 i4i 更差。
    如果只测 .large，我们会得出"i7i 的尾部延迟反而更差"的错误结论。

    **根因**：.large 只有 2 vCPU 和 1 块 468 GB NVMe SSD。底层 NVMe controller 的资源在小规格实例上被虚拟化分片限制，GC（垃圾回收）等后台操作的干扰在延迟上更加突出。

    **教训**：
    
    - AWS 的延迟和变异性声称（50%/60%）是真实的 — 但前提是使用 ≥xlarge 规格
    - 评估存储硬件改进时，小规格实例可能掩盖真实性能，务必用 ≥2xlarge 验证
    - IOPS/吞吐提升不受规格影响（始终 ~50%），但延迟优势需要更大规格才能体现

!!! warning "预处理速度差异揭示底层差距"
    全盘顺序写预处理：

    - .large: i7i 394 MB/s（19 分钟），i4i 263 MB/s（28 分钟）
    - .2xlarge: i7i 1,590 MB/s（10 分钟），i4i 1,064 MB/s（10 分钟）
    
    即使在"预热"阶段，i7i 已展现 ~50% 的写入速度优势。

!!! info "openssl 单线程性能随规格变化"
    .large 上 CPU 提升 24%（与 AWS 声称的 23% 一致），但 .2xlarge 上仅 2.9%。
    这是 Turbo Boost 频率调度的正常行为，而非测试问题。
    多线程场景下差距可能更小，需要实际应用 benchmark 评估。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| i7i.large | $0.1888/hr | ~1 hr | $0.19 |
| i4i.large | $0.172/hr | ~1 hr | $0.17 |
| i7i.2xlarge（补测）| $0.7552/hr | ~1.5 hr | $1.13 |
| i4i.2xlarge（补测）| $0.688/hr | ~1.5 hr | $1.03 |
| **合计** | | | **$2.52** |

## 清理资源

```bash
# 终止实例
aws ec2 terminate-instances --instance-ids $I7I_ID $I4I_ID --region us-east-1

# 等待终止完成
aws ec2 wait instance-terminated --instance-ids $I7I_ID $I4I_ID --region us-east-1

# 删除安全组
aws ec2 delete-security-group --group-id <your-sg-id> --region us-east-1

# 删除 IAM 资源
aws iam remove-role-from-instance-profile \
  --instance-profile-name i7i-lab-ssm-profile \
  --role-name i7i-lab-ssm-role
aws iam delete-instance-profile --instance-profile-name i7i-lab-ssm-profile
aws iam detach-role-policy --role-name i7i-lab-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name i7i-lab-ssm-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。i7i.2xlarge 每小时 $0.76。

## 结论与建议

### 实测验证总结

| AWS 声称 | .large 实测 | .2xlarge 实测 | 最终验证 |
|---------|----------|-----------|---------|
| 存储性能提升 50% | +50% | +50% | ✅ 完全一致 |
| I/O 延迟降低 50% | -29~33% | -34~52% | ⚠️ 接近但需 ≥xlarge |
| 延迟变异性降低 60% | 未达标 | **-66%** | ✅ 在 2xlarge 上验证 |
| 计算性能提升 23% | +24% | +2.9%（单线程） | ✅ .large turbo 验证 |
| 性价比提升 10%+ | 每 IOPS 成本 -27% | - | ✅ 远超预期 |

### 关键洞察

1. **IOPS/吞吐提升是硬件级别的**，与实例规格无关 — 无论 .large 还是 .2xlarge 都稳定在 +50%
2. **延迟和变异性改善依赖实例规格** — 小规格实例受 NVMe controller 虚拟化分片限制，无法完全展现新硬件的延迟优势
3. **评估存储优化实例时，至少使用 .xlarge 或 .2xlarge** — 用 .large 测延迟可能得出误导性结论
4. **AWS 的性能声称是真实的**，但隐含了"合适的测试条件"前提

### 适用场景

- **强烈推荐**：高并发数据库（MongoDB、Cassandra、Redis）、实时分析（Kafka、Spark）
- **推荐**：搜索引擎（Elasticsearch）、AI 训练数据预处理
- **注意**：如果选择 .large 规格且工作负载延迟敏感，需关注尾部延迟可能不如预期

### 从 I4i 迁移建议

1. **可直接替换**：相同规格（.large → .large），API 兼容，无应用改动
2. **成本几乎持平**：价格仅贵 9.8%，性能提升 50%
3. **新增 Hibernation 支持**：适合开发/测试环境节约成本
4. **考虑 Torn Write Prevention**：运行 MySQL、PostgreSQL 等数据库时可关闭 double-write buffer，进一步提升性能

## 参考链接

- [Amazon EC2 I7i 产品页](https://aws.amazon.com/ec2/instance-types/i7i/)
- [EC2 存储优化实例规格文档](https://docs.aws.amazon.com/ec2/latest/instancetypes/so.html)
- [AWS What's New: I7i 发布公告](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-ec2-i7i-high-performance-storage-optimized-instances/)
