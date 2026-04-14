---
tags:
  - Compute
---

# Amazon ECS Managed Instances 实战：全托管 EC2 容器计算的新选择

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $2.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

在 ECS 的计算选项中，你一直面临一个两难：

- **Fargate**：全托管，零运维，但不能选实例类型、不支持 GPU、每个 task 独占隔离环境（成本较高）
- **EC2 + ASG**：完全灵活，支持 GPU 和各种实例，但你得自己管理 AMI、补丁、扩缩容、实例生命周期

2025 年 9 月，AWS 推出了 **ECS Managed Instances** —— 一个介于两者之间的新选项。它让 AWS 帮你管理 EC2 实例的生命周期（创建、扩缩、补丁），同时保留 EC2 的全部能力：GPU、自定义实例类型、多 task 共享实例、特权容器等。

本文通过实际部署，验证 ECS Managed Instances 的核心能力：自动实例选型、多 task 置放、自定义 Capacity Provider，以及与 Fargate 的兼容性。

## 前置条件

- AWS 账号（需要 IAM、ECS、EC2 权限）
- AWS CLI v2 已配置
- 一个 VPC（可使用默认 VPC）和安全组

## 核心概念

### ECS 三种计算选项对比

| 维度 | Fargate | ECS Managed Instances | EC2 + ASG |
|------|---------|----------------------|-----------|
| 运维负担 | 零 | 极低（AWS 全托管） | 高（自己管） |
| 实例选择 | 不可选 | 可指定（attribute-based） | 完全控制 |
| GPU 支持 | ❌ | ✅ | ✅ |
| 特权容器 | 受限 | ✅（CAP_NET_ADMIN 等） | ✅ |
| 多 task / 实例 | 1:1（隔离） | N:1（共享） | N:1（共享） |
| 自动补丁 | N/A | 14 天自动 drain + replace | 自己管 |
| OS | Amazon Linux / Windows | Bottlerocket（仅 Linux） | 自选 AMI |

### 关键架构

```
你的 task definition (CPU, Memory 需求)
         │
         ▼
   Capacity Provider
   ├── 默认 CP：AWS 自动选最优成本实例
   └── 自定义 CP：通过 instanceRequirements 指定约束
         │
         ▼
   AWS 自动管理的 EC2 (Bottlerocket)
   ├── 多 task 共享实例
   ├── 14 天自动 patch
   └── 主动工作负载整合
```

### 两个必需的 IAM 角色

1. **Infrastructure Role**（`ecsInfrastructureRole`）：让 ECS 代你管理 EC2 基础设施
2. **Instance Profile**（`ecsInstanceRole`）：EC2 实例使用的角色，运行 ECS Agent

!!! warning "命名约定"
    使用 AWS 托管策略时，Instance Profile **必须**命名为 `ecsInstanceRole`。

## 动手实践

### Step 1: 创建 IAM 角色

```bash
# 创建 Infrastructure Role
cat > /tmp/ecs-infra-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAccessToECSForInfrastructureManagement",
      "Effect": "Allow",
      "Principal": { "Service": "ecs.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
    --role-name ecsInfrastructureRole \
    --assume-role-policy-document file:///tmp/ecs-infra-trust.json

aws iam attach-role-policy \
    --role-name ecsInfrastructureRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForManagedInstances
```

```bash
# 创建 Instance Role + Instance Profile
cat > /tmp/ecs-instance-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
    --role-name ecsInstanceRole \
    --assume-role-policy-document file:///tmp/ecs-instance-trust.json

aws iam attach-role-policy \
    --role-name ecsInstanceRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonECSInstanceRolePolicyForManagedInstances

aws iam create-instance-profile --instance-profile-name ecsInstanceRole
aws iam add-role-to-instance-profile \
    --instance-profile-name ecsInstanceRole \
    --role-name ecsInstanceRole
```

### Step 2: 创建安全组

```bash
# 替换为你的 VPC ID
VPC_ID="vpc-xxxxxxxx"

SG_ID=$(aws ec2 create-security-group \
    --group-name ecs-mi-test-sg \
    --description "ECS Managed Instances test" \
    --vpc-id $VPC_ID \
    --region us-east-1 \
    --query 'GroupId' --output text)

echo "Security Group: $SG_ID"

# 仅允许你的 IP 访问 HTTP（请替换为你的 IP）
MY_IP=$(curl -s ifconfig.me)
aws ec2 authorize-security-group-ingress \
    --group-id $SG_ID \
    --protocol tcp --port 80 \
    --cidr ${MY_IP}/32 \
    --region us-east-1
```

### Step 3: 创建集群和 Capacity Provider

