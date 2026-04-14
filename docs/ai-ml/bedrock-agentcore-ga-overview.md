---
tags:
  - AgentCore
  - What's New
---

# Amazon Bedrock AgentCore GA 全景实战：从零到生产的完整体验

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-25

## 背景

Amazon Bedrock AgentCore 于 2025 年 7 月 Preview，2025 年 10 月正式 GA。它是 AWS 为 AI Agent 提供的完整平台，覆盖从构建、部署到运维的全生命周期。

GA 版本带来了一系列企业级增强：VPC 支持、PrivateLink、CloudFormation、A2A 协议、自管理 Memory 策略等。本文通过 **实际动手操作**，带你快速体验 AgentCore 的核心服务——从 CLI 创建第一个 Agent，到 Gateway 工具集成、Memory 配置和 Observability 监控。

!!! tip "系列文章"
    本文是 AgentCore 系列的总览篇。如果你对单一服务有兴趣，可以参考：

    - [Runtime Shell Command 实战](../agentcore-runtime-shell-command/)
    - [Runtime WebRTC 双向流实战](../agentcore-webrtc-streaming/)
    - [Policy Cedar 策略实战](../bedrock-agentcore-policy-ga/)
    - [Session Storage 持久化文件系统实测](../agentcore-persistent-filesystems/)

## 前置条件

- AWS 账号（需要 Bedrock、IAM、Lambda、CloudWatch 权限）
- Python 3.10+
- AWS CLI v2 已配置

## 核心概念

### AgentCore 九大服务一览

AgentCore 不是单一服务，而是一个 **模块化平台**，包含 9 个可独立或组合使用的服务：

| 服务 | 功能 | GA 状态 |
|------|------|---------|
| **Runtime** | 安全的 Serverless Agent 运行环境（microVM 隔离、8 小时执行窗口）| ✅ GA |
| **Memory** | 短期记忆（会话内）+ 长期记忆（跨会话知识提取）| ✅ GA |
| **Gateway** | 将 API/Lambda/MCP Server 转为 Agent 可用的工具端点 | ✅ GA |
| **Identity** | Agent 身份认证与授权（OAuth、Cognito、Okta 等）| ✅ GA |
| **Code Interpreter** | 隔离的代码执行沙箱（Python/JS/TS）| ✅ GA |
| **Browser** | 云端浏览器，让 Agent 操作网页 | ✅ GA |
| **Observability** | 基于 CloudWatch + OTEL 的全链路追踪 | ✅ GA |
| **Policy** | 基于 Cedar 策略语言的 Agent 工具调用控制 | ✅ GA |
| **Evaluations** | LLM-as-a-Judge 自动评估 Agent 质量 | ⚠️ Preview |

### Preview → GA 关键变化

| 维度 | Preview | GA |
|------|---------|-----|
| 网络安全 | 公网模式 | + VPC、PrivateLink、CloudFormation |
| 协议支持 | MCP | + A2A（Agent-to-Agent）协议 |
| Memory | 托管策略 | + self-managed 策略（自定义管线）|
| Gateway 认证 | OAuth only | + IAM 授权 |
| Observability | 基础日志 | + CloudWatch Dashboard、OTEL 兼容 |
| 定价 | 免费 Preview | 按使用量计费（I/O wait 不计费）|
| Region | 9 个 | 14-15 个（按服务不同）|

### 支持的框架和模型

AgentCore 的核心理念是**框架无关 + 模型无关**：

- **框架**：Strands Agents、LangGraph、CrewAI、LlamaIndex、Google ADK、OpenAI Agents SDK
- **模型**：Amazon Bedrock（Claude、Nova、Llama）、OpenAI、Gemini、Anthropic 直连
- **协议**：HTTP、MCP（Model Context Protocol）、A2A（Agent-to-Agent）

## 动手实践

### Step 1: 安装 AgentCore Starter Toolkit

```bash
pip install bedrock-agentcore-starter-toolkit
```

安装后可以使用 `agentcore` CLI。GA 版本包含 17 个子命令：

```
agentcore --help
```

输出关键子命令：

| 命令 | 功能 |
|------|------|
| `create` | 创建 Agent 项目（支持选择框架/模型/IaC）|
| `dev` | 本地开发服务器（热重载）|
| `deploy` | 部署到 AgentCore Runtime（无需 Docker）|
| `invoke` | 调用已部署的 Agent |
| `status` | 查看部署状态 |
| `destroy` | 销毁资源 |
| `gateway` | 管理 MCP Gateway |
| `memory` | 管理 Memory 资源 |
| `obs` | 查询 Observability 数据 |
| `policy` | 管理 Cedar 策略引擎 |
| `eval` | 运行 Agent 评估 |

### Step 2: 创建第一个 Agent

