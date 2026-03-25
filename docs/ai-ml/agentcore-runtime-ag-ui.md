# Amazon Bedrock AgentCore Runtime AG-UI 协议实战：标准化 Agent-Frontend 事件流交互

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $0.50（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-25

## 背景

AI Agent 与前端 UI 的交互一直缺乏标准化。传统方式下，开发者要么轮询 API 获取最终结果，要么自定义 SSE/WebSocket 格式来实现流式体验。不同 Agent 框架的前端集成各自为政，增加了全栈开发的复杂度。

**AG-UI (Agent-User Interface)** 是 CopilotKit 开源的事件驱动协议，专门解决 Agent → 前端的实时通信问题。它定义了标准化的事件类型（文本流、Tool 调用、状态同步），让前端可以用统一方式渲染 Agent 的思考过程。

Amazon Bedrock AgentCore Runtime 现已原生支持 AG-UI 协议——这是继 HTTP/SSE 和 WebSocket 之后的第三个前端协议。AgentCore Runtime 负责认证、会话隔离和自动扩缩，开发者只需专注 Agent 逻辑和前端渲染。

**AG-UI 在 AgentCore 协议栈中的定位：**

| 协议 | 端口 | 用途 | 方向 |
|------|------|------|------|
| MCP | 8000 | Agent 获取工具和上下文 | Agent ↔ Tools |
| A2A | 9000 | Agent 间通信协作 | Agent ↔ Agent |
| AG-UI | 8080 | Agent → 前端 UI 交互 | Agent → User |

三者互补：MCP 给 Agent 能力，A2A 让 Agent 协作，AG-UI 把结果呈现给用户。

## 前置条件

- AWS 账号（需要 `bedrock-agentcore:*` 和 `bedrock:InvokeModel` 权限）
- AWS CLI v2 已配置 Profile
- Python 3.12+
- 已启用 Bedrock Claude Sonnet 模型访问（us-west-2）

## 核心概念

### AG-UI 事件类型

AG-UI 的核心是标准化的事件流。与普通 SSE 返回原始文本不同，AG-UI 每个事件都有明确的 `type` 字段，前端可以据此做精确渲染：

| 事件类型 | 用途 | 前端渲染 |
|---------|------|---------|
| `RUN_STARTED` | Agent 开始处理 | 显示"思考中"动画 |
| `TEXT_MESSAGE_START/CONTENT/END` | 流式文本输出 | 逐字显示回复 |
| `TOOL_CALL_START` | Tool 调用开始 | 显示"正在调用 XX 工具" |
| `TOOL_CALL_ARGS` | Tool 调用参数 | 展示参数详情 |
| `TOOL_CALL_END` | Tool 调用完成 | 标记工具执行完毕 |
| `TOOL_CALL_RESULT` | Tool 返回结果 | 渲染工具输出 |
| `STATE_SNAPSHOT` | 状态快照 | 更新 UI 状态（进度条等） |
| `RUN_FINISHED` | Agent 完成 | 移除"思考中"状态 |
| `RUN_ERROR` | 错误事件 | 显示错误信息 |

### AG-UI vs 原生 API 对比

| 维度 | 原生 Strands API | AG-UI 协议 |
|------|-----------------|-----------|
| 输出格式 | 最终文本 blob | 结构化事件流 |
| Tool 调用可见性 | 仅最终结果 | START → ARGS → END → RESULT 全链路 |
| 状态同步 | 无 | STATE_SNAPSHOT/STATE_DELTA |
| 前端集成 | 需自定义解析 | CopilotKit 等框架原生支持 |
| 适用场景 | 后端调用 | 前端 UI 实时渲染 |

## 动手实践

### Step 1: 准备环境

```bash
# 创建项目目录和虚拟环境
mkdir -p ~/agui-test && cd ~/agui-test
python3.12 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install fastapi uvicorn ag-ui-strands
```

安装完成后确认版本：

```bash
pip show ag-ui-strands ag-ui-protocol strands-agents | grep -E "^(Name|Version)"
# ag-ui-strands 0.1.2
# ag-ui-protocol 0.1.14
# strands-agents 1.33.0
```

### Step 2: 创建 AG-UI Server

创建 `my_agui_server.py`，包含自定义 Tool 以测试完整事件链：

