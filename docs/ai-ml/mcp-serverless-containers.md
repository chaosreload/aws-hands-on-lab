# 实测 AWS MCP Server：用 AI 助手一键部署 ECS 和 Lambda 应用

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $3-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-24

## 背景

2025 年 5 月，AWS 开源了 4 个 Model Context Protocol (MCP) Server，让 AI 代码助手能直接操作 AWS 的 Serverless 和 Container 服务：

- **AWS Serverless MCP Server** — SAM 应用全生命周期管理
- **Amazon ECS MCP Server** — 容器化 + ECS Express Mode 部署
- **Amazon EKS MCP Server** — K8s 集群管理
- **Finch MCP Server** — 本地容器管理

这意味着你可以用自然语言告诉 AI 助手"帮我把这个 Flask 应用部署到 ECS"，它会自动生成 Dockerfile、推送镜像到 ECR、创建 ECS 服务——全程不需要你写一行 CloudFormation。

本文实测 ECS MCP Server 和 Serverless MCP Server，走完从安装到部署到清理的完整流程。

## 前置条件

- AWS 账号（需要 ECS、ECR、Lambda、CloudFormation、IAM 权限）
- AWS CLI v2 已配置 profile
- Docker 已安装
- Python 3.10+ 和 [uv](https://astral.sh/uv)
- [mcporter](https://github.com/nicosus/mcporter)（MCP CLI 客户端，用于命令行调用 MCP 工具）

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装 mcporter
npm install -g mcporter
```

## 核心概念

### MCP Server 是什么？

Model Context Protocol (MCP) 是 Anthropic 提出的标准协议，让 AI 助手能调用外部工具。MCP Server 提供工具，MCP Client（如 Amazon Q CLI、Cursor、VS Code）调用工具。

AWS 的 MCP Server 本质上是把 AWS SDK 操作封装成 AI 可调用的工具。比如 ECS MCP Server 提供 `containerize_app`、`build_and_push_image_to_ecr`、`ecs_resource_management` 等工具，AI 助手可以理解用户意图后自动选择和调用这些工具。

### 两种 ECS MCP Server

| 维度 | 开源版 (awslabs/mcp) | AWS 托管版 (preview) |
|------|-------------------|--------------------|
| 安装 | `uvx awslabs.ecs-mcp-server@latest` | `mcp-proxy-for-aws` + SigV4 |
| 部署能力 | ✅ Express Mode 全生命周期 | ❌ 只读（监控/排查） |
| 容器化 | ✅ Dockerfile 指导 + ECR 推送 | ❌ |
| 安全模型 | 本地运行，需广泛 IAM 权限 | SigV4 认证 + CloudTrail 审计 |
| 状态 | GA | Preview |

### ECS Express Mode

Express Mode 是 ECS 的简化部署模式，只需要 3 样东西就能部署：

1. 容器镜像
2. Task Execution Role
3. Infrastructure Role

ECS 自动配置 ALB（含 SSL/TLS）、自动扩缩、CloudWatch 日志、网络——无需 CloudFormation 模板。

## 动手实践

### Part 1: ECS MCP Server — 完整部署流程

#### Step 1: 配置 MCP Server

```bash
# 添加 ECS MCP Server（启用写操作和敏感数据访问）
mcporter config add ecs-mcp \
  --command "uvx --from awslabs.ecs-mcp-server@latest ecs-mcp-server" \
  --env ALLOW_WRITE=true \
  --env ALLOW_SENSITIVE_DATA=true \
  --env AWS_PROFILE=your-profile \
  --env AWS_REGION=us-east-1

# 验证安装
mcporter list ecs-mcp
# 输出: ecs-mcp (10 tools, ~6s)
```

ECS MCP Server 提供 10 个工具，其中 7 个是核心 ECS 工具，3 个是内嵌的 AWS Knowledge 工具（自动代理到 `knowledge-mcp.global.api.aws`），让 AI 助手在操作过程中随时查阅官方文档。

#### Step 2: 准备测试应用

创建一个简单的 Flask 应用：

```bash
mkdir -p /tmp/mcp-test-app

cat > /tmp/mcp-test-app/app.py << 'EOF'
from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({
        "message": "Hello from MCP Test App!",
        "service": "ECS Express Mode",
        "version": "1.0.0"
    })

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
EOF

cat > /tmp/mcp-test-app/requirements.txt << 'EOF'
flask==3.1.0
gunicorn==23.0.0
EOF

cat > /tmp/mcp-test-app/Dockerfile << 'EOF'
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]
EOF
```

!!! tip "ECR Public 基础镜像认证"
    ECR Public 未认证拉取限制为 1 次/秒（且最多 500GB/月），`docker build` 默认未认证拉取，容易触发 403。在 build 前先认证即可正常使用 `public.ecr.aws` 镜像：
    ```bash
    aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
    ```

#### Step 3: 获取容器化指导

```bash
mcporter call ecs-mcp.containerize_app \
  app_path=/tmp/mcp-test-app port=8080
```

`containerize_app` 返回 Dockerfile 最佳实践、docker-compose 模板、Hadolint 验证指导等——它是**指导性工具**，不会自动生成 Dockerfile。你需要根据指导手动创建（或让 AI 助手生成）。

#### Step 4: 创建 IAM 角色

Express Mode 需要两个 IAM 角色：

```bash
# Task Execution Role
aws iam create-role \
  --role-name ecsTaskExecutionRole \
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
  --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Infrastructure Role
aws iam create-role \
  --role-name ecsInfrastructureRoleForExpressServices \
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
  --role-name ecsInfrastructureRoleForExpressServices \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
```

!!! warning "IAM 策略名称大小写坑"
    策略名是 `AmazonECSInfrastructureRoleforExpressGatewayServices`（注意 `for` 是小写 f），不是 `...ForExpress...`。

#### Step 5: 构建并推送镜像到 ECR

```bash
mcporter call ecs-mcp.build_and_push_image_to_ecr \
  app_name=mcp-test-app \
  app_path=/tmp/mcp-test-app \
  tag=v1
```

MCP Server 自动完成以下操作：

1. 通过 CloudFormation 创建 ECR 仓库
2. 创建带 ECR push/pull 权限的 IAM 角色
3. 登录 ECR
4. 构建 Docker 镜像（linux/amd64）
5. 推送到 ECR

返回结果：

```json
{
  "repository_uri": "595842667825.dkr.ecr.us-east-1.amazonaws.com/mcp-test-app-repo",
  "image_tag": "v1",
  "full_image_uri": "595842667825.dkr.ecr.us-east-1.amazonaws.com/mcp-test-app-repo:v1",
  "stack_name": "mcp-test-app-ecr-infrastructure"
}
```

#### Step 6: 验证前置条件

```bash
mcporter call ecs-mcp.validate_ecs_express_mode_prerequisites \
  image_uri=<你的 full_image_uri>
```

验证通过会返回 "All prerequisites validated successfully"。

#### Step 7: 部署 ECS Express Mode 服务

```bash
mcporter call ecs-mcp.ecs_resource_management \
  --args '{
    "api_operation": "CreateExpressGatewayService",
    "api_params": {
      "serviceName": "mcp-test-svc",
      "primaryContainer": {
        "image": "<你的 full_image_uri>",
        "containerPort": 8080
      },
      "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsTaskExecutionRole",
      "infrastructureRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsInfrastructureRoleForExpressServices",
      "cpu": "256",
      "memory": "512",
      "healthCheckPath": "/health"
    }
  }'
