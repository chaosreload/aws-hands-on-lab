# AWS IAM 新增 VPC Endpoint Condition Keys 实战：可扩展的网络边界控制

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

随着企业在 AWS 上的工作负载增长，VPC 和 VPC Endpoint 的数量也在增加。此前，要实现"只允许来自我们自己网络的请求访问 S3 数据"，你需要在 IAM 策略中逐个枚举每个 VPC ID (`aws:SourceVpc`) 或 VPC Endpoint ID (`aws:SourceVpce`)。策略随 VPC 增长而膨胀、维护困难、审计噩梦。

**2025 年 8 月 29 日**，AWS IAM 发布了三个新的全局条件键，从根本上解决这个问题：

- **`aws:VpceAccount`** — 按账户维度：请求必须通过指定 AWS 账户拥有的 VPC Endpoint 发起
- **`aws:VpceOrgPaths`** — 按 OU 维度：请求必须通过指定 OU 路径下的 VPC Endpoint 发起
- **`aws:VpceOrgID`** — 按组织维度：请求必须通过指定 Organization 内的 VPC Endpoint 发起

本文将通过实际部署和测试，验证 `aws:VpceAccount` 条件键在 S3 Bucket Policy 中的行为，特别是在**不同请求来源**（Interface Endpoint、Gateway Endpoint、公网）和**不同策略模式**下的表现。

## 前置条件

- AWS 账号（需要 EC2、VPC、S3、IAM、SSM 权限）
- AWS CLI v2 已配置
- 一个可 SSH 的管理机（用于公网请求对比测试）

## 核心概念

### 新旧条件键对比

| 维度 | 旧键 `aws:SourceVpc` / `aws:SourceVpce` | 新键 `aws:VpceAccount` / `VpceOrgID` / `VpceOrgPaths` |
|------|----------------------------------------|-------------------------------------------------------|
| **粒度** | 单个 VPC / VPC Endpoint | 账户 / OU / 组织 |
| **扩展性** | 需枚举所有 VPC/VPCE ID | 自动覆盖新增 VPC Endpoint |
| **策略大小** | 随 VPC 增长膨胀 | 固定大小 |
| **审计** | 难（长列表） | 易（语义清晰） |
| **服务支持** | 广泛 | 部分服务 |

### 关键限制

1. **仅支持部分 AWS 服务**（目前包括 S3、KMS 等，详见 [IAM 文档](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html)）
2. **敏感条件键**：`aws:VpceAccount` 和 `aws:VpceOrgID` 不允许使用通配符
3. **请求不经过 VPC Endpoint 时键不存在**，需配合 `IfExists` 操作符设计策略

## 动手实践

### Step 1: 创建 VPC 和网络基础设施

```bash
# 设置环境变量
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1

# 创建 VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.200.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=vpce-condkey-test-vpc}]' \
  --query 'Vpc.VpcId' --output text)
echo "VPC: $VPC_ID"

# 启用 DNS（Interface Endpoint 需要）
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support '{"Value":true}'

# 创建子网
SUBNET_ID=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID --cidr-block 10.200.1.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=vpce-condkey-test-subnet}]' \
  --query 'Subnet.SubnetId' --output text)
echo "Subnet: $SUBNET_ID"

# 创建 Internet Gateway（用于公网对比测试）
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=vpce-condkey-test-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --vpc-id $VPC_ID --internet-gateway-id $IGW_ID

# 创建路由表
RT_ID=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=vpce-condkey-test-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $RT_ID --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID
aws ec2 associate-route-table --route-table-id $RT_ID --subnet-id $SUBNET_ID

# 创建安全组（仅允许 VPC 内 HTTPS 流量，禁止公网入站）
SG_ID=$(aws ec2 create-security-group \
  --group-name vpce-condkey-test-sg \
  --description "VPC Endpoint test - NO public inbound" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID --protocol tcp --port 443 --cidr 10.200.0.0/16
echo "Security Group: $SG_ID (HTTPS from VPC only)"
```

### Step 2: 创建 S3 Bucket 和 VPC Endpoints

