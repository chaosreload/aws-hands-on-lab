# Amazon EC2 I7i 存储优化实例实测：NVMe 性能提升 50% 的真实数据

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $0.36
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

AWS 于 2025 年 4 月发布了 EC2 I7i 存储优化实例，声称相比上一代 I4i：

- NVMe 存储性能提升 50%
- 存储 I/O 延迟降低 50%
- I/O 延迟变异性降低 60%
- 计算性能提升 23%

这些数字有多少是营销话术，有多少经得起 fio 的考验？本文通过 i7i.large vs i4i.large 的完整对比测试，用实测数据说明白。

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

### 为什么选 .large 做对比

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

## 测试结果

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

**结论**：延迟在高负载下降低 ~33%，低负载下降低 ~29%。AWS 声称的"50% lower latency"在 .large 规格上未完全复现，可能适用于更大规格。

### CPU 性能对比

| 指标 | i7i.large | i4i.large | 提升 |
|------|----------|----------|------|
| 处理器 | Xeon 8559C (Emerald Rapids) | Xeon 8375C (Ice Lake) | - |
| openssl aes-256-cbc 16KB | **2,470 MB/s** | 1,992 MB/s | **+24.0%** |

**结论**：CPU 性能提升 24%，与 AWS 声称的 23% 基本一致 ✅

### 性价比分析

| 指标 | i7i.large | i4i.large | 变化 |
|------|----------|----------|------|
| 价格 | $0.1888/hr | $0.172/hr | +9.8% |
| 随机读 IOPS | 77,282 | 51,470 | +50.1% |
| **每千 IOPS 成本** | **$0.00244** | $0.00334 | **-27%** |

**结论**：价格贵 9.8%，但 IOPS 提升 50%，每 IOPS 成本降低 27%。性价比提升远超 10% ✅

## 踩坑记录

!!! warning "低负载延迟尖峰"
    i7i.large 在 iodepth=1 时 p99.9 出现 254 μs 尖峰，高于 i4i 的 119 μs。
    这可能是第 3 代 Nitro SSD 的 GC（垃圾回收）行为导致。
    **实测发现，官方未记录。** 建议对延迟敏感的场景关注尾部延迟而非仅看平均值。

!!! warning "预处理速度差异揭示底层差距"
    全盘顺序写预处理：i7i 达到 394 MB/s（19 分钟），i4i 仅 263 MB/s（28 分钟）。
    即使在"预热"阶段，i7i 已展现 50% 的写入速度优势。

!!! info "AWS 延迟声称的解读"
    AWS 声称"50% lower storage I/O latency"和"60% reduced I/O latency variability"。
    实测在 .large 规格上延迟降低 29-33%（而非 50%），变异性改善不明显。
    **已查文档确认：** 这些数值可能基于更大规格实例（如 48xlarge/metal）的测试条件。
    .large 规格的 SSD 只有 1 块 468GB 盘，无法完全发挥 Nitro SSD 的并行 I/O 优势。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| i7i.large | $0.1888/hr | ~1 hr | $0.19 |
| i4i.large | $0.172/hr | ~1 hr | $0.17 |
| **合计** | | | **$0.36** |

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
    Lab 完成后请执行清理步骤，避免产生意外费用。i7i.large 每小时 $0.19。

## 结论与建议

### 实测验证总结

| AWS 声称 | 实测结果 | 验证 |
|---------|---------|------|
| 存储性能提升 50% | +50% (IOPS & 吞吐) | ✅ 完全一致 |
| I/O 延迟降低 50% | -29%~33% (.large) | ⚠️ 部分符合 |
| 延迟变异性降低 60% | 高负载下 -32% | ⚠️ 部分符合 |
| 计算性能提升 23% | +24% | ✅ 基本一致 |
| 性价比提升 10%+ | 每 IOPS 成本 -27% | ✅ 远超预期 |

### 适用场景

- **强烈推荐**：高并发数据库（MongoDB、Cassandra、Redis）、实时分析（Kafka、Spark）
- **推荐**：搜索引擎（Elasticsearch）、AI 训练数据预处理
- **注意**：如果工作负载是低并发、延迟超敏感型（如单线程 OLTP），需额外评估尾部延迟

### 从 I4i 迁移建议

1. **可直接替换**：相同规格（.large → .large），API 兼容，无应用改动
2. **成本几乎持平**：价格仅贵 9.8%，性能提升 50%
3. **新增 Hibernation 支持**：适合开发/测试环境节约成本
4. **考虑 Torn Write Prevention**：运行 MySQL、PostgreSQL 等数据库时可关闭 double-write buffer，进一步提升性能

## 参考链接

- [Amazon EC2 I7i 产品页](https://aws.amazon.com/ec2/instance-types/i7i/)
- [EC2 存储优化实例规格文档](https://docs.aws.amazon.com/ec2/latest/instancetypes/so.html)
- [AWS What's New: I7i 发布公告](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-ec2-i7i-high-performance-storage-optimized-instances/)
