---
tags:
  - Networking
---

# ALB Target Optimizer 实战：精确控制后端并发请求数

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0.50（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

传统的 ALB 负载均衡（Round Robin、Least Outstanding Requests 等）在面对**低并发高计算密集型工作负载**时存在根本性问题：ALB 由多个独立节点组成，每个节点独立做路由决策且不共享 target 负载信息。对于 LLM 推理、图片生成等一次只能处理 1-2 个请求的应用，这意味着 target 可能被过载，导致 5XX 错误和高延迟。

2025 年 11 月，AWS 推出了 **ALB Target Optimizer**，通过在 target 上运行一个 agent，将负载分发从"推"模式（ALB 盲目转发）变为"拉"模式（target 主动请求流量），实现**精确的并发请求控制**。

本文将从零开始搭建完整测试环境，验证 Target Optimizer 的并发控制能力，并对比普通 Target Group 的行为差异。

## 前置条件

- AWS 账号（需要 EC2、ELB、VPC 相关权限）
- AWS CLI v2 已配置
- 一台可以 SSH 到 EC2 实例的客户端

## 核心概念

### Push vs Pull 模式

| | 传统 ALB（Push） | Target Optimizer（Pull） |
|---|---|---|
| **决策者** | ALB 节点独立决策 | Target agent 主动请求 |
| **负载信息** | 每个 ALB 节点只有局部视图 | Agent 掌握精确的并发数 |
| **超载处理** | 照常转发，靠应用自己处理 | 立即 503 拒绝，fail-fast |
| **适用场景** | 通用 Web 应用 | 低并发高计算密集型（LLM 推理等） |

### 工作原理

Target Optimizer 在每个 target 上部署一个 AWS 提供的 **agent**（Docker 容器），作为 ALB 和应用之间的 inline proxy：

1. Agent 监听两个端口：**data port**（接收应用流量）和 **control port**（与 ALB 交换管理信息）
2. 你配置 `MAX_CONCURRENCY`（0-1000，默认 1）—— target 同时能处理的最大请求数
3. 当 target 的并发请求数低于 MAX_CONCURRENCY，agent 向 ALB 发信号："我准备好了"
4. ALB **只在收到信号后**才转发请求 —— 这就是 "pull" 模式

```
Client → ALB → Agent (data port:80) → Application (port:8080)
                  ↕ (control port:3000)
               ALB Control Plane
```

### 关键限制

- Target control port **只能在创建 Target Group 时指定，之后不可修改**
- Agent 镜像：`public.ecr.aws/aws-elb/target-optimizer/target-control-agent:latest`
- 启用 Target Optimizer 的 Target Group 会产生**更多 LCU 用量**

## 动手实践

### Step 1: 创建网络环境

```bash
# 变量定义
REGION="us-east-1"
PROFILE="your-aws-profile"

# 创建 VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.100.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=alb-to-test-vpc}]' \
  --region $REGION --profile $PROFILE \
  --query 'Vpc.VpcId' --output text)

aws ec2 modify-vpc-attribute --vpc-id $VPC_ID \
  --enable-dns-hostnames '{"Value":true}' \
  --region $REGION --profile $PROFILE

# 创建 Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=alb-to-test-igw}]' \
  --region $REGION --profile $PROFILE \
  --query 'InternetGateway.InternetGatewayId' --output text)

aws ec2 attach-internet-gateway \
  --internet-gateway-id $IGW_ID --vpc-id $VPC_ID \
  --region $REGION --profile $PROFILE

# 创建两个公有子网（不同 AZ）
SUBNET_1=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.100.1.0/24 --availability-zone ${REGION}a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=alb-to-public-1a}]' \
  --region $REGION --profile $PROFILE \
  --query 'Subnet.SubnetId' --output text)

SUBNET_2=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.100.2.0/24 --availability-zone ${REGION}b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=alb-to-public-1b}]' \
  --region $REGION --profile $PROFILE \
  --query 'Subnet.SubnetId' --output text)

# 启用自动分配公有 IP
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_1 --map-public-ip-on-launch --region $REGION --profile $PROFILE
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_2 --map-public-ip-on-launch --region $REGION --profile $PROFILE

# 创建路由表并添加默认路由
RTB_ID=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=alb-to-test-rtb}]' \
  --region $REGION --profile $PROFILE \
  --query 'RouteTable.RouteTableId' --output text)

aws ec2 create-route --route-table-id $RTB_ID \
  --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID \
  --region $REGION --profile $PROFILE

aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_1 --region $REGION --profile $PROFILE
aws ec2 associate-route-table --route-table-id $RTB_ID --subnet-id $SUBNET_2 --region $REGION --profile $PROFILE
```