```bash
# 创建集群
aws ecs create-cluster \
    --cluster-name mi-test-cluster \
    --region us-east-1

# 创建默认 Capacity Provider（自动选型）
# 替换 subnet-xxx 和 sg-xxx 为你的值
cat > /tmp/mi-default-cp.json << 'EOF'
{
    "name": "mi-default-cp",
    "cluster": "mi-test-cluster",
    "managedInstancesProvider": {
        "infrastructureRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/ecsInfrastructureRole",
        "instanceLaunchTemplate": {
            "ec2InstanceProfileArn": "arn:aws:iam::YOUR_ACCOUNT_ID:instance-profile/ecsInstanceRole",
            "networkConfiguration": {
                "subnets": ["subnet-xxx", "subnet-yyy"],
                "securityGroups": ["sg-xxx"]
            },
            "storageConfiguration": {
                "storageSizeGiB": 30
            },
            "monitoring": "basic"
        }
    }
}
EOF

aws ecs create-capacity-provider \
    --cli-input-json file:///tmp/mi-default-cp.json \
    --region us-east-1
```

等待 Capacity Provider 变为 ACTIVE（通常 15-20 秒）：

```bash
aws ecs describe-capacity-providers \
    --capacity-providers mi-default-cp \
    --region us-east-1 \
    --query 'capacityProviders[0].{status:status,updateStatus:updateStatus}'
```

```bash
# 设为集群默认策略
cat > /tmp/cluster-cp-strategy.json << 'EOF'
{
    "cluster": "mi-test-cluster",
    "capacityProviders": ["mi-default-cp"],
    "defaultCapacityProviderStrategy": [
        {"capacityProvider": "mi-default-cp", "weight": 1}
    ]
}
EOF

aws ecs put-cluster-capacity-providers \
    --cli-input-json file:///tmp/cluster-cp-strategy.json \
    --region us-east-1
```

### Step 4: 注册 Task Definition 并部署 Service

```bash
cat > /tmp/mi-task-def.json << 'EOF'
{
    "family": "mi-httpd-test",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["MANAGED_INSTANCES"],
    "cpu": "256",
    "memory": "512",
    "containerDefinitions": [
        {
            "name": "httpd",
            "image": "public.ecr.aws/docker/library/httpd:latest",
            "portMappings": [
                {"containerPort": 80, "hostPort": 80, "protocol": "tcp"}
            ],
            "essential": true,
            "entryPoint": ["sh", "-c"],
            "command": [
                "echo '<html><body><h1>Hello from ECS Managed Instances!</h1></body></html>' > /usr/local/apache2/htdocs/index.html && httpd-foreground"
            ]
        }
    ]
}
EOF

aws ecs register-task-definition \
    --cli-input-json file:///tmp/mi-task-def.json \
    --region us-east-1
```

```bash
# 创建 Service（2 个任务）
cat > /tmp/mi-service.json << 'EOF'
{
    "cluster": "mi-test-cluster",
    "serviceName": "mi-httpd-svc",
    "taskDefinition": "mi-httpd-test",
    "desiredCount": 2,
    "networkConfiguration": {
        "awsvpcConfiguration": {
            "subnets": ["subnet-xxx", "subnet-yyy"],
            "securityGroups": ["sg-xxx"]
        }
    }
}
EOF

aws ecs create-service \
    --cli-input-json file:///tmp/mi-service.json \
    --region us-east-1
```

!!! warning "assignPublicIp 不支持"
    与 Fargate 不同，ECS Managed Instances **不支持** `assignPublicIp: ENABLED`。
    如果你的容器需要外网访问，请使用公有子网（实例自动获得公有 IP）或配置 NAT Gateway。

### Step 5: 验证部署结果

```bash
# 检查 service 状态
aws ecs describe-services \
    --cluster mi-test-cluster \
    --services mi-httpd-svc \
    --region us-east-1 \
    --query 'services[0].{desired:desiredCount,running:runningTasksCount,events:events[:3]}'

# 查看容器实例详情（AWS 自动选的 EC2）
CI_ARNS=$(aws ecs list-container-instances \
    --cluster mi-test-cluster \
    --region us-east-1 \
    --query 'containerInstanceArns' --output text)

aws ecs describe-container-instances \
    --cluster mi-test-cluster \
    --container-instances $CI_ARNS \
    --region us-east-1 \
    --query 'containerInstances[*].{instanceId:ec2InstanceId,type:attributes[?name==`ecs.instance-type`].value|[0],tasks:runningTasksCount}'
```

### Step 6: 验证多 task 置放

