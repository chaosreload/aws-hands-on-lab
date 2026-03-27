# Amazon Bedrock Server-Side Custom Tools 实测：用 Lambda 实现 MCP 协议的服务端工具调用

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $5（推理 token + Lambda 执行）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

!!! tip "系列文章"
    本文是 Bedrock Responses API 系列的进阶篇。如果你还没有了解 Responses API 的基础功能（有状态对话、多模型支持），建议先阅读 [Bedrock Responses API 实战](../bedrock/bedrock-responses-api.md)。

## 背景

Amazon Bedrock 的 Responses API 支持两种 Tool Calling 模式：

- **Client-Side**：模型返回 `function_call` → 客户端执行工具 → 将结果发回模型 → 模型生成最终回复（需 2 次 API 调用）
- **Server-Side**：客户端将 Lambda ARN 传给 Bedrock → Bedrock 直接调用 Lambda → 获取结果 → 传回模型（1 次 API 调用）

Server-Side 模式的优势很明显：**减少网络 round-trip、简化客户端代码、工具执行在 AWS 安全边界内**。但这个 2026 年 1 月发布的新功能实际表现如何？

**本文将回答三个问题**：

1. 如何用 Lambda 实现 MCP 协议供 Bedrock Server-Side 调用？
2. Server-Side vs Client-Side 的实际延迟和代码复杂度差异？
3. 多工具、错误处理等边界场景表现如何？

## 前置条件

- AWS 账号，具备 Lambda 和 Bedrock 权限
- AWS CLI v2 已配置
- Python 3.10+，已安装 `openai` SDK（`pip install openai`）
- Bedrock API Key（[创建方法见文档](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html)）

## 核心概念

### Server-Side Tool Calling 架构

```
                     ① 请求（含 Lambda ARN）
┌──────────┐  ──────────────────────────>  ┌────────────────┐
│  客户端   │                               │   Bedrock      │
│ (1次调用) │  <──────────────────────────  │   Mantle       │
└──────────┘     ⑤ 最终回复                 │                │
                                            │  ② tools/list  │
                                            │  ③ tools/call  │
                                            │  ④ 结果→模型    │
                                            └───────┬────────┘
                                                    │ ②③
                                                    ▼
                                            ┌──────────────┐
                                            │   Lambda      │
                                            │  (MCP 协议)   │
                                            └──────────────┘
```

### Lambda 必须实现的 MCP 协议

Bedrock 要求 Lambda 函数实现 JSON-RPC 2.0 格式的 MCP (Model Context Protocol) 两个方法：

| 方法 | 用途 | 调用时机 |
|------|------|---------|
| `tools/list` | 返回工具定义（名称、描述、参数 schema） | 每次请求开始时 |
| `tools/call` | 执行具体工具逻辑 | 模型决定调用工具时 |

### 关键约束

| 约束 | 说明 |
|------|------|
| 支持模型 | 仅 GPT-OSS 20B / 120B |
| 工具类型 | `function`（client-side）或 `mcp`（server-side） |
| 端点 | bedrock-mantle.{region}.api.aws |
| 认证 | Bedrock API Key（Bearer Token）|
| 计费 | 仅 token 费用，无额外工具调用费 |

## 动手实践

### Step 1: 创建 Lambda 执行角色

```bash
# 创建 IAM 角色
aws iam create-role \
  --role-name bedrock-tool-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region us-east-1

# 附加基础执行策略
aws iam attach-role-policy \
  --role-name bedrock-tool-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### Step 2: 创建 MCP 协议的 Lambda 函数

编写 Lambda 函数 `lambda_function.py`：

```python
import json

def lambda_handler(event, context):
    method = event.get("method")
    params = event.get("params", {})
    request_id = event.get("id")

    # Bedrock 首先调用 tools/list 发现可用工具
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "get_most_popular_song",
                        "description": "Get the most popular song on a radio station",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "station_name": {
                                    "type": "string",
                                    "description": "Radio station name"
                                }
                            },
                            "required": ["station_name"]
                        }
                    }
                ]
            }
        }

    # Bedrock 在模型决定使用工具后调用 tools/call
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name == "get_most_popular_song":
            stations = {
                "Radio Free Mars": "Starman by David Bowie (1247 plays)",
                "Neo Tokyo FM": "Plastic Love by Mariya Takeuchi (892 plays)",
                "Cloud Nine Radio": "Blinding Lights by The Weeknd (2103 plays)",
            }
            station = arguments.get("station_name", "")
            result = stations.get(station, "Station not found: " + station)

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result}]
                }
            }

    # 未知方法返回错误
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "Method not found"}
    }
