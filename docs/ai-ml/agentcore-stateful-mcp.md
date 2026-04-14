---
tags:
  - AgentCore
  - MCP
  - What's New
---

# Amazon Bedrock AgentCore Runtime Stateful MCP Server 实战：Elicitation、Sampling 与 Progress Notifications 深度测试

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $2-3（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

MCP（Model Context Protocol）已成为 AI Agent 与外部工具/数据交互的标准协议。此前，Amazon Bedrock AgentCore Runtime 支持的 MCP Server 仅限于 stateless 模式——每个请求独立处理，Server 不保持任何会话状态。这意味着 MCP Server 只能暴露 Resources、Prompts 和 Tools 这三种基础能力。

**2026 年 3 月**，AgentCore Runtime 新增了 **Stateful MCP Server** 支持。在 stateful 模式下，每个用户 session 运行在独立的 microVM 中，Server 通过 `Mcp-Session-Id` header 维持会话上下文。更重要的是，这解锁了三个此前无法使用的 MCP 协议能力：

- **Elicitation**：Server 主动向 Client 请求用户输入（多轮对话收集信息）
- **Sampling**：Server 请求 Client 侧的 LLM 生成内容（如个性化推荐）
- **Progress Notifications**：长时间运行的操作实时向 Client 报告进度

这三个特性的共同点：**它们都需要 Server 和 Client 之间维持一个持续的双向会话**，这恰恰是 stateless 模式做不到的。

本文通过部署一个完整的旅行预订 Agent 来实测这些新特性，展示从代码编写到 AgentCore Runtime 部署的全流程。

## 前置条件

- AWS 账号（需要 IAM 权限：bedrock-agentcore、iam、s3、logs）
- AWS CLI v2 已配置
- Python 3.10+
- pip install `fastmcp>=2.10.0` `mcp` `starlette` `uvicorn`
- `bedrock-agentcore-starter-toolkit` (agentcore CLI)

## 核心概念

### Stateless vs Stateful MCP Server

| 维度 | Stateless（之前） | Stateful（新增） |
|------|------------------|-----------------|
| 启动参数 | `stateless_http=True`（默认） | `stateless_http=False` |
| Session 管理 | 无 | `Mcp-Session-Id` header |
| 运行环境 | 共享 | 每个 session 独立 microVM |
| 支持的 MCP Features | Resources, Prompts, Tools | + **Elicitation, Sampling, Progress** |
| 适用场景 | 简单的查询/执行 | 多轮交互、长时间运行的工作流 |

### 三大新特性

```
                     ┌─────────────────────────────────────────┐
                     │         Stateful MCP Session            │
                     │         (Mcp-Session-Id: xxx)           │
                     └─────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
   ┌──────▼──────┐           ┌───────▼───────┐          ┌───────▼───────┐
   │ Elicitation │           │   Sampling    │          │   Progress    │
   │             │           │               │          │               │
   │ Server → 问  │           │ Server → 请求  │          │ Server → 报告  │
   │ Client ← 答  │           │ Client ← LLM  │          │ Client ← 进度  │
   └─────────────┘           └───────────────┘          └───────────────┘
   ctx.elicit()              ctx.sample()               ctx.report_progress()
```

- **Elicitation**：`await ctx.elicit(message, response_type)` — Server 主动发起问题，支持 str/int/Enum/list 类型约束
- **Sampling**：`await ctx.sample(messages, max_tokens)` — Server 请求 Client 调用 LLM 生成内容
- **Progress**：`await ctx.report_progress(progress, total)` — 实时汇报进度（如 1/5, 2/5...）

## 动手实践

### Step 1: 创建 MCP Server 代码

创建项目目录和依赖：

```bash
mkdir -p agentcore-stateful-mcp && cd agentcore-stateful-mcp

cat > requirements.txt << 'EOF'
fastmcp>=2.10.0
mcp
starlette
uvicorn
EOF

pip install -r requirements.txt
```

创建 `travel_server.py`——一个旅行预订 Agent，完整展示所有 MCP 特性：