```bash
# 获取当前账户 ID
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)

# 创建 S3 Bucket
aws s3 mb s3://vpce-condkey-test-${ACCOUNT_ID}
echo "test content" | aws s3 cp - s3://vpce-condkey-test-${ACCOUNT_ID}/test.txt

# 创建 S3 Interface Endpoint（PrivateLink，按小时计费）
VPCE_IF=$(aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID --vpc-endpoint-type Interface \
  --service-name com.amazonaws.us-east-1.s3 \
  --subnet-ids $SUBNET_ID --security-group-ids $SG_ID \
  --tag-specifications 'ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=vpce-condkey-test-s3if}]' \
  --query 'VpcEndpoint.VpcEndpointId' --output text)
echo "S3 Interface Endpoint: $VPCE_IF"

# 创建 S3 Gateway Endpoint（免费）
VPCE_GW=$(aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID --vpc-endpoint-type Gateway \
  --service-name com.amazonaws.us-east-1.s3 \
  --route-table-ids $RT_ID \
  --tag-specifications 'ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=vpce-condkey-test-s3gw}]' \
  --query 'VpcEndpoint.VpcEndpointId' --output text)
echo "S3 Gateway Endpoint: $VPCE_GW"
```

!!! note "等待 Endpoint 就绪"
    Interface Endpoint 创建后需要 2-5 分钟变为 `available` 状态。可以用以下命令监控：
    ```bash
    aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $VPCE_IF \
      --query 'VpcEndpoints[0].State' --output text
    ```

### Step 3: 创建 EC2 实例（通过 SSM 访问）

```bash
# 创建 IAM Role
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role --role-name vpce-condkey-test-role \
  --assume-role-policy-document file:///tmp/trust-policy.json
aws iam attach-role-policy --role-name vpce-condkey-test-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name vpce-condkey-test-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# 创建 Instance Profile
aws iam create-instance-profile --instance-profile-name vpce-condkey-test-profile
aws iam add-role-to-instance-profile \
  --instance-profile-name vpce-condkey-test-profile \
  --role-name vpce-condkey-test-role
sleep 10  # 等待 IAM 传播

# 创建 SSM VPC Endpoints（用于 Session Manager 访问 EC2）
for SVC in ssm ssmmessages ec2messages; do
  aws ec2 create-vpc-endpoint \
    --vpc-id $VPC_ID --vpc-endpoint-type Interface \
    --service-name com.amazonaws.us-east-1.$SVC \
    --subnet-ids $SUBNET_ID --security-group-ids $SG_ID \
    --private-dns-enabled \
    --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=vpce-condkey-test-$SVC}]"
done

# 获取最新 AL2023 AMI
AMI_ID=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameters[0].Value' --output text)

# 启动 EC2 实例
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID --instance-type t3.micro \
  --subnet-id $SUBNET_ID --security-group-ids $SG_ID \
  --iam-instance-profile Name=vpce-condkey-test-profile \
  --associate-public-ip-address \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=vpce-condkey-test-ec2}]' \
  --query 'Instances[0].InstanceId' --output text)
echo "EC2 Instance: $INSTANCE_ID"
```

### Step 4: 设置 Deny 型 Bucket Policy 并测试

```bash
# 获取 Interface Endpoint 的 DNS 名称
VPCE_DNS=$(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids $VPCE_IF \
  --query 'VpcEndpoints[0].DnsEntries[0].DnsName' --output text)

# 设置 Deny 策略：拒绝非本账户 VPC Endpoint 的请求
cat > /tmp/deny-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyIfNotFromOurVpceAccount",
    "Effect": "Deny",
    "Principal": "*",
    "Action": ["s3:GetObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::vpce-condkey-test-${ACCOUNT_ID}",
      "arn:aws:s3:::vpce-condkey-test-${ACCOUNT_ID}/*"
    ],
    "Condition": {
      "StringNotEqualsIfExists": {
        "aws:VpceAccount": "${ACCOUNT_ID}"
      },
      "BoolIfExists": {
        "aws:ViaAWSService": "false"
      }
    }
  }]
}
EOF
aws s3api put-bucket-policy --bucket vpce-condkey-test-${ACCOUNT_ID} \
  --policy file:///tmp/deny-policy.json
```

### Step 5: 验证结果

通过 SSM 在 EC2 上执行测试命令：

