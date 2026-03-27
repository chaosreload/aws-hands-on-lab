# CloudWatch Agent 采集 EBS 详细性能统计实战：精准定位存储性能瓶颈

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

你是否遇到过这样的场景：应用延迟突然飙升，但 CPU 和内存都看起来正常？根本原因很可能藏在 EBS 存储层 —— 你的卷正在被 IOPS 或吞吐量限制"掐脖子"，但传统的 CloudWatch EBS 指标粒度不够，无法告诉你**到底是卷的限制还是实例的限制**在起作用。

2025 年 6 月 9 日，AWS 发布了 CloudWatch Agent 对 EBS 详细性能统计的支持。通过在 CloudWatch Agent 配置中添加 `diskio` 段，你可以直接从 NVMe 驱动层采集 11 个高精度指标，包括读写操作数、字节数、耗时，以及关键的**性能超限时间** —— 精确到微秒级别地告诉你应用何时超出了卷或实例的 IOPS/吞吐量上限。

本文将从零开始配置 CloudWatch Agent 采集 EBS 详细性能统计，用 `fio` 模拟正常和超限负载，对比 CloudWatch 指标与 NVMe 原生统计的数据差异，最后设置告警实现主动监控。

## 前置条件

- AWS 账号，具备 EC2、IAM、CloudWatch 相关权限
- AWS CLI v2 已配置
- Nitro-based EC2 实例（本文使用 t3.medium）

## 核心概念

### EBS NVMe 详细性能统计是什么？

EBS NVMe block device 原生提供实时高精度 I/O 性能统计，以**累积计数器**形式呈现。这些统计从卷挂载到实例的那一刻开始累积，直到卷被卸载。

### 之前 vs 现在

| 维度 | 之前（标准 CloudWatch EBS 指标） | 现在（NVMe 详细性能统计） |
|------|------|------|
| 数据来源 | CloudWatch hypervisor 层采集 | NVMe 驱动层直接暴露 |
| 最小粒度 | 1 分钟（Detailed Monitoring: 5 分钟） | 1 秒 |
| 超限检测 | 无直接指标，需推算 | ✅ 精确的 IOPS/吞吐量超限时间（微秒） |
| 瓶颈定位 | 无法区分卷限制 vs 实例限制 | ✅ 分别报告卷超限和实例超限 |
| 延迟分布 | 仅平均值 | ✅ 延迟直方图（ebsnvme/nvme-cli） |
| 费用 | 免费（EBS 指标） | NVMe 统计免费，CW Agent 发布为自定义指标按 CW 定价 |

### 11 个 CW Agent 可采集的 EBS 指标

CloudWatch Agent 通过 `diskio` 配置段采集以下 NVMe 指标：

| 指标 | CloudWatch 指标名 | 说明 |
|------|------------------|------|
| 读操作数 | `diskio_ebs_total_read_ops` | 完成的读操作累积总数 |
| 写操作数 | `diskio_ebs_total_write_ops` | 完成的写操作累积总数 |
| 读字节数 | `diskio_ebs_total_read_bytes` | 读传输累积总字节 |
| 写字节数 | `diskio_ebs_total_write_bytes` | 写传输累积总字节 |
| 读耗时 | `diskio_ebs_total_read_time` | 读操作累积耗时（μs） |
| 写耗时 | `diskio_ebs_total_write_time` | 写操作累积耗时（μs） |
| 卷 IOPS 超限 | `diskio_ebs_volume_performance_exceeded_iops` | IOPS 超出卷预配上限的累积时间（μs） |
| 卷吞吐量超限 | `diskio_ebs_volume_performance_exceeded_tp` | 吞吐量超出卷预配上限的累积时间（μs） |
| 实例 IOPS 超限 | `diskio_ebs_ec2_instance_performance_exceeded_iops` | IOPS 超出实例上限的累积时间（μs） |
| 实例吞吐量超限 | `diskio_ebs_ec2_instance_performance_exceeded_tp` | 吞吐量超出实例上限的累积时间（μs） |
| 队列深度 | `diskio_ebs_volume_queue_length` | 当前等待完成的读写操作数（瞬时值） |