```python
# my_agui_server.py
import uvicorn
from datetime import datetime, UTC
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from ag_ui_strands import StrandsAgent
from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

# 自定义工具 — 用于验证 Tool Call 事件流
@tool
def get_current_time(timezone: str = "UTC") -> str:
    """Get the current time in the specified timezone."""
    now = datetime.now(UTC)
    return f"Current time ({timezone}): {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"

@tool
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression safely."""
    allowed = set("0123456789+-*/.() ")
    if not all(c in allowed for c in expression):
        return "Error: Invalid characters in expression"
    try:
        result = eval(expression)
        return f"Result: {expression} = {result}"
    except Exception as e:
        return f"Error: {str(e)}"

# 使用 Bedrock Claude 模型
model = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
    region_name="us-west-2",
    max_tokens=1024,
)

strands_agent = Agent(
    model=model,
    system_prompt="You are a helpful assistant. Use tools when appropriate.",
    tools=[get_current_time, calculate],
)

# AG-UI 协议封装
agui_agent = StrandsAgent(
    agent=strands_agent,
    name="agui_demo_agent",
    description="Demo agent with tool support for AG-UI protocol",
)

app = FastAPI()

@app.post("/invocations")
async def invocations(input_data: dict, request: Request):
    """AG-UI 主端点 — 返回 SSE 事件流"""
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    async def event_generator():
        run_input = RunAgentInput(**input_data)
        async for event in agui_agent.run(run_input):
            yield encoder.encode(event)

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type()
    )

@app.get("/ping")
async def ping():
    return JSONResponse({"status": "Healthy"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

关键点：

- `StrandsAgent` 将 Strands Agent 封装为 AG-UI 兼容的事件生成器
- `EventEncoder` 根据 Accept header 自动选择编码格式（SSE 或 WebSocket）
- 端口 8080 + 路径 `/invocations` 是 AgentCore Runtime 的 AG-UI 协议约定

### Step 3: 本地测试

启动 Server：

```bash
AWS_PROFILE=your-profile python my_agui_server.py
```

测试基本对话：

```bash
curl -N -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "threadId": "test-001",
    "runId": "run-001",
    "state": {},
    "messages": [{"role": "user", "content": "Say hello!", "id": "msg-1"}],
    "tools": [],
    "context": [],
    "forwardedProps": {}
  }'
```

你会看到标准 SSE 格式的事件流：

```
data: {"type":"RUN_STARTED","threadId":"test-001","runId":"run-001"}
data: {"type":"STATE_SNAPSHOT","snapshot":{}}
data: {"type":"TEXT_MESSAGE_START","messageId":"...","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":"Hello"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":" there!"}
data: {"type":"TEXT_MESSAGE_END","messageId":"..."}
data: {"type":"STATE_SNAPSHOT","snapshot":{}}
data: {"type":"RUN_FINISHED","threadId":"test-001","runId":"run-001"}
```

测试 Tool Call 事件流：

```bash
curl -N -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "threadId": "test-002",
    "runId": "run-002",
    "state": {},
    "messages": [{"role": "user", "content": "What is 42 * 17?", "id": "msg-2"}],
    "tools": [],
    "context": [],
    "forwardedProps": {}
  }'
```

Tool Call 事件链完整可见：

```
data: {"type":"TOOL_CALL_START","toolCallId":"tooluse_xxx","toolCallName":"calculate","parentMessageId":"..."}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"tooluse_xxx","delta":"{\"expression\": \"42 * 17\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"tooluse_xxx"}
data: {"type":"TOOL_CALL_RESULT","messageId":"...","toolCallId":"tooluse_xxx","content":"\"Result: 42 * 17 = 714\""}
```

### Step 4: 部署到 AgentCore Runtime

安装部署工具：

```bash
pip install bedrock-agentcore-starter-toolkit
```

!!! note "依赖提示"
    `direct_code_deploy` 模式需要安装 [uv](https://docs.astral.sh/uv/)：`curl -LsSf https://astral.sh/uv/install.sh | sh`

创建 `requirements.txt`：

```
fastapi
uvicorn
ag-ui-strands
```

配置并部署：

```bash
# 配置 AG-UI 协议部署
agentcore configure \
  -e my_agui_server.py \
  -n agui_demo_server \
  -p AGUI \
  -r us-west-2 \
  -rf requirements.txt \
  -dt direct_code_deploy \
  --runtime PYTHON_3_12 \
  -ni

# 部署到 AWS
agentcore deploy
```

