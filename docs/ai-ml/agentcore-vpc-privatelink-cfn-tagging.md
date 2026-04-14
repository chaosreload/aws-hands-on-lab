---
tags:
  - AgentCore
  - Networking
  - CloudFormation
  - What's New
---

# Amazon Bedrock AgentCore 企业级部署：VPC 连接、PrivateLink、CloudFormation 与资源标签实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $1-2（主要是 NAT Gateway 和 VPC Endpoint 按小时计费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27
    - **状态**: Amazon Bedrock AgentCore 目前为 Preview

## 背景

Amazon Bedrock AgentCore 是 AWS 推出的 AI Agent 基础设施服务，提供 Runtime（部署和扩展 AI Agent）、Browser（网页交互）、Code Interpreter（代码执行）等能力。2025 年 9 月 25 日，AgentCore 新增了四项企业级能力：

- **VPC 连接** — Agent 可安全访问 VPC 内的私有资源（数据库、内部 API）
- **AWS PrivateLink** — 从 VPC 内部私有访问 AgentCore API，不经过公网
- **CloudFormation** — 13 种资源类型支持 IaC 自动化部署
- **资源标签** — 15 种资源支持打标签，实现成本分配和访问控制

这些能力让 AgentCore 从"开发者预览"迈向"企业生产就绪"，解决了安全团队最关心的网络隔离、私有访问和基础设施自动化问题。

## 前置条件

- AWS 账号，配置了 `BedrockAgentCoreFullAccess` 权限的 IAM 用户/角色
- AWS CLI v2 已配置
- AgentCore 所在 Region 支持（us-east-1、us-west-2、ap-southeast-2、eu-central-1）

## 核心概念

### VPC 连接：Agent 如何访问私有资源

```
┌─────────────────── Your VPC ───────────────────┐
│                                                 │
│  ┌─ Private Subnet (az1) ──┐                   │
│  │  ENI ← AgentCore SLR    │  ┌─────────────┐  │
│  │  (10.99.10.x)           │──│  RDS / API   │  │
│  └─────────────────────────┘  └─────────────┘  │
│                                                 │
│  ┌─ Private Subnet (az2) ──┐                   │
│  │  ENI ← AgentCore SLR    │                   │
│  │  (10.99.11.x)           │                   │
│  └─────────────────────────┘                   │
│                    │                            │
│              NAT Gateway                        │
│                    │                            │
│              Internet Gateway                   │
└─────────────────────────────────────────────────┘
```

当你为 AgentCore 配置 VPC 连接时：

1. AgentCore 通过服务关联角色 `AWSServiceRoleForBedrockAgentCoreNetwork` 在你的 VPC 中创建 **弹性网络接口（ENI）**
2. ENI 分配私有 IP，通过安全组控制网络访问
3. 同一子网和安全组配置的 Agent 之间 **共享 ENI**
4. Agent 删除后，ENI 最长保留 **8 小时**后自动移除

!!! warning "关键限制"
    - 必须使用**私有子网**，公有子网不提供 Internet 连接
    - Browser Tool 需要 Internet → 必须配置 NAT Gateway
    - 子网必须位于**支持的可用区**（us-east-1: use1-az1, use1-az2, use1-az4）

### PrivateLink：三个端点覆盖全服务

| 端点类型 | 服务名 | 覆盖范围 |
|---------|--------|---------|
| 数据面 | `com.amazonaws.{region}.bedrock-agentcore` | Runtime, Tools, Memory, Identity |
| 控制面 | `com.amazonaws.{region}.bedrock-agentcore-control` | Runtime/Memory 管理 |
| Gateway | `com.amazonaws.{region}.bedrock-agentcore.gateway` | AgentCore Gateway |

### CloudFormation：13 个资源类型

AgentCore 支持完整的 IaC 自动化，覆盖 Runtime、Tools、Gateway、Memory、Identity、Policy、Evaluator 全系列。

### 资源标签

15 种资源支持标签管理，每个资源最多 50 个标签。支持在创建时指定标签，也可以后续通过 `tag-resource` 添加。

## 动手实践

### Step 1: 创建 VPC 基础设施

创建专用 VPC，包含公有子网（放 NAT Gateway）和两个私有子网（放 AgentCore ENI）：