!!! tip "关键洞察：四个超限指标是核心价值"
    传统 EBS 指标只能看到 IOPS 和吞吐量"是多少"，但无法直接告诉你"有没有被限制"。这四个 `exceeded` 指标**精确量化了被限流的时间**，而且分别区分了**卷级别限制**和**实例级别限制**，帮你快速定位瓶颈在哪一层。

### 限制条件

- 仅支持 **Nitro-based EC2 实例**
- 支持**所有 EBS 卷类型**（gp2/gp3/io1/io2/st1/sc1）
- Multi-Attach 卷支持，统计按各实例独立
- NVMe 原生统计**免费**，CW Agent 发布的自定义指标按 CloudWatch 定价收费

## 动手实践

### Step 1: 创建 IAM Role

CloudWatch Agent 需要权限发送指标到 CloudWatch，SSM 用于远程管理实例。

```bash
# 创建信任策略文件
cat > /tmp/ec2-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# 创建 IAM Role
aws iam create-role \
  --role-name CWAgentEBSTestRole \
  --assume-role-policy-document file:///tmp/ec2-trust-policy.json

# 附加策略
aws iam attach-role-policy \
  --role-name CWAgentEBSTestRole \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

aws iam attach-role-policy \
  --role-name CWAgentEBSTestRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# 创建 Instance Profile
aws iam create-instance-profile \
  --instance-profile-name CWAgentEBSTestProfile

aws iam add-role-to-instance-profile \
  --instance-profile-name CWAgentEBSTestProfile \
  --role-name CWAgentEBSTestRole
```

### Step 2: 启动 EC2 实例 + 附加 EBS 卷

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI_ID=$(aws ec2 describe-images \
  --owners amazon \
  --filters "Name=name,Values=al2023-ami-2*-x86_64" "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" \
  --output text \
  --region us-east-1)

# 创建安全组（仅允许出站，通过 SSM 管理，无入站规则）
SG_ID=$(aws ec2 create-security-group \
  --group-name cw-ebs-test-sg \
  --description "CW Agent EBS test - SSM only, no inbound" \
  --region us-east-1 \
  --query GroupId --output text)

# 启动 Nitro 实例（t3.medium）
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type t3.medium \
  --iam-instance-profile Name=CWAgentEBSTestProfile \
  --security-group-ids $SG_ID \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=cw-ebs-test}]" \
  --region us-east-1 \
  --query "Instances[0].InstanceId" --output text)

aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-east-1

# 创建 gp3 测试卷（3000 IOPS / 125 MiB/s 基线配置）
AZ=$(aws ec2 describe-instances \
  --instance-ids $INSTANCE_ID \
  --query "Reservations[0].Instances[0].Placement.AvailabilityZone" \
  --output text --region us-east-1)

VOL_ID=$(aws ec2 create-volume \
  --volume-type gp3 --size 10 --iops 3000 --throughput 125 \
  --availability-zone $AZ \
  --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=cw-ebs-test-data}]" \
  --region us-east-1 \
  --query VolumeId --output text)

aws ec2 wait volume-available --volume-ids $VOL_ID --region us-east-1

aws ec2 attach-volume \
  --volume-id $VOL_ID \
  --instance-id $INSTANCE_ID \
  --device /dev/xvdf \
  --region us-east-1
```

### Step 3: 安装 CloudWatch Agent + fio

通过 SSM 在实例上执行命令（无需 SSH Key）：

```bash
aws ssm send-command \
  --instance-ids $INSTANCE_ID \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["yum install -y amazon-cloudwatch-agent fio nvme-cli"]}' \
  --region us-east-1
```

### Step 4: 配置 CloudWatch Agent 采集 EBS 指标

创建 CloudWatch Agent 配置文件，在 `metrics_collected.diskio` 中指定所有 11 个 EBS 指标：

```json
{
  "agent": {
    "metrics_collection_interval": 10,
    "debug": false
  },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": {
      "InstanceId": "${aws:InstanceId}"
    },
    "metrics_collected": {
      "diskio": {
        "measurement": [
          "ebs_total_read_ops",
          "ebs_total_write_ops",
          "ebs_total_read_bytes",
          "ebs_total_write_bytes",
          "ebs_total_read_time",
          "ebs_total_write_time",
          "ebs_volume_performance_exceeded_iops",
          "ebs_volume_performance_exceeded_tp",
          "ebs_ec2_instance_performance_exceeded_iops",
          "ebs_ec2_instance_performance_exceeded_tp",
          "ebs_volume_queue_length"
        ],
        "metrics_collection_interval": 10
      }
    }
  }
}
```

将配置写入实例并启动 Agent：

```bash
# 将上述 JSON 配置 base64 编码后写入实例
aws ssm send-command \
  --instance-ids $INSTANCE_ID \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["echo <BASE64_ENCODED_CONFIG> | base64 -d > /opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-agent.json","amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-agent.json","amazon-cloudwatch-agent-ctl -a status"]}' \
  --region us-east-1