```

Express Mode 自动完成：

- 创建 ECS 集群（如果 default 不存在）
- 配置 Fargate 任务
- 创建 ALB + Target Group + 安全组
- 配置 HTTPS (*.ecs.region.on.aws 域名)
- 设置自动扩缩 (默认 1-20 tasks, CPU 60% 目标)

约 **5-7 分钟**后，服务可访问：

```bash
curl https://mc-xxxx.ecs.us-east-1.on.aws/
# {"message":"Hello from MCP Test App!","service":"ECS Express Mode","version":"1.0.0"}

curl https://mc-xxxx.ecs.us-east-1.on.aws/health
# {"status":"healthy"}
```

### Part 2: Serverless MCP Server — SAM 应用部署

#### Step 1: 配置 MCP Server

```bash
# 注意：Serverless MCP 的写权限用 CLI flag，不是环境变量
mcporter config add serverless-mcp \
  --command "uvx --from awslabs.aws-serverless-mcp-server@latest awslabs.aws-serverless-mcp-server --allow-write --allow-sensitive-data-access" \
  --env AWS_PROFILE=your-profile \
  --env AWS_REGION=us-east-1

mcporter list serverless-mcp
# 输出: serverless-mcp (25 tools)
```

#### Step 2: 初始化 SAM 项目

```bash
mcporter call serverless-mcp.sam_init \
  --args '{
    "project_name": "mcp-sam-test",
    "runtime": "python3.10",
    "project_directory": "/tmp",
    "dependency_manager": "pip",
    "architecture": "x86_64"
  }'