部署完成后你会获得 Agent ARN：

```
arn:aws:bedrock-agentcore:us-west-2:<account-id>:runtime/agui_demo_server-XXXXXXXXXX
```

### Step 5: 远程调用已部署的 AG-UI Server

使用 SigV4 认证调用：

```python
import asyncio
import json
from urllib.parse import quote
from uuid import uuid4
import httpx
from httpx_sse import aconnect_sse
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

AGENT_ARN = "arn:aws:bedrock-agentcore:us-west-2:<account-id>:runtime/<your-agent>"
REGION = "us-west-2"

async def invoke_agui_agent(message: str):
    session = boto3.Session(profile_name="your-profile", region_name=REGION)
    credentials = session.get_credentials().get_frozen_credentials()

    escaped_arn = quote(AGENT_ARN, safe='')
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{escaped_arn}/invocations"

    payload = {
        "threadId": str(uuid4()),
        "runId": str(uuid4()),
        "state": {},
        "messages": [{"role": "user", "content": message, "id": str(uuid4())}],
        "tools": [], "context": [], "forwardedProps": {}
    }

    body = json.dumps(payload)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(aws_request)

    async with httpx.AsyncClient(timeout=60) as client:
        async with aconnect_sse(
            client, "POST", url, headers=dict(aws_request.headers), content=body
        ) as event_source:
            async for event in event_source.aiter_sse():
                if event.data:
                    parsed = json.loads(event.data)
                    print(f"[{parsed['type']}] {json.dumps(parsed, ensure_ascii=False)}")

asyncio.run(invoke_agui_agent("What is 42 * 17? Use the calculate tool."))
```

## 测试结果

### 事件流格式验证

| 测试场景 | 事件数量 | 事件链 | 状态 |
|---------|---------|--------|------|
| 纯文本对话 | 10 | RUN_STARTED → STATE_SNAPSHOT → TEXT_MESSAGE_START → CONTENT(×4) → END → STATE_SNAPSHOT → RUN_FINISHED | ✅ |
| 单 Tool 调用 | ~20 | ...→ TOOL_CALL_START → ARGS → END → RESULT → TEXT_MESSAGE_* → ... | ✅ |
| 双 Tool 并行调用 | ~25 | 两组 TOOL_CALL 事件 + 两个 TOOL_CALL_RESULT | ✅ |
| 远程部署调用 | ~20 | 事件类型和顺序与本地完全一致 | ✅ |

### 边界条件测试

| 测试场景 | 预期 | 实际结果 |
|---------|------|---------|
| 空 messages 数组 | 报错或 RUN_ERROR | ⚠️ HTTP 200，Agent 自动生成欢迎语 |
| 无效 JSON body | HTTP 400/422 | HTTP 422 (FastAPI 校验拦截) |
| 31 条消息历史 (3.6KB) | 正常处理 | ✅ 返回 23 个事件，处理正常 |

**关键发现**：AG-UI 协议层不做 payload 校验——`RunAgentInput` 的验证由你的容器代码处理。空 messages 不会触发 `RUN_ERROR`，这是因为 Agent 将其视为对话初始化。

### AG-UI vs 原生 API 对比

| 对比维度 | 原生 Strands API | AG-UI 协议 |
|---------|-----------------|-----------|
| 返回格式 | `AgentResult` 对象（最终文本） | SSE 事件流（结构化中间状态） |
| Tool Call 过程 | 不可见，仅含最终结果 | TOOL_CALL_START → ARGS → END → RESULT |
| 前端适配工作 | 需自行设计流式协议 | CopilotKit 等框架原生支持 |
| 状态同步 | 不支持 | STATE_SNAPSHOT 自动推送 |

## 踩坑记录