```bash
# 创建一个基于 Strands 框架 + Bedrock 模型的 Agent
# 注意：项目名只支持纯字母数字（无 - 或 _）
agentcore create \
  -p strandsbasic \
  -t basic \
  --agent-framework Strands \
  --model-provider Bedrock \
  --non-interactive \
  --no-venv
```

!!! warning "踩坑：项目名限制"
    项目名只允许字母数字（a-z, 0-9），**不支持** `-` 和 `_`。如果使用了会报错：
    `Invalid value: Project must only contain alphanumeric characters (no '-' or '_') up to 36 chars.`
    **已查文档确认**，这是 Starter Toolkit 的设计限制。

创建后的项目结构：

```
strandsbasic/
├── .bedrock_agentcore.yaml   # 部署配置
├── pyproject.toml             # 依赖管理
├── src/
│   ├── main.py               # Agent 入口
│   ├── mcp_client/client.py  # MCP 客户端
│   └── model/load.py         # 模型加载
└── test/
    └── test_main.py          # 测试
```

生成的 Agent 代码（`src/main.py`）关键部分：

```python
from strands import Agent, tool
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@tool
def add_numbers(a: int, b: int) -> int:
    """Return the sum of two numbers"""
    return a + b

@app.entrypoint
async def invoke(payload, context):
    # 自动集成 Code Interpreter + MCP 工具
    code_interpreter = AgentCoreCodeInterpreter(
        region=REGION,
        session_name=session_id,
        auto_create=True,
        persist_sessions=True
    )

    agent = Agent(
        model=load_model(),
        tools=[code_interpreter.code_interpreter, add_numbers] + tools
    )

    stream = agent.stream_async(payload.get("prompt"))
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]
```

默认模板**自动集成**了 Code Interpreter 和 MCP 客户端（含 Exa 网页搜索），开箱即可代码执行 + 联网搜索。

#### 对比：LangGraph 框架模板

```bash
agentcore create \
  -p langgraphbasic \
  -t basic \
  --agent-framework LangChain_LangGraph \
  --model-provider Bedrock \
  --non-interactive \
  --no-venv
```

LangGraph 模板更简洁，使用 `create_agent()` 和 `graph.ainvoke()` 模式：

```python
from langchain.agents import create_agent

graph = create_agent(llm, tools=tools + [add_numbers])
result = await graph.ainvoke({"messages": [HumanMessage(content=prompt)]})
```

### Step 3: 部署到 AgentCore Runtime

```bash
cd strandsbasic

# 部署到云端（Direct Code Deploy，无需 Docker）
agentcore deploy -auc
```

!!! tip "三种部署模式"
    - `agentcore deploy`：**推荐**，直接上传 Python 代码到 Runtime
    - `agentcore deploy --local`：本地运行（需要 Docker）
    - `agentcore deploy --local-build`：本地构建 + 云端部署

部署过程约 **90 秒**，自动完成以下操作：

1. 创建 IAM 执行角色
2. 解析依赖并打包（约 51 MB）
3. 上传到 S3
4. 创建 Runtime Agent
5. 配置 CloudWatch 日志 + X-Ray Trace + OTEL

```
✅ Deployment completed successfully
Agent ARN: arn:aws:bedrock-agentcore:us-west-2:595842667825:runtime/strandsbasic_Agent-hDMwKQHUz8
```

### Step 4: 调用 Agent

```bash
# 简单调用
agentcore invoke '{"prompt": "What is 2+3? Use the add_numbers tool."}'
```

输出：
```
The result of 2 + 3 is **5**.
```

Agent 自动识别了 `add_numbers` 工具并调用。再试一个需要 Code Interpreter 的复杂任务：

```bash
agentcore invoke '{"prompt": "Calculate the factorial of 7 using code."}'
```

输出：
```
7! = 5,040
```

Agent 自动调用了 Code Interpreter，在隔离的沙箱中执行 Python 代码并返回结果。

### Step 5: 创建 Memory

```bash
# 创建带语义提取 + 摘要两种长期记忆策略的 Memory
agentcore memory create agentcoregamemory \
  --strategies '[{"semanticMemoryStrategy": {"name": "UserPreferences"}}, {"summaryMemoryStrategy": {"name": "SessionSummary"}}]' \
  --wait
```

Memory 创建耗时约 **5 分钟**（创建向量索引等底层资源）。

!!! warning "踩坑：Memory 创建超时"
    默认 `--wait` 超时为 300 秒，但 Memory 创建可能超过这个时间。如果超时了不用担心，Memory 仍在后台创建中。
    用 `agentcore memory list` 检查状态。**实测发现，官方未记录**超时默认值。