```

#### Step 3: 构建和部署

```bash
# 构建（建议直接用 sam build，MCP 的 sam_build 可能静默失败）
cd /tmp/mcp-sam-test && sam build

# 通过 MCP 部署
mcporter call serverless-mcp.sam_deploy \
  --args '{
    "application_name": "mcp-sam-test",
    "project_directory": "/tmp/mcp-sam-test"
  }'
```

部署完成后可以通过 API Gateway 访问：

```bash
curl https://<api-id>.execute-api.us-east-1.amazonaws.com/Prod/hello/
# {"message": "hello world"}
```

## 测试结果

### 功能对比

| 能力 | ECS MCP Server | Serverless MCP Server |
|------|---------------|---------------------|
| 工具数量 | 10 (含 3 个 aws-knowledge) | 25 |
| 部署方式 | ECS Express Mode (Fargate) | SAM + CloudFormation |
| 端到端部署 | ✅ 一条命令完成全部 | ⚠️ 需要手动 sam build |
| 内嵌文档查询 | ✅ aws-knowledge 代理 | ❌ |
| 写权限控制 | 环境变量 `ALLOW_WRITE` | CLI flag `--allow-write` |
| 最终产物 | HTTPS URL + ALB + 自动扩缩 | API Gateway + Lambda |
| 部署时间 | ~5-7 分钟 | ~2-3 分钟 |
| 清理方式 | `delete_app` 工具 | `aws cloudformation delete-stack` |

### 安全设计

Express Mode 自动创建的安全组遵循最小权限原则：

| 安全组 | 入站规则 | 说明 |
|--------|---------|------|
| ALB SG | 0.0.0.0/0 on 80/443 | 公网 ALB 标准配置 |
| Task SG | 仅 ALB SG → 8080 | 任务仅接受 ALB 流量 |

## 踩坑记录

!!! warning "ECR Public 拉取 403 Forbidden"
    `docker build` 拉取 `public.ecr.aws` 镜像时返回 403，根因是 **ECR Public 未认证拉取限制为 1 次/秒**（且最多 500GB/月流量）。Docker build 默认是未认证拉取，频繁构建时极易触发限流。

    **解决方案**：在 `docker build` 之前先认证 ECR Public：

    ```bash
    aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
    ```

    认证后拉取限制提升到 **10 次/秒**，使用 `public.ecr.aws/docker/library/python:3.10-slim` 完全没问题，不需要改用 Docker Hub。在 ECS/Fargate/EC2 等 AWS 内部资源上运行时，认证后同样是 10 次/秒，不受未认证限制影响。

!!! warning "IAM 角色传播延迟"
    创建 IAM 角色后立即调用 Express Mode 可能报错 "Unable to assume the service linked role"。建议等待 15-30 秒让 IAM 传播完成。**已查文档确认：IAM 最终一致性是已知行为。**

!!! warning "SAM 部署 Region 不受环境变量控制"
    设置 `AWS_REGION=us-east-1` 后，SAM CLI 仍可能部署到其他 region（读 `samconfig.toml` 的 `region` 配置）。确保在 `samconfig.toml` 中指定正确 region。**实测发现，官方未特别记录。**

!!! warning "两个 MCP Server 的写权限控制方式不一致"
    ECS MCP Server 用环境变量 `ALLOW_WRITE=true`，Serverless MCP Server 用 CLI flag `--allow-write`。配置时注意区分。**实测发现，官方未特别记录。**

!!! warning "build_and_push 失败后残留资源"
    如果 `build_and_push_image_to_ecr` 在 Docker build 阶段失败，CloudFormation stack（ECR repo + IAM role）已创建。重试前需要先 `aws cloudformation delete-stack` 清理。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ECS Fargate (0.25 vCPU, 0.5GB) | $0.01/hr | ~1 hr | $0.01 |
| ALB | $0.0225/hr | ~1 hr | $0.02 |
| ECR 存储 | $0.10/GB | ~0.1 GB | $0.01 |
| Lambda (128MB) | $0.0000002/req | ~10 req | ~$0.00 |
| API Gateway | $3.50/M req | ~10 req | ~$0.00 |
| **合计** | | | **~$0.05** |

## 清理资源

### 清理 ECS 资源

```bash
# 1. 删除 Express Mode 服务（使用 MCP 工具）
mcporter call ecs-mcp.delete_app \
  service_arn="arn:aws:ecs:us-east-1:<ACCOUNT>:service/default/mcp-test-svc" \
  app_name=mcp-test-app

