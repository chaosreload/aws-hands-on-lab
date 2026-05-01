---
tags:
  - Bedrock
  - Mantle
  - OpenAI API
  - Guardrails
  - What's New
---

# Amazon Bedrock Mantle OpenAI 兼容 API 实测：支持矩阵、Claude 走哪条路、Guardrail 生效端点

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 40 分钟
    - **预估费用**: < $1（13 次左右 API 调用 + 一个临时 Guardrail）
    - **Region**: us-east-1
    - **最后验证**: 2026-05-01

## 背景

Amazon Bedrock 近期通过新端点 `bedrock-mantle.{region}.api.aws` 暴露了 **OpenAI 兼容的 Responses API 与 Chat Completions API**，目标是让已有 OpenAI SDK 代码只改 `base_url` 和 `api_key` 就能迁移。

笔者（AWS SA）在看到公告时的第一反应是："Bedrock 支持 OpenAI 兼容 API ✅，那 Claude 也能用 OpenAI SDK 调了"。这个直觉是错的——而且错得很具体。实测之后得到的结论是：

> Bedrock Mantle 提供的是一组 **OpenAI 兼容的 API 路径**，而不是一个"对所有模型都兼容 OpenAI 的统一入口"。哪些模型能走这些路径、Guardrail 在哪个端点生效、Claude Sonnet 4.6 应当怎么调用，都各自有不同的答案。

本文用 15 项实测（含反面测试）把这个支持矩阵画清楚，帮读者避开从公告到落地中间的三类误读。

## 前置条件

- AWS 账号，在目标 Region 已开通 Amazon Bedrock 并订阅所用模型
- `AmazonBedrockLimitedAccess` 或等效权限；另需 `bedrock:CreateGuardrail` / `DeleteGuardrail` 以复现 Guardrail 部分
- Python ≥ 3.10、`pip install openai boto3 aws-bedrock-token-generator requests`
- AWS CLI v2 已配置默认凭证

使用 [Bedrock short-term API key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html) 作为 `OPENAI_API_KEY`。短期 Key 有效期最长 12 小时，只能在生成它的 Region 使用。

## 核心概念

### 端点支持的 API 一览

| 端点 | Host | OpenAI Responses | OpenAI Chat Completions | Anthropic Messages | Converse/Invoke |
|---|---|---|---|---|---|
| `bedrock-mantle.{region}.api.aws` | `/v1/*`、`/anthropic/v1/messages` | ✅ | ✅ | ✅ | ❌ |
| `bedrock-runtime.{region}.amazonaws.com` | 原生路径 + `/openai/v1/*` | ❌ | ✅ | ✅ | ✅ |

来源：[Endpoints supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html)。两个端点对 Chat Completions 的 URL 前缀不同：

- Mantle：`https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions`
- Runtime：`https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions`（多一段 `/openai`）

### 模型能走哪条路径，以模型卡为准

| 模型 | Mantle Model ID | Runtime Model ID | Responses API | Chat Completions | Messages API |
|---|---|---|---|---|---|
| `openai.gpt-oss-120b` | `openai.gpt-oss-120b` | `openai.gpt-oss-120b-1:0` | ✅ | ✅ | — |
| `anthropic.claude-opus-4-7` | `anthropic.claude-opus-4-7` | `anthropic.claude-opus-4-7` | ❌ | ❌ | ✅ |
| `anthropic.claude-haiku-4-5` | `anthropic.claude-haiku-4-5` | `anthropic.claude-haiku-4-5` | ❌ | ❌ | ✅ |
| Claude Sonnet 4.6 | 未挂载 | `anthropic.claude-sonnet-4-6` / geo `us.anthropic.claude-sonnet-4-6` | ❌ | ❌ | ✅ |

来源：[gpt-oss-120b 模型卡](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html)、[Claude Sonnet 4.6 模型卡](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)。

关键点：**OpenAI 兼容 API 只给 `openai.*` 家模型使用**；Anthropic 模型在 Mantle 上走 `/anthropic/v1/messages` 的 Anthropic native 路径。Claude Sonnet 4.6 在 us-east-1 无 In-Region 部署，需用 Cross-Region inference ID `us.anthropic.claude-sonnet-4-6`。