```bash
# 设置变量
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1

# 创建 VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.99.0.0/16 \
  --region $AWS_REGION \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=agentcore-vpc}]' \
  --query 'Vpc.VpcId' --output text)

# 启用 DNS
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID \
  --enable-dns-support '{"Value":true}' --region $AWS_REGION
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID \
  --enable-dns-hostnames '{"Value":true}' --region $AWS_REGION

# 创建子网（注意选择支持的 AZ）
PUB_SUBNET=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.99.1.0/24 --availability-zone us-east-1a \
  --region $AWS_REGION \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=agentcore-public}]' \
  --query 'Subnet.SubnetId' --output text)

PRIV_SUBNET_1=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.99.10.0/24 --availability-zone us-east-1a \
  --region $AWS_REGION \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=agentcore-private-1}]' \
  --query 'Subnet.SubnetId' --output text)

PRIV_SUBNET_2=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.99.11.0/24 --availability-zone us-east-1b \
  --region $AWS_REGION \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=agentcore-private-2}]' \
  --query 'Subnet.SubnetId' --output text)

echo "VPC: $VPC_ID"
echo "Public Subnet: $PUB_SUBNET"
echo "Private Subnet 1: $PRIV_SUBNET_1"
echo "Private Subnet 2: $PRIV_SUBNET_2"
```

配置路由（IGW + NAT Gateway）：

```bash
# Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway --region $AWS_REGION \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=agentcore-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID \
  --vpc-id $VPC_ID --region $AWS_REGION

# NAT Gateway
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc --region $AWS_REGION \
  --query 'AllocationId' --output text)
NAT_ID=$(aws ec2 create-nat-gateway --subnet-id $PUB_SUBNET \
  --allocation-id $EIP_ALLOC --region $AWS_REGION \
  --query 'NatGateway.NatGatewayId' --output text)

echo "Waiting for NAT Gateway..."
aws ec2 wait nat-gateway-available --nat-gateway-ids $NAT_ID --region $AWS_REGION

# 公有子网路由表
PUB_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PUB_RT \
  --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID --region $AWS_REGION
aws ec2 associate-route-table --route-table-id $PUB_RT \
  --subnet-id $PUB_SUBNET --region $AWS_REGION

# 私有子网路由表（通过 NAT）
PRIV_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PRIV_RT \
  --destination-cidr-block 0.0.0.0/0 --nat-gateway-id $NAT_ID --region $AWS_REGION
aws ec2 associate-route-table --route-table-id $PRIV_RT \
  --subnet-id $PRIV_SUBNET_1 --region $AWS_REGION
aws ec2 associate-route-table --route-table-id $PRIV_RT \
  --subnet-id $PRIV_SUBNET_2 --region $AWS_REGION
```

创建安全组：

```bash
SG_ID=$(aws ec2 create-security-group \
  --group-name agentcore-sg \
  --description "AgentCore VPC - internal only" \
  --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'GroupId' --output text)

# 仅允许 VPC 内部通信（禁止 0.0.0.0/0 入站！）
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID --protocol -1 \
  --source-group $SG_ID --region $AWS_REGION

echo "Security Group: $SG_ID"
```

### Step 2: 创建 VPC 模式的 Code Interpreter

```bash
# 创建 VPC 模式的 Code Interpreter（含标签）
aws bedrock-agentcore-control create-code-interpreter \
  --name "my_vpc_code_interpreter" \
  --description "VPC-connected code interpreter" \
  --network-configuration "{
    \"networkMode\": \"VPC\",
    \"vpcConfig\": {
      \"subnets\": [\"$PRIV_SUBNET_1\", \"$PRIV_SUBNET_2\"],
      \"securityGroups\": [\"$SG_ID\"]
    }
  }" \
  --tags '{"Project":"agentcore-lab","Environment":"test"}' \
  --region $AWS_REGION
```

!!! note "创建时间"
    VPC 模式的 Code Interpreter 创建需要 **3-5 分钟**（因为要在 VPC 中创建 ENI），而 PUBLIC 模式秒级完成。使用以下命令检查状态：

    ```bash
    aws bedrock-agentcore-control get-code-interpreter \
      --code-interpreter-id <your-id> \
      --region $AWS_REGION \
      --query '[status,networkConfiguration]'
    ```

验证 ENI 已创建：

```bash
aws ec2 describe-network-interfaces --region $AWS_REGION \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkInterfaces[?Description==``].[NetworkInterfaceId,SubnetId,PrivateIpAddress]' \
  --output table
```

