---
tags:
  - Compute
---

# Amazon ECS Express Mode 实战：一条命令部署生产级容器应用

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

部署一个容器应用到 AWS ECS，传统流程需要依次创建 Cluster、Task Definition、Service、ALB、Target Group、Security Group、Listener、Auto Scaling 策略……十几个资源，几十个参数。对于想快速验证想法的开发者来说，这个门槛太高了。

**Amazon ECS Express Mode** 把这一切压缩成了一条命令。你只需要提供容器镜像 + 两个 IAM Role，它就自动配好 ALB（含 HTTPS）、Auto Scaling、安全组、CloudWatch Logs，给你一个可访问的 HTTPS 端点。

关键是：底层资源全在你账户里，完全可见可控。不是黑盒，只是帮你跳过了重复的基础设施搭建。

## 前置条件

- AWS 账号（需要 IAM、ECS、EC2、ELB、CloudWatch 权限）
- AWS CLI v2（≥ 2.34）已配置
- 一个可用的 VPC + 至少 2 个公有子网（有 Internet Gateway）

## 核心概念

### Express Mode vs 传统 ECS Fargate 部署

| 维度 | Express Mode | 传统 ECS Fargate |
|------|-------------|-----------------|
| 部署步骤 | **1 条命令** | 创建 Cluster → Task Def → Service → ALB → TG → SG → Listener... |
| 必需参数 | 3 个（镜像、执行角色、基础设施角色） | 数十个参数 |
| ALB | 自动创建，多服务共享（最多 25 个） | 手动创建配置 |
| Auto Scaling | 默认开启（CPU 60%） | 需手动配置 |
| HTTPS | 自动配置 SSL/TLS + AWS 域名 | 需自行配置 Route 53 + ACM |
| 灵活性 | 创建后可完全自定义底层资源 | 完全自定义 |
| Launch Type | 仅 Fargate | Fargate 或 EC2 |
| 定价 | 无额外费用，只付底层资源费用 | 同 |

### 自动创建的 12 类资源

一条 `create-express-gateway-service` 命令，Express Mode 自动配置：

1. **ECS Service**（Fargate，含 Service Revision）
2. **Application Load Balancer**（internet-facing）
3. **HTTPS Listener**（Port 443，TLS 1.2）
4. **Listener Rule**（host-header based routing）
5. **Target Group**（IP 类型，含健康检查）
6. **ACM Certificate**（AWS 自动签发）
7. **ALB Security Group**（开放 80/443 入站）
8. **Task Security Group**（仅允许来自 ALB SG 的入站）
9. **CloudWatch Log Group**
10. **Auto Scaling Target**（min=1, max=20）
11. **Auto Scaling Policy**（Target Tracking，CPU 60%）
12. **DNS 域名**（`ex-*.ecs.<region>.on.aws`）

## 动手实践

### Step 1: 创建 IAM Roles

Express Mode 需要两个角色：

```bash
# Task Execution Role（ECS Agent 用来拉镜像、写日志）
aws iam create-role \
  --role-name ecs-express-execution-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs-tasks.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region us-east-1

aws iam attach-role-policy \
  --role-name ecs-express-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Infrastructure Role（ECS 用来创建/管理 ALB、SG、Auto Scaling）
aws iam create-role \
  --role-name ecs-express-infrastructure-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ecs.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region us-east-1

aws iam attach-role-policy \
  --role-name ecs-express-infrastructure-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
```

### Step 2: 准备网络环境

```bash
# 创建 VPC
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.100.0.0/16 \
  --query 'Vpc.VpcId' --output text --region us-east-1)

aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames

# 创建 Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
  --query 'InternetGateway.InternetGatewayId' --output text --region us-east-1)

aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID \
  --region us-east-1

# 创建 2 个公有子网（ALB 至少需要 2 个 AZ）
SUBNET_1=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.100.1.0/24 --availability-zone us-east-1a \
  --query 'Subnet.SubnetId' --output text --region us-east-1)

SUBNET_2=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.100.2.0/24 --availability-zone us-east-1b \
  --query 'Subnet.SubnetId' --output text --region us-east-1)

# 配置路由表
RTB_ID=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'RouteTables[0].RouteTableId' --output text --region us-east-1)

aws ec2 create-route --route-table-id $RTB_ID \
  --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID --region us-east-1

aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_1 \
  --region us-east-1
aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_2 \
  --region us-east-1
```

### Step 3: 创建 ECS Cluster