## 动手实践

以下测试都在 us-east-1 执行。完整脚本放在文末仓库链接，下文只展示关键片段与实测输出。

### Step 1: 设置 OpenAI 客户端指向 Mantle

```python
from aws_bedrock_token_generator import provide_token
from openai import OpenAI

api_key = provide_token(region="us-east-1")  # short-term Bedrock API key

mantle = OpenAI(
    api_key=api_key,
    base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
)
```

`provide_token()` 基于默认 AWS 凭证生成 SigV4-sealed 的 bearer token；官方文档将其称为 short-term API key。

### Step 2: Responses API 基础调用

```python
resp = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "In one sentence, explain what Amazon Bedrock is."}],
)
print(resp.id, resp.status, resp.usage)
```

**实测输出**：

```
resp_dk3m5cjjg7x3g7e3lwedzorqpywkxv557yq3u4uys4vbeggdcekq  completed
Usage(input_tokens=78, output_tokens=85, total_tokens=163)
latency: 1667 ms
```

响应对象包含 30+ 字段，除了 `output`/`usage`/`status` 之外还有 `previous_response_id`、`background`、`conversation`、`reasoning`、`prompt_cache_key`、`service_tier` 等——这些字段对应了 Responses API 的 stateful、异步、推理专用能力。

### Step 3: Responses API 流式

```python
stream = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "Count from 1 to 10, one number per line."}],
    stream=True,
)
for event in stream:
    print(event.type)
```

**实测输出**（节选事件类型统计）：

```
response.created          1
response.in_progress      1
response.output_item.added ...
response.output_text.delta (多次)
response.output_text.done  1
response.completed         1
共 15 个事件，TTFB = 1340 ms，total = 1429 ms
```

Responses API 的事件流是 **语义事件**（而非 Chat Completions 的逐 token chunk），比如 `response.output_text.delta` 携带一段文本 delta、`response.completed` 标志结束。

### Step 4: Responses API 用 `previous_response_id` 串多轮

多轮不再需要把 history 拼回请求体：

```python
r1 = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "My favorite color is teal. Remember this."}],
)

r2 = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "What is my favorite color? Respond with just the color word."}],
    previous_response_id=r1.id,
)
print(r2.output[0].content[0].text)  # -> "teal."
```

三轮对话均只传入当轮用户消息 + `previous_response_id`，模型在第 2、3 轮都能正确回答 `teal`。上下文由服务端维护。

### Step 5: Responses API 调工具（OpenAI function calling 语法）

```python
tools = [{
    "type": "function",
    "name": "get_current_weather",
    "description": "Return the current weather for a given city.",
    "parameters": {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["city"],
    },
}]

r1 = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "What's the weather in Tokyo? Use the tool."}],
    tools=tools,
)
# 检视 r1.output 会看到一个 type=function_call 的输出：
#   name=get_current_weather, arguments='{"city":"Tokyo","unit":"celsius"}'

# 执行本地工具后，把结果通过 function_call_output 回传
r2 = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{
        "type": "function_call_output",
        "call_id": "<来自 r1 的 call_id>",
        "output": '{"temperature": 22, "condition": "sunny"}',
    }],
    previous_response_id=r1.id,
    tools=tools,
)
```

两轮后得到最终回答。与 Bedrock 原生 Converse 的 tool-use schema 不同，Responses API 用的是 OpenAI 一致的 `function_call` / `function_call_output` 对。

### Step 6: Responses API 后台任务

```python
created = mantle.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "Write a 150-word story about a lighthouse keeper..."}],
    background=True,
)
# created.status 立即是 "in_progress"
while True:
    r = mantle.responses.retrieve(created.id)
    if r.status in ("completed", "failed", "cancelled", "incomplete"):
        break
    time.sleep(1)
```

**实测输出**：

```
initial=in_progress  poll 次数=4  final=completed
total=6147 ms  output=880 tokens
```

