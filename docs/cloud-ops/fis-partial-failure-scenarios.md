---
tags:
  - Cloud Operations
---

# AWS FIS 新场景实测：用 AZ Slowdown 和 Cross-AZ Packet Loss 模拟灰色故障

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

你的应用突然变慢了，但所有健康检查都显示正常。延迟从 10ms 飙到 200ms，部分请求超时，但不是所有请求都受影响。这不是完全宕机，而是**灰色故障（Gray Failure）**——比完全中断更常见、更难检测、更难定位的部分降级。

2025 年 11 月，AWS Fault Injection Service（FIS）在 Scenario Library 中新增了两个专门模拟灰色故障的场景：

- **AZ: Application Slowdown** — 在单个 AZ 内注入网络延迟
- **Cross-AZ: Traffic Slowdown** — 在跨 AZ 流量上注入丢包

本文将通过实测对比这两个场景的效果，用真实数据展示灰色故障对网络性能的量化影响。

## 前置条件

- AWS 账号（需要 FIS、EC2、IAM、SSM 权限）
- AWS CLI v2 已配置
- 基本了解 VPC 网络和 AZ 概念

## 核心概念

### 两个新场景对比

| 维度 | AZ: Application Slowdown | Cross-AZ: Traffic Slowdown |
|------|-------------------------|---------------------------|
| 模拟故障 | 单 AZ 内资源间网络延迟升高 | 跨 AZ 出站流量丢包 |
| 默认参数 | 200ms 延迟, 100% 流量, 30min | 15% 丢包, 100% 出站流量, 30min |
| 底层机制 | SSM 文档 `AWSFIS-Run-Network-Latency-Sources` | SSM 文档 `AWSFIS-Run-Network-Packet-Loss-Sources` |
| 可调参数 | AZ、延迟(ms)、流量百分比、时长 | AZ、丢包率(%)、流量百分比、时长 |
| 适用场景 | 验证应用对延迟升高的敏感度 | 验证跨 AZ 通信降级时的容错能力 |

### Scenario Library vs CLI

FIS Scenario Library 是 **Console-only** 体验——你无法通过 API/CLI 直接调用场景模板。但场景本质上是预配置的 Experiment Template，我们可以手动构造等效的模板通过 CLI 运行。

关键差异：Console 创建的模板默认 `emptyTargetResolutionMode=skip`（无目标时跳过），CLI 创建的模板默认 `fail`（无目标时实验失败）。

## 动手实践

### 测试架构

```
┌─── us-east-1a ───┐     ┌─── us-east-1b ───┐
│  Instance A       │     │  Instance B       │
│  10.0.1.73        │◄───►│  10.0.2.253       │
│  (故障注入目标)    │     │  (跨 AZ 测量)      │
│                   │     └──────────────────┘
│  Instance C       │
│  10.0.1.82        │
│  (同 AZ 测量)     │
└──────────────────┘
```

### Step 1: 创建 VPC 和网络

```bash
# 创建 VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=fis-test-vpc}]" \
  --region us-east-1 \
  --query "Vpc.VpcId" --output text)

aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support '{"Value":true}' --region us-east-1
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}' --region us-east-1

# 创建子网
SUBNET_A=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.0.1.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=fis-test-subnet-1a}]" \
  --region us-east-1 --query "Subnet.SubnetId" --output text)

SUBNET_B=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.0.2.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=fis-test-subnet-1b}]" \
  --region us-east-1 --query "Subnet.SubnetId" --output text)

# 配置 Internet Gateway（SSM Agent 需要出站访问）
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=fis-test-igw}]" \
  --region us-east-1 --query "InternetGateway.InternetGatewayId" --output text)

aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID --region us-east-1

# 配置路由
RTB_ID=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
  --region us-east-1 --query "RouteTables[0].RouteTableId" --output text)
aws ec2 create-route --route-table-id $RTB_ID --destination-cidr-block 0.0.0.0/0 \
  --gateway-id $IGW_ID --region us-east-1
aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_A --region us-east-1
aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_B --region us-east-1

# 启用公有 IP 自动分配
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_A --map-public-ip-on-launch --region us-east-1
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_B --map-public-ip-on-launch --region us-east-1
```