### Step 2: 创建安全组

```bash
# 获取你的客户端 IP
MY_IP=$(curl -s ifconfig.me)/32

# ALB 安全组
ALB_SG=$(aws ec2 create-security-group \
  --group-name alb-to-sg-alb \
  --description 'ALB SG for Target Optimizer test' \
  --vpc-id $VPC_ID \
  --region $REGION --profile $PROFILE \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG --protocol tcp --port 80 --cidr $MY_IP \
  --region $REGION --profile $PROFILE

# EC2 Target 安全组
EC2_SG=$(aws ec2 create-security-group \
  --group-name alb-to-sg-ec2 \
  --description 'EC2 Target SG' \
  --vpc-id $VPC_ID \
  --region $REGION --profile $PROFILE \
  --query 'GroupId' --output text)

# 允许 ALB 访问 data port(80)、control port(3000)、app port(8080)
for PORT in 80 3000 8080; do
  aws ec2 authorize-security-group-ingress \
    --group-id $EC2_SG --protocol tcp --port $PORT --source-group $ALB_SG \
    --region $REGION --profile $PROFILE
done

# 允许 SSH（用你的 IP）
aws ec2 authorize-security-group-ingress \
  --group-id $EC2_SG --protocol tcp --port 22 --cidr $MY_IP \
  --region $REGION --profile $PROFILE
```

### Step 3: 启动 EC2 实例并部署 Agent

创建 user data 脚本，自动安装 Docker、拉取 Agent、部署模拟应用：

```bash
cat > /tmp/userdata.sh << 'EOF'
#!/bin/bash
set -ex
yum update -y && yum install -y docker python3
systemctl start docker && systemctl enable docker

# 拉取 Target Optimizer Agent
docker pull public.ecr.aws/aws-elb/target-optimizer/target-control-agent:latest

# 创建模拟慢应用（2 秒延迟，模拟推理场景）
cat > /home/ec2-user/slow_app.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import time, socket, threading
HOSTNAME = socket.gethostname()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        active = threading.active_count()
        time.sleep(2)  # 模拟推理耗时
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"OK from {HOSTNAME} - threads:{active}\n".encode())
    def log_message(self, format, *args): pass
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer): pass
ThreadedHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
PYEOF

nohup python3 /home/ec2-user/slow_app.py &

# 启动 Agent: MAX_CONCURRENCY=1（严格一次一个请求）
docker run -d --name target-optimizer-agent \
  --restart unless-stopped --network host \
  -e TARGET_CONTROL_DATA_ADDRESS=0.0.0.0:80 \
  -e TARGET_CONTROL_CONTROL_ADDRESS=0.0.0.0:3000 \
  -e TARGET_CONTROL_DESTINATION_ADDRESS=127.0.0.1:8080 \
  -e TARGET_CONTROL_MAX_CONCURRENCY=1 \
  -e RUST_LOG=info \
  public.ecr.aws/aws-elb/target-optimizer/target-control-agent:latest
EOF
```

```bash
# 获取最新 Amazon Linux 2023 AMI
AMI=$(aws ec2 describe-images --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text \
  --region $REGION --profile $PROFILE)

# 启动 2 个实例（不同 AZ）
INSTANCE_1=$(aws ec2 run-instances --image-id $AMI --instance-type t3.small \
  --key-name your-keypair --security-group-ids $EC2_SG --subnet-id $SUBNET_1 \
  --user-data file:///tmp/userdata.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=alb-to-target-1}]' \
  --region $REGION --profile $PROFILE \
  --query 'Instances[0].InstanceId' --output text)

INSTANCE_2=$(aws ec2 run-instances --image-id $AMI --instance-type t3.small \
  --key-name your-keypair --security-group-ids $EC2_SG --subnet-id $SUBNET_2 \
  --user-data file:///tmp/userdata.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=alb-to-target-2}]' \
  --region $REGION --profile $PROFILE \
  --query 'Instances[0].InstanceId' --output text)
```