```

验证 Agent 状态应显示 `"status": "running"` 和 `"configstatus": "configured"`。

### Step 5: 生成 I/O 负载并观察指标

**正常负载测试**（1000 IOPS，远低于 gp3 的 3000 IOPS 上限）：

```bash
# 格式化并挂载测试卷
mkfs.xfs /dev/nvme1n1
mkdir -p /mnt/data
mount /dev/nvme1n1 /mnt/data

# 正常负载：1000 IOPS 随机读，60 秒
fio --name=normal-read \
  --filename=/mnt/data/testfile \
  --size=1G --bs=4k --iodepth=4 \
  --rw=randread --ioengine=libaio --direct=1 \
  --rate_iops=1000 --runtime=60 --time_based
```

**超限负载测试**（尝试远超 3000 IOPS 上限）：

```bash
# 超限负载：4 并发 × 64 队列深度随机读，60 秒
fio --name=heavy-read \
  --filename=/mnt/data/testfile \
  --size=1G --bs=4k --iodepth=64 \
  --rw=randread --ioengine=libaio --direct=1 \
  --numjobs=4 --runtime=60 --time_based
```

### Step 6: 查看 NVMe 原生统计（对比参考）

在实例上使用 `ebsnvme` 脚本或 `nvme-cli` 直接查看 NVMe 原始统计：

```bash
# 方式 1：ebsnvme 脚本
wget -q https://raw.githubusercontent.com/amazonlinux/amazon-ec2-utils/refs/heads/main/ebsnvme -O /tmp/ebsnvme
chmod +x /tmp/ebsnvme
python3 /tmp/ebsnvme stats /dev/nvme1n1

# 方式 2：nvme-cli
nvme amzn stats /dev/nvme1n1
```

### Step 7: 查询 CloudWatch 指标数据

```bash
aws cloudwatch get-metric-data \
  --metric-data-queries '[
    {"Id":"read_ops","MetricStat":{"Metric":{"Namespace":"CWAgent","MetricName":"diskio_ebs_total_read_ops","Dimensions":[{"Name":"InstanceId","Value":"'$INSTANCE_ID'"},{"Name":"VolumeId","Value":"'$VOL_ID'"}]},"Period":60,"Stat":"Average"}},
    {"Id":"iops_exceeded","MetricStat":{"Metric":{"Namespace":"CWAgent","MetricName":"diskio_ebs_volume_performance_exceeded_iops","Dimensions":[{"Name":"InstanceId","Value":"'$INSTANCE_ID'"},{"Name":"VolumeId","Value":"'$VOL_ID'"}]},"Period":60,"Stat":"Average"}}
  ]' \
  --start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --region us-east-1
