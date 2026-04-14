---
description: "Compare three AWS DataSync credential management modes with Secrets Manager integration for SMB, HDFS, and object storage locations."
tags:
  - Storage
---
# AWS DataSync 集成 Secrets Manager：三种凭证管理模式实测对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $2.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-22

## 背景

AWS DataSync 在传输数据时，需要凭证来访问 SMB、HDFS、Object Storage 等存储位置。此前部分位置类型需要通过 API 或控制台直接传递密码，存在安全隐患。

**2026 年 3 月更新**：DataSync 现已支持对**所有位置类型**使用 AWS Secrets Manager 进行凭证管理，提供三种灵活的凭证管理模式，满足从快速上手到企业合规的不同需求。

## 前置条件

- AWS 账号，具有 `AWSDataSyncFullAccess` 权限
- AWS CLI v2 已配置
- 一个可用的 VPC 及公有子网（DataSync Agent 需访问公网）

## 核心概念

DataSync 凭证管理现在支持三种模式：

| 模式 | API 参数 | KMS 加密 | Secret 生命周期 | 适用场景 |
|------|----------|---------|----------------|----------|
| **服务托管 + 默认密钥** | 直传 Password（隐式 ManagedSecretConfig） | AWS 托管密钥 | DataSync 自动创建/删除 | 快速上手、开发测试 |
| **服务托管 + 自定义 KMS** | CmkSecretConfig | 用户提供 KMS key | DataSync 自动创建/删除 | 企业合规、审计要求 |
| **用户完全自管** | CustomSecretConfig | 用户自选 | 用户自行管理 | 集中凭证管理、自定义轮换 |

**关键限制**：`CmkSecretConfig` 和 `CustomSecretConfig` **互斥**，不可同时使用。

## 动手实践

本 Lab 使用 SMB 位置类型，在同一台 Samba 服务器上依次测试三种凭证管理模式，并进行端到端数据传输验证。

### Step 1: 准备基础设施

#### 1.1 网络和安全组

```bash
# 变量设置
REGION="us-east-1"
PROFILE="your-profile"  # 替换为你的 AWS CLI profile
VPC_ID="your-vpc-id"    # 替换为你的 VPC ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile $PROFILE)

# 创建公有子网（DataSync Agent 需访问公网）
SUBNET_ID=$(aws ec2 create-subnet \
    --vpc-id $VPC_ID --cidr-block 10.1.10.0/24 \
    --availability-zone ${REGION}a \
    --query 'Subnet.SubnetId' --output text \
    --region $REGION --profile $PROFILE)
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_ID --map-public-ip-on-launch \
    --region $REGION --profile $PROFILE

# 创建 Samba 服务器安全组
SAMBA_SG=$(aws ec2 create-security-group \
    --group-name datasync-lab-samba \
    --description "Samba server for DataSync lab" \
    --vpc-id $VPC_ID \
    --query 'GroupId' --output text \
    --region $REGION --profile $PROFILE)
aws ec2 authorize-security-group-ingress \
    --group-id $SAMBA_SG --protocol tcp --port 445 \
    --cidr 10.1.0.0/16 \
    --region $REGION --profile $PROFILE

# 创建 DataSync Agent 安全组
AGENT_SG=$(aws ec2 create-security-group \
    --group-name datasync-lab-agent \
    --description "DataSync Agent" \
    --vpc-id $VPC_ID \
    --query 'GroupId' --output text \
    --region $REGION --profile $PROFILE)
aws ec2 authorize-security-group-ingress \
    --group-id $AGENT_SG --protocol tcp --port 80 \
    --cidr 0.0.0.0/0 \
    --region $REGION --profile $PROFILE
```

#### 1.2 部署 Samba 服务器

