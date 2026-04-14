---
tags:
  - Security
---

# AWS Network Firewall Active Threat Defense 实战：基于 MadPot 威胁情报自动阻断恶意流量

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $2-3（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

2025 年 6 月，AWS Network Firewall 发布了 **Active Threat Defense (ATD)** 功能。这是一个基于 Amazon 内部威胁情报服务 [MadPot](https://www.aboutamazon.com/news/aws/amazon-madpot-stops-cybersecurity-crime) 的托管规则组，能够自动识别和阻断与已知恶意基础设施的通信——包括 C2 服务器、恶意软件分发 URL、加密货币挖矿池等。

**核心价值**：不需要你维护威胁情报 feed，不需要手动更新规则，AWS 基于全球基础设施观测到的威胁活动持续更新规则。

## 前置条件

- AWS 账号，具备 Network Firewall、EC2、IAM、CloudWatch Logs 权限
- AWS CLI v2 已配置
- 建议使用 us-east-1（所有 Network Firewall 可用 Region 均支持 ATD）

## 核心概念

### ATD 是什么？

| 维度 | 说明 |
|------|------|
| **类型** | AWS 托管 Stateful Rule Group |
| **规则组名称** | `AttackInfrastructureStrictOrder` (Strict order) / `AttackInfrastructureActionOrder` (Action order) |
| **最大规则容量** | 15,000 条（独立于其他 rule group 的容量限制） |
| **威胁情报来源** | Amazon MadPot — AWS 内部威胁情报和干扰服务 |
| **更新方式** | 自动更新，无需用户操作 |
| **协议覆盖** | TCP、TLS、HTTP、outbound UDP |

### 五类威胁指标

| 指标组 | 流量方向 | 指标类型 | 典型场景 |
|--------|---------|---------|---------|
| Command and Control (C2) | Egress | IPs, Domains | 远程控制被入侵系统 |
| Malware Staging | Ingress/Egress | URLs | 恶意软件和攻击工具分发 |
| Sinkholes | Egress | Domains | 之前被滥用的恶意基础设施 |
| OOB App Security Testing | Egress | IPs, Domains | 注入载荷的出站验证 |
| Crypto-mining Pool | Egress | IPs, Domains | 加密货币挖矿池 |

### ATD vs 现有 AWS 托管规则组

ATD 和现有的 `BotNetCommandAndControlDomains`、`MalwareDomains` 等托管规则组是**互补关系**：

- **现有规则组**：基于已知的恶意域名/IP 静态列表
- **ATD**：基于 MadPot 实时观测到的**动态、活跃**威胁活动，覆盖范围更广

### 与 GuardDuty 联动

如果你同时使用 Amazon GuardDuty，相关威胁情报发现会标记为 `Amazon Active Threat Defense`。ATD 可以自动阻断 GuardDuty 检测到的以下类型威胁：

- `Backdoor:EC2/C&CActivity.B`、`Backdoor:Runtime/C&CActivity.B!DNS`
- `CryptoCurrency:EC2/BitcoinTool.B`、`CryptoCurrency:Runtime/BitcoinTool.B!DNS`
- `Trojan:EC2/BlackholeTraffic!DNS`、`UnauthorizedAccess:EC2/MaliciousIPCaller.Custom`

## 动手实践

### 架构概览

```
Internet Gateway
       |
  [IGW Ingress Route Table] ─→ 10.0.2.0/24 → Firewall Endpoint
       |
  Firewall Subnet (10.0.1.0/24)
  └── Network Firewall Endpoint (vpce-xxx)
       |
  [Protected Route Table] ─→ 0.0.0.0/0 → Firewall Endpoint
       |
  Protected Subnet (10.0.2.0/24)
  └── EC2 Instance (SSM 访问，无入站规则)
```

### Step 1: 创建 VPC 和子网

```bash
# 设置变量
export AWS_DEFAULT_REGION=us-east-1
export AWS_PROFILE=your-profile  # 替换为你的 profile

# 创建 VPC
VPC=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=atd-test-vpc}]' \
  --query 'Vpc.VpcId' --output text)
echo "VPC: $VPC"

# 启用 DNS 主机名
aws ec2 modify-vpc-attribute --vpc-id $VPC --enable-dns-hostnames '{"Value":true}'

# 创建 Firewall 子网
FW_SUBNET=$(aws ec2 create-subnet \
  --vpc-id $VPC --cidr-block 10.0.1.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=atd-fw-subnet}]' \
  --query 'Subnet.SubnetId' --output text)
echo "Firewall Subnet: $FW_SUBNET"

# 创建 Protected 子网
PROT_SUBNET=$(aws ec2 create-subnet \
  --vpc-id $VPC --cidr-block 10.0.2.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=atd-protected-subnet}]' \
  --query 'Subnet.SubnetId' --output text)
echo "Protected Subnet: $PROT_SUBNET"

# 创建并关联 Internet Gateway
IGW=$(aws ec2 create-internet-gateway \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=atd-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id $IGW --vpc-id $VPC
echo "IGW: $IGW"
```

### Step 2: 创建 Firewall Policy 并关联 ATD Rule Group

```bash
# 创建 Firewall Policy（Strict order + ATD + pass-all 兜底规则）
# 先创建 pass-all 规则组（允许正常流量通过）
aws network-firewall create-rule-group \
  --rule-group-name atd-pass-all \
  --type STATEFUL --capacity 10 \
  --rule-group '{
    "RulesSource": {
      "RulesString": "pass ip any any -> any any (msg:\"Allow all traffic\"; sid:1; rev:1;)"
    },
    "StatefulRuleOptions": {"RuleOrder": "STRICT_ORDER"}
  }'

# 获取 pass-all ARN
PASS_ARN=$(aws network-firewall describe-rule-group \
  --rule-group-name atd-pass-all --type STATEFUL \
  --query 'RuleGroupResponse.RuleGroupArn' --output text)

# 创建 Firewall Policy
# ATD（优先级 1）→ 先检查恶意流量
# pass-all（优先级 2）→ 放行正常流量
# 默认操作 drop_established → 兜底丢弃
cat > /tmp/fw-policy.json << EOF
{
  "StatelessDefaultActions": ["aws:forward_to_sfe"],
  "StatelessFragmentDefaultActions": ["aws:forward_to_sfe"],
  "StatefulRuleGroupReferences": [
    {
      "ResourceArn": "arn:aws:network-firewall:us-east-1:aws-managed:stateful-rulegroup/AttackInfrastructureStrictOrder",
      "Priority": 1
    },
    {
      "ResourceArn": "${PASS_ARN}",
      "Priority": 2
    }
  ],
  "StatefulDefaultActions": ["aws:drop_established", "aws:alert_established"],
  "StatefulEngineOptions": {"RuleOrder": "STRICT_ORDER"}
}
EOF

aws network-firewall create-firewall-policy \
  --firewall-policy-name atd-test-policy \
  --firewall-policy file:///tmp/fw-policy.json
```

!!! tip "Strict Order 规则评估逻辑"
    在 Strict Order 模式下，规则按 Priority 数字从小到大依次评估。ATD 在 Priority 1 先检查恶意流量（drop），pass-all 在 Priority 2 放行所有正常流量。这样可以实现"仅阻断已知威胁，其余全部放行"的效果。

### Step 3: 创建 Network Firewall

```bash
# 创建 CloudWatch Log Groups
aws logs create-log-group --log-group-name /aws/network-firewall/atd-test/alert
aws logs create-log-group --log-group-name /aws/network-firewall/atd-test/flow

# 创建 Firewall
POLICY_ARN=$(aws network-firewall describe-firewall-policy \
  --firewall-policy-name atd-test-policy \
  --query 'FirewallPolicyResponse.FirewallPolicyArn' --output text)

aws network-firewall create-firewall \
  --firewall-name atd-test-firewall \
  --firewall-policy-arn $POLICY_ARN \
  --vpc-id $VPC \
  --subnet-mappings SubnetId=$FW_SUBNET

# 等待 Firewall 就绪（约 4 分钟）
echo "等待 Firewall 部署..."
while true; do
  STATUS=$(aws network-firewall describe-firewall \
    --firewall-name atd-test-firewall \
    --query 'FirewallStatus.Status' --output text)
  echo "Status: $STATUS"
  [ "$STATUS" = "READY" ] && break
  sleep 15
done

# 获取 Firewall Endpoint ID
FW_ENDPOINT=$(aws network-firewall describe-firewall \
  --firewall-name atd-test-firewall \
  --query 'FirewallStatus.SyncStates.*.Attachment.EndpointId' --output text)
echo "Firewall Endpoint: $FW_ENDPOINT"
```

### Step 4: 配置日志和路由

```bash
# 配置 Alert + Flow 日志
aws network-firewall update-logging-configuration \
  --firewall-name atd-test-firewall \
  --logging-configuration '{
    "LogDestinationConfigs": [{
      "LogType": "ALERT",
      "LogDestinationType": "CloudWatchLogs",
      "LogDestination": {"logGroup": "/aws/network-firewall/atd-test/alert"}
    }]
  }'

aws network-firewall update-logging-configuration \
  --firewall-name atd-test-firewall \
  --logging-configuration '{
    "LogDestinationConfigs": [
      {
        "LogType": "ALERT",
        "LogDestinationType": "CloudWatchLogs",
        "LogDestination": {"logGroup": "/aws/network-firewall/atd-test/alert"}
      },
      {
        "LogType": "FLOW",
        "LogDestinationType": "CloudWatchLogs",
        "LogDestination": {"logGroup": "/aws/network-firewall/atd-test/flow"}
      }
    ]
  }'

# 配置路由表
# 1. Firewall 子网: 0.0.0.0/0 → IGW
FW_RTB=$(aws ec2 create-route-table --vpc-id $VPC \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=atd-fw-rtb}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $FW_RTB \
  --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW
aws ec2 associate-route-table --route-table-id $FW_RTB --subnet-id $FW_SUBNET

# 2. Protected 子网: 0.0.0.0/0 → Firewall Endpoint
PROT_RTB=$(aws ec2 create-route-table --vpc-id $VPC \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=atd-protected-rtb}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PROT_RTB \
  --destination-cidr-block 0.0.0.0/0 --vpc-endpoint-id $FW_ENDPOINT
aws ec2 associate-route-table --route-table-id $PROT_RTB --subnet-id $PROT_SUBNET

# 3. IGW Ingress 路由: 回程流量 → Firewall Endpoint
IGW_RTB=$(aws ec2 create-route-table --vpc-id $VPC \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=atd-igw-rtb}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $IGW_RTB \
  --destination-cidr-block 10.0.2.0/24 --vpc-endpoint-id $FW_ENDPOINT
aws ec2 associate-route-table --route-table-id $IGW_RTB --gateway-id $IGW
```

### Step 5: 部署测试 EC2 并验证

```bash
# 创建 IAM Role（SSM 访问，无需 SSH）
aws iam create-role --role-name atd-test-ssm-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
aws iam attach-role-policy --role-name atd-test-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name atd-test-ssm-profile
aws iam add-role-to-instance-profile \
  --instance-profile-name atd-test-ssm-profile \
  --role-name atd-test-ssm-role
sleep 10  # 等待 IAM 传播

# 创建 Security Group（无入站规则！）
SG=$(aws ec2 create-security-group \
  --group-name atd-test-sg \
  --description 'ATD test - egress only' \
  --vpc-id $VPC --query 'GroupId' --output text)

# 获取最新 Amazon Linux 2023 AMI
AMI=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameters[0].Value' --output text)

# 启动 EC2
INSTANCE=$(aws ec2 run-instances \
  --image-id $AMI --instance-type t3.micro \
  --subnet-id $PROT_SUBNET \
  --security-group-ids $SG \
  --iam-instance-profile Name=atd-test-ssm-profile \
  --associate-public-ip-address \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=atd-test-ec2}]' \
  --query 'Instances[0].InstanceId' --output text)

# 等待 SSM 就绪
aws ec2 wait instance-running --instance-ids $INSTANCE
echo "等待 SSM 注册..."
while true; do
  SSM=$(aws ssm describe-instance-information \
    --filters Key=InstanceIds,Values=$INSTANCE \
    --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$SSM" = "Online" ] && break
  sleep 10
done
echo "SSM ready!"
```

### Step 6: 验证 ATD 工作状态

```bash
# 确认 ATD rule group 已同步
aws network-firewall describe-firewall \
  --firewall-name atd-test-firewall \
  --query 'FirewallStatus.SyncStates.*.Config' --output yaml

# 查看 ATD rule group 元数据
aws network-firewall describe-rule-group-metadata \
  --rule-group-arn "arn:aws:network-firewall:${AWS_DEFAULT_REGION}:aws-managed:stateful-rulegroup/AttackInfrastructureStrictOrder"

# 通过 SSM 测试正常流量
aws ssm send-command \
  --instance-ids $INSTANCE \
  --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "curl -s -o /dev/null -w \"aws.amazon.com: %{http_code} %{time_total}s\" https://aws.amazon.com -m10",
    "echo",
    "curl -s -o /dev/null -w \"google.com: %{http_code} %{time_total}s\" https://www.google.com -m10",
    "echo",
    "curl -s -o /dev/null -w \"httpbin.org: %{http_code} %{time_total}s\" https://httpbin.org/get -m10"
  ]'
```

### Step 7: 切换 Alert/Drop 模式

```bash
# 切换 ATD 到 Alert 模式（用于观察，不阻断）
UPDATE_TOKEN=$(aws network-firewall describe-firewall-policy \
  --firewall-policy-name atd-test-policy \
  --query 'UpdateToken' --output text)

# 在 ATD rule group reference 中添加 Override
cat > /tmp/alert-mode.json << EOF
{
  "StatelessDefaultActions": ["aws:forward_to_sfe"],
  "StatelessFragmentDefaultActions": ["aws:forward_to_sfe"],
  "StatefulRuleGroupReferences": [
    {
      "ResourceArn": "arn:aws:network-firewall:${AWS_DEFAULT_REGION}:aws-managed:stateful-rulegroup/AttackInfrastructureStrictOrder",
      "Priority": 1,
      "Override": {"Action": "DROP_TO_ALERT"}
    },
    {
      "ResourceArn": "${PASS_ARN}",
      "Priority": 2
    }
  ],
  "StatefulDefaultActions": ["aws:drop_established", "aws:alert_established"],
  "StatefulEngineOptions": {"RuleOrder": "STRICT_ORDER"}
}
EOF

aws network-firewall update-firewall-policy \
  --firewall-policy-name atd-test-policy \
  --firewall-policy file:///tmp/alert-mode.json \
  --update-token $UPDATE_TOKEN
```

!!! tip "生产环境最佳实践"
    建议先在 Alert 模式下运行 1-2 周，观察 CloudWatch Logs 中的 alert 事件，确认没有误报后再切换到 Drop 模式。

## 测试结果

### 正常流量测试（ATD Drop 模式）

| 目标 | 协议 | HTTP 状态码 | 响应时间 | 结果 |
|------|------|------------|---------|------|
| aws.amazon.com | HTTPS | 200 | 56ms | ✅ 通过 |
| www.google.com | HTTPS | 200 | 59ms | ✅ 通过 |
| httpbin.org | HTTPS | 200 | 114ms | ✅ 通过 |
| example.com | HTTP | 200 | ~100ms | ✅ 通过 |

**结论**：ATD 在 Drop 模式下不影响正常流量。恶意流量拦截效果详见下方[恶意流量实测](#恶意流量实测)章节。

### CloudWatch 指标数据

| 时间段 (UTC) | Received Packets | Passed Packets | Dropped Packets |
|-------------|-----------------|----------------|-----------------|
| 07:04-07:09 | 0 | 0 | 0 |
| 07:09-07:14 | 48,123 | 48,123 | 0 |
| 07:14-07:19 | 1,099 | 1,100 | 0 |

正常流量 **100% 通过，0 drops**。ATD 基于精确的威胁指标（经过验证的 MadPot 情报），不会产生误报。

### Flow 日志示例

```json
{
  "firewall_name": "atd-test-firewall",
  "availability_zone": "us-east-1a",
  "event": {
    "event_type": "netflow",
    "src_ip": "10.0.2.92",
    "dest_ip": "13.226.238.96",
    "dest_port": 443,
    "proto": "TCP",
    "app_proto": "tls",
    "netflow": {
      "pkts": 18,
      "bytes": 1660,
      "state": "closed"
    }
  }
}
```

## 踩坑记录

!!! warning "Managed Rule Group 不可检视"
    `aws network-firewall describe-rule-group` 对 ATD managed rule group 返回 `InvalidRequestException: Managed rule groups must be described using DescribeRuleGroupMetadata`。只能通过 `describe-rule-group-metadata` 查看元数据（名称、描述、容量），无法看到具体的 Suricata 规则内容。**已查文档确认：这是 managed rule group 的设计行为。**

!!! warning "日志配置需分步添加"
    `update-logging-configuration` 不能一次性添加多个新的 LogDestinationConfig，需要先添加第一个（如 ALERT），再更新为包含两个的完整配置。**实测发现，官方未明确记录此限制。**

!!! warning "Strict Order 下 pass 规则的评估行为"
    在 Strict Order 模式下，`pass ip any any` 规则会在 IP 层匹配第一个 SYN 包后立即放行整个 flow，即使更高优先级（更小 Priority 数字）的 rule group 中有应用层（HTTP/TLS）的 drop 规则。如果需要同时使用应用层 drop 规则和 IP 层 pass 规则，需要仔细设计优先级和规则范围。**已查 Suricata 文档确认：这是 Suricata strict order 的预期行为。**

!!! warning "Firewall 部署耗时"
    从 `create-firewall` 到 `READY` 状态约需 **4 分钟**。Policy 更新则几乎即时同步（秒级）。规划部署流水线时需预留 Firewall 创建等待时间。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Network Firewall Endpoint | $0.395/hr | ~1 hr | $0.40 |
| ATD 数据处理附加费 | ~$0.065/GB | <0.01 GB | <$0.01 |
| EC2 (t3.micro) | $0.0104/hr | ~1 hr | $0.01 |
| CloudWatch Logs | $0.50/GB | <0.01 GB | <$0.01 |
| **合计** | | | **~$0.42** |

## 清理资源

```bash
# 1. 终止 EC2
aws ec2 terminate-instances --instance-ids $INSTANCE
aws ec2 wait instance-terminated --instance-ids $INSTANCE

# 2. 删除 Network Firewall（需等待 DELETING 完成）
aws network-firewall delete-firewall --firewall-name atd-test-firewall
echo "等待 Firewall 删除..."
sleep 120  # Firewall 删除需要约 2 分钟

# 3. 删除 Firewall Policy
aws network-firewall delete-firewall-policy --firewall-policy-name atd-test-policy

# 4. 删除自定义 Rule Groups
aws network-firewall delete-rule-group --rule-group-name atd-pass-all --type STATEFUL

# 5. 删除 CloudWatch Log Groups
aws logs delete-log-group --log-group-name /aws/network-firewall/atd-test/alert
aws logs delete-log-group --log-group-name /aws/network-firewall/atd-test/flow

# 6. 删除 IAM 资源
aws iam remove-role-from-instance-profile \
  --instance-profile-name atd-test-ssm-profile --role-name atd-test-ssm-role
aws iam delete-instance-profile --instance-profile-name atd-test-ssm-profile
aws iam detach-role-policy --role-name atd-test-ssm-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name atd-test-ssm-role

# 7. 清理 VPC 资源（先检查 ENI 残留）
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=$SG \
  --query 'NetworkInterfaces[*].NetworkInterfaceId'
# 确认无残留 ENI 后：
aws ec2 delete-security-group --group-id $SG

# 删除路由表关联和路由表
for RTB in $FW_RTB $PROT_RTB $IGW_RTB; do
  ASSOC=$(aws ec2 describe-route-tables --route-table-ids $RTB \
    --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text)
  [ -n "$ASSOC" ] && aws ec2 disassociate-route-table --association-id $ASSOC
  aws ec2 delete-route-table --route-table-id $RTB
done

# 删除子网
aws ec2 delete-subnet --subnet-id $FW_SUBNET
aws ec2 delete-subnet --subnet-id $PROT_SUBNET

# 分离并删除 IGW
aws ec2 detach-internet-gateway --internet-gateway-id $IGW --vpc-id $VPC
aws ec2 delete-internet-gateway --internet-gateway-id $IGW

# 删除 VPC
aws ec2 delete-vpc --vpc-id $VPC
```

!!! danger "务必清理"
    Network Firewall 按小时计费（$0.395/hr/endpoint），如不清理每天约 $9.48。Lab 完成后请立即执行清理。

## 恶意流量实测

前面的测试验证了 ATD 对正常流量"无影响"。但 ATD 的核心价值是**阻断恶意流量**。本节通过向已知恶意目标发起连接，验证 ATD 的 Drop 和 Alert 功能。

### 测试方法

**测试目标来源**（公开威胁情报）：

| 来源 | 目标类型 | 数量 |
|------|---------|------|
| [abuse.ch Feodo Tracker](https://feodotracker.abuse.ch/) | C2 botnet IP | 5 个 |
| 公开矿池域名 | Crypto mining pool | 7 个 |
| CERT.PL sinkhole | Sinkhole domain | 2 个 |

**安全保障**：

- 所有测试流量从隔离 VPC 内的 EC2 发起（通过 SSM，无 SSH 入站）
- ATD Drop 模式下，恶意流量在 firewall 层被丢弃，**不会实际到达目标**
- Security Group 无任何入站规则
- 测试完成后立即清理所有资源

### Drop 模式测试结果

ATD 默认 Drop 模式，测试从 EC2 尝试连接各类恶意目标：

| 目标 | 类型 | Drop 模式结果 | ATD 触发 |
|------|------|-------------|---------|
| `stratum.slushpool.com` (172.65.65.63) | Mining pool | ❌ 超时 (5.0s) | ✅ blocked |
| `xmrpool.eu` (57.129.130.178) | Mining pool | ❌ 超时 (5.0s) | ✅ blocked |
| `pool.supportxmr.com` (104.243.33.118) | Mining pool | ❌ 超时 (5.0s) | ✅ blocked |
| `pool.supportxmr.com` (104.243.43.115) | Mining pool | ❌ 超时 (5.0s) | ✅ blocked |
| `pool.minergate.com` (49.12.80.39) | Mining pool | SSL 错误 (0.4s) | ❌ |
| `monerohash.com` (66.23.198.161) | Mining pool | HTTP 200 (0.05s) | ❌ |
| 50.16.16.211 | C2 (Feodo) | HTTP 200 (0.005s) | ❌ |
| 162.243.103.246 | C2 (Feodo) | HTTP 200 (0.02s) | ❌ |
| `sinkhole.cert.pl` | Sinkhole | 超时 (5.0s) | ❌ |
| `aws.amazon.com` (正常) | Baseline | ✅ HTTP 200 (0.06s) | — |

**关键发现**：ATD 成功拦截了 **4 个矿池 IP**，均在 TCP SYN 级别被丢弃（表现为连接超时）。C2 IP（来自 Feodo Tracker）未被拦截——这说明 ATD 基于 MadPot **自己的** 威胁情报，不是第三方 feed 的简单聚合。

### CloudWatch Alert 日志（Drop 模式）

ATD 拦截时生成的 alert 日志示例：

```json
{
  "firewall_name": "atd-maltest-firewall",
  "event": {
    "event_type": "alert",
    "alert": {
      "severity": 3,
      "signature_id": 1700154080,
      "signature": "traffic_to_mining [172[.]65[.]65[.]63]",
      "action": "blocked",
      "metadata": {
        "category": ["mining"],
        "class": ["suspicious_endpoint"],
        "expiry": ["2026-03-29T07:00:06Z"],
        "threat_names": ["[suspicious:mining/stratum]"]
      }
    },
    "verdict": { "action": "drop" },
    "src_ip": "10.0.2.170",
    "dest_ip": "172.65.65.63",
    "dest_port": 443,
    "proto": "TCP",
    "direction": "to_server"
  }
}
```

日志中的关键字段：

- `alert.action: "blocked"` — 流量被阻断
- `verdict.action: "drop"` — 防火墙执行 drop
- `metadata.category: ["mining"]` — 威胁分类：加密货币挖矿
- `metadata.threat_names` — 具体威胁类型（如 `mining/stratum`）
- `metadata.expiry` — 该规则的过期时间（规则持续更新）

### Alert 模式测试结果

切换 ATD 为 Alert 模式（`Override: DROP_TO_ALERT`），重新测试同样的矿池目标：

| 目标 | Alert 模式结果 | 日志 action |
|------|-------------|-----------|
| `stratum.slushpool.com` (172.65.65.63) | 连接被远端拒绝 (0.06s) | ✅ `allowed` |
| `xmrpool.eu` (57.129.130.178) | 连接被远端拒绝 (0.16s) | ✅ `allowed` |
| `pool.supportxmr.com` (104.243.33.118) | 连接被远端拒绝 (0.03s) | ✅ `allowed` |

Alert 模式下的日志对比：

```json
{
  "alert": {
    "signature": "traffic_to_mining [172[.]65[.]65[.]63]",
    "action": "allowed",      // ← Drop 模式为 "blocked"
    "metadata": {
      "category": ["mining"],
      "threat_names": ["[suspicious:mining/stratum]"]
    }
  },
  "verdict": { "action": "pass" }  // ← Drop 模式为 "drop"
}
```

**行为差异**：Alert 模式下流量**通过防火墙**到达目标（被远端拒绝是因为矿池不接受 HTTPS 请求），但日志中仍然记录了威胁检测事件。

### 对比实验：有 ATD vs 无 ATD

| 目标 | 有 ATD (Drop) | 无 ATD | 有 ATD (Alert) |
|------|-------------|--------|--------------|
| `stratum.slushpool.com` | ❌ 超时 8.0s | 拒绝 0.06s | 拒绝 0.06s |
| `xmrpool.eu` | ❌ 超时 5.0s | 拒绝 0.17s | 拒绝 0.16s |
| `pool.supportxmr.com` | ❌ 超时 5.0s | 拒绝 0.06s | 拒绝 0.03s |
| `aws.amazon.com` (正常) | ✅ 200 0.06s | ✅ 200 0.06s | ✅ 200 0.06s |

**结论**：
- **Drop 模式**：恶意流量在 firewall 层被丢弃，表现为连接超时（SYN 被 drop）
- **无 ATD**：流量正常通过 firewall，到达远端（被远端拒绝或响应）
- **Alert 模式**：流量行为与无 ATD 相同，但生成 alert 日志
- **正常流量**：三种模式下均不受影响

### CloudWatch Metrics

| 指标 | 08:05-08:10 (Drop模式测试) | 08:10-08:15 (Alert模式) | 08:15-08:20 (无ATD) |
|------|--------------------------|----------------------|-------------------|
| DroppedPackets | **12** | 0 | 0 |
| PassedPackets | 47,220 | 613 | 528 |
| ReceivedPackets | 47,232 | 613 | 526 |

Drop 模式期间 `DroppedPackets = 12`，与 4 次 TCP SYN 重试（3 retries × 4 targets = 12 packets）一致。

## 结论与建议

### ATD 适合的场景

1. **所有使用 Network Firewall 的 VPC**：ATD 是零配置的附加安全层，建议默认启用
2. **安全合规要求高的工作负载**：金融、医疗等行业，自动阻断已知威胁
3. **与 GuardDuty 配合使用**：GuardDuty 检测 + ATD 自动阻断，形成闭环

### 生产环境建议

1. **先 Alert 后 Drop**：初始部署使用 `Override: DROP_TO_ALERT`，观察 1-2 周确认无误报
2. **配合 Flow 日志**：启用 CloudWatch Logs 监控所有流量行为
3. **Strict Order 优先**：AWS 推荐使用 Strict Order，可精确控制规则评估顺序
4. **注意额外费用**：ATD 有独立的数据处理附加费，高流量场景需评估成本
5. **Deep Threat Inspection**：如对数据隐私敏感，可通过 console/API opt-out

### 局限性

- **无法查看具体规则**：Managed rule group 的规则内容不可见，无法精确知道拦截了哪些 IP/域名（实测中第三方威胁 feed 如 Feodo Tracker 的 C2 IP 未被拦截，说明 ATD 使用 MadPot 独立的威胁情报）
- **被动防御**：只能阻断 MadPot 已知的威胁，新型 0-day 攻击仍需其他安全层防护
- **额外成本**：高流量场景下 ATD 数据处理费可能显著

## 参考链接

- [AWS Network Firewall Active Threat Defense 文档](https://docs.aws.amazon.com/network-firewall/latest/developerguide/aws-managed-rule-groups-atd.html)
- [ATD 威胁指标说明](https://docs.aws.amazon.com/network-firewall/latest/developerguide/atd-indicators.html)
- [ATD 与 GuardDuty 集成](https://docs.aws.amazon.com/network-firewall/latest/developerguide/nwfw-atd-guardduty-use-case.html)
- [Deep Threat Inspection](https://docs.aws.amazon.com/network-firewall/latest/developerguide/atd-deep-threat-inspection.html)
- [Suricata Rule Evaluation Order](https://docs.aws.amazon.com/network-firewall/latest/developerguide/suricata-rule-evaluation-order.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/06/aws-network-firewall-active-threat-defense/)
- [MadPot 介绍](https://www.aboutamazon.com/news/aws/amazon-madpot-stops-cybersecurity-crime)