```bash
# 扩到 3 个 task，观察是否复用现有实例
aws ecs update-service \
    --cluster mi-test-cluster \
    --service mi-httpd-svc \
    --desired-count 3 \
    --region us-east-1

# 等待约 30 秒后检查
aws ecs describe-container-instances \
    --cluster mi-test-cluster \
    --container-instances $CI_ARNS \
    --region us-east-1 \
    --query 'containerInstances[*].{instanceId:ec2InstanceId,type:attributes[?name==`ecs.instance-type`].value|[0],tasks:runningTasksCount}'
```

### Step 7: 创建自定义 Capacity Provider（指定实例规格）

```bash
cat > /tmp/mi-custom-cp.json << 'EOF'
{
    "name": "mi-custom-cp",
    "cluster": "mi-test-cluster",
    "managedInstancesProvider": {
        "infrastructureRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/ecsInfrastructureRole",
        "instanceLaunchTemplate": {
            "ec2InstanceProfileArn": "arn:aws:iam::YOUR_ACCOUNT_ID:instance-profile/ecsInstanceRole",
            "instanceRequirements": {
                "vCpuCount": {"min": 2, "max": 4},
                "memoryMiB": {"min": 2048, "max": 8192}
            },
            "networkConfiguration": {
                "subnets": ["subnet-xxx"],
                "securityGroups": ["sg-xxx"]
            },
            "storageConfiguration": {"storageSizeGiB": 30},
            "monitoring": "basic"
        }
    }
}
EOF

aws ecs create-capacity-provider \
    --cli-input-json file:///tmp/mi-custom-cp.json \
    --region us-east-1
```

## 测试结果

### 默认 vs 自定义 Capacity Provider 实例选型对比

| Capacity Provider | instanceRequirements | 自动选型结果 | vCPU | 内存 | 实例家族 |
|-------------------|---------------------|-------------|------|------|---------|
| mi-default-cp | 无（全自动） | **c7a.medium** | 1 | ~1.6 GB | AMD 计算优化 |
| mi-custom-cp | vCPU:2-4, Mem:2-8GB | **c6a.large** | 2 | ~3.5 GB | AMD 计算优化 |

**关键发现**：

- 默认 CP 选择了满足 task 需求的**最小成本实例** —— c7a.medium (1 vCPU) 刚好能跑 256 CPU / 512 MiB 的 task
- 自定义 CP 在指定范围内也选择了成本优化的 AMD 实例
- 两者都倾向 **AMD (c6a/c7a)** 系列，而非 Intel，反映了 AWS 的成本优化策略

### 多 task 置放验证

| 场景 | 实例数 | task 数 | task/实例 分布 |
|------|--------|---------|---------------|
| 初始部署 (desired=2) | 2 | 2 | 1:1 |
| 扩容后 (desired=3) | 2（未新增） | 3 | 2:1 |

第 3 个 task 被放到了已有实例上，验证了多 task 置放能力。

### Task Definition 兼容性

| requiresCompatibilities | 实际 compatibilities |
|------------------------|---------------------|
| ["MANAGED_INSTANCES"] | ["EC2", "MANAGED_INSTANCES", "FARGATE"] |
| ["FARGATE", "MANAGED_INSTANCES"] | ["EC2", "FARGATE", "MANAGED_INSTANCES"] |

同一个 task definition 可以同时在 Fargate 和 Managed Instances 上运行，方便渐进式迁移。

## 踩坑记录

!!! warning "踩坑 1: assignPublicIp 不支持（已查文档确认）"
    Fargate 服务常用的 `assignPublicIp: ENABLED` 在 Managed Instances 上**直接报错**。
    这是因为 Managed Instances 的 EC2 实例（而非 task ENI）才是网络出口。
    如果在公有子网部署，实例会自动获得公有 IP；私有子网需要 NAT Gateway。

!!! warning "踩坑 2: 自定义 CP 的 instanceRequirements 参数结构"
    不能直接指定 `instanceTypes: ["t3.small"]`，而是使用 **attribute-based instance type selection**：
    
    - 必需：`vCpuCount` (min/max) + `memoryMiB` (min/max)
    - 可选：`allowedInstanceTypes`, `cpuManufacturers`, `acceleratorTypes` 等
    - `allowedInstanceTypes` 与 vCpuCount/memoryMiB 有交叉验证，参数不匹配直接报错

!!! warning "踩坑 3: Instance Profile 命名限制（已查文档确认）"
    使用 AWS 托管的 Infrastructure policy 时，Instance Profile **必须**以 `ecsInstanceRole` 命名。
    这个限制在 `iam:PassRole` 条件中硬编码，使用其他名称会导致权限错误。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| c7a.medium × 2（默认 CP） | ~$0.036/hr | ~0.5 hr | ~$0.04 |