```bash
# 获取最新 Amazon Linux 2023 AMI
AL2023_AMI=$(aws ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
    --query 'Images | sort_by(@, &CreationDate) | [-1].ImageId' --output text \
    --region $REGION --profile $PROFILE)

# 启动 Samba 实例（user-data 自动配置 Samba）
SAMBA_INSTANCE=$(aws ec2 run-instances \
    --image-id $AL2023_AMI \
    --instance-type t3.micro \
    --subnet-id $SUBNET_ID \
    --security-group-ids $SAMBA_SG \
    --user-data '#!/bin/bash
yum install -y samba
mkdir -p /srv/samba/share && chmod 777 /srv/samba/share
echo "Hello DataSync" > /srv/samba/share/test.txt
cat > /etc/samba/smb.conf <<EOF
[global]
workgroup = WORKGROUP
security = user
map to guest = never
[share]
path = /srv/samba/share
browsable = yes
writable = yes
valid users = datasync
EOF
useradd -M datasync
echo -e "TestPassword123!\nTestPassword123!" | smbpasswd -a datasync -s
systemctl enable --now smb' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=datasync-lab-samba}]' \
    --query 'Instances[0].InstanceId' --output text \
    --region $REGION --profile $PROFILE)

SAMBA_IP=$(aws ec2 describe-instances --instance-ids $SAMBA_INSTANCE \
    --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text \
    --region $REGION --profile $PROFILE)
echo "Samba 服务器私有 IP: $SAMBA_IP"
```

#### 1.3 部署并激活 DataSync Agent

```bash
# 获取 DataSync Agent AMI
AGENT_AMI=$(aws ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=aws-datasync-*' 'Name=state,Values=available' \
    --query 'Images | sort_by(@, &CreationDate) | [-1].ImageId' --output text \
    --region $REGION --profile $PROFILE)

# 启动 Agent 实例（m5.xlarge 是最低要求）
AGENT_INSTANCE=$(aws ec2 run-instances \
    --image-id $AGENT_AMI \
    --instance-type m5.xlarge \
    --subnet-id $SUBNET_ID \
    --security-group-ids $AGENT_SG \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=datasync-lab-agent}]' \
    --query 'Instances[0].InstanceId' --output text \
    --region $REGION --profile $PROFILE)

# 等待实例就绪
aws ec2 wait instance-status-ok --instance-ids $AGENT_INSTANCE \
    --region $REGION --profile $PROFILE

AGENT_PUBLIC_IP=$(aws ec2 describe-instances --instance-ids $AGENT_INSTANCE \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text \
    --region $REGION --profile $PROFILE)

# 获取激活密钥并激活 Agent
ACTIVATION_KEY=$(curl -s "http://${AGENT_PUBLIC_IP}/?gatewayType=SYNC&activationRegion=${REGION}&no_redirect")
echo "激活密钥: $ACTIVATION_KEY"

AGENT_ARN=$(aws datasync create-agent \
    --activation-key $ACTIVATION_KEY \
    --agent-name datasync-lab-agent \
    --query 'AgentArn' --output text \
    --region $REGION --profile $PROFILE)
echo "Agent ARN: $AGENT_ARN"
```

#### 1.4 创建辅助资源

```bash
# S3 目标桶
BUCKET_NAME="datasync-lab-${ACCOUNT_ID}"
aws s3 mb "s3://${BUCKET_NAME}" --region $REGION --profile $PROFILE

# DataSync S3 访问角色
aws iam create-role --role-name datasync-lab-s3-role \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "datasync.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }' --profile $PROFILE

aws iam put-role-policy --role-name datasync-lab-s3-role \
    --policy-name s3-access \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
                "Resource": "arn:aws:s3:::'${BUCKET_NAME}'"
            },
            {
                "Effect": "Allow",
                "Action": ["s3:AbortMultipartUpload", "s3:DeleteObject", "s3:GetObject", "s3:ListMultipartUploadParts", "s3:PutObject", "s3:GetObjectTagging", "s3:PutObjectTagging"],
                "Resource": "arn:aws:s3:::'${BUCKET_NAME}'/*"
            }
        ]
    }' --profile $PROFILE

S3_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/datasync-lab-s3-role"

# DataSync 服务关联角色（首次使用时创建）
aws iam create-service-linked-role --aws-service-name datasync.amazonaws.com \
    --profile $PROFILE 2>/dev/null || echo "SLR 已存在"
```

### Step 2: 模式一 — 服务托管 + 默认密钥（ManagedSecretConfig）

最简单的模式：直接传递 Password，DataSync 自动在 Secrets Manager 中创建并管理 secret。