### Step 2: 创建安全组

```bash
# 创建 SG — 仅允许 VPC 内部互通
SG_ID=$(aws ec2 create-security-group \
  --group-name fis-test-sg \
  --description "FIS test - VPC internal only" \
  --vpc-id $VPC_ID \
  --region us-east-1 --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID --protocol -1 --cidr 10.0.0.0/16 --region us-east-1
```

!!! danger "安全提示"
    不要开放 `0.0.0.0/0` 入站规则。FIS 实验通过 SSM Agent 执行，不需要 SSH 入站端口。

### Step 3: 创建 IAM 角色

```bash
# EC2 Instance Profile（SSM Agent 需要）
cat > /tmp/ec2-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
  ]
}
EOF

aws iam create-role --role-name fis-test-ec2-ssm-role \
  --assume-role-policy-document file:///tmp/ec2-trust.json
aws iam attach-role-policy --role-name fis-test-ec2-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name fis-test-ec2-profile
aws iam add-role-to-instance-profile \
  --instance-profile-name fis-test-ec2-profile --role-name fis-test-ec2-ssm-role

# FIS Experiment Role
cat > /tmp/fis-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "fis.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"aws:SourceAccount": "YOUR_ACCOUNT_ID"}}
    }
  ]
}
EOF

aws iam create-role --role-name fis-experiment-role \
  --assume-role-policy-document file:///tmp/fis-trust.json
aws iam attach-role-policy --role-name fis-experiment-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorEC2Access
aws iam attach-role-policy --role-name fis-experiment-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorSSMAccess

# 场景需要额外权限
cat > /tmp/fis-extra.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances", "ec2:DescribeSubnets",
        "tag:GetResources",
        "ssm:SendCommand", "ssm:CancelCommand",
        "ssm:ListCommands", "ssm:ListCommandInvocations"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy --role-name fis-experiment-role \
  --policy-name FISScenarioExtra --policy-document file:///tmp/fis-extra.json
```