```bash
# 测试 1: 通过 Interface Endpoint 访问 → 应该成功
aws ssm send-command --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"aws s3 ls s3://vpce-condkey-test-${ACCOUNT_ID}/ \
    --endpoint-url https://bucket.${VPCE_DNS} --region us-east-1 2>&1\"]"

# 测试 2: 通过 Gateway Endpoint 访问（不指定 --endpoint-url）→ 应该成功
aws ssm send-command --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"aws s3 ls s3://vpce-condkey-test-${ACCOUNT_ID}/ \
    --region us-east-1 2>&1\"]"

# 测试 3: 从公网直接访问（在管理机上执行）→ 应该被拒绝
aws s3 ls s3://vpce-condkey-test-${ACCOUNT_ID}/
# 预期输出: AccessDenied
```

## 测试结果

| 场景 | 请求路径 | Bucket Policy 条件 | 结果 | 说明 |
|------|---------|-------------------|------|------|
| **T1** EC2 → Interface Endpoint → S3 | `--endpoint-url` 指定 VPCE | Deny if VpceAccount ≠ 本账户 | ✅ **允许** | VpceAccount 匹配，Deny 不触发 |
| **T2** EC2 → Interface Endpoint → S3 | `--endpoint-url` 指定 VPCE | Deny if VpceAccount ≠ 错误账户 | ❌ **拒绝** | VpceAccount 不匹配，Deny 触发 |
| **T3** 管理机 → 公网 → S3 | 无 VPC Endpoint | Deny if VpceAccount ≠ 本账户 (IfExists) | ❌ **拒绝** | 键缺失时 IfExists 为 true → Deny 生效 |
| **T4** EC2 → Gateway Endpoint → S3 | 默认路由表路由 | Deny if VpceAccount is Null | ✅ **允许** | **Gateway Endpoint 也填充 VpceAccount** |
| **T5** EC2 → 无 VPCE → EC2 API | Identity policy | Deny if VpceAccount ≠ 本账户 | ❌ **拒绝** | 不支持的服务，键缺失 → 意外 Deny |

### 关键发现 1: Gateway Endpoint 同样支持新条件键

这是本次实验最重要的发现之一。官方文档指出 Gateway Endpoint "不使用 AWS PrivateLink"，但我们的 Null 条件测试证明 **S3 Gateway Endpoint 同样填充 `aws:VpceAccount` 条件键**。

```json
{
  "Condition": {"Null": {"aws:VpceAccount": "true"}}
}
```
→ 公网请求被 Deny（键缺失）
→ Gateway Endpoint 请求通过（键存在）

### 关键发现 2: 对不支持的服务使用新条件键会导致意外拒绝

当我们在 identity-based policy 中对 EC2 DescribeInstances 使用 `aws:VpceAccount` 条件键时：

- EC2 API 不通过 S3 VPC Endpoint → 键缺失
- `StringNotEqualsIfExists` 对缺失键返回 true → Deny 生效

!!! danger "注意"
    在 SCP 或 identity-based policy 中使用这些新条件键时，**必须限制 Action 为已支持的服务**。否则会导致不支持的服务全部被拒绝。已查文档确认。

### 关键发现 3: Deny 策略中 StringNotEquals 和 StringNotEqualsIfExists 行为相同

在我们的测试中，两种操作符在 Deny 场景下对公网请求的处理完全相同——都触发了 Deny。这可能是因为 S3 将缺失的条件键视为"不等于"任何指定值。

AWS 官方博客推荐使用 `IfExists` 版本，这更多是为了语义清晰和跨服务行为一致性。

## 踩坑记录

!!! warning "踩坑 1: Deny 型 Bucket Policy 会锁定公网管理通道"
    设置 Deny + `aws:VpceAccount` 的 bucket policy 后，从公网（如管理机）执行 `s3api put-bucket-policy` 也会被拒绝。**必须从 VPC Endpoint 内部修改策略**。

    建议：在 bucket policy 中保留一条基于 `aws:SourceVpce` 的 Allow 语句作为管理逃生门。已查文档确认这是最佳实践。

!!! warning "踩坑 2: SSM 传复杂 JSON 策略的引号问题"
    通过 SSM RunShellScript 传递包含 JSON 的命令时，多层引号转义极易出错。

    **解决方案**：将 JSON 文件 base64 编码后传递给 EC2，在实例上解码后使用 `file://` 引用。