# 2. 删除 IAM 角色
aws iam detach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name ecsTaskExecutionRole

aws iam detach-role-policy --role-name ecsInfrastructureRoleForExpressServices \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
aws iam delete-role --role-name ecsInfrastructureRoleForExpressServices
```

### 清理 Serverless 资源

```bash
# 删除 SAM 应用（CloudFormation stack）
aws cloudformation delete-stack --stack-name mcp-sam-test --region us-east-1

# 删除 S3 部署桶中的对象（如果有残留）
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ECS Fargate 按秒计费，ALB 按小时计费。

## 结论与建议

### 适合场景

- **快速原型**: AI 助手 + MCP Server 可以在 10 分钟内把代码部署到 AWS，非常适合 PoC 和 Demo
- **开发者生产力**: 不熟悉 AWS 的开发者可以用自然语言描述需求，AI 助手自动选择工具完成部署
- **Platform Team**: 封装 MCP Server 作为内部开发者平台的标准工具

### 不适合场景

- **生产环境**: 开源版明确标注"不推荐用于生产"，缺少细粒度权限控制
- **复杂架构**: Express Mode 适合简单 Web 应用，多服务/微服务架构需要传统 ECS 服务配置
- **高安全要求**: 开源版运行在本地，需要广泛 IAM 权限；生产环境建议用 `ALLOW_WRITE=false` 只读模式

### 最佳实践

1. **默认只读**: 开发/测试环境用 `ALLOW_WRITE=true`，其他环境保持 `false`
2. **配合 AI IDE 使用**: MCP Server 的真正价值在于配合 Cursor、Amazon Q CLI 等 AI IDE，命令行调用只是验证工具可用性
3. **关注 AWS 托管版**: 托管版 ECS MCP Server 目前是 preview，GA 后建议迁移——SigV4 认证 + CloudTrail 审计更适合企业

## 参考链接

- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/05/new-model-context-protocol-servers-aws-serverless-containers/)
- [ECS Express Mode 文档](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html)
- [awslabs/mcp GitHub](https://github.com/awslabs/mcp)
- [ECS MCP Server PyPI](https://pypi.org/project/awslabs.ecs-mcp-server/)
- [Serverless MCP Server PyPI](https://pypi.org/project/awslabs.aws-serverless-mcp-server/)
- [Model Context Protocol 规范](https://modelcontextprotocol.io/)