```bash
# 检查状态
agentcore memory list
```

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Memory ID                    ┃ Status     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ agentcoregamemory-P3pbrv5Lbw │ ✓ ACTIVE   │
└──────────────────────────────┴────────────┘
```

查看 Memory 详情，确认两个策略都已激活：

```bash
agentcore memory get agentcoregamemory-P3pbrv5Lbw
```

```
Strategies (2):
  • SessionSummary (SUMMARIZATION)
  • UserPreferences (SEMANTIC)
```

### Step 6: 创建 Gateway（将 Lambda 转为 MCP 工具）

```bash
# 一键创建 MCP Gateway（自动配置 Cognito 认证）
agentcore gateway create-mcp-gateway \
  --name agentcoregagw \
  --region us-west-2
```

Gateway CLI 自动完成一整条认证链路：

1. 创建 Cognito User Pool + 域名
2. 创建 Resource Server + Client
3. 等待 DNS 传播
4. 创建 Gateway 并等待就绪
5. 配置 Observability（日志 + Trace）

```
✅ Gateway is ready
Gateway URL: https://agentcoregagw-e3dbfexdum.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp
```

添加 Lambda 工具作为 Gateway Target：

```bash
agentcore gateway create-mcp-gateway-target \
  --gateway-arn "arn:aws:bedrock-agentcore:us-west-2:595842667825:gateway/agentcoregagw-e3dbfexdum" \
  --gateway-url "https://agentcoregagw-e3dbfexdum.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp" \
  --role-arn "arn:aws:iam::595842667825:role/AgentCoreGatewayExecutionRole" \
  --name "weathertool" \
  --target-type lambda \
  --region us-west-2
```

CLI 自动创建示例 Lambda 并配置权限。Target 创建后约 5 秒就绪：

```
✅ Target is ready (ID: QJAITK7JVE)
```

### Step 7: 查看 Observability 数据

部署时已自动配置 OTEL instrumentation。CloudWatch 中可以看到两类日志流：

**Runtime Logs**（`[runtime-logs-{sessionId}]`）：
```
Tool #1: add_numbers
Tool #1: code_interpreter
```

**OTEL Telemetry Logs**（`otel-rt-logs`）：完整的 OpenTelemetry span 数据，包含：

- `gen_ai.system: aws.bedrock` — 模型调用追踪
- `strands.event_loop.cycle_count` — Agent 推理循环次数
- `http.server.duration` — 请求延迟指标
- 完整的工具调用链：输入 → 工具结果 → LLM 响应

```json
{
  "http.server.duration": {"Sum": 5169, "Count": 1},
  "http.server.request.size": {"Sum": 52, "Count": 1},
  "http.status_code": "200"
}
```

!!! warning "踩坑：OTEL span export 偶尔 400"
    在 OTEL logs 中会看到 `Failed to export span batch code: 400` 错误。
    但 **数据仍然到达 CloudWatch Logs 和 Metrics**，不影响功能。
    **实测发现，官方未记录**。可能是 OTEL exporter 版本与后端协议的兼容性问题。

### Step 8: 体验 Evaluations（Preview）

AgentCore Evaluations 提供 13 个内置评估器：

```bash
agentcore eval evaluator list
```

| 评估器 | 级别 | 用途 |
|--------|------|------|
| Builtin.Helpfulness | TRACE | 用户视角评估回复有用性 |
| Builtin.Correctness | TRACE | 事实准确性 |
| Builtin.ToolSelectionAccuracy | TOOL_CALL | 工具选择准确性 |
| Builtin.ToolParameterAccuracy | TOOL_CALL | 工具参数准确性 |
| Builtin.GoalSuccessRate | SESSION | 会话目标达成率 |
| Builtin.Harmfulness | TRACE | 有害内容检测 |
| Builtin.Faithfulness | TRACE | 来源忠实度 |
| ... | | 共 13 个 |

!!! note "Preview 限制"
    Evaluations 目前仅在 4 个 Region 可用（us-east-1、us-west-2、eu-central-1、ap-southeast-2）。

## 测试结果

### 全链路耗时

| 操作 | 耗时 | 说明 |
|------|------|------|
| `agentcore create` | ~3s | 项目模板生成 |
| `agentcore deploy` | ~90s | 含依赖解析、打包、上传、Runtime 创建 |
| 首次 `invoke`（冷启动）| ~5s | microVM 启动 + Agent 初始化 |
| 后续 `invoke`（热调用）| ~2-3s | LLM 推理时间为主 |
| Memory 创建 | ~5min | 含向量索引创建 |
| Gateway 创建 | ~60s | 含 Cognito 域名 DNS 传播 |

### 部署包大小

| 框架 | 包大小 |
|------|--------|
| Strands（含 Code Interpreter + MCP）| 51.55 MB |

### OTEL 指标

| 指标 | 值 |
|------|-----|
| http.server.duration（Tool Use 场景）| ~5,169 ms |
| Bedrock API 调用延迟 | ~104-113 ms |
| Agent 推理循环 | 2 cycles（prompt → tool → response）|

## 踩坑记录

!!! warning "5 个踩坑点"

    1. **项目名不支持 `-`/`_`** — 只能纯字母数字，最长 36 字符。**已查文档确认**，是 CLI 限制。

    2. **Memory 创建可能超时** — 默认 300s wait，但创建可能需要 5+ 分钟。超时后 Memory 仍在后台创建，用 `memory list` 检查。**实测发现，官方未记录**。

    3. **Session ID 最小 33 字符** — 不能用短 ID（如 "test123"），必须 UUID 格式。**已查文档确认**。

    4. **OTEL span export 偶尔 400** — 不影响 CloudWatch 数据写入，但日志中会出现错误信息。**实测发现，官方未记录**。

    5. **deploy 默认 Region** — `agentcore deploy` 使用 config 中的 region（默认可能不是你期望的 region），建议显式设置 `AWS_REGION`。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Runtime（3 次 invoke，~30s 活跃时间）| $0.0895/vCPU-hr | ~0.01 hr | ~$0.001 |
| Memory（创建 + 闲置）| 按 API 调用计费 | 极少 | ~$0.001 |
| Gateway（创建 + 测试）| $5/M InvokeTool | ~3 次 | ~$0.00 |
| Lambda | $0.20/M requests | ~5 次 | ~$0.00 |
| S3（部署包存储）| $0.023/GB-month | 51 MB | ~$0.001 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 销毁 Runtime Agent
cd /tmp/agentcore-ga-test/strandsbasic
agentcore destroy

# 2. 删除 Memory
agentcore memory delete agentcoregamemory-P3pbrv5Lbw

# 3. 删除 Gateway Target
agentcore gateway delete-mcp-gateway-target \
  --gateway-id agentcoregagw-e3dbfexdum \
  --target-id QJAITK7JVE

# 4. 删除 Gateway
agentcore gateway delete-mcp-gateway \
  --gateway-id agentcoregagw-e3dbfexdum

# 5. 删除 Lambda
aws lambda delete-function --function-name agentcore-ga-weather-tool --region us-west-2
aws lambda delete-function --function-name AgentCoreLambdaTestFunction --region us-west-2

# 6. 清理 S3 部署桶
aws s3 rm s3://bedrock-agentcore-codebuild-sources-595842667825-us-west-2 --recursive
aws s3 rb s3://bedrock-agentcore-codebuild-sources-595842667825-us-west-2

# 7. 删除 Cognito User Pool
aws cognito-idp delete-user-pool --user-pool-id us-west-2_CsxD1seNG --region us-west-2

# 8. 清理 IAM Roles（确认无其他服务依赖后）
aws iam detach-role-policy --role-name lambda-basic-execution \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name lambda-basic-execution
```