```bash
# 创建 SMB 位置 — 只需 Password，不需要额外配置
LOCATION_MANAGED=$(aws datasync create-location-smb \
    --server-hostname $SAMBA_IP \
    --subdirectory /share \
    --user datasync \
    --password 'TestPassword123!' \
    --agent-arns $AGENT_ARN \
    --query 'LocationArn' --output text \
    --region $REGION --profile $PROFILE)
echo "Location (Managed): $LOCATION_MANAGED"
```

验证 DataSync 自动创建了 Secrets Manager secret：

```bash
# 查看自动创建的 secret
aws secretsmanager list-secrets \
    --filters Key=name,Values=aws-datasync \
    --query 'SecretList[*].[Name, KmsKeyId]' --output table \
    --region $REGION --profile $PROFILE
```

输出示例：

```
+-------------------------------------+-------+
|  aws-datasync!loc-0df012e8cfdd0dce6 |  None |
+-------------------------------------+-------+
```

**关键观察**：

- Secret 命名格式为 `aws-datasync!loc-{location-id}`
- `KmsKeyId` 为 `None`，表示使用 AWS 默认托管密钥
- `describe-location-smb` 返回中可见 `ManagedSecretConfig.SecretArn`

### Step 3: 模式二 — 服务托管 + 自定义 KMS 密钥（CmkSecretConfig）

适用于需要自控加密密钥的合规场景。

```bash
# 创建 KMS 密钥（必须是对称加密 ENCRYPT_DECRYPT 类型）
KMS_KEY_ID=$(aws kms create-key \
    --description "DataSync Secret Encryption Key" \
    --query 'KeyMetadata.KeyId' --output text \
    --region $REGION --profile $PROFILE)
KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}"

# 添加 KMS key policy — 允许 DataSync SLR 解密
# 注意：必须保留 root 账号完全访问权限
aws kms put-key-policy --key-id $KMS_KEY_ID --policy-name default \
    --policy '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnableIAMUserPermissions",
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::'${ACCOUNT_ID}':root"},
                "Action": "kms:*",
                "Resource": "*"
            },
            {
                "Sid": "AllowDataSyncSLRDecrypt",
                "Effect": "Allow",
                "Principal": {
                    "AWS": "arn:aws:iam::'${ACCOUNT_ID}':role/aws-service-role/datasync.amazonaws.com/AWSServiceRoleForDataSync"
                },
                "Action": "kms:Decrypt",
                "Resource": "*"
            }
        ]
    }' --region $REGION --profile $PROFILE

# 创建 SMB 位置 — 使用 CmkSecretConfig
LOCATION_CMK=$(aws datasync create-location-smb \
    --server-hostname $SAMBA_IP \
    --subdirectory /share \
    --user datasync \
    --password 'TestPassword123!' \
    --agent-arns $AGENT_ARN \
    --cmk-secret-config '{"KmsKeyArn": "'${KMS_KEY_ARN}'"}' \
    --query 'LocationArn' --output text \
    --region $REGION --profile $PROFILE)
echo "Location (CMK): $LOCATION_CMK"
```

验证 secret 使用了自定义 KMS 密钥：

```bash
aws secretsmanager list-secrets \
    --filters Key=name,Values=aws-datasync \
    --query 'SecretList[*].[Name, KmsKeyId]' --output table \
    --region $REGION --profile $PROFILE
```

输出示例：

```
+-------------------------------------+-----------------------------------------------------------------------+
|  aws-datasync!loc-0df012e8cfdd0dce6 |  None                                                                 |
|  aws-datasync!loc-0fe5d2283f3c02e8e |  arn:aws:kms:us-east-1:595842667825:key/61ae233c-56b7-4ca0-ae24-...   |
+-------------------------------------+-----------------------------------------------------------------------+
```

**对比**：模式一的 `KmsKeyId=None`（默认密钥） vs 模式二的自定义 KMS key ARN。

### Step 4: 模式三 — 用户完全自管（CustomSecretConfig）

适用于需要集中管理凭证、自定义轮换策略的场景。