适用于长耗时 prompt、要做 fire-and-forget 的场景。`retrieve` 也可配合 webhook 消费。

### Step 7: 两端点 Chat Completions 对比

对同一个 prompt `"Reply with the single word: OK"` 分别打两端点：

| 端点 | Model ID | latency | prompt_tokens | completion_tokens | response.id 前缀 |
|---|---|---|---|---|---|
| `bedrock-mantle/v1` | `openai.gpt-oss-120b` | 1245 ms | 84 | 35 | `chatcmpl-` |
| `bedrock-runtime/openai/v1` | `openai.gpt-oss-120b-1:0` | 1301 ms | 84 | 32 | `chatcmpl-` |

响应 JSON 顶层字段一致（`id`/`object`/`created`/`model`/`choices`/`usage`/`service_tier`/`system_fingerprint`）。**注意模型 ID 不同**：Mantle 是 `openai.gpt-oss-120b`；Runtime 是 `openai.gpt-oss-120b-1:0`（带 `-1:0` 版本后缀）。

样本量 N=1，延迟数字仅作示意，不作 SLA 论断。

### Step 8: 反面测试——OpenAI SDK + Claude model ID

大多数使用者最容易掉进的坑是直接把 Claude Sonnet 4.6 的 model ID 替换到 OpenAI 客户端里。实测三组组合：

**8a. Mantle + Responses API + Claude 4.6：**

```python
mantle.responses.create(model="us.anthropic.claude-sonnet-4-6", input=[...])
```

报错：

```http
HTTP 404
{"error":{"code":"not_found_error","type":"invalid_request_error",
 "message":"The model 'us.anthropic.claude-sonnet-4-6' does not exist"}}
```

**8b. Mantle + Chat Completions + Claude 4.6：** 同样 404 / `not_found_error` / 同一措辞。

**8c. Runtime + Chat Completions + Claude 4.6：**

```http
HTTP 404
{"error":{"code":"model_not_found","type":"not_found_error",
 "message":"The model doesn't exist or doesn't support this API. Retry your request with a different model ID."}}
```

两端点返回的 **错误 code 和 message 都不同**：Mantle 版是"model does not exist"（强调目录缺席），Runtime 版是"doesn't exist or doesn't support this API"（兼顾两种原因）。

### Step 9: Claude 走 Bedrock 原生 API（对照基线）

```python
import boto3, json

client = boto3.client("bedrock-runtime", region_name="us-east-1")

# Converse
converse = client.converse(
    modelId="us.anthropic.claude-sonnet-4-6",
    messages=[{"role": "user", "content": [{"text": "Reply with the single word: OK"}]}],
    inferenceConfig={"maxTokens": 20},
)
# latency ≈ 1716 ms, usage input=14, output=4

# Invoke（Anthropic Messages JSON）
invoke = client.invoke_model(
    modelId="us.anthropic.claude-sonnet-4-6",
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "max_tokens": 20,
    }),
    accept="application/json",
    contentType="application/json",
)
# latency ≈ 1487 ms, 同样的 14/4 tokens
```

两条路径等价，Converse 返回 Bedrock 统一的 schema（含 `metrics.latencyMs`），Invoke 保留 Anthropic 原生的 `content[]` + `usage.cache_read_input_tokens` 等字段。

### Step 10: Mantle 上的 Claude 模型只能走 Anthropic Messages

Mantle `/v1/models` 确实列出了 Claude Opus 4.7 和 Haiku 4.5（但没有 Sonnet 4.6）：

```bash
curl -s -H "Authorization: Bearer $BEDROCK_API_KEY" \
  https://bedrock-mantle.us-east-1.api.aws/v1/models | jq '.data[].id'
```

**实测**：40 个模型，包含 `anthropic.claude-haiku-4-5`、`anthropic.claude-opus-4-7`、`openai.gpt-oss-120b` 等；**无 `anthropic.claude-sonnet-4-6`**。

对这些 Anthropic 模型使用 OpenAI API：