### Step 4: 启动 EC2 实例

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI_ID=$(aws ec2 describe-images --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023*-x86_64" "Name=state,Values=available" \
  --region us-east-1 \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)

# 等待 IAM 传播
sleep 10

# Instance A (us-east-1a) — 故障注入目标 + 测量源
INST_A=$(aws ec2 run-instances \
  --image-id $AMI_ID --instance-type t3.micro \
  --subnet-id $SUBNET_A --security-group-ids $SG_ID \
  --iam-instance-profile Name=fis-test-ec2-profile \
  --tag-specifications "ResourceType=instance,Tags=[
    {Key=Name,Value=fis-test-instance-a},
    {Key=AZApplicationSlowdown,Value=LatencyForEC2},
    {Key=CrossAZTrafficSlowdown,Value=PacketLossForEC2}]" \
  --region us-east-1 --query "Instances[0].InstanceId" --output text)

# Instance B (us-east-1b) — 跨 AZ 测量目标
INST_B=$(aws ec2 run-instances \
  --image-id $AMI_ID --instance-type t3.micro \
  --subnet-id $SUBNET_B --security-group-ids $SG_ID \
  --iam-instance-profile Name=fis-test-ec2-profile \
  --tag-specifications "ResourceType=instance,Tags=[
    {Key=Name,Value=fis-test-instance-b},
    {Key=AZApplicationSlowdown,Value=LatencyForEC2},
    {Key=CrossAZTrafficSlowdown,Value=PacketLossForEC2}]" \
  --region us-east-1 --query "Instances[0].InstanceId" --output text)

# Instance C (us-east-1a) — 同 AZ 测量目标
INST_C=$(aws ec2 run-instances \
  --image-id $AMI_ID --instance-type t3.micro \
  --subnet-id $SUBNET_A --security-group-ids $SG_ID \
  --iam-instance-profile Name=fis-test-ec2-profile \
  --tag-specifications "ResourceType=instance,Tags=[
    {Key=Name,Value=fis-test-instance-c},
    {Key=AZApplicationSlowdown,Value=LatencyForEC2}]" \
  --region us-east-1 --query "Instances[0].InstanceId" --output text)

# 等待实例就绪
aws ec2 wait instance-running --instance-ids $INST_A $INST_B $INST_C --region us-east-1
```

!!! tip "标签很重要"
    FIS 场景通过**标签**定位目标资源。`AZApplicationSlowdown=LatencyForEC2` 和 `CrossAZTrafficSlowdown=PacketLossForEC2` 是 Scenario Library 的默认标签。你也可以在 Console 中通过 "Edit shared parameters" 自定义标签。

### Step 5: 安装测试工具

```bash
# 通过 SSM 安装 iperf3（无需 SSH）
aws ssm send-command \
  --instance-ids $INST_A $INST_B $INST_C \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["sudo dnf install -y iperf3"]' \
  --region us-east-1

# 在 Instance B 和 C 上启动 iperf3 server
aws ssm send-command \
  --instance-ids $INST_B $INST_C \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["nohup iperf3 -s -D"]' \
  --region us-east-1
```

### Step 6: 采集基准数据

```bash
# 获取 Instance B 和 C 的私有 IP
IP_B=$(aws ec2 describe-instances --instance-ids $INST_B --region us-east-1 \
  --query "Reservations[0].Instances[0].PrivateIpAddress" --output text)
IP_C=$(aws ec2 describe-instances --instance-ids $INST_C --region us-east-1 \
  --query "Reservations[0].Instances[0].PrivateIpAddress" --output text)

# 基准 ping（跨 AZ）
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"ping -c 20 -i 0.2 $IP_B\"]" \
  --region us-east-1

# 基准 ping（同 AZ）
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"ping -c 20 -i 0.2 $IP_C\"]" \
  --region us-east-1

# 基准 iperf3（跨 AZ）
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"iperf3 -c $IP_B -t 10\"]" \
  --region us-east-1
```

### Step 7: 创建 FIS 实验模板

```bash
ROLE_ARN=$(aws iam get-role --role-name fis-experiment-role \
  --query "Role.Arn" --output text)

# AZ Application Slowdown 模板
cat > /tmp/az-slowdown.json << EOF
{
  "description": "AZ Application Slowdown - 200ms latency within us-east-1a",
  "targets": {
    "ec2-instances-az": {
      "resourceType": "aws:ec2:instance",
      "resourceTags": {"AZApplicationSlowdown": "LatencyForEC2"},
      "filters": [
        {"path": "Placement.AvailabilityZone", "values": ["us-east-1a"]},
        {"path": "State.Name", "values": ["running"]}
      ],
      "selectionMode": "ALL"
    }
  },
  "actions": {
    "ec2-latency": {
      "actionId": "aws:ssm:send-command",
      "parameters": {
        "documentArn": "arn:aws:ssm:us-east-1::document/AWSFIS-Run-Network-Latency-Sources",
        "documentParameters": "{\"DurationSeconds\":\"120\",\"DelayMilliseconds\":\"200\",\"Sources\":\"us-east-1a\",\"InstallDependencies\":\"True\"}",
        "duration": "PT3M"
      },
      "targets": {"Instances": "ec2-instances-az"}
    }
  },
  "stopConditions": [{"source": "none"}],
  "roleArn": "${ROLE_ARN}",
  "tags": {"Name": "fis-az-slowdown"}
}
EOF

TMPL_AZ=$(aws fis create-experiment-template \
  --cli-input-json file:///tmp/az-slowdown.json \
  --region us-east-1 --query "experimentTemplate.id" --output text)

# Cross-AZ Traffic Slowdown 模板
cat > /tmp/cross-az-slowdown.json << EOF
{
  "description": "Cross-AZ Traffic Slowdown - 15% packet loss from us-east-1a",
  "targets": {
    "ec2-instances-az": {
      "resourceType": "aws:ec2:instance",
      "resourceTags": {"CrossAZTrafficSlowdown": "PacketLossForEC2"},
      "filters": [
        {"path": "Placement.AvailabilityZone", "values": ["us-east-1a"]},
        {"path": "State.Name", "values": ["running"]}
      ],
      "selectionMode": "ALL"
    }
  },
  "actions": {
    "ec2-packet-loss": {
      "actionId": "aws:ssm:send-command",
      "parameters": {
        "documentArn": "arn:aws:ssm:us-east-1::document/AWSFIS-Run-Network-Packet-Loss-Sources",
        "documentParameters": "{\"DurationSeconds\":\"120\",\"LossPercent\":\"15\",\"Sources\":\"us-east-1b\",\"TrafficType\":\"egress\",\"InstallDependencies\":\"True\"}",
        "duration": "PT3M"
      },
      "targets": {"Instances": "ec2-instances-az"}
    }
  },
  "stopConditions": [{"source": "none"}],
  "roleArn": "${ROLE_ARN}",
  "tags": {"Name": "fis-cross-az-slowdown"}
}
EOF

TMPL_XAZ=$(aws fis create-experiment-template \
  --cli-input-json file:///tmp/cross-az-slowdown.json \
  --region us-east-1 --query "experimentTemplate.id" --output text)
```

!!! warning "Sources 参数必填"
    `AWSFIS-Run-Network-Latency-Sources` 和 `AWSFIS-Run-Network-Packet-Loss-Sources` 的 `Sources` 参数标注为 Required 但没有默认值。缺失会导致 "A required parameter for the document is missing" 错误。可用值包括：AZ 名称（如 `us-east-1a`）、IP/CIDR、域名或 `ALL`。

### Step 8: 运行实验并测量

```bash
# 实验 1: AZ Application Slowdown
EXP_AZ=$(aws fis start-experiment --experiment-template-id $TMPL_AZ \
  --region us-east-1 --query "experiment.id" --output text)

# 等待 40 秒让故障注入生效
sleep 40

# 测量同 AZ 延迟
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"ping -c 20 -i 0.5 $IP_C\"]" \
  --region us-east-1

# 测量跨 AZ 延迟（预期不受影响）
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"ping -c 20 -i 0.5 $IP_B\"]" \
  --region us-east-1

# 测量带宽影响
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"iperf3 -c $IP_C -t 10\"]" \
  --region us-east-1

# 停止实验
aws fis stop-experiment --id $EXP_AZ --region us-east-1
sleep 15

# 实验 2: Cross-AZ Traffic Slowdown
EXP_XAZ=$(aws fis start-experiment --experiment-template-id $TMPL_XAZ \
  --region us-east-1 --query "experiment.id" --output text)
sleep 40

# 测量跨 AZ 丢包
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"ping -c 40 -i 0.2 $IP_B\"]" \
  --region us-east-1

# 测量带宽影响
aws ssm send-command --instance-ids $INST_A \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"iperf3 -c $IP_B -t 10\"]" \
  --region us-east-1

# 停止实验
aws fis stop-experiment --id $EXP_XAZ --region us-east-1
```

## 测试结果

### 基准 vs 故障注入：网络延迟对比

| 场景 | 路径 | 延迟 (avg) | 带宽 | 丢包 |
|------|------|-----------|------|------|
| **基准** | A→B (跨 AZ) | 0.472 ms | 4.57 Gbps | 0% |
| **基准** | A→C (同 AZ) | 0.324 ms | — | 0% |
| **AZ Slowdown 200ms** | A→C (同 AZ) | **400.1 ms** | **61.3 Mbps** | 0% |
| **AZ Slowdown 200ms** | A→B (跨 AZ) | 0.458 ms | — | 0% |
| **Cross-AZ 15% 丢包** | A→B (跨 AZ) | 0.473 ms | **19.9 Mbps** | **12.5%** |
| **Cross-AZ 50% 丢包** | A→B (跨 AZ) | 0.472 ms | — | **60%** |

### 关键数据解读

**AZ Application Slowdown (200ms)**:

- 同 AZ 延迟从 0.324ms 飙升到 **400.1ms**（×1234 倍）
- round-trip 约 400ms 是因为 200ms 延迟作用于 ingress 方向，去程和回程各加 200ms
- TCP 带宽从 4.57 Gbps 暴跌到 **61.3 Mbps**（-98.7%）— 这是因为 TCP 吞吐量 ≈ 窗口大小 / RTT
- **跨 AZ 流量完全不受影响** ✅ — `Sources=us-east-1a` 精确限定了影响范围

**Cross-AZ Traffic Slowdown (15% 丢包)**:

- 实测 12.5% 丢包（40 样本的统计偏差正常范围）
- TCP 带宽从 4.57 Gbps 暴跌到 **19.9 Mbps**（-99.6%）
- TCP 重传从 125 次增到 **467 次**（×3.7 倍）
- TCP 拥塞控制对丢包极度敏感，15% 丢包就足以摧毁 TCP 吞吐

### FlowsPercent 的 "流" 级别语义

| 配置 | 预期 | 实测 |
|------|------|------|
| 50ms 延迟, FlowsPercent=25% | 25% 的 ping 包受影响 | **所有** 40 个 ping 包都延迟 ~50ms |

`FlowsPercent` 在 **flow（网络流/连接）级别** 选择，不是 packet（数据包）级别。ICMP ping 是单个 flow，一旦被选中，该 flow 100% 的包都会受影响。

**实际含义**：对于有多个连接的应用（如 Web 服务器），25% flows 意味着大约 25% 的用户连接体验降级，每个受影响连接上的所有请求都会变慢。

## 踩坑记录

!!! warning "SSM 文档 Sources 参数必填"
    `AWSFIS-Run-Network-Latency-Sources` 和 `AWSFIS-Run-Network-Packet-Loss-Sources` 的 `Sources` 参数没有默认值。如果在 `documentParameters` 中遗漏，实验会立即失败并报 "undefined parameter" 错误。Scenario Library Console 界面会自动填充 AZ 名称，但手动构造 CLI 模板时必须显式指定。**已查文档确认。**

!!! warning "emptyTargetResolutionMode 差异"
    通过 Console Scenario Library 创建的模板默认 `emptyTargetResolutionMode=skip`（无目标时 action 跳过），但通过 CLI `create-experiment-template` 创建的模板默认 `fail`（无目标时实验失败）。实测确认：空目标触发 `InvalidTarget: Target resolution returned empty set.`。如果你的实验包含 ECS/EKS action 但环境中没有对应资源，通过 CLI 创建时建议显式设置为 `skip`。**已查文档确认。**

!!! note "FlowsPercent 不是 PacketPercent"
    官方文档描述 FlowsPercent 为 "percentage of network flows"。实测确认这是在连接/会话级别的选择，不是数据包级别。这意味着在多连接场景下，被选中的连接会 100% 受影响，未选中的完全不受影响。**实测发现，官方文档描述基本一致但未详细展开。**

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.micro × 3 | $0.0104/hr | ~1 hr | $0.03 |
| FIS actions | $0.10/action-min | ~6 min | $0.60 |
| **合计** | | | **< $1.00** |

FIS 按 action-minute 计费，不管影响多少资源。如果实验有 3 个并行 action（EC2 + ECS + EKS），每个 30 分钟 = 90 action-minutes = $9.00。但本实验每次只运行 1 个 EC2 action，时长缩短到 2-3 分钟，所以成本极低。

## 清理资源

```bash
# 1. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids $INST_A $INST_B $INST_C --region us-east-1
aws ec2 wait instance-terminated --instance-ids $INST_A $INST_B $INST_C --region us-east-1

# 2. 删除 FIS 实验模板
aws fis delete-experiment-template --id $TMPL_AZ --region us-east-1
aws fis delete-experiment-template --id $TMPL_XAZ --region us-east-1

# 3. 删除安全组（先检查 ENI 残留）
aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=$SG_ID" \
  --region us-east-1 --query "NetworkInterfaces[].NetworkInterfaceId"
# 确认为空后:
aws ec2 delete-security-group --group-id $SG_ID --region us-east-1

# 4. 删除子网
aws ec2 delete-subnet --subnet-id $SUBNET_A --region us-east-1
aws ec2 delete-subnet --subnet-id $SUBNET_B --region us-east-1

# 5. 删除 IGW
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID --region us-east-1
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID --region us-east-1

# 6. 删除 VPC
aws ec2 delete-vpc --vpc-id $VPC_ID --region us-east-1

# 7. 删除 IAM 角色
aws iam remove-role-from-instance-profile --instance-profile-name fis-test-ec2-profile \
  --role-name fis-test-ec2-ssm-role
aws iam delete-instance-profile --instance-profile-name fis-test-ec2-profile
aws iam detach-role-policy --role-name fis-test-ec2-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name fis-test-ec2-ssm-role
aws iam detach-role-policy --role-name fis-experiment-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorEC2Access
aws iam detach-role-policy --role-name fis-experiment-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorSSMAccess
aws iam delete-role-policy --role-name fis-experiment-role --policy-name FISScenarioExtra
aws iam delete-role --role-name fis-experiment-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。EC2 实例即使空闲也会持续计费。

## 结论与建议

### 场景选择指南

| 你想验证什么 | 使用哪个场景 |
|------------|------------|
| 应用对延迟升高的敏感度 | AZ: Application Slowdown |
| 超时阈值是否合理 | AZ: Application Slowdown（调高延迟到接近超时值）|
| AZ 疏散决策练习 | 两者均可 |
| 跨 AZ 通信降级容错 | Cross-AZ: Traffic Slowdown |
| 重试/熔断机制验证 | Cross-AZ: Traffic Slowdown |

### 生产环境建议

1. **从小开始**：先用低延迟（50ms）或低丢包（5%）测试，逐步加大
2. **设置 Stop Conditions**：关联 CloudWatch Alarm，当关键指标超阈值自动停止实验
3. **FlowsPercent 理解清楚**：25% flows ≠ 25% packets，在多连接场景下区别很大
4. **TCP 对丢包极度敏感**：仅 15% 丢包就导致 99.6% 带宽下降，这就是为什么灰色故障如此棘手
5. **停止实验 = 立即恢复**：SSM 文档内置 rollback 脚本，实测 <15 秒完全恢复

### 一句话总结

灰色故障不是"要么好要么坏"，而是"看起来还行但实际已经在出问题"。FIS 的这两个新场景让你可以在安全的环境中提前体验这种模糊地带——15% 丢包听起来不多，但足以让 TCP 吞吐暴跌 99.6%。知道这一点，比等到生产环境出事再发现要好得多。

## 参考链接

- [FIS Scenario Library 用户指南](https://docs.aws.amazon.com/fis/latest/userguide/scenario-library.html)
- [AZ: Application Slowdown 场景详情](https://docs.aws.amazon.com/fis/latest/userguide/az-application-slowdown-scenario.html)
- [Cross-AZ: Traffic Slowdown 场景详情](https://docs.aws.amazon.com/fis/latest/userguide/cross-az-traffic-slowdown-scenario.html)
- [FIS SSM 文档参考](https://docs.aws.amazon.com/fis/latest/userguide/actions-ssm-agent.html)
- [FIS 定价](https://aws.amazon.com/fis/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/aws-fis-test-scenarios-partial-failures/)