```

部署 Lambda：

```bash
# 打包并部署
zip function.zip lambda_function.py

aws lambda create-function \
  --function-name bedrock-tool-song \
  --runtime python3.12 \
  --role arn:aws:iam::<ACCOUNT_ID>:role/bedrock-tool-lambda-role \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://function.zip \
  --timeout 30 \
  --region us-east-1
```

### Step 3: 授予 Bedrock 调用权限

```bash
aws lambda add-permission \
  --function-name bedrock-tool-song \
  --statement-id bedrock-invoke \
  --action lambda:InvokeFunction \
  --principal bedrock.amazonaws.com \
  --region us-east-1
```

### Step 4: Server-Side 调用（一次 API 请求完成）

```python
from openai import OpenAI

client = OpenAI(
    api_key="<YOUR_BEDROCK_API_KEY>",
    base_url="https://bedrock-mantle.us-east-1.api.aws/v1"
)

resp = client.responses.create(
    model="openai.gpt-oss-120b",
    max_tool_calls=2,  # 重要：限制工具调用次数
    tools=[{
        "type": "mcp",
        "server_label": "song_service",
        "connector_id": "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:bedrock-tool-song",
        "require_approval": "never",
    }],
    input="What is the most popular song on Radio Free Mars?",
)

# 检查输出
for item in resp.output:
    if item.type == "mcp_list_tools":
        print("Discovered tools:", [t.name for t in item.tools])
    elif item.type == "mcp_call":
        print(f"Tool call: {item.name} [{item.status}]")
        print(f"  Output: {item.output}")
```

实际输出：

```
Discovered tools: ['get_most_popular_song']
Tool call: get_most_popular_song [completed]
  Output: {"content":[{"type":"text","text":"Starman by David Bowie (1247 plays)"}]}
Tool call: get_most_popular_song [completed]
  Output: {"content":[{"type":"text","text":"Starman by David Bowie (1247 plays)"}]}
Tool call: get_most_popular_song [incomplete]
  Output: None
```

!!! warning "注意"
    GPT-OSS 模型在 server-side 模式下会循环调用工具，不会生成最终文本回复。`max_tool_calls` 参数用于限制循环次数。详见踩坑记录。

### Step 5: Client-Side 对比（两次 API 请求）

```python
import json

# Step 1: 让模型决定是否调用工具
resp1 = client.responses.create(
    model="openai.gpt-oss-120b",
    tools=[{
        "type": "function",
        "name": "get_most_popular_song",
        "description": "Get the most popular song on a radio station",
        "parameters": {
            "type": "object",
            "properties": {
                "station_name": {"type": "string"}
            },
            "required": ["station_name"]
        }
    }],
    input="What is the most popular song on Radio Free Mars?",
)

# Step 2: 客户端执行工具，发送结果
for item in resp1.output:
    if item.type == "function_call":
        tool_result = "Starman by David Bowie (1247 plays this week)"
        resp2 = client.responses.create(
            model="openai.gpt-oss-120b",
            previous_response_id=resp1.id,
            input=[{
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": tool_result
            }],
        )
        print("Final answer:", resp2.output_text)