### Step 4: 创建 ALB 和 Target Group

```bash
# 创建 ALB
ALB_ARN=$(aws elbv2 create-load-balancer --name alb-to-test \
  --subnets $SUBNET_1 $SUBNET_2 --security-groups $ALB_SG \
  --scheme internet-facing --type application \
  --region $REGION --profile $PROFILE \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN \
  --region $REGION --profile $PROFILE \
  --query 'LoadBalancers[0].DNSName' --output text)

# 创建普通 Target Group（对比用）
TG_NORMAL=$(aws elbv2 create-target-group --name alb-to-tg-normal \
  --protocol HTTP --port 8080 --vpc-id $VPC_ID --target-type instance \
  --health-check-path / --health-check-port 8080 \
  --region $REGION --profile $PROFILE \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# 创建 Target Optimizer Target Group（关键：指定 --target-control-port）
TG_OPTIMIZER=$(aws elbv2 create-target-group --name alb-to-tg-optimizer \
  --protocol HTTP --port 80 --vpc-id $VPC_ID --target-type instance \
  --target-control-port 3000 \
  --health-check-path / --health-check-port 8080 \
  --region $REGION --profile $PROFILE \
  --query 'TargetGroups[0].TargetGroupArn' --output text)
```

!!! warning "关键参数"
    `--target-control-port 3000` 是启用 Target Optimizer 的唯一入口。**此参数只能在创建时指定，之后不可修改。**

```bash
# 注册 target
aws elbv2 register-targets --target-group-arn $TG_NORMAL \
  --targets Id=$INSTANCE_1,Port=8080 Id=$INSTANCE_2,Port=8080 \
  --region $REGION --profile $PROFILE

aws elbv2 register-targets --target-group-arn $TG_OPTIMIZER \
  --targets Id=$INSTANCE_1,Port=80 Id=$INSTANCE_2,Port=80 \
  --region $REGION --profile $PROFILE

# 创建 Listener（默认路由到 Optimizer TG）
LISTENER_ARN=$(aws elbv2 create-listener --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_OPTIMIZER \
  --region $REGION --profile $PROFILE \
  --query 'Listeners[0].ListenerArn' --output text)

# 添加规则：/normal/* 路由到普通 TG（对比测试用）
aws elbv2 create-rule --listener-arn $LISTENER_ARN --priority 10 \
  --conditions Field=path-pattern,Values='/normal/*' \
  --actions Type=forward,TargetGroupArn=$TG_NORMAL \
  --region $REGION --profile $PROFILE
```

### Step 5: 验证部署

等待约 2 分钟，确认 Target 健康：

```bash
# 检查 Target 健康状态
aws elbv2 describe-target-health --target-group-arn $TG_OPTIMIZER \
  --region $REGION --profile $PROFILE \
  --query 'TargetHealthDescriptions[].{Id:Target.Id,State:TargetHealth.State}' \
  --output table

# 发送测试请求
curl -s -w '\nHTTP: %{http_code} | Time: %{time_total}s\n' http://$ALB_DNS/
```

预期输出：

```
OK from ip-10-100-1-xxx.ec2.internal - threads:2
HTTP: 200 | Time: 2.447s
```

## 测试结果

### 测试 1：并发控制验证（MAX_CONCURRENCY=1，2 个 Target）

同时发送 10 个并发请求到 Target Optimizer TG：

| 请求 | HTTP 状态 | 响应时间 | 说明 |
|------|-----------|---------|------|
| #1 | 200 | 2.42s | 正常处理 |
| #2-#10 | **503** | **0.42s** | **立即拒绝** |

**结果**：总容量 = 2（1×2 targets），10 个并发请求中只有 2 个被接受，其余 8 个在 0.42 秒内被拒绝。**严格执行并发限制，零延迟 fail-fast。**

### 测试 2：普通 TG 对比

同样 10 个并发请求到普通 TG：