```bash
# 1. 预创建 Secrets Manager secret（必须是 plain text，不是 JSON）
SECRET_ARN=$(aws secretsmanager create-secret \
    --name datasync-lab-smb-password \
    --secret-string 'TestPassword123!' \
    --query 'ARN' --output text \
    --region $REGION --profile $PROFILE)

# 2. 创建 DataSync 信任的 IAM Role
aws iam create-role --role-name datasync-lab-secret-role \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "datasync.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": "'${ACCOUNT_ID}'"},
                "ArnLike": {"aws:SourceArn": "arn:aws:datasync:'${REGION}':'${ACCOUNT_ID}':*"}
            }
        }]
    }' --profile $PROFILE

# 3. 授权 Role 访问 secret
aws iam put-role-policy --role-name datasync-lab-secret-role \
    --policy-name secret-access \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            "Resource": "'${SECRET_ARN}'"
        }]
    }' --profile $PROFILE

SECRET_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/datasync-lab-secret-role"

# 等待 IAM 策略传播
sleep 15

# 4. 创建 SMB 位置 — 使用 CustomSecretConfig（注意：不传 Password）
LOCATION_CUSTOM=$(aws datasync create-location-smb \
    --server-hostname $SAMBA_IP \
    --subdirectory /share \
    --user datasync \
    --agent-arns $AGENT_ARN \
    --custom-secret-config '{"SecretArn": "'${SECRET_ARN}'", "SecretAccessRoleArn": "'${SECRET_ROLE_ARN}'"}' \
    --query 'LocationArn' --output text \
    --region $REGION --profile $PROFILE)
echo "Location (Custom): $LOCATION_CUSTOM"
```

确认没有创建额外的服务托管 secret：

```bash
aws secretsmanager list-secrets \
    --filters Key=name,Values=aws-datasync \
    --query 'SecretList[*].Name' --output table \
    --region $REGION --profile $PROFILE
```

**关键观察**：`aws-datasync` 前缀的 secret 数量未增加，DataSync 完全使用用户提供的 secret。

### Step 5: 端到端数据传输验证

使用模式一创建的 SMB 位置进行实际数据传输：

```bash
# 创建 S3 目标位置
S3_LOCATION=$(aws datasync create-location-s3 \
    --s3-bucket-arn "arn:aws:s3:::${BUCKET_NAME}" \
    --s3-config '{"BucketAccessRoleArn": "'${S3_ROLE_ARN}'"}' \
    --subdirectory /datasync-test/ \
    --query 'LocationArn' --output text \
    --region $REGION --profile $PROFILE)

# 创建传输任务
TASK_ARN=$(aws datasync create-task \
    --source-location-arn $LOCATION_MANAGED \
    --destination-location-arn $S3_LOCATION \
    --name datasync-lab-transfer \
    --query 'TaskArn' --output text \
    --region $REGION --profile $PROFILE)

# 执行传输
EXECUTION_ARN=$(aws datasync start-task-execution \
    --task-arn $TASK_ARN \
    --query 'TaskExecutionArn' --output text \
    --region $REGION --profile $PROFILE)

# 等待完成（约 2-3 分钟）
echo "等待传输完成..."
while true; do
    STATUS=$(aws datasync describe-task-execution \
        --task-execution-arn $EXECUTION_ARN \
        --query 'Status' --output text \
        --region $REGION --profile $PROFILE)
    echo "状态: $STATUS"
    [ "$STATUS" = "SUCCESS" ] || [ "$STATUS" = "ERROR" ] && break
    sleep 10
done

# 验证结果
aws datasync describe-task-execution \
    --task-execution-arn $EXECUTION_ARN \
    --query '{Status:Status, Files:FilesTransferred, Bytes:BytesTransferred}' \
    --region $REGION --profile $PROFILE

aws s3 ls "s3://${BUCKET_NAME}/datasync-test/" --recursive \
    --region $REGION --profile $PROFILE
```

## 测试结果

### 三种模式对比

| 维度 | ManagedSecretConfig | CmkSecretConfig | CustomSecretConfig |
|------|--------------------|-----------------|--------------------|
| **配置复杂度** | ⭐ 最简单 | ⭐⭐ 需配置 KMS policy | ⭐⭐⭐ 需预创建 secret + IAM role |
| **加密控制** | AWS 默认密钥 | 自定义 KMS key | 用户自选 |
| **Secret 生命周期** | DataSync 自动管理 | DataSync 自动管理 | 用户完全控制 |
| **额外 IAM 要求** | 仅 AWSDataSyncFullAccess | + KMS key policy (SLR Decrypt) | + IAM role (GetSecretValue) |
| **Secret 前缀** | `aws-datasync!loc-xxx` | `aws-datasync!loc-xxx` | 用户自定义 |
| **删除行为** | 随 location 自动删除 | 随 location 自动删除 | **不受影响**（用户自管） |
| **计费** | 不额外收费 | 不额外收费 | $0.40/secret/月 |
| **适用场景** | 开发测试 | 企业合规审计 | 集中凭证管理、自定义轮换 |