!!! danger "务必清理"
    虽然本 Lab 的费用极低（< $0.01），但 Runtime Agent 和 Gateway 如果长时间运行可能产生费用。建议 Lab 完成后立即清理。

## 结论与建议

### AgentCore GA 的核心价值

1. **极低的上手门槛**：`agentcore create` + `agentcore deploy`，从零到部署只需 **2 分钟 + 90 秒**，无需 Docker、无需管理基础设施
2. **框架和模型灵活性**：支持 6 个主流 Agent 框架和任意模型，不锁定技术栈
3. **企业级安全**：microVM 隔离、VPC/PrivateLink 支持、完整的 OTEL 可观测性
4. **模块化设计**：9 个服务可独立使用，按需组合

### 适用场景

| 场景 | 推荐服务组合 |
|------|-------------|
| 快速原型 | Runtime + Code Interpreter |
| 企业客服 Agent | Runtime + Memory + Gateway + Policy |
| 多 Agent 协作 | Runtime（A2A 协议）+ Gateway + Observability |
| Agent 工具平台 | Gateway + Identity + Policy |
| Agent 质量监控 | Observability + Evaluations |

### 生产环境建议

- **网络**：使用 VPC 模式 + PrivateLink 而非公网模式
- **认证**：通过 Identity 集成企业 IdP（Okta、Entra ID、Cognito）
- **治理**：用 Policy（Cedar 策略）控制 Agent 可调用的工具范围
- **监控**：启用 Observability，对接 CloudWatch 告警 + 第三方 APM
- **评估**：使用 Evaluations 建立 Agent 质量基线，部署前/后持续评估

## 参考链接

- [Amazon Bedrock AgentCore 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available/)
- [GA 博客文章](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-is-now-generally-available/)
- [AgentCore Starter Toolkit](https://github.com/aws/bedrock-agentcore-starter-toolkit)
- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [AgentCore Region 可用性](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
