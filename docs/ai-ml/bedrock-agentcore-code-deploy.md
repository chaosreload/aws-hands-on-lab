---
tags:
  - AgentCore
  - What's New
---

# Amazon Bedrock AgentCore Runtime：使用 Direct Code Deployment 快速部署 AI Agent

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 20 分钟
    - **预估费用**: < $0.20（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

部署 AI Agent 到生产环境通常需要构建 Docker 容器、配置 ECR、设置 CodeBuild 等一系列步骤。对于快速原型验证和迭代开发，这个流程过重了。

2025 年 11 月，Amazon Bedrock AgentCore Runtime 新增了 **Direct Code Deployment** 功能，支持直接上传 Python zip 包部署 Agent，无需 Docker，大幅降低了部署门槛。

本文将通过完整的动手实践，带你体验 Direct Code Deployment 的全流程，并对比首次部署与更新部署的性能差异。

## 前置条件

- AWS 账号（需要 `BedrockAgentCoreFullAccess` 权限 + Starter Toolkit 所需的 IAM/S3/CodeBuild 权限）
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 已安装（Python 包管理器）
- Python 3.10+
- AWS CLI v2 已配置
- Bedrock Model Access：需启用 Claude Sonnet 4 模型

## 核心概念

AgentCore Runtime 提供两种部署方式：

| 维度 | Direct Code Deploy（新增）| Container Deploy |
|------|--------------------------|-----------------|
| **部署方式** | ZIP 文件上传 | Docker 容器 |
| **包大小限制** | 250MB（压缩）/ 750MB（解压）| 2GB |
| **Session 创建速率** | **25/s** | 0.16/s（约 100/min）|
| **后续更新速度** | 快（依赖缓存复用）| 较慢（完整镜像重建）|
| **运行时管理** | AWS 自动补丁 | 用户自己维护基础镜像 |
| **架构** | 仅 arm64 | 自定义 |
| **语言** | 仅 Python（3.10-3.14）| 任意 |

**简单来说**：包小于 250MB 且用 Python 的 Agent → 选 Direct Code Deploy。需要自定义运行时或超大包 → 选 Container。

### 支持的协议

AgentCore Runtime 支持四种协议，Direct Code Deploy 全部适用：

- **HTTP**（port 8080）— 标准 REST API / WebSocket
- **MCP**（port 8000）— Model Context Protocol
- **A2A**（port 9000）— Agent-to-Agent 通信
- **AGUI**（port 8080）— Agent-to-UI 交互

### 定价

按实际使用量计费，I/O 等待时间不计 CPU 费用：

- **CPU**: $0.0895/vCPU-hour
- **Memory**: $0.00945/GB-hour
- **S3 存储**: S3 Standard 定价（100MB Agent ≈ $0.0023/月）

## 动手实践

### Step 1: 初始化项目

```bash
# 安装 uv（如尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 创建项目
uv init agentcore_code_deploy --python 3.13
cd agentcore_code_deploy

# 安装核心依赖
uv add bedrock-agentcore strands-agents

# 安装 Starter Toolkit（开发工具）
uv add --dev bedrock-agentcore-starter-toolkit
```

### Step 2: 创建 Agent 项目

```bash
uv run agentcore create \
    --project-name codedeploy \
    --template basic \
    --agent-framework Strands \
    --model-provider Bedrock \
    --non-interactive \
    --no-venv
```

这会生成以下结构：

```
codedeploy/
├── .bedrock_agentcore.yaml    # 部署配置
├── src/
│   ├── main.py                # Agent 入口
│   ├── model/load.py          # 模型加载
│   └── mcp_client/client.py   # MCP 客户端
└── test/
```

### Step 3: 编写 Agent 代码

编辑 `codedeploy/src/main.py`，替换为简化版：

```python
import os
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
REGION = os.getenv("AWS_REGION", "us-east-1")

@tool
def add_numbers(a: int, b: int) -> int:
    """Return the sum of two numbers"""
    return a + b

@tool
def get_greeting(name: str) -> str:
    """Return a personalized greeting"""
    return f"Hello, {name}! Welcome to AgentCore Runtime."

@app.entrypoint
async def invoke(payload, context):
    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        region_name=REGION
    )

    agent = Agent(
        model=model,
        system_prompt="You are a helpful assistant. Use tools when appropriate.",
        tools=[add_numbers, get_greeting]
    )

    stream = agent.stream_async(payload.get("prompt", "Hello!"))
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]

if __name__ == "__main__":
    app.run()
```

!!! warning "注意：使用 Inference Profile ID"
    AgentCore Runtime 中调用 Bedrock 模型时，**必须使用 Inference Profile ID**（如 `us.anthropic.claude-sonnet-4-20250514-v1:0`），不能直接使用 Model ID（如 `anthropic.claude-sonnet-4-20250514-v1:0`）。否则会报 `ValidationException: Invocation of model ID ... with on-demand throughput isn't supported`。

### Step 4: 部署到 AgentCore Runtime

```bash
cd codedeploy

# 首次部署
uv run agentcore deploy
```

Starter Toolkit 会自动完成以下操作：

1. **创建 IAM 执行角色**（包含 Bedrock、CloudWatch Logs、X-Ray 权限）
2. **交叉编译依赖**（为 arm64 架构构建 Python wheels）
3. **打包 ZIP**（源码 + 依赖）
4. **创建 S3 Bucket** 并上传 ZIP
5. **创建 Agent** 并配置 Endpoint