!!! warning "注意事项"

    **1. Agent name 命名规则** — 已查文档确认
    
    Agent name 只支持字母、数字和下划线，**不支持连字符**。`agui-test` 会报错，改用 `agui_test` 即可。

    **2. direct_code_deploy 依赖 uv** — 实测发现，官方文档未明确
    
    使用 `direct_code_deploy` 部署类型时，`agentcore deploy` 需要 `uv` 工具来构建依赖包。如果未安装会报 "uv not found" 错误。解决：`curl -LsSf https://astral.sh/uv/install.sh | sh`。

    **3. AG-UI 不做请求校验** — 已查文档确认
    
    官方文档明确说明："Amazon Bedrock AgentCore passes request payloads directly to your container without validation." 这意味着输入校验完全由你的容器代码负责。

    **4. ARM64 容器要求** — 已查文档确认
    
    AgentCore Runtime 要求 ARM64 架构容器。使用 `direct_code_deploy` 时自动处理（uv 会交叉编译 ARM64 依赖），但自行构建容器时需注意 `--platform linux/arm64`。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Bedrock Claude Sonnet 推理 | ~$0.003/1K input + $0.015/1K output | 6 次调用 | ~$0.50 |
| AgentCore Runtime | 按请求计费 | 测试量 | ~$0.10 |
| S3 存储（部署包） | $0.023/GB/月 | 25MB | < $0.01 |
| CloudWatch Logs | $0.50/GB | 极少量 | < $0.01 |
| **合计** | | | **~$0.61** |

## 清理资源

```bash
# 1. 删除 AgentCore Runtime Agent
aws bedrock-agentcore delete-agent-runtime \
  --agent-runtime-id agui_test_server-OQOILHC28E \
  --region us-west-2 \
  --profile your-profile

# 2. 删除 Memory
aws bedrock-agentcore delete-memory \
  --memory-id agui_test_server_mem-ZtwuUY5aig \
  --region us-west-2 \
  --profile your-profile

# 3. 删除 S3 部署包
aws s3 rm s3://bedrock-agentcore-codebuild-sources-<account-id>-us-west-2/agui_test_server/ \
  --recursive --region us-west-2 --profile your-profile

# 4. 删除 CloudWatch Log Groups
aws logs delete-log-group \
  --log-group-name /aws/bedrock-agentcore/runtimes/agui_test_server-OQOILHC28E-DEFAULT \
  --region us-west-2 --profile your-profile

# 5. 清理 IAM Role（如不再需要 AgentCore）
aws iam detach-role-policy \
  --role-name AmazonBedrockAgentCoreSDKRuntime-us-west-2-<suffix> \
  --policy-arn <policy-arn> --profile your-profile
aws iam delete-role \
  --role-name AmazonBedrockAgentCoreSDKRuntime-us-west-2-<suffix> \
  --profile your-profile
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。AgentCore Runtime 按请求计费，不清理不会产生大额费用，但建议保持账号整洁。

## 结论与建议

**AG-UI 解决了什么问题**：标准化了 AI Agent 与前端 UI 的通信协议。之前每个 Agent 框架自定义的流式格式，现在有了统一的事件类型和语义。

**适合什么场景**：

- 需要在 Web 前端实时展示 Agent 思考过程的应用
- 使用 CopilotKit 等框架构建 Agent UI 的项目
- 需要 Tool Call 可视化（让用户看到 Agent 调用了什么工具）
- 多 Agent 系统的前端展示层

**生产环境建议**：

1. **认证选择**：面向终端用户用 OAuth 2.0（Cognito User Pool），内部服务间调用用 SigV4
2. **错误处理**：监听 `RUN_ERROR` 事件，在前端展示友好错误信息
3. **结合 CopilotKit**：AG-UI 是 CopilotKit 的原生协议，推荐配合 `@copilotkit/react-core` 使用，可获得开箱即用的 Agent UI 组件
4. **协议选择**：如果只需要后端调用 Agent，HTTP/SSE 够用；需要丰富的前端交互体验，选 AG-UI

**与 WebRTC 的关系**：AgentCore Runtime 的 WebRTC 支持（通过 KVS TURN）专注于语音/视频的实时双向流。AG-UI 专注于文本/UI 交互的标准化。两者互补——语音用 WebRTC，文本/UI 用 AG-UI。

## 参考链接

- [AG-UI 协议官方文档](https://docs.ag-ui.com/introduction)
- [Deploy AG-UI servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui.html)
- [AG-UI Protocol Contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui-protocol-contract.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-agentcore-runtime-ag-ui-protocol/)
- [AG-UI Dojo (交互式示例)](https://dojo.ag-ui.com/)
- [CopilotKit + AWS Strands 集成](https://docs.copilotkit.ai/aws-strands)