| c6a.large × 1（自定义 CP） | ~$0.077/hr | ~0.3 hr | ~$0.02 |
| ECS 管理费 | 按 EC2 费用百分比 | — | ~$0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除 Services（设 desired=0 并删除）
aws ecs update-service --cluster mi-test-cluster --service mi-httpd-svc --desired-count 0 --region us-east-1
aws ecs update-service --cluster mi-test-cluster --service mi-custom-svc --desired-count 0 --region us-east-1
sleep 30
aws ecs delete-service --cluster mi-test-cluster --service mi-httpd-svc --region us-east-1
aws ecs delete-service --cluster mi-test-cluster --service mi-custom-svc --region us-east-1

# 2. 移除集群 Capacity Provider 关联
aws ecs put-cluster-capacity-providers \
    --cluster mi-test-cluster \
    --capacity-providers [] \
    --default-capacity-provider-strategy [] \
    --region us-east-1

# 3. 删除 Capacity Providers
aws ecs delete-capacity-provider --capacity-provider mi-default-cp --region us-east-1
aws ecs delete-capacity-provider --capacity-provider mi-custom-cp --region us-east-1

# 4. 等待 EC2 实例自动终止（Managed Instances 会自动清理）
# 确认实例已终止
aws ec2 describe-instances \
    --filters "Name=tag:aws:ecs:clusterName,Values=mi-test-cluster" \
    --region us-east-1 \
    --query 'Reservations[*].Instances[*].{id:InstanceId,state:State.Name}'

# 5. 删除集群
aws ecs delete-cluster --cluster mi-test-cluster --region us-east-1

# 6. 反注册 Task Definitions
aws ecs deregister-task-definition --task-definition mi-httpd-test:1 --region us-east-1
aws ecs deregister-task-definition --task-definition mi-dual-compat:1 --region us-east-1

# 7. 清理安全组（先检查 ENI 残留）
aws ec2 describe-network-interfaces \
    --filters "Name=group-id,Values=sg-xxx" \
    --region us-east-1 \
    --query 'NetworkInterfaces[*].{id:NetworkInterfaceId,status:Status}'
# 确认无残留后删除
aws ec2 delete-security-group --group-id sg-xxx --region us-east-1

# 8. 清理 IAM
aws iam remove-role-from-instance-profile --instance-profile-name ecsInstanceRole --role-name ecsInstanceRole
aws iam delete-instance-profile --instance-profile-name ecsInstanceRole
aws iam detach-role-policy --role-name ecsInstanceRole --policy-arn arn:aws:iam::aws:policy/AmazonECSInstanceRolePolicyForManagedInstances
aws iam delete-role --role-name ecsInstanceRole
aws iam detach-role-policy --role-name ecsInfrastructureRole --policy-arn arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForManagedInstances
aws iam delete-role --role-name ecsInfrastructureRole
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。Managed Instances 的 EC2 实例在删除 Capacity Provider 后会自动终止，
    但 IAM 角色和安全组需要手动清理。

## 结论与建议

### ECS Managed Instances 适合谁？

| 场景 | 推荐 |
|------|------|
| 需要 GPU / 特定实例类型 | ✅ Managed Instances |
| 需要特权容器 (CAP_NET_ADMIN 等) | ✅ Managed Instances |
| 小 task 多、想降低成本 | ✅ Managed Instances（多 task 共享实例） |
| 从 Fargate 迁移、想降成本 | ✅ Managed Instances（task def 兼容） |
| 零运维 + 简单 web app | 🤔 Fargate 仍更简单 |
| 需要 Windows 容器 | ❌ 不支持（仅 Bottlerocket Linux） |
| 需要完全控制 AMI | ❌ 使用 EC2 + ASG |

### 生产环境建议

1. **从 Fargate 迁移**：先用 dual-compat task def 测试，确认行为一致后切换 CP
2. **网络规划**：不支持 assignPublicIp，使用私有子网 + NAT Gateway 是最佳实践
3. **实例选型**：先用默认 CP 让 AWS 自动选，有特殊需求再用自定义 CP 的 instanceRequirements
4. **安全**：Bottlerocket + 14 天自动 patch + 无 SSH = 安全基线高，适合合规场景

## 参考链接

- [AWS What's New: Amazon ECS Managed Instances](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-ecs-managed-instances/)
- [官方文档: Architect for Amazon ECS Managed Instances](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ManagedInstances.html)
- [Getting Started with CLI](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/getting-started-managed-instances-cli.html)
- [ECS Managed Instances 产品页](https://aws.amazon.com/ecs/managed-instances/)
- [Infrastructure IAM Role](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/infrastructure_IAM_role.html)
- [Instance Profile](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-instance-profile.html)