```python
mantle.responses.create(model="anthropic.claude-opus-4-7", input=[...])
# HTTP 400 validation_error
# "The model 'anthropic.claude-opus-4-7' does not support the '/v1/responses' API"

mantle.chat.completions.create(model="anthropic.claude-opus-4-7", messages=[...])
# HTTP 400 validation_error
# "The model 'anthropic.claude-opus-4-7' does not support the '/v1/chat/completions' API"
```

走 Mantle 的 Anthropic native 路径则可以：

```bash
curl -X POST https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages \
  -H "Authorization: Bearer $BEDROCK_API_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: bedrock-2023-05-31" \
  -d '{"model":"anthropic.claude-opus-4-7","max_tokens":20,
       "messages":[{"role":"user","content":"Reply with OK."}]}'
# HTTP 200
```

### Step 11: Guardrail 在两端点的行为差异

在 Console 或 CLI 创建一个开启 VIOLENCE/HATE 过滤的 Guardrail：

```bash
aws bedrock create-guardrail --region us-east-1 \
  --cli-input-json file://guardrail.json
# 返回 guardrailId=9bq7insnsizo, version=DRAFT
```

同 prompt（一段含仇恨内容的翻译请求）分别打两端点，带上 Guardrail header：

```python
for base, model in [
    ("https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "openai.gpt-oss-120b-1:0"),
    ("https://bedrock-mantle.us-east-1.api.aws/v1",               "openai.gpt-oss-120b"),
]:
    c = OpenAI(api_key=api_key, base_url=base)
    r = c.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": hate_prompt}],
        extra_headers={
            "X-Amzn-Bedrock-GuardrailIdentifier": "9bq7insnsizo",
            "X-Amzn-Bedrock-GuardrailVersion": "DRAFT",
            "X-Amzn-Bedrock-Trace": "ENABLED",
        },
    )
```

**实测对比**：

| 端点 | response.id 前缀 | usage.prompt_tokens | content | 解读 |
|---|---|---|---|---|
| `bedrock-runtime` | `bedrock-guardrails-c64164bf` | **0** | "INPUT_BLOCKED: This request was blocked by a guardrail." | Guardrail 前置拦截，未进入模型，无 token 计费 |
| `bedrock-mantle` | `chatcmpl-1e27ced2...` | **86** | "I'm sorry, but I can't help with that." | 请求直达模型，模型自带安全拒绝，token 正常计费 |

对照 gpt-oss-120b 的[官方模型卡](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html)："Features supported using `bedrock-mantle` endpoint" 一栏**未列出 Guardrails**，"Features supported using `bedrock-runtime` endpoint" 一栏明确列 Yes Guardrails。文档与实测一致：Guardrail header 只在 `bedrock-runtime` 生效。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T01 | Mantle Responses 基础 | ✅ | 78/85 tokens, 1667 ms | 响应含 30+ 字段 |
| T02 | Mantle Responses streaming | ✅ | TTFB 1340 ms, 15 events | 语义事件流 |
| T03 | Mantle Responses stateful (`previous_response_id`) | ✅ | 3 轮均正确召回上下文 | 服务端维护 history |
| T04 | Mantle Responses tool use | ✅ | 1 次 function_call → function_call_output | OpenAI 原生语法 |
| T05 | Mantle Responses background | ✅ | 4 次 poll, total 6147 ms | 异步流程跑通 |
| T06 | Mantle Chat Completions | ✅ | 84/35 tokens, 1245 ms | 基线 |
| T07 | Runtime Chat Completions | ✅ | 84/32 tokens, 1301 ms | Model ID 带 `-1:0` 后缀 |
| T08a | Runtime + Guardrail 触发 | ✅ | id=`bedrock-guardrails-*`, 0 token | 前置拦截 |
| T08b | Runtime + Guardrail 放行 | ✅ | 正常回答 | |
| T09a | Mantle + Guardrail header + 触发 prompt | ⚠️ | id=`chatcmpl-*`, 86 prompt tokens 计费 | Header 未生效 |
| T09b | Mantle + Guardrail header + 安全 prompt | ✅ | 正常回答 | |
| T10 | Claude 4.6 Converse | ✅ | 14/4 tokens, 1716 ms | 基线 |
| T10b | Claude 4.6 Invoke (Messages JSON) | ✅ | 14/4 tokens, 1487 ms | |
| T11 | Claude 4.6 Messages on Mantle | ❌ | 404, not found in Mantle catalog | Sonnet 4.6 未挂载 Mantle |
| T12a | OpenAI SDK + Claude 4.6 → Mantle Responses | ❌ (预期) | HTTP 404 `not_found_error` | 错误措辞见正文 |
| T12b | OpenAI SDK + Claude 4.6 → Mantle Chat | ❌ (预期) | HTTP 404 `not_found_error` | |
| T13 | OpenAI SDK + Claude 4.6 → Runtime Chat | ❌ (预期) | HTTP 404 `model_not_found` | 措辞与 Mantle 不同 |
| T14a | GET Mantle `/v1/models` | ✅ | 40 个模型 | 含 Opus 4.7 / Haiku 4.5，无 Sonnet 4.6 |
| T14b | OpenAI SDK + Claude Opus 4.7 → Mantle | ❌ (预期) | HTTP 400 `validation_error`：不支持该 API | 路径精准报错 |
| T14c | Anthropic native `/anthropic/v1/messages` + Claude Opus 4.7 | ✅ | 200 OK | |