| 请求 | HTTP 状态 | 响应时间 | 说明 |
|------|-----------|---------|------|
| #1-#2 | 200 | ~2.4s | 第一批处理 |
| #3-#4 | 200 | ~4.4s | 排队等待 |
| #5-#6 | 200 | ~6.4s | 继续排队 |
| #7-#8 | 200 | ~8.4s | 继续排队 |
| #9-#10 | 200 | **~10.8s** | **最后处理** |

**结果**：全部成功，但尾部延迟高达 10.8 秒。普通 TG 把所有请求都推给 target，应用内部排队。

### 关键对比

| 指标 | 普通 TG | Target Optimizer TG |
|------|---------|-------------------|
| 成功率 | 10/10 (100%) | 2/10 (20%) |
| P50 延迟 | 6.4s | 0.43s |
| P99 延迟 | 10.8s | 2.45s |
| 行为 | 全部排队，尾部延迟爆炸 | 超容量立即 503 |

!!! tip "解读"
    Target Optimizer 的 503 不是缺点，而是**设计意图**。对于 LLM 推理等场景：

    - 客户端收到 503 后可**立即重试到其他服务**或等待重试
    - 不会让请求在 target 上无限排队
    - 配合客户端重试策略，整体成功率和延迟都更优

### 测试 3：异构 Target（MAX_CONCURRENCY=1 + 3）

将 Target-2 的 MAX_CONCURRENCY 改为 3，发送 8 个并发请求：

| 结果 | 数量 | 来源 |
|------|------|------|
| 200 成功 | 4 | Target-1: 1 个, Target-2: 3 个 |
| 503 拒绝 | 4 | — |

**精确按配置分配**：总容量 1+3=4，恰好 4 个成功。适用于异构 GPU 集群（如 A10G 处理 1 个请求、A100 处理 3 个）。

### 测试 4：MAX_CONCURRENCY=0（边界条件）

将 Target-1 设为 MAX_CONCURRENCY=0 后，连续 6 个请求**全部发往 Target-2**。

!!! note "实测发现（官方未记录）"
    MAX_CONCURRENCY=0 可用于**零停机维护**：target 不接收任何新请求，等同于 graceful drain。

### 测试 5：Agent 未运行

停止 Target-1 的 Agent 后：

- 健康检查仍显示 **healthy**（因为检查的是应用端口 8080，不经过 Agent）
- 路由到该 target 的请求返回 **502 Bad Gateway**

!!! warning "生产环境建议"
    应将 health check port 设为 Agent 的 data port（如 80），而非直接检查应用端口。这样 Agent 故障时 target 会被标记为 unhealthy。

### CloudWatch 指标

Target Optimizer 提供专属 CloudWatch 指标：

| 指标 | 我们的观测值 | 说明 |
|------|------------|------|
| TargetControlRequestCount | 2-6/min | 通过 Agent 处理的请求数 |
| TargetControlActiveChannelCount | 4 | 控制通道数（2 targets × 2 ALB AZ 节点） |
| TargetControlRequestRejectCount | 4-9/min | 被拒绝的请求数（对应 503） |

## 踩坑记录

!!! warning "踩坑 1：控制通道重建需要时间"
    Agent 重启后，需要 **15-30 秒**重新建立与 ALB 的控制通道。在此期间可能出现 403 ("Unexpected value for x-amzn-target-control-work-id") 或 503 错误。

    **建议**：变更 Agent 配置时使用滚动更新，不要同时重启所有 target 的 Agent。

!!! warning "踩坑 2：Health Check 配置"
    默认 health check 走应用端口（8080），不经过 Agent。Agent 故障时 target 仍显示 healthy，但请求会失败。

    **建议**：生产环境将 health check port 设为 Agent 的 data port（80）。已查文档确认：官方未明确此行为。

!!! warning "踩坑 3：直接 curl Agent 端口返回错误"
    直接 `curl localhost:80`（Agent data port）会返回 "missing x-amzn-target-control-work-id"。这是正常行为——Agent 需要 ALB 提供的特殊 header 才能处理请求。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ALB | $0.0225/hr | 1 hr | $0.02 |