### Step 3: 创建 PrivateLink VPC 端点

```bash
# 数据面端点（Runtime, Tools, Memory, Identity）
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --vpc-endpoint-type Interface \
  --service-name com.amazonaws.us-east-1.bedrock-agentcore \
  --subnet-ids $PRIV_SUBNET_1 $PRIV_SUBNET_2 \
  --security-group-ids $SG_ID \
  --private-dns-enabled \
  --region $AWS_REGION

# 控制面端点
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --vpc-endpoint-type Interface \
  --service-name com.amazonaws.us-east-1.bedrock-agentcore-control \
  --subnet-ids $PRIV_SUBNET_1 $PRIV_SUBNET_2 \
  --security-group-ids $SG_ID \
  --private-dns-enabled \
  --region $AWS_REGION

# Gateway 端点
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --vpc-endpoint-type Interface \
  --service-name com.amazonaws.us-east-1.bedrock-agentcore.gateway \
  --subnet-ids $PRIV_SUBNET_2 \
  --security-group-ids $SG_ID \
  --private-dns-enabled \
  --region $AWS_REGION
```

!!! warning "Gateway 端点 AZ 兼容性"
    实测发现 Gateway 端点不支持所有 AZ。在 us-east-1 中，`us-east-1a (use1-az1)` 不受 Gateway 端点支持，需使用 `us-east-1b (use1-az2)` 等其他 AZ 的子网。数据面和控制面端点则支持 use1-az1。

验证端点状态：

```bash
aws ec2 describe-vpc-endpoints --region $AWS_REGION \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'VpcEndpoints[*].[ServiceName,State,DnsEntries[0].DnsName]' \
  --output table
```

Private DNS 生效后，VPC 内的调用会自动路由到 PrivateLink：

- `bedrock-agentcore.us-east-1.amazonaws.com` → 数据面
- `bedrock-agentcore-control.us-east-1.amazonaws.com` → 控制面
- `gateway.bedrock-agentcore.us-east-1.amazonaws.com` → Gateway

### Step 4: CloudFormation 自动化部署

以下 CloudFormation 模板演示如何通过 IaC 部署 AgentCore Code Interpreter：

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'AgentCore Code Interpreter - VPC + Tags'

Parameters:
  SubnetIds:
    Type: List<AWS::EC2::Subnet::Id>
    Description: Private subnet IDs
  SecurityGroupId:
    Type: AWS::EC2::SecurityGroup::Id
    Description: Security group ID

Resources:
  PublicCodeInterpreter:
    Type: AWS::BedrockAgentCore::CodeInterpreterCustom
    Properties:
      Name: cfn_public_ci
      Description: "Public mode code interpreter"
      NetworkConfiguration:
        NetworkMode: PUBLIC
      Tags:
        Project: agentcore-lab
        DeployedBy: cloudformation

  VpcCodeInterpreter:
    Type: AWS::BedrockAgentCore::CodeInterpreterCustom
    Properties:
      Name: cfn_vpc_ci
      Description: "VPC mode code interpreter"
      NetworkConfiguration:
        NetworkMode: VPC
        VpcConfig:
          Subnets: !Ref SubnetIds
          SecurityGroups:
            - !Ref SecurityGroupId
      Tags:
        Project: agentcore-lab
        DeployedBy: cloudformation
        NetworkMode: VPC

Outputs:
  PublicCIId:
    Value: !Ref PublicCodeInterpreter
  VpcCIId:
    Value: !Ref VpcCodeInterpreter
```

!!! warning "CFN Tags 格式"
    AgentCore 的 CloudFormation Tags 使用 **Map 格式**（`Key: Value`），不是标准 CFN 的 Array 格式（`- Key: k, Value: v`）。如果使用 Array 格式会报错 `expected type: JSONObject, found: JSONArray`。

部署 Stack：

```bash
aws cloudformation create-stack \
  --stack-name agentcore-lab \
  --template-body file://agentcore-template.yaml \
  --parameters \
    ParameterKey=SubnetIds,ParameterValue="$PRIV_SUBNET_1\\,$PRIV_SUBNET_2" \
    ParameterKey=SecurityGroupId,ParameterValue=$SG_ID \
  --region $AWS_REGION