延迟均为单次采样，仅作示意。

## 踩坑记录

!!! warning "坑 1：OpenAI 兼容 ≠ 全模型 OpenAI 兼容"
    Mantle 的 `/v1/responses`、`/v1/chat/completions` 只接受 `openai.*` 家模型的 ID。把 Claude、Mistral、Qwen 的 model ID 直接塞进去，得到 404 `not_found_error`（Mantle 侧）或 400 `validation_error`（Mantle 侧，对已挂载但不支持该 API 的模型）。
    
    每一个模型卡的 "APIs supported" 列是真实契约，以模型卡为准。

!!! warning "坑 2：Guardrail header 在 Mantle 上被静默忽略"
    `X-Amzn-Bedrock-GuardrailIdentifier` / `X-Amzn-Bedrock-GuardrailVersion` 只在 `bedrock-runtime` 端生效。对 Mantle 端带相同 header 不会报错，但请求会绕过 Guardrail 直达模型，token 正常计费，`response.id` 前缀为 `chatcmpl-*` 而非 `bedrock-guardrails-*`。
    
    来源：[gpt-oss-120b 模型卡](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html)列出的 Bedrock 特性表中，Guardrails 仅出现在 `bedrock-runtime` 行。生产需要 Guardrail 时，改用 Runtime 端点（Chat Completions 通过 `/openai/v1/` 前缀或 Converse/Invoke）。

!!! warning "坑 3：Chat Completions 的 Model ID 在两端点不同"
    同一个模型在 Mantle 上是 `openai.gpt-oss-120b`，在 Runtime 上是 `openai.gpt-oss-120b-1:0`（带 `-1:0` 版本后缀）。跨端点迁移代码时不能仅改 `base_url`，还需要调整 `model` 字段。

!!! warning "坑 4：同样叫 Claude，Sonnet 4.6 / Opus 4.7 / Haiku 4.5 走的路径不一样"
    - Sonnet 4.6：仅 `bedrock-runtime`，只能走 Converse / Invoke / Messages，且 us-east-1 必须用 `us.anthropic.claude-sonnet-4-6` 跨区域推理 ID。
    - Opus 4.7 / Haiku 4.5：Runtime 和 Mantle 都有，但 Mantle 侧只接受 `/anthropic/v1/messages`；用 OpenAI SDK `responses.create` / `chat.completions.create` 会被 Mantle 以 400 `validation_error` 拒绝。
    
    计划用 OpenAI 生态的 agent 框架接 Claude 前，先以模型卡确认再写代码。

!!! info "无关宏旨的小差异"
    - 两端点 Chat Completions 的响应 JSON 顶层字段一致，但 Mantle 版本的 `content` 在 gpt-oss 上可能包含 `<reasoning>...</reasoning>` 块（推理痕迹），Runtime 版本也有。应用层记得剥离再展示给终端用户。