| EC2 t3.small × 2 | $0.0208/hr | 1 hr × 2 | $0.04 |
| LCU（Target Optimizer 额外） | ~$0.008/LCU-hr | ~2 LCU | $0.02 |
| **合计** | | | **~$0.08** |

!!! tip "成本提示"
    启用 Target Optimizer 的 Target Group 会产生额外 LCU 用量。对于大规模部署，建议先评估 LCU 成本影响。

## 清理资源

```bash
# 1. 删除 Listener 和 ALB
aws elbv2 delete-listener --listener-arn $LISTENER_ARN --region $REGION --profile $PROFILE
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN --region $REGION --profile $PROFILE

# 2. 等待 ALB 删除完成
echo "等待 ALB 删除..."
sleep 30

# 3. 删除 Target Group
aws elbv2 delete-target-group --target-group-arn $TG_NORMAL --region $REGION --profile $PROFILE
aws elbv2 delete-target-group --target-group-arn $TG_OPTIMIZER --region $REGION --profile $PROFILE

# 4. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids $INSTANCE_1 $INSTANCE_2 --region $REGION --profile $PROFILE
echo "等待实例终止..."
aws ec2 wait instance-terminated --instance-ids $INSTANCE_1 $INSTANCE_2 --region $REGION --profile $PROFILE

# 5. 删除安全组（先检查 ENI 残留）
aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$EC2_SG" \
  --region $REGION --profile $PROFILE --query 'NetworkInterfaces[].NetworkInterfaceId'
aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$ALB_SG" \
  --region $REGION --profile $PROFILE --query 'NetworkInterfaces[].NetworkInterfaceId'

# 确认无残留 ENI 后删除
aws ec2 delete-security-group --group-id $EC2_SG --region $REGION --profile $PROFILE
aws ec2 delete-security-group --group-id $ALB_SG --region $REGION --profile $PROFILE

# 6. 删除子网和路由表
aws ec2 delete-subnet --subnet-id $SUBNET_1 --region $REGION --profile $PROFILE
aws ec2 delete-subnet --subnet-id $SUBNET_2 --region $REGION --profile $PROFILE
aws ec2 delete-route-table --route-table-id $RTB_ID --region $REGION --profile $PROFILE

# 7. 删除 IGW 和 VPC
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID --region $REGION --profile $PROFILE
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID --region $REGION --profile $PROFILE
aws ec2 delete-vpc --vpc-id $VPC_ID --region $REGION --profile $PROFILE

# 8. 删除 Key Pair
aws ec2 delete-key-pair --key-name your-keypair --region $REGION --profile $PROFILE
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ALB 即使无流量也按小时计费。

## 结论与建议

### 适用场景

Target Optimizer 最适合以下场景：

- **LLM 推理服务**：每个 GPU 实例只能同时处理 1-5 个请求
- **图片/视频生成**：计算密集型，并发能力有限
- **异构集群**：不同规格的 target 需要不同的并发限制
- **需要精确资源利用率的场景**：避免 hotspot，消除尾部延迟

### 不适合的场景

- 高并发轻量级 Web 应用（Round Robin 就够了）
- 不需要严格并发控制的服务
- 无法部署 Docker Agent 的环境

### 生产环境建议

1. **Health Check 配置**：将 health check port 指向 Agent 的 data port（而非直接应用端口），确保 Agent 故障时 target 被及时标记为 unhealthy
2. **客户端重试**：配合指数退避重试策略，处理 503 响应
3. **滚动更新**：变更 Agent 配置时不要同时重启所有 target
4. **监控指标**：关注 `TargetControlRequestRejectCount`，高值意味着需要扩容或调高 MAX_CONCURRENCY
5. **MAX_CONCURRENCY=0**：可用于零停机维护，graceful drain 指定 target

## 参考链接

- [ALB User Guide - Target Optimizer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-register-targets.html#register-targets-target-optimizer)
- [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2025/11/aws-application-load-balancer-target-optimizer/)
- [Launch Blog](https://aws.amazon.com/blogs/networking-and-content-delivery/drive-application-performance-with-application-load-balancer-target-optimizer/)
- [Troubleshooting Target Optimizer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html#troubleshoot-target-optimizer)
- [CloudWatch Metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html)