```

## 测试结果

### fio 性能对比：正常 vs 超限

| 指标 | 正常负载（1000 IOPS） | 超限负载（max IOPS） | 变化 |
|------|------|------|------|
| 实际 IOPS | 999 | 3,049 | gp3 上限 3000 |
| 带宽 | 4.0 MiB/s | 11.9 MiB/s | 3x |
| 平均延迟 | 597 μs | 82,620 μs | **138 倍增加** |
| p99 延迟 | 1.2 ms | 305 ms | 254 倍 |
| 磁盘利用率 | 6.28% | 99.88% | 饱和 |

### EBS NVMe 超限指标

| 指标 | 正常负载后 | 超限负载后 | 说明 |
|------|------|------|------|
| IOPS exceeded | 0 μs | 41,892,491 μs（≈42s） | ✅ 60 秒测试中 70% 时间在被 IOPS 限流 |
| Throughput exceeded | 2,219,379 μs | 2,219,379 μs（未变） | 仅初始文件 layout 时触发 |
| EC2 Instance IOPS exceeded | 0 μs | 0 μs | t3.medium 实例非瓶颈 |
| EC2 Instance TP exceeded | 0 μs | 0 μs | t3.medium 实例非瓶颈 |
| Queue length | 0 | 2 | 超限时队列积压 |

!!! success "关键发现"
    **超限指标精确区分了瓶颈层级**：IOPS exceeded 仅在**卷级别**触发（`volume_performance_exceeded_iops`），而**实例级别**始终为 0（`ec2_instance_performance_exceeded_iops`）。这意味着 t3.medium 的 EBS 性能余量充足，瓶颈完全在 gp3 卷的 3000 IOPS 上限。生产环境中，如果实例级别指标也非零，说明需要升级实例类型。

### CloudWatch 指标数据（60 秒周期）

| 时间 (UTC) | read_ops/分钟 | IOPS exceeded (μs) | TP exceeded (μs) | queue_length |
|---|---|---|---|---|
| 23:46 | 70 | 0 | 91,172 | 0.2 |
| 23:47 | 9,000 | 0 | 309,115 | 0.5 |
| 23:48 | 4,077 | 599,525 | 0 | 0.3 |
| 23:49 | 27,433 | 6,382,557 | 0 | 2.0 |
| 23:50 | 0 | 0 | 0 | 0 |

### CW Agent 指标 vs NVMe 原生统计

| 维度 | CW Agent (diskio_ebs_*) | ebsnvme / nvme-cli |
|------|------|------|
| 数据处理 | **差值（delta）**—— Agent 自动计算相邻采集间的增量 | **累积值（counter）**—— 从卷挂载起一直累加 |
| 输出位置 | CloudWatch 自定义指标，可设告警/Dashboard | 实例本地 CLI 输出 |
| 延迟直方图 | ❌ 不采集 | ✅ 提供 28 档延迟分布 |
| 多卷区分 | 通过 `VolumeId` 维度自动区分 | 需手动指定设备名 |

### NVMe 延迟直方图示例（ebsnvme 独有）

正常负载时的读延迟分布：

```
Read IO Latency Histogram (us)
=================================
Lower       Upper        IO Count
=================================
[256      - 512     ] => 15,869   (26%)
[512      - 1024    ] => 43,877   (73%)  ← 大部分读在 0.5-1ms
[1024     - 2048    ] => 514      (0.8%)
[2048     - 4096    ] => 66
[4096     - 8192    ] => 3
```

!!! note "延迟直方图的价值"
    CW Agent 不采集延迟直方图，但 `ebsnvme` 和 `nvme-cli` 可以直接获取。直方图比平均延迟更有诊断价值 —— 它能揭示延迟是均匀分布还是存在长尾（bimodal distribution），后者往往暗示间歇性限流。

## 踩坑记录

!!! warning "注意事项"

    **1. CW Agent 指标维度是 VolumeId，不是 device name**
    
    查询 CloudWatch 指标时，维度是 `InstanceId` + `VolumeId`（如 `vol-0940721d12dfcde37`），**不是** `name=nvme1n1`。如果用错维度会查不到数据。（实测发现，官方文档未明确说明维度格式）

    **2. CW Agent 的 delta processor 自动做差值**
    
    NVMe 原始统计是**累积计数器**，但 CW Agent 配置了 `diskio` 后会自动启用 delta processor，发布到 CloudWatch 的是**增量值**。这意味着 CW 中看到的 `read_ops=1000` 表示"过去一个采集周期内新增 1000 次读"，而非总计。（已查文档确认：CW Agent 日志中 `delta processor required because metrics with diskio are set`）

    **3. Throughput exceeded 可能在意外场景触发**
    
    我们在正常读测试（1000 IOPS）时并未预期触发吞吐量超限，但 fio 的 `--size=1G` 参数在首次运行时会先顺序写入 1GB 文件（layout phase），这个瞬间写入速度超过了 gp3 的 125 MiB/s 基线吞吐量，导致 `throughput_exceeded` 累积了 2.2 秒。（AWS 限制，非操作问题）

    **4. CW Agent 需要 root 权限才能访问 NVMe ioctl**
    
    官方文档指出 "CloudWatch agent binary requires ioctl permissions for NVMe driver devices"。默认以 root 运行的 CW Agent 无需额外配置，但如果用 `run_as_user: cwagent`，需要确保该用户有 NVMe 设备的 ioctl 权限。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| t3.medium | $0.0416/hr | ~0.5 hr | $0.02 |
| gp3 10GB | $0.08/GB/month | ~1 hr | < $0.01 |
| CW 自定义指标 | $0.30/metric/month | 11 指标 × 2 卷 | Free Tier 覆盖 |
| NVMe 原始统计 | 免费 | - | $0.00 |
| **合计** | | | **< $0.05** |

!!! tip "生产环境成本估算"
    假设 10 台实例，每台 2 个 EBS 卷，采集全部 11 个指标：10 × 2 × 11 = 220 个自定义指标 × $0.30/月 = **$66/月**。如果只关注 4 个 exceeded 指标 + queue_length（最有价值的 5 个），成本降至 **$30/月**。

## 清理资源

```bash
# 1. 终止 EC2 实例（会自动删除 root 卷）
aws ec2 terminate-instances \
  --instance-ids $INSTANCE_ID \
  --region us-east-1