### 边界条件测试

| 测试 | 操作 | 结果 | 错误信息 |
|------|------|------|----------|
| KMS 无 SLR 权限 | CmkSecretConfig + 无权限 KMS key | ❌ `InvalidRequestException` | "DataSync SLR does not have access to the KMS key" |
| 两种 Config 共存 | CmkSecretConfig + CustomSecretConfig | ❌ `ValidationException` | "CmkSecretConfig cannot be provided with CustomSecretConfig" |
| JSON 格式 Secret | CustomSecretConfig + JSON secret | ⚠️ **创建成功** | 格式验证延迟到 task 执行阶段 |

### 端到端传输数据

| 指标 | 数值 |
|------|------|
| 传输文件数 | 2 |
| 传输字节数 | 40 bytes |
| 准备耗时 | 1,466 ms |
| 传输耗时 | 1,000 ms |
| 校验耗时 | 1,053 ms |
| 总耗时 | 3,728 ms |
| 最终状态 | ✅ SUCCESS |

## 踩坑记录

!!! warning "踩坑 1：KMS Key Policy 必须包含 DataSync SLR"
    使用 CmkSecretConfig 时，如果 KMS key policy 未授权 `AWSServiceRoleForDataSync` 的 `kms:Decrypt` 权限，`create-location-smb` 会直接失败。**已查文档确认**：这是设计行为，DataSync SLR 需要解密权限才能在 task 执行时读取 secret。

!!! warning "踩坑 2：CustomSecretConfig 的 Secret 必须是 plain text"
    文档要求 secret 值为 "plain text"（密码类）或 "binary"（Kerberos keytab），但实测发现使用 JSON 格式（如 `{"password": "xxx"}`）创建 location 时不会报错。**实测发现，官方未记录**：格式验证延迟到 task 执行阶段，这可能导致用户困惑。建议始终使用纯文本格式存储密码。

!!! warning "踩坑 3：删除 Location 前需先删除关联的 Task"
    尝试删除正在被 Task 使用的 Location 会返回 `InvalidRequestException: Location is in use`。需要先删除 Task，再删除 Location。**已查文档确认**：这是预期行为。

!!! warning "踩坑 4：IAM 策略传播需要等待"
    创建 CustomSecretConfig 的 IAM Role 后，如果立即创建 DataSync Location，可能因 IAM 策略尚未传播而失败。建议等待 15-30 秒。**已查文档确认**：IAM 最终一致性特性。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 Samba (t3.micro) | $0.0104/hr | 1 hr | $0.01 |
| EC2 DataSync Agent (m5.xlarge) | $0.192/hr | 1 hr | $0.19 |
| KMS Key | $1.00/月 | < 1 天 | ~$0.03 |
| DataSync 传输 | $0.0125/GB | < 1 KB | $0.00 |
| S3 存储 | $0.023/GB | < 1 KB | $0.00 |
| Secrets Manager (服务托管) | 免费 | — | $0.00 |
| **合计** | | | **~$0.23** |

## 清理资源