```

### Step 5: 标签管理

```bash
# 获取资源 ARN
CI_ARN=$(aws bedrock-agentcore-control get-code-interpreter \
  --code-interpreter-id <your-id> \
  --region $AWS_REGION \
  --query 'codeInterpreterArn' --output text)

# 添加标签
aws bedrock-agentcore-control tag-resource \
  --resource-arn $CI_ARN \
  --tags '{"CostCenter":"ai-team","Owner":"platform-eng"}' \
  --region $AWS_REGION

# 查看标签
aws bedrock-agentcore-control list-tags-for-resource \
  --resource-arn $CI_ARN \
  --region $AWS_REGION

# 删除标签
aws bedrock-agentcore-control untag-resource \
  --resource-arn $CI_ARN \
  --tag-keys "Owner" \
  --region $AWS_REGION
```

## 测试结果

### PUBLIC vs VPC 模式对比

| 指标 | PUBLIC 模式 | VPC 模式 | 差异 |
|------|------------|---------|------|
| 资源创建时间 | 秒级 READY | 3-5 分钟 | VPC 需创建 ENI |
| 会话创建时间 | 秒级 | 秒级 | 无明显差异 |
| 会话超时 | 900s | 900s | 相同 |
| 私有资源访问 | ❌ | ✅ | VPC 核心价值 |
| ENI 创建 | 无 | 自动创建 | 每个子网 1 个 ENI |

### PrivateLink 端点测试

| 端点 | 状态 | Private DNS | 备注 |
|------|------|------------|------|
| 数据面 (bedrock-agentcore) | ✅ available | bedrock-agentcore.us-east-1.amazonaws.com | 支持 az1 + az2 |
| 控制面 (bedrock-agentcore-control) | ✅ available | bedrock-agentcore-control.us-east-1.amazonaws.com | 支持 az1 + az2 |
| Gateway (bedrock-agentcore.gateway) | ✅ available | gateway.bedrock-agentcore.us-east-1.amazonaws.com | **仅 az2**（实测） |

### CloudFormation 部署结果

| 资源 | 类型 | 创建时间 | 状态 |
|------|------|---------|------|
| PublicCodeInterpreter | CodeInterpreterCustom | ~7 秒 | CREATE_COMPLETE |
| VpcCodeInterpreter | CodeInterpreterCustom | ~45 秒 | CREATE_COMPLETE（含 eventual consistency check） |

### 边界测试结果

| 测试 | 操作 | 结果 |
|------|------|------|
| 不支持的 AZ (use1-az5) | 创建 VPC CI | CREATE_FAILED，错误信息明确列出支持的 AZ |
| 无 NAT Gateway 子网 | 创建 VPC CI | CI 可创建（ENI 不依赖 NAT），但代码的外网访问受限 |

## 踩坑记录

!!! warning "踩坑 1: CloudFormation Tags 格式不一致"
    AgentCore CFN 资源的 Tags 属性使用 **Map 格式**（`Key: Value`），而非 AWS 标准的 Array 格式。这是 Preview 阶段的实现差异，使用错误格式会导致 `Properties validation failed: expected type: JSONObject, found: JSONArray`。**已查文档确认：CFN Schema 定义 Tags 为 object 类型。**

!!! warning "踩坑 2: Gateway PrivateLink 端点 AZ 限制"
    在 us-east-1 中，Gateway 端点 (`com.amazonaws.us-east-1.bedrock-agentcore.gateway`) 不支持 us-east-1a (use1-az1)，而数据面和控制面端点支持。创建时会报 `InvalidParameter: The VPC endpoint service does not support the availability zone of the subnet`。**实测发现，官方文档未记录此 AZ 差异。** 建议在多个 AZ 中尝试。

!!! warning "踩坑 3: VPC CI 创建为异步操作"
    VPC 模式创建 Code Interpreter 时，API 立即返回 `status: CREATING`，不会同步报错。即使提供了不支持的 AZ，也先返回成功再异步变为 `CREATE_FAILED`。建议创建后轮询 `get-code-interpreter` 确认状态。**已查文档确认：这是设计行为。**

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| NAT Gateway | $0.045/hr | 1 hr | $0.045 |
| NAT Gateway 数据处理 | $0.045/GB | ~0.01 GB | ~$0.001 |
| VPC Endpoint (×3) | $0.01/hr/AZ | 1 hr × 5 AZ | $0.05 |
| Elastic IP | $0.005/hr | 1 hr | $0.005 |
| AgentCore (Preview) | Preview 定价 | 测试用量 | ~$0 |
| **合计** | | | **~$0.10** |

## 清理资源

```bash
# 1. 删除 CloudFormation Stack
aws cloudformation delete-stack --stack-name agentcore-lab --region $AWS_REGION
aws cloudformation wait stack-delete-complete --stack-name agentcore-lab --region $AWS_REGION