aws ec2 wait instance-terminated \
  --instance-ids $INSTANCE_ID \
  --region us-east-1

# 2. 删除附加 EBS 卷
aws ec2 delete-volume \
  --volume-id $VOL_ID \
  --region us-east-1

# 3. 删除安全组
aws ec2 delete-security-group \
  --group-id $SG_ID \
  --region us-east-1

# 4. 清理 IAM 资源
aws iam remove-role-from-instance-profile \
  --instance-profile-name CWAgentEBSTestProfile \
  --role-name CWAgentEBSTestRole

aws iam delete-instance-profile \
  --instance-profile-name CWAgentEBSTestProfile

aws iam detach-role-policy \
  --role-name CWAgentEBSTestRole \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

aws iam detach-role-policy \
  --role-name CWAgentEBSTestRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam delete-role --role-name CWAgentEBSTestRole
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。CW 自定义指标在停止发送后不会继续计费，但 EC2 实例和 EBS 卷会持续产生费用。

## 结论与建议

### 适用场景

- **存储性能 SLA 监控**：通过 `exceeded` 指标设置告警，在应用受影响前主动发现限流
- **容量规划决策**：根据超限频率决定是否需要升级 EBS 卷配置或 EC2 实例类型
- **故障根因分析**：区分瓶颈是在 EBS 卷层还是 EC2 实例 EBS 带宽层
- **I/O 模式分析**：结合读写比例、字节数、队列深度，理解应用 I/O 特征

### 生产环境建议

1. **优先监控 5 个关键指标**：4 个 `exceeded` + `queue_length`，性价比最高
2. **设置 CloudWatch Alarm**：当 `volume_performance_exceeded_iops > 0` 时告警，表示卷开始被限流
3. **结合 ebsnvme 做深度诊断**：CW 指标发现问题后，登录实例用 `ebsnvme stats` 查看延迟直方图做根因分析
4. **EKS 场景**：使用 CloudWatch Observability EKS add-on v4.1.0+，启用 EBS CSI driver metrics 自动采集

### 三种采集方式选型

| 场景 | 推荐方式 | 理由 |
|------|---------|------|
| 持续监控 + 告警 | CW Agent `diskio` | 自动推送到 CW，支持 Alarm + Dashboard |
| 一次性诊断 | `ebsnvme` 脚本 | 含延迟直方图，无需配置 |
| EKS 容器环境 | CW Observability add-on 或 Prometheus | 原生集成，自动发现 |
| Windows 实例 | `nvme_amzn.exe` | 需 AWSNVMe driver v1.7.0+ |

## 参考链接

- [Amazon EBS detailed performance statistics](https://docs.aws.amazon.com/ebs/latest/userguide/nvme-detailed-performance-stats.html)
- [Collect Amazon EBS NVMe driver metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-metrics-EBS-Collect.html)
- [CloudWatch Agent Configuration File Details](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Agent-Configuration-File-Details.html)
- [Amazon CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)
- [AWS What's New: Amazon CloudWatch agent adds support for EBS detailed performance statistics](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-cloudwatch-agent-ebs-performance-statistics/)