```

输出：

```
Final answer: The most popular song on Radio Free Mars right now is
**"Starman" – David Bowie**, which has been played 1,247 times this week.
```

## 测试结果

### Server-Side vs Client-Side 对比

| 维度 | Server-Side (MCP) | Client-Side (function) |
|------|-------------------|----------------------|
| API 调用次数 | 1 次 | 2 次 |
| 总延迟 | 2.84s (120b) / 1.98s (20b) | 1.61s (1.06 + 0.55) |
| 最终文本输出 | ❌ 无（模型循环调用） | ✅ 完整文本回复 |
| 代码行数 | ~10 行 | ~25 行 |
| 工具执行位置 | AWS 服务端 | 客户端本地 |

### 多工具发现测试

同时注册两个 Lambda（song_service + weather_service）：

| 检查点 | 结果 |
|--------|------|
| 两个工具均被 mcp_list_tools 发现 | ✅ |
| 模型根据问题选择正确工具 | ⚠️ 选择了 weather 但循环调用 |
| 并行工具调用 | ❌ 未观察到 |

### 错误处理测试

| 场景 | 行为 | 结果 |
|------|------|------|
| Lambda 超时（3s timeout + 60s sleep） | mcp_call status=`failed` | ✅ 优雅处理 |
| Lambda 语法错误 | 返回 `invalid_prompt` + 详细错误 | ✅ 有用的错误信息 |
| 内置 notes/tasks 工具 | `unknown variant` 错误 | ❌ Bedrock 不支持 |

## 踩坑记录

!!! warning "GPT-OSS 模型工具调用无限循环（实测发现，官方未记录）"
    这是本次测试最大的发现。无论 20B 还是 120B，GPT-OSS 模型在 server-side 模式下会**无限循环调用同一工具**，始终不生成最终文本回复。

    - 不设 `max_tool_calls`：模型调用工具 40+ 次，直到命中默认限制
    - 设 `max_tool_calls=1`：调用 1 次后尝试第 2 次被截断（status=incomplete）
    - 添加明确 instructions 要求"不要重复调用"：无效
    - **对比**：同一模型的 client-side 模式完全正常，1 次工具调用后直接生成文本

    **结论**：Server-side 架构已就绪，但 GPT-OSS 模型目前无法正确完成 server-side 多轮推理循环。建议等待更多模型支持后再用于生产。

!!! warning "内置工具（notes/tasks）在 Bedrock 上不可用（实测发现，与文档不一致）"
    官方 tool-use 文档详细描述了 `notes` 和 `tasks` 两个内置工具，但 Bedrock Mantle 端点返回：
    ```
    Invalid 'tools': unknown variant `notes`, expected `function` or `mcp`
    ```
    目前 Bedrock 仅支持 `function` 和 `mcp` 两种工具类型。

!!! tip "max_tool_calls 是必要参数"
    在 GPT-OSS 模型的 server-side 模式下，不设置 `max_tool_calls` 会导致无限循环，消耗大量 token。建议始终设置一个合理上限（如 2-5）。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| GPT-OSS 120B 推理 | ~$0.006/1K tokens | ~2000 tokens | ~$0.01 |
| GPT-OSS 20B 推理 | ~$0.002/1K tokens | ~500 tokens | ~$0.001 |
| Lambda 执行 | $0.20/1M requests | ~50 requests | ~$0.00 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 删除 Lambda 函数
aws lambda delete-function --function-name bedrock-tool-song --region us-east-1
aws lambda delete-function --function-name bedrock-tool-weather --region us-east-1
aws lambda delete-function --function-name bedrock-tool-error --region us-east-1

# 删除 IAM 角色（先解绑策略）
aws iam detach-role-policy \
  --role-name bedrock-tool-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name bedrock-tool-lambda-role

# 删除 API Key（如不再需要）
aws iam delete-service-specific-credential \
  --user-name <USERNAME> \
  --service-specific-credential-id <CREDENTIAL_ID>
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。Lambda 函数即使不调用也可能产生 CloudWatch Logs 存储费用。

## 结论与建议

### Server-Side Custom Tools 现状评估

| 方面 | 评价 |
|------|------|
| 技术架构 | ✅ 完善 — MCP 协议、Lambda 集成、错误处理均就绪 |
| 模型支持 | ❌ 不足 — 仅 GPT-OSS，且存在循环调用 bug |
| 内置工具 | ❌ 不可用 — notes/tasks 文档有描述但未实现 |
| 生产就绪 | ⚠️ 尚未 — 等待更多模型支持 |

### 何时用 Server-Side vs Client-Side？

**现阶段建议使用 Client-Side**：

- GPT-OSS server-side 有循环 bug，无法可靠生成最终回复
- Client-side 延迟更低（1.61s vs 2.84s）且输出稳定
- Client-side 支持所有 Mantle 上的 38 个模型

**Server-Side 的未来价值**（等模型支持修复后）：

- 工具执行在 AWS 安全边界内，适合访问 VPC 内部资源
- 客户端代码极简（10 行 vs 25 行）
- 支持 AgentCore Gateway 集成，实现集中化工具管理

### 与已有 Responses API 文章的关系

| Responses API 基础篇 | 本文（Server-Side Tools 进阶篇） |
|------|------|
| API 入门、有状态对话 | Server-Side Tool Calling 深度实测 |
| 38 个模型验证 | GPT-OSS 工具调用行为分析 |
| previous_response_id 链式调用 | MCP 协议 Lambda 实现 |

## 参考链接

- [Bedrock Tool Use 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html)
- [Bedrock Mantle (OpenAI APIs) 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-bedrock-server-side-custom-tools-responses-api/)
- [OpenAI Function Calling 指南](https://platform.openai.com/docs/guides/function-calling)
- [OpenAI MCP Connectors 指南](https://platform.openai.com/docs/guides/tools-connectors-mcp)