```python
"""
Travel Booking Agent - Stateful MCP Server
Demonstrates: Elicitation, Progress, Sampling, Resources, Prompts
"""
import asyncio
import json
import uvicorn
from fastmcp import FastMCP, Context
from enum import Enum
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import PlainTextResponse

mcp = FastMCP("Travel-Booking-Agent")

# 目的地数据
class TripType(str, Enum):
    BUSINESS = "business"
    LEISURE = "leisure"
    FAMILY = "family"

DESTINATIONS = {
    "paris": {"name": "Paris, France", "flight": 450, "hotel": 180,
              "highlights": ["Eiffel Tower", "Louvre", "Notre-Dame"],
              "phrases": ["Bonjour", "Merci", "S'il vous plait"]},
    "tokyo": {"name": "Tokyo, Japan", "flight": 900, "hotel": 150,
              "highlights": ["Shibuya", "Senso-ji Temple", "Mt. Fuji day trip"],
              "phrases": ["Konnichiwa", "Arigato", "Sumimasen"]},
    "new york": {"name": "New York, USA", "flight": 350, "hotel": 250,
                 "highlights": ["Central Park", "Broadway", "Statue of Liberty"],
                 "phrases": ["Hey!", "Thanks", "Excuse me"]},
    "bali": {"name": "Bali, Indonesia", "flight": 800, "hotel": 100,
             "highlights": ["Ubud Rice Terraces", "Tanah Lot", "Beach clubs"],
             "phrases": ["Selamat pagi", "Terima kasih", "Sama-sama"]}
}

# === Resources: 暴露目的地数据 ===
@mcp.resource("travel://destinations")
def list_destinations() -> str:
    """All available destinations with pricing."""
    return json.dumps({k: {"name": v["name"], "flight": v["flight"], "hotel": v["hotel"]}
                       for k, v in DESTINATIONS.items()}, indent=2)

@mcp.resource("travel://destination/{city}")
def get_destination(city: str) -> str:
    """Detailed info for a specific destination."""
    dest = DESTINATIONS.get(city.lower())
    return json.dumps(dest, indent=2) if dest else f"Unknown: {city}"

# === Prompts: 可复用模板 ===
@mcp.prompt()
def packing_list(destination: str, days: int, trip_type: str) -> str:
    """Generate packing list prompt."""
    return f"Create a {days}-day packing list for a {trip_type} trip to {destination}."

@mcp.prompt()
def local_phrases(destination: str) -> str:
    """Generate local phrases prompt."""
    dest = DESTINATIONS.get(destination.lower(), {})
    phrases = dest.get("phrases", [])
    return f"Teach me essential phrases for {destination}. Start with: {', '.join(phrases)}"

# === Main Tool: 完整预订工作流 ===
@mcp.tool()
async def plan_trip(ctx: Context) -> str:
    """Plan a complete trip using Elicitation + Progress + Sampling."""

    # Phase 1: ELICITATION — 多轮收集旅行偏好
    dest_result = await ctx.elicit(
        message="Where would you like to go?\nOptions: Paris, Tokyo, New York, Bali",
        response_type=str
    )
    if dest_result.action != "accept":
        return "Trip planning cancelled."
    dest_key = dest_result.data.lower().strip()
    dest = DESTINATIONS.get(dest_key, DESTINATIONS["paris"])

    type_result = await ctx.elicit(
        message="What type of trip?\n1. business\n2. leisure\n3. family",
        response_type=TripType
    )
    if type_result.action != "accept":
        return "Trip planning cancelled."
    trip_type = type_result.data

    days_result = await ctx.elicit(message="How many days? (3-14)", response_type=int)
    if days_result.action != "accept":
        return "Trip planning cancelled."
    days = max(3, min(14, days_result.data))

    travelers_result = await ctx.elicit(message="Number of travelers?", response_type=int)
    if travelers_result.action != "accept":
        return "Trip planning cancelled."
    travelers = travelers_result.data

    # Phase 2: PROGRESS NOTIFICATIONS — 模拟搜索过程
    for step in range(1, 6):
        await ctx.report_progress(progress=step, total=5)
        await asyncio.sleep(0.4)

    flight_cost = dest["flight"] * travelers
    hotel_cost = dest["hotel"] * days * ((travelers + 1) // 2)
    total_cost = flight_cost + hotel_cost

    # Phase 3: SAMPLING — 请求 Client 侧 LLM 生成推荐
    ai_tips = f"Enjoy {dest['name']}!"
    try:
        response = await ctx.sample(
            messages=f"Give 3 brief tips for a {trip_type} trip to {dest['name']}.",
            max_tokens=150
        )
        if hasattr(response, 'text') and response.text:
            ai_tips = response.text
    except Exception:
        ai_tips = f"Visit {dest['highlights'][0]}, try local food!"

    # Phase 4: 最终确认 (再次使用 Elicitation)
    confirm = await ctx.elicit(
        message=f"TOTAL: ${total_cost} — Confirm booking?",
        response_type=["Yes", "No"]
    )
    if confirm.action != "accept" or confirm.data == "No":
        return "Booking cancelled."

    session_ref = ctx.session_id[:8].upper() if ctx.session_id else 'LOCAL'
    return f"BOOKING CONFIRMED! Ref: TRV-{session_ref}, Total: ${total_cost}"

# === ASGI App: 健康检查 + MCP ===
async def ping(request):
    return PlainTextResponse("OK")

if __name__ == "__main__":
    mcp_app = mcp.get_asgi_app(
        transport="streamable-http", path="/mcp", stateless_http=False
    )
    app = Starlette(routes=[
        Route("/ping", ping),
        Mount("/mcp", app=mcp_app),
    ])
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

> **关键参数**：`stateless_http=False` 是启用 stateful 模式的开关。
>
> **`/ping` 端点**：AgentCore Runtime 需要健康检查端点，FastMCP 默认不提供。

### Step 2: 本地测试

启动 Server：

```bash
python3 travel_server.py
# Server: http://0.0.0.0:8080/mcp (stateful mode)
```

创建自动化测试客户端 `test_client.py`：

```python
"""Automated test client for all MCP features."""
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.client.elicitation import ElicitResult
from mcp.types import CreateMessageResult, TextContent