```bash
aws ecs create-cluster --cluster-name express-demo-cluster --region us-east-1
```

### Step 4: 一条命令部署 Express Mode 服务

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws ecs create-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-nginx \
  --execution-role-arn arn:aws:iam::${ACCOUNT_ID}:role/ecs-express-execution-role \
  --infrastructure-role-arn arn:aws:iam::${ACCOUNT_ID}:role/ecs-express-infrastructure-role \
  --primary-container '{"image": "nginx:latest", "containerPort": 80}' \
  --health-check-path "/" \
  --cpu 256 --memory 512 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[],assignPublicIp=ENABLED}" \
  --region us-east-1
```

API 返回包含自动生成的 HTTPS 端点：

```json
{
  "ingressPaths": [{
    "accessType": "PUBLIC",
    "endpoint": "https://ex-xxxxxxxx.ecs.us-east-1.on.aws"
  }]
}
```

### Step 5: 等待部署完成并验证

```bash
# 查看服务状态
aws ecs describe-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-nginx \
  --region us-east-1

# 约 8-9 分钟后，访问 endpoint
curl -s https://ex-xxxxxxxx.ecs.us-east-1.on.aws
# 返回 nginx 默认页面 → 部署成功！
```

### Step 6: 部署第二个服务（验证 ALB 共享）

```bash
aws ecs create-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-httpd \
  --execution-role-arn arn:aws:iam::${ACCOUNT_ID}:role/ecs-express-execution-role \
  --infrastructure-role-arn arn:aws:iam::${ACCOUNT_ID}:role/ecs-express-infrastructure-role \
  --primary-container '{"image": "httpd:latest", "containerPort": 80}' \
  --health-check-path "/" \
  --cpu 256 --memory 512 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[],assignPublicIp=ENABLED}" \
  --region us-east-1
```

两个服务共享同一个 ALB，通过 host-header routing 区分流量。第二个服务部署仅需 ~3.5 分钟（ALB 已存在）。

### Step 7: 零停机更新镜像版本

```bash
aws ecs update-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-nginx \
  --primary-container '{"image": "nginx:1.27", "containerPort": 80}' \
  --region us-east-1