部署成功后会显示 Agent ARN 和 CloudWatch Logs 路径。

### Step 5: 调用 Agent

```bash
# 测试工具调用
uv run agentcore invoke '{"prompt": "What is 42 + 58? Use the add_numbers tool."}'
# 预期输出: The sum of 42 + 58 is 100.

# 测试另一个工具
uv run agentcore invoke '{"prompt": "Please greet Archie using the get_greeting tool."}'
# 预期输出: Hello, Archie! Welcome to AgentCore Runtime.
```

### Step 6: 查看状态

```bash
uv run agentcore status
```

输出包含 Agent ARN、Endpoint 状态、Region、Account、网络模式和 CloudWatch Logs 路径。

### Step 7: 更新部署

修改 Agent 代码后，重新部署：

```bash
uv run agentcore deploy --auto-update-on-conflict
```

## 测试结果

### 部署性能对比

| 场景 | 耗时 | 包大小 | 依赖缓存 |
|------|------|--------|---------|
| **首次部署** | 34 秒 | 51.59 MB | ❌ 首次构建 |
| **更新部署**（仅改代码） | 24 秒 | 51.59 MB | ✅ 命中缓存 |
| **差异** | **-10 秒（29% 更快）** | — | 跳过依赖安装 |

更新部署时，Toolkit 检测到依赖未变化，直接复用缓存（"Using cached dependencies"），仅重新打包源码。

### 250MB 包大小限制验证

| 测试 | 包大小 | 结果 |
|------|--------|------|
| 正常部署 | 51.59 MB | ✅ 成功 |
| 超限部署 | 251.65 MB | ❌ 明确报错 |

超限时的报错信息清晰：`Package size (251.65 MB) exceeds 250MB limit. Consider reducing dependencies.`

验证为**客户端校验**，在上传前即拦截。

### Quota 关键指标

| Quota | Direct Code Deploy | Container |
|-------|-------------------|-----------|
| Session 创建速率 | 25/s | ~1.67/s (100/min) |
| 包大小（压缩）| 250 MB | 2 GB |
| 包大小（解压）| 750 MB | — |
| Agent 总数 | 1000/账号 | 1000/账号 |
| Endpoint/Agent | 10 | 10 |

## 踩坑记录

!!! warning "踩坑 1：Model ID vs Inference Profile ID"
    在 AgentCore Runtime 执行环境中，Bedrock 模型调用必须使用 **Inference Profile ID**（带 Region 前缀如 `us.`），不能用裸 Model ID。这与本地开发环境行为不同。**已查文档确认**：这是 Bedrock ConverseStream API 的要求。

!!! warning "踩坑 2：项目名限制"
    `agentcore create --project-name` 只允许**字母和数字**，不支持 `-` 或 `_`，最长 36 字符。如果用了不合规的字符会报错。**实测发现，官方文档未明确记录此限制**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AgentCore Runtime (CPU) | $0.0895/vCPU-hr | ~0.001 hr | < $0.01 |
| AgentCore Runtime (Memory) | $0.00945/GB-hr | ~0.001 hr | < $0.01 |
| Bedrock Claude Sonnet 4 | ~$0.003/1K input + $0.015/1K output | 3 次调用 | ~$0.05 |
| S3 存储 | $0.023/GB-month | 51.59 MB | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 删除 Agent、Endpoint 及关联资源
cd codedeploy
uv run agentcore destroy

# 手动确认清理（可选）
# 检查 S3 Bucket
aws s3 ls s3://bedrock-agentcore-codebuild-sources-${ACCOUNT_ID}-us-east-1/ \
    --region us-east-1

# 检查 IAM Role（Toolkit 创建的 Role 需手动清理）
aws iam list-roles --query 'Roles[?contains(RoleName, `BedrockAgentCore`)].RoleName' \
    --output text
```

!!! danger "务必清理"
    Lab 完成后请执行 `agentcore destroy` 清理资源。AgentCore Runtime 按使用量计费，空闲 Session 在 IdleRuntimeSessionTimeout（默认 15 分钟）后自动释放，但 Agent 和 Endpoint 资源会持续存在。

## 结论与建议

**Direct Code Deployment 是 AgentCore Runtime 的重要补充**：

1. **快速原型验证**：从代码到部署仅需 34 秒，更新仅需 24 秒，显著优于容器部署流程
2. **零 Docker 依赖**：消除了构建容器镜像的复杂度，降低了入门门槛
3. **Session 创建速率优势**：25/s 对比容器的 ~1.67/s，适合高并发场景
4. **依赖缓存智能化**：自动检测依赖变化，未变化时复用缓存，加速迭代

**推荐使用场景**：

- Agent 原型开发和快速迭代
- 部署包 < 250MB 的 Python Agent
- 需要高 Session 创建速率的场景
- 从开发到生产的渐进式部署（先 Direct Deploy 原型 → 成熟后切 Container）

**限制需注意**：

- 仅支持 Python（3.10-3.14）
- 仅支持 arm64 架构
- 包大小限制 250MB（压缩）
- 无法自定义运行时环境（AWS 管理补丁）

## 参考链接

- [AgentCore Runtime Direct Code Deployment 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html)
- [AgentCore Starter Toolkit](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-toolkit.html)
- [AgentCore Runtime Service Contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html)
- [AgentCore Runtime IAM 权限](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [AgentCore 支持的 Region](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-bedrock-agentcore-runtime-code-deployment/)