# 2. 删除手动创建的 Code Interpreter
aws bedrock-agentcore-control delete-code-interpreter \
  --code-interpreter-id <your-vpc-ci-id> --region $AWS_REGION
aws bedrock-agentcore-control delete-code-interpreter \
  --code-interpreter-id <your-public-ci-id> --region $AWS_REGION

# 3. 删除 VPC Endpoints
for vpce in $(aws ec2 describe-vpc-endpoints --region $AWS_REGION \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'VpcEndpoints[*].VpcEndpointId' --output text); do
  aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $vpce --region $AWS_REGION
done

# 4. 等待 AgentCore ENI 释放（最长 8 小时，通常几分钟内）
echo "Checking for remaining ENIs..."
aws ec2 describe-network-interfaces --region $AWS_REGION \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'NetworkInterfaces[*].[NetworkInterfaceId,Description,Status]' \
  --output table

# 5. 删除 NAT Gateway 和 EIP
aws ec2 delete-nat-gateway --nat-gateway-id $NAT_ID --region $AWS_REGION
sleep 60  # 等待 NAT Gateway 完全删除
aws ec2 release-address --allocation-id $EIP_ALLOC --region $AWS_REGION

# 6. 删除子网、路由表、安全组、IGW、VPC
# （等 ENI 完全释放后再执行）
aws ec2 delete-subnet --subnet-id $PRIV_SUBNET_1 --region $AWS_REGION
aws ec2 delete-subnet --subnet-id $PRIV_SUBNET_2 --region $AWS_REGION
aws ec2 delete-subnet --subnet-id $PUB_SUBNET --region $AWS_REGION
aws ec2 delete-route-table --route-table-id $PRIV_RT --region $AWS_REGION
aws ec2 delete-route-table --route-table-id $PUB_RT --region $AWS_REGION
aws ec2 delete-security-group --group-id $SG_ID --region $AWS_REGION
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID \
  --vpc-id $VPC_ID --region $AWS_REGION
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID --region $AWS_REGION
aws ec2 delete-vpc --vpc-id $VPC_ID --region $AWS_REGION
```

!!! danger "务必清理"
    NAT Gateway ($0.045/hr) 和 VPC Endpoint ($0.01/hr/AZ) 会持续产生费用。清理 VPC 前务必先检查 ENI 残留，AgentCore 创建的 ENI 可能延迟释放。

## 结论与建议

### 适用场景

- **金融/医疗等合规行业** — VPC + PrivateLink 实现完全私有化，满足数据不出 VPC 的合规要求
- **多环境 CI/CD** — CloudFormation 模板化部署，一键创建 dev/staging/prod 环境
- **成本治理** — 标签配合 AWS Cost Explorer 实现 Agent 级别的成本分摊

### 生产建议

1. **至少两个 AZ 的私有子网**，确保高可用
2. **在 VPC Endpoint 上配置 Endpoint Policy**，限制 API 访问范围
3. **使用 CloudFormation 部署**，避免手动管理资源漂移
4. **统一标签策略**，建议至少打 Project、Environment、Owner 三个标签
5. **开启 VPC Flow Logs**，监控 AgentCore ENI 的网络流量

### 当前限制（Preview）

- Evaluations 数据面尚不支持 PrivateLink（控制面支持）
- Gateway PrivateLink 端点的 AZ 支持范围可能比数据面/控制面小
- CFN Tags 使用 Map 格式，与 AWS 标准 Array 格式不一致

## 参考链接

- [Configuring VPC for AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)
- [Use Interface VPC endpoints (AWS PrivateLink)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html)
- [AgentCore CloudFormation Reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/AWS_BedrockAgentCore.html)
- [Tagging AgentCore resources](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/tagging.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-bedrock-agentcore-runtime-browser-code-interpreter-vpc-privatelink-cloudformation-tagging/)