!!! warning "踩坑 3: VPC Endpoint 删除后 ENI 残留"
    删除 Interface Endpoint 后，关联的 ENI 不会立即释放，导致子网和安全组无法删除。

    **解决方案**：等待 1-2 分钟后检查 ENI 残留（`describe-network-interfaces --filters group-id`），手动删除 `available` 状态的孤立 ENI。已查文档确认这是预期行为。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 Interface Endpoint | $0.01/hr | ~2 hr | $0.02 |
| SSM Interface Endpoints (×3) | $0.01/hr each | ~2 hr | $0.06 |
| EC2 t3.micro | $0.0104/hr | ~2 hr | $0.02 |
| S3 Gateway Endpoint | 免费 | - | $0.00 |
| S3 存储 + 请求 | - | 极少 | $0.00 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除 Bucket Policy 和 S3 Bucket
aws s3api delete-bucket-policy --bucket vpce-condkey-test-${ACCOUNT_ID}
aws s3 rb s3://vpce-condkey-test-${ACCOUNT_ID} --force

# 2. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids $INSTANCE_ID
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID

# 3. 删除所有 VPC Endpoints
VPCE_IDS=$(aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'VpcEndpoints[].VpcEndpointId' --output text)
aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $VPCE_IDS

# 4. 等待 ENI 释放（重要！）
sleep 120
# 检查并删除残留 ENI
aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$SG_ID" \
  --query 'NetworkInterfaces[?Status==`available`].NetworkInterfaceId' --output text | \
  xargs -I {} aws ec2 delete-network-interface --network-interface-id {}

# 5. IAM 清理
aws iam detach-role-policy --role-name vpce-condkey-test-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam detach-role-policy --role-name vpce-condkey-test-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam remove-role-from-instance-profile \
  --instance-profile-name vpce-condkey-test-profile \
  --role-name vpce-condkey-test-role
aws iam delete-instance-profile --instance-profile-name vpce-condkey-test-profile
aws iam delete-role --role-name vpce-condkey-test-role

# 6. 删除 VPC 资源（按依赖顺序）
aws ec2 disassociate-route-table --association-id $(aws ec2 describe-route-tables \
  --route-table-ids $RT_ID --query 'RouteTables[0].Associations[0].RouteTableAssociationId' \
  --output text)
aws ec2 delete-subnet --subnet-id $SUBNET_ID
aws ec2 delete-security-group --group-id $SG_ID
aws ec2 delete-route-table --route-table-id $RT_ID
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID
aws ec2 delete-vpc --vpc-id $VPC_ID
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。Interface Endpoint 按小时计费，**忘记删除 = 持续产生费用**。

## 结论与建议

### 适用场景

| 场景 | 推荐方案 |
|------|---------|
| **组织级网络边界**（100+ VPC） | ✅ `aws:VpceOrgID` — 一个条件覆盖整个组织 |
| **按 OU 隔离**（财务/市场/生产/开发） | ✅ `aws:VpceOrgPaths` — 按 OU 边界自动隔离 |
| **跨账户数据访问控制** | ✅ `aws:VpceAccount` — 精确到账户维度 |
| **单 VPC 精确控制** | 继续用 `aws:SourceVpc` / `aws:SourceVpce` |
| **需覆盖所有 AWS 服务** | 继续用旧键（新键目前仅支持部分服务） |

### 生产环境建议

1. **先查支持的服务列表**：在 RCP 或 SCP 中使用前，确认目标服务支持这些条件键
2. **Deny 策略必须有逃生门**：预留管理通道，避免被自己的策略锁定
3. **用 `IfExists` 操作符**：虽然实测中 `StringNotEquals` 表现一致，但 `IfExists` 语义更清晰
4. **渐进式部署**：先在非生产账户 / 单个 OU 测试，确认行为后再推广
5. **配合 CloudTrail 审计**：使用新条件键后，通过 CloudTrail 验证请求是否按预期被允许/拒绝

## 参考链接

- [AWS IAM Global Condition Context Keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html)
- [AWS What's New: IAM launches new VPC endpoint condition keys](https://aws.amazon.com/about-aws/whats-new/2025/08/aws-iam-new-vpc-endpoint-condition-keys/)
- [AWS Security Blog: Use scalable controls to help prevent access from unexpected networks](https://aws.amazon.com/blogs/security/use-scalable-controls-to-help-prevent-access-from-unexpected-networks/)
- [Data Perimeter Policy Examples (GitHub)](https://github.com/aws-samples/data-perimeter-policy-examples)