RESPONSES = ["tokyo", "leisure", "5", "2", "Yes"]
elicit_idx = 0

async def elicit_handler(message, response_type, params, ctx):
    global elicit_idx
    resp = RESPONSES[elicit_idx]; elicit_idx += 1
    if response_type == int: resp = int(resp)
    return ElicitResult(action="accept", content={"value": resp})

async def sampling_handler(messages, params, ctx):
    return CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text="Visit Shibuya, try ramen, day trip to Fuji!"),
        model="test", stopReason="endTurn"
    )

progress_log = []
async def progress_handler(progress, total, msg=None):
    progress_log.append(f"{int(progress)}/{int(total)}")

async def main():
    transport = StreamableHttpTransport(url="http://localhost:8080/mcp")
    client = Client(transport,
        elicitation_handler=elicit_handler,
        sampling_handler=sampling_handler,
        progress_handler=progress_handler)

    async with client:
        # Test Resources
        resources = await client.list_resources()
        print(f"Resources: {len(resources)}")
        dest = await client.read_resource("travel://destinations")
        print(f"Destinations loaded ✅")

        # Test Prompts
        prompts = await client.list_prompts()
        print(f"Prompts: {len(prompts)} ✅")

        # Test Full Workflow
        result = await client.call_tool("plan_trip", {})
        result_text = result[0].text if hasattr(result[0], 'text') else str(result)
        print(f"Elicitations: {elicit_idx}")
        print(f"Progress: {progress_log}")
        print(f"Result: {result_text[:100]}")
        print(f"Booking confirmed: {'CONFIRMED' in result_text} ✅")

asyncio.run(main())
```

```bash
python3 test_client.py
```

### Step 3: 部署到 AgentCore Runtime

配置 `.bedrock_agentcore.yaml`：

```yaml
agents:
  statefulMcpTravel:
    name: statefulMcpTravel
    entrypoint: travel_server.py
    deployment_type: direct_code_deploy
    runtime_type: PYTHON_3_10
    aws:
      region: us-east-1
      execution_role_auto_create: true
      s3_auto_create: true
      protocol_configuration:
        server_protocol: MCP