## 选型建议

| 需求 | 推荐端点 | 模型 ID 形式 | API |
|---|---|---|---|
| 新项目、希望用 OpenAI SDK 生态 | `bedrock-mantle` | `openai.gpt-oss-120b` / `openai.gpt-oss-20b` | Responses 或 Chat Completions |
| 需要 stateful 多轮 / 异步后台 / server-side tool | `bedrock-mantle` | `openai.*` | Responses API |
| 必须用 Bedrock Guardrail 做内容过滤 | `bedrock-runtime` | 模型卡列出 runtime 支持的 ID | Chat Completions via `/openai/v1/` 或 Converse |
| 已有 Anthropic SDK 代码迁移到 Bedrock | Mantle 或 Runtime | `anthropic.*` | `/anthropic/v1/messages`（同一套 Messages 语义） |
| 需要调用 Claude Sonnet 4.6 | `bedrock-runtime` | `us.anthropic.claude-sonnet-4-6` | Converse 首选，Invoke 亦可 |
| 想用 OpenAI SDK 调 Claude | 不可行 | — | Anthropic 模型不接受 OpenAI Responses / Chat Completions API |

## 费用明细

| 资源 | 说明 | 费用 |
|---|---|---|
| `openai.gpt-oss-120b` 调用 | 共约 1500 input + 1600 output tokens（T01-T09, T15） | < $0.05 |
| `us.anthropic.claude-sonnet-4-6` 调用 | 共约 50 input + 20 output tokens（T10, T10b） | < $0.01 |
| `anthropic.claude-opus-4-7` / `anthropic.claude-haiku-4-5` 调用 | 6 次 400/200 响应的探针 | < $0.01 |
| Bedrock Guardrail（DRAFT 版本） | 创建无固定费用，调用按次计 | < $0.01 |
| **合计** | | **< $0.10** |

## 清理资源

```bash
# 删除测试 Guardrail
aws bedrock delete-guardrail --region us-east-1 \
  --guardrail-identifier 9bq7insnsizo

# 如果启用了 model invocation logging，检查并按需删除日志流
aws logs describe-log-streams --region us-east-1 \
  --log-group-name /aws/bedrock/modelinvocations 2>/dev/null || true
```

Short-term API key 无需显式撤销，12 小时后自动失效。IAM 凭证本身如仍有效，仅需停止持有并将本地环境变量清空。

!!! danger "务必清理"
    Guardrail 创建不收费但保留占用一个 quota 名额；测试结束后建议删除。

## 结论

Amazon Bedrock 通过 `bedrock-mantle` 端点引入的 OpenAI 兼容 API，实际价值比"可以用 OpenAI SDK 了"要精细：

1. **Responses API 是 Mantle 独占能力**——stateful 多轮、background 异步、OpenAI 一致的 tool-use 语法，在 `bedrock-runtime` 上都拿不到。
2. **OpenAI 兼容路径仅对 `openai.*` 家模型生效**，Claude/Mistral/Qwen 等走同端点的 `/anthropic/v1/messages` 或只能回到 Runtime 端点。
3. **Guardrail 是 `bedrock-runtime` 的特性**，Mantle 上附带 Guardrail header 不会报错也不会拦截；依赖合规审计的项目需要明确用 Runtime 端点。
4. **Claude Sonnet 4.6 这种未挂载到 Mantle 的模型**，无论调用方式如何变都得回到 Runtime。

概括成一条简单准则：**先查模型卡的 "APIs supported" 与 "Endpoints supported" 列，再决定用哪个 SDK 和哪个 base_url**。

## 参考链接

- [Endpoints supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html)
- [Generate responses using OpenAI APIs (Mantle)](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [Invoke a model with the OpenAI Chat Completions API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
- [gpt-oss-120b model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-oss-120b.html)
- [Claude Sonnet 4.6 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)
- [Amazon Bedrock API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [OpenAI Responses API reference](https://platform.openai.com/docs/api-reference/responses)