```bash
# 1. 删除 DataSync 资源（按依赖顺序）
aws datasync delete-task --task-arn $TASK_ARN \
    --region $REGION --profile $PROFILE
# 删除所有 DataSync location
for LOC in $LOCATION_MANAGED $LOCATION_CMK $LOCATION_CUSTOM $S3_LOCATION; do
    aws datasync delete-location --location-arn $LOC \
        --region $REGION --profile $PROFILE 2>/dev/null
done
aws datasync delete-agent --agent-arn $AGENT_ARN \
    --region $REGION --profile $PROFILE

# 2. 删除 Secrets Manager secret（用户自建的）
aws secretsmanager delete-secret --secret-id datasync-lab-smb-password \
    --force-delete-without-recovery \
    --region $REGION --profile $PROFILE

# 3. 删除 IAM 资源
aws iam delete-role-policy --role-name datasync-lab-s3-role --policy-name s3-access \
    --profile $PROFILE
aws iam delete-role --role-name datasync-lab-s3-role --profile $PROFILE
aws iam delete-role-policy --role-name datasync-lab-secret-role --policy-name secret-access \
    --profile $PROFILE
aws iam delete-role --role-name datasync-lab-secret-role --profile $PROFILE

# 4. 删除 KMS Key（计划 7 天后删除）
aws kms schedule-key-deletion --key-id $KMS_KEY_ID --pending-window-in-days 7 \
    --region $REGION --profile $PROFILE

# 5. 清空并删除 S3 桶
aws s3 rb "s3://${BUCKET_NAME}" --force --region $REGION --profile $PROFILE

# 6. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids $SAMBA_INSTANCE $AGENT_INSTANCE \
    --region $REGION --profile $PROFILE
aws ec2 wait instance-terminated --instance-ids $SAMBA_INSTANCE $AGENT_INSTANCE \
    --region $REGION --profile $PROFILE

# 7. 清理网络资源（VPC 相关：先检查 ENI 残留）
# 检查安全组关联的 ENI
aws ec2 describe-network-interfaces \
    --filters "Name=group-id,Values=${SAMBA_SG}" \
    --query 'NetworkInterfaces[*].[NetworkInterfaceId,Status]' \
    --region $REGION --profile $PROFILE
aws ec2 describe-network-interfaces \
    --filters "Name=group-id,Values=${AGENT_SG}" \
    --query 'NetworkInterfaces[*].[NetworkInterfaceId,Status]' \
    --region $REGION --profile $PROFILE

# 确认无残留 ENI 后删除安全组
aws ec2 delete-security-group --group-id $SAMBA_SG --region $REGION --profile $PROFILE
aws ec2 delete-security-group --group-id $AGENT_SG --region $REGION --profile $PROFILE

# 删除子网、路由表、IGW
aws ec2 delete-subnet --subnet-id $SUBNET_ID --region $REGION --profile $PROFILE
aws ec2 delete-route-table --route-table-id $RTB_ID --region $REGION --profile $PROFILE
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID \
    --region $REGION --profile $PROFILE
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID \
    --region $REGION --profile $PROFILE

# 8. 删除 Key Pair
aws ec2 delete-key-pair --key-name datasync-lab \
    --region $REGION --profile $PROFILE
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。DataSync Agent EC2（m5.xlarge）每小时约 $0.19，请及时关闭。

## 结论与建议

**模式选择指南**：

- **开发测试/快速验证** → 直接传 Password（ManagedSecretConfig），零额外配置
- **企业合规/审计** → CmkSecretConfig + 自定义 KMS key，满足密钥控制要求
- **多账号/集中管理** → CustomSecretConfig，配合 Secrets Manager 轮换策略和跨账号访问

**生产环境建议**：

1. **优先使用 CmkSecretConfig 或 CustomSecretConfig**，避免在 API 调用中直接传递凭证
2. **KMS key policy 提前配置好 DataSync SLR 权限**，否则创建 location 会失败
3. **CustomSecretConfig 的 secret 值必须是 plain text**，不要使用 JSON 格式（虽然创建时不报错，但 task 执行可能失败）
4. **配置 confused deputy 防护**：CustomSecretConfig 的 IAM role trust policy 中添加 `aws:SourceAccount` 和 `aws:SourceArn` 条件

## 参考链接

- [Managing credentials with AWS Secrets Manager](https://docs.aws.amazon.com/datasync/latest/userguide/location-credentials.html)
- [AWS DataSync now supports AWS Secrets Manager for all location types](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-datasync-secrets-manager/)
- [AWS managed policies for DataSync](https://docs.aws.amazon.com/datasync/latest/userguide/security-iam-awsmanpol.html)
- [CmkSecretConfig API Reference](https://docs.aws.amazon.com/datasync/latest/userguide/API_CmkSecretConfig.html)
- [CustomSecretConfig API Reference](https://docs.aws.amazon.com/datasync/latest/userguide/API_CustomSecretConfig.html)