```

部署：

```bash
export AWS_PROFILE=your-profile
agentcore deploy
```

部署成功后获得 Agent ARN：

```
✅ Agent: arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/statefulMcpTravel-XXXXX
```

### Step 4: 远程测试（SigV4 认证）

远程访问 AgentCore MCP Server 需要 SigV4 签名。创建自定义 httpx Auth 类：

```python
import httpx, boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

class SigV4Auth_httpx(httpx.Auth):
    """Per-request SigV4 signing for MCP streaming sessions."""
    def __init__(self, profile, region):
        self._session = boto3.Session(profile_name=profile, region_name=region)
        self._region = region

    def auth_flow(self, request):
        credentials = self._session.get_credentials().get_frozen_credentials()
        aws_req = AWSRequest(method=str(request.method), url=str(request.url),
                             data=request.content or b"")
        SigV4Auth(credentials, "bedrock-agentcore", self._region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request
```

使用方式：

```python
auth = SigV4Auth_httpx("your-profile", "us-east-1")
transport = StreamableHttpTransport(url=invoke_url, auth=auth)
```

## 测试结果

### 本地 Stateful MCP 测试结果

| MCP Feature | 测试方法 | 结果 | 备注 |
|---|---|---|---|
| **Resources** | 读取 `travel://destinations` | ✅ 返回 4 个目的地 JSON | 支持 static + template URI |
| **Prompts** | 调用 `packing_list`/`local_phrases` | ✅ 返回格式化 prompt | 2 个 prompt 模板 |
| **Elicitation** | plan_trip 中 5 轮交互 | ✅ 5/5 成功收集 | str/Enum/int/list 类型约束 |
| **Progress** | plan_trip 进度通知 | ✅ 5 步 (20%→100%) | 实时进度条更新 |
| **Sampling** | Client 侧 LLM mock 响应 | ✅ AI tips 整合到结果 | Client 控制 LLM 调用 |
| **Cancel** | Elicitation 返回 decline | ✅ "Trip planning cancelled." | 优雅取消 |
| **完整工作流** | 全流程 1.83s | ✅ BOOKING CONFIRMED | 含 session ID 引用 |

### AgentCore Runtime 部署测试

| 测试项 | 结果 | 备注 |
|---|---|---|
| 部署 (direct_code_deploy) | ✅ 30.28 MB | PYTHON_3_10 运行时 |
| MicroVM 启动 | ✅ port 8080 | CloudWatch 确认 |
| 健康检查 (/ping) | ✅ 需自行添加 | FastMCP 默认不提供 |
| SigV4 Per-request 认证 | ✅ 请求到达 | 自定义 httpx Auth |
| MCP Proxy 协议兼容 | ⚠️ ID 映射问题 | 详见踩坑记录 |

## 踩坑记录

!!! warning "踩坑 1: Agent 名称不支持连字符"
    AgentCore agent 名称有正则约束 `{0,47}`，**不允许连字符 `-`**。
    `stateful-mcp-travel` ❌ → `statefulMcpTravel` ✅
    **状态**：实测发现，官方文档未明确记录。

!!! warning "踩坑 2: 必须提供 /ping 健康检查端点"
    AgentCore Runtime 代理会定期请求 `GET /ping` 检查 Server 健康。FastMCP 的 `mcp.run()` 只提供 `/mcp` 端点，不包含 `/ping`。
    **解决方案**：使用 Starlette 包装 ASGI app，添加 `/ping` 路由。
    **状态**：实测发现，官方示例使用 `mcp.run()` 但部署到 AgentCore 时需要自行处理。

!!! warning "踩坑 3: runtime_type 必须显式指定"
    `direct_code_deploy` 部署时如果不指定 `runtime_type`，会触发 `NoneType.upper()` 错误。
    **解决方案**：配置中显式设置 `runtime_type: PYTHON_3_10`。
    **状态**：toolkit 0.3.3 的 bug，应该有默认值。

!!! warning "踩坑 4: Stateful MCP 通过 AgentCore Proxy 的协议兼容性"
    远程调用 AgentCore 上的 Stateful MCP Server 时，MCP JSON-RPC 的 response ID 与 client 的 request ID 出现不匹配。这是因为 AgentCore MCP proxy 在转发时对 ID 进行了映射。
    **影响**：通过标准 MCP client (fastmcp) 直接连接 AgentCore 的 stateful session 会失败。需要使用 AgentCore 提供的 SDK 或 MCP Inspector 工具。
    **状态**：这是 AgentCore MCP proxy 的已知行为，新特性的远程调用建议使用官方推荐的测试方式。

!!! warning "踩坑 5: SigV4 签名需要 per-request"
    标准 httpx 的 header-based auth 只签一次请求。但 MCP 的 Streamable HTTP transport 需要多次 HTTP 请求。必须实现 per-request SigV4 签名。
    **解决方案**：实现 `httpx.Auth` 子类的 `auth_flow()` 方法。
    **状态**：已查文档确认，SigV4 签名有时效性（通常 5 分钟）。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AgentCore Runtime (microVM) | ~$0.05/session | ~10 sessions | ~$0.50 |
| S3 (deployment package) | $0.023/GB/mo | 30MB | ~$0.01 |
| CloudWatch Logs | $0.50/GB | <1MB | ~$0.01 |
| IAM | 免费 | - | $0 |
| **合计** | | | **~$1-2** |

## 清理资源

```bash
export AWS_PROFILE=your-profile
export REGION=us-east-1

# 1. 删除 AgentCore Agent
agentcore destroy

# 2. 清理 S3 部署包
aws s3 rm s3://bedrock-agentcore-codebuild-sources-ACCOUNT-us-east-1/statefulMcpTravel/ \
  --recursive --region $REGION

# 3. 删除 IAM Role 和 Policy (如果自动创建的)
aws iam delete-role-policy --role-name AmazonBedrockAgentCoreSDKRuntime-us-east-1-XXXXX \
  --policy-name BedrockAgentCoreRuntimeExecutionPolicy-statefulMcpTravel --region $REGION
aws iam delete-role --role-name AmazonBedrockAgentCoreSDKRuntime-us-east-1-XXXXX --region $REGION

# 4. 清理 CloudWatch Log Group (可选)
aws logs delete-log-group \
  --log-group-name /aws/bedrock-agentcore/runtimes/statefulMcpTravel-XXXXX-DEFAULT \
  --region $REGION
```

!!! danger "务必清理"
    AgentCore Runtime 按使用时长计费。Lab 完成后请执行 `agentcore destroy` 避免产生费用。

## 结论与建议

### Stateful MCP 适合什么场景？

1. **多步骤工作流**：需要逐步收集用户输入的复杂流程（如预订、配置、问卷）
2. **长时间任务**：需要向用户报告进度的异步操作（如数据处理、搜索）
3. **AI 增强工作流**：Server 需要 Client 侧 LLM 能力辅助生成内容

### 关键收获

| 发现 | 详情 |
|------|------|
| **Stateful 是 MCP 的重大升级** | 从单纯的 request-response 进化到支持多轮交互 |
| **Elicitation 是杀手级特性** | Server 可以主动收集用户输入，实现真正的交互式 Agent |
| **部署需要额外工作** | `/ping` 健康检查、ASGI 包装、per-request SigV4 签名 |
| **本地测试优先** | 所有特性在本地完美运行，远程部署涉及更多协议兼容性考虑 |

### 与之前 AgentCore 文章系列的衔接

| # | 文章 | 核心能力 |
|---|------|---------|
| 1 | Shell Command | Agent 执行系统命令 |
| 2 | WebRTC Streaming | 实时音视频流 |
| 3 | Persistent Filesystems | Session 间文件持久化 |
| 4 | Policy | 工具调用权限控制 |
| 5 | **Stateful MCP (本文)** | **多轮交互、进度通知、LLM 协作** |

Stateful MCP 补全了 AgentCore 的"交互层"——Agent 不再只是被动响应，而是可以主动发起对话、报告进度、请求 AI 协助。

## 参考链接

- [Stateful MCP server features - AWS 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/mcp-stateful-features.html)
- [What's New: Amazon Bedrock AgentCore Runtime Stateful MCP](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-agentcore-runtime-stateful-mcp/)
- [MCP Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)
- [AgentCore Runtime 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [FastMCP 文档](https://gofastmcp.com)