```

Express Mode 使用 **Canary 部署策略**（5% canary → 3min bake → 全量切换 → 3min bake），全程零停机。

## 测试结果

### 完整测试矩阵

| # | 测试项 | 预期 | 实际结果 | 关键数据 |
|---|--------|------|---------|---------|
| T1 | 基本部署 | 一条命令创建完整应用栈 | ✅ 通过 | HTTPS 端点可访问 |
| T2 | 部署耗时 | 3-5 分钟 | ⚠️ 8.5 分钟 | ALB 3min + 健康检查 4min |
| T3 | 资源审计 | 自动创建多类资源 | ✅ 通过 | 12 类资源一条命令创建 |
| T4 | ALB 共享 | 复用同一 ALB | ✅ 通过 | host-header routing，第 2 个服务 3.5min |
| T5 | 零停机更新 | 无中断更新 | ✅ 通过 | Canary 部署，10min15s，全程 HTTP 200 |
| T6 | 安全组审计 | 分层安全模型 | ✅ 通过 | Task SG 仅限 ALB SG 入站 |
| T7 | 健康检查 | 默认 /ping | ✅ 通过 | 可自定义，4-5min 到 healthy |
| T8 | 无效镜像 | 报错提示 | ⚠️ 不校验 | API 不验证镜像，Circuit Breaker 12min 触发 |
| T9 | 删除清理 | 清理所有资源 | ⚠️ 部分残留 | 不清理 Log Groups 和 Cluster |

### 安全组分层模型

Express Mode 自动创建的安全组遵循最佳实践：

```
Internet → ALB SG (0.0.0.0/0:443) → Task SG (仅 ALB SG:80) → Container
```

- **ALB SG**：入站开放 80/443（面向公网的 LB，合理配置）
- **Task SG**：入站 **仅允许来自 ALB SG** 的 80 端口流量 — 不直接暴露容器
- 无 0.0.0.0/0 到容器的直接入站规则 ✅

### 部署时间线

```
0:00  ─── create-express-gateway-service 调用
0:03  ─── API 返回（服务创建）
1:11  ─── ALB 开始 provisioning
3:17  ─── ALB active
3:59  ─── 第一个 Task 启动
4:28  ─── Task 注册到 Target Group
5:24  ─── Task running
8:33  ─── Endpoint 返回 HTTP 200 ✅
```

## 踩坑记录

!!! warning "ALB 残留影响新部署"
    Express Mode 在 VPC 级别缓存 ALB 引用。如果之前有 Express 服务被删除但 ALB 被手动移除（或状态异常），新建的服务会尝试复用不存在的 ALB，导致 `LoadBalancerNotFoundException`。**建议**：始终使用 `delete-express-gateway-service` 而不是手动删除底层资源。（实测发现，官方未记录）

!!! warning "API 不校验镜像存在性"
    `create-express-gateway-service` 不检查镜像是否存在，直接返回成功。错误要等到 Task 启动后才暴露，Circuit Breaker 需要约 12 分钟才会触发。这与传统 ECS 行为一致，但对 Express Mode"简化体验"的定位来说，前置校验会更友好。（实测发现，已查文档确认为预期行为）

!!! warning "删除不清理 Log Groups 和 Cluster"
    `delete-express-gateway-service` 会清理 ECS Service、ALB、Target Group、Security Group、ACM Certificate，但 **不清理 CloudWatch Log Groups 和 ECS Cluster**。需要手动清理。（实测发现，设计合理——日志需保留审计，集群可能被其他服务使用）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Fargate (0.25 vCPU) | $0.04048/hr | ~3 hr × 2 任务 | ~$0.24 |
| Fargate (0.5 GB) | $0.004445/hr | ~3 hr × 2 任务 | ~$0.03 |
| ALB | $0.0225/hr | ~3 hr | ~$0.07 |
| 数据传输 | - | 微量 | ~$0.00 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
# 1. 删除 Express Mode 服务（自动清理 ALB/TG/SG/ACM）
aws ecs delete-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-nginx \
  --region us-east-1

aws ecs delete-express-gateway-service \
  --cluster express-demo-cluster \
  --service-name my-httpd \
  --region us-east-1

# 2. 等待 2-3 分钟让 Express Mode 完成清理

# 3. 手动清理残留的 Log Groups
aws logs delete-log-group \
  --log-group-name /aws/ecs/express-demo-cluster/my-nginx-xxxx \
  --region us-east-1

aws logs delete-log-group \
  --log-group-name /aws/ecs/express-demo-cluster/my-httpd-xxxx \
  --region us-east-1

# 4. 删除空集群
aws ecs delete-cluster --cluster express-demo-cluster --region us-east-1

# 5. 清理网络资源
aws ec2 delete-subnet --subnet-id $SUBNET_1 --region us-east-1
aws ec2 delete-subnet --subnet-id $SUBNET_2 --region us-east-1
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID \
  --region us-east-1
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID --region us-east-1
aws ec2 delete-vpc --vpc-id $VPC_ID --region us-east-1

# 6. 清理 IAM Roles（可选保留）
aws iam detach-role-policy --role-name ecs-express-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name ecs-express-execution-role

aws iam detach-role-policy --role-name ecs-express-infrastructure-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
aws iam delete-role --role-name ecs-express-infrastructure-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ALB 按小时收费（$0.0225/hr ≈ $16/月）。

## 结论与建议

**Express Mode 适合的场景：**

- 🚀 **快速原型验证** — 8.5 分钟从零到可访问的 HTTPS 端点
- 👥 **开发者自助部署** — 不需要深度 AWS 知识，3 个参数搞定
- 💰 **多服务成本优化** — 最多 25 个服务共享一个 ALB
- 🔄 **CI/CD 友好** — 一条 `update` 命令触发 Canary 部署

**不适合的场景：**

- 需要 EC2 Launch Type（GPU、自定义 AMI）
- 需要精细控制 ALB 规则（路径路由、WAF 集成等）
- 需要超过 25 个服务的大规模部署

**生产环境建议：**

1. 部署后立即检查自动创建的安全组规则
2. 为 CloudWatch Log Group 配置保留策略（默认永久保留）
3. 利用 `describe-express-gateway-service` 获取完整资源清单
4. 使用 `delete-express-gateway-service` 清理，不要手动删除底层资源
5. 部署后的底层资源可以通过 CloudFormation/CDK/Terraform 接管

## 参考链接

- [Amazon ECS Express Mode 官方文档](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html)
- [AWS What's New: Announcing Amazon ECS Express Mode](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode/)
- [create-express-gateway-service CLI Reference](https://docs.aws.amazon.com/cli/latest/reference/ecs/create-express-gateway-service.html)
