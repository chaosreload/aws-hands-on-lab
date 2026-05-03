---
tags:
  - Bedrock
  - Mantle
  - OpenAI API
  - Anthropic Messages
  - What's New
---

# Amazon Bedrock Mantle API 支持矩阵实测：40 × 3 枚举出三条 API 各挂了哪些模型

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟（复现 40 × 3 枚举）
    - **预估费用**: < $0.10（每次调用 `max_tokens=8`，大部分请求被路由层拒绝无推理费用）
    - **Region**: us-east-1
    - **最后验证**: 2026-05-02

## TL;DR

Mantle 是 Amazon Bedrock 的多厂商模型入口，不是 "OpenAI 兼容专用通道"。在 us-east-1 实测 `client.models.list()` 返回的 **40 个模型 × 3 条 Mantle API** 共 120 个组合，得出三条结论：

1. **Chat Completions API 是 Mantle 上的通用入口** — 11 家厂商中的 **38/40** 模型（除了两款 Claude）都可调用 `POST /v1/chat/completions`。
2. **Responses API 目前仅开放给两款 gpt-oss 模型** — `openai.gpt-oss-120b` 和 `openai.gpt-oss-20b`，这与 [官方公告](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-responses-api-from-openai/) 的 "currently available for OpenAI's GPT OSS 20B/120B models" 一致。同家族的 `openai.gpt-oss-safeguard-*` **不**在 Responses 白名单里。
3. **Anthropic Messages API（`/anthropic/v1/messages`）目前仅 Claude Haiku 4.5 和 Opus 4.7 两款模型** — 路径是 `bedrock-mantle.<region>.api.aws/anthropic/v1/messages`，和 OpenAI 兼容路径同一个域名下共存。

对于未支持的 (模型, API) 组合，Mantle 入口直接返回 HTTP 400：

```
The model '<MODEL_ID>' does not support the '/<api>' API
```

这是路由级验证，不消耗推理 token。

## 背景：这篇为什么单独成文

本文是 [《Amazon Bedrock Mantle OpenAI 兼容 API 实测》（2026-05-01）](./bedrock-mantle-openai-compatible.md) 的修正版。旧文把"OpenAI 兼容端点不接受 `anthropic.claude-haiku-4-5` 作为模型参数"这一个事实，错误地推广为"OpenAI 兼容 API 只给 `openai.*` 家模型"。

错误根因：旧文 T14 只对 Anthropic 一家做反面测试，样本覆盖不足，却对"整个 OpenAI 兼容端点"下了结论。本文直接枚举 `client.models.list()` 返回的所有 40 个模型，对三条 API 各打一发 `max_tokens=8` 的请求，得到完整矩阵。

具体勘误见 [勘误说明](#勘误说明) 一节。

## 前置条件

- AWS 账号，在 us-east-1 开通 Amazon Bedrock 并订阅所用模型（本文重点使用 `openai.*` / `anthropic.*` / `zai.*` 等 Mantle 模型）
- IAM 权限：`AmazonBedrockLimitedAccess` 或等效
- Python ≥ 3.10、`pip install openai anthropic requests`
- Bedrock short-term API key（作为 `OPENAI_API_KEY` 传给 OpenAI SDK），最长 12 小时有效

## 核心概念

### 三个端点，两种调用姿态

Bedrock 推理操作有两个 endpoint（官方 [Endpoints supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html)）：

| Endpoint | Host | 支持的 API |
|---|---|---|
| `bedrock-mantle` | `bedrock-mantle.{region}.api.aws` | Responses API、Chat Completions、Anthropic Messages API |
| `bedrock-runtime` | `bedrock-runtime.{region}.amazonaws.com` | InvokeModel、Converse、Chat Completions、Anthropic Messages API |

**注意**：Anthropic Messages API（`/model/.../invoke` 的 Anthropic JSON schema，或 Converse 等价 schema）在两个端点上都可用，这点官方文档讲得比较隐晦。本文只聚焦 `bedrock-mantle` 这一侧。

Mantle 下同一个域名承载三条 API：

| Mantle API | HTTP Path | 所调用的 SDK |
|---|---|---|
| Responses API | `POST /v1/responses` | OpenAI SDK（`client.responses.create`）|
| Chat Completions | `POST /v1/chat/completions` | OpenAI SDK（`client.chat.completions.create`）|
| Anthropic Messages | `POST /anthropic/v1/messages` | Anthropic SDK（`client.messages.create`）|

一条 API 在 Mantle 能打哪些模型，由入口的路由白名单决定，与模型是否"属于"OpenAI 或 Anthropic 无关。下面把这张白名单画出来。

## 动手实践

### Step 1：列出 Mantle 上的所有模型

Mantle 支持 OpenAI 标准的 `GET /v1/models`：

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://bedrock-mantle.us-east-1.api.aws/v1",
    api_key="<Bedrock API Key>",  # 或设置 OPENAI_API_KEY
)

for m in client.models.list().data:
    print(m.id, "|", getattr(m, "owned_by", ""))
```

在 us-east-1（2026-05-01 执行）返回 **40 个模型**，分布在 11 家厂商。节选：

```
anthropic.claude-haiku-4-5             | anthropic
anthropic.claude-opus-4-7              | anthropic
deepseek.v3.1                          | deepseek
deepseek.v3.2                          | deepseek
google.gemma-3-4b-it                   | google
google.gemma-3-12b-it                  | google
google.gemma-3-27b-it                  | google
minimax.minimax-m2                     | minimax
mistral.mistral-large-3-675b-instruct  | mistral
moonshotai.kimi-k2.5                   | moonshot
nvidia.nemotron-super-3-120b           | nvidia
openai.gpt-oss-20b                     | openai
openai.gpt-oss-120b                    | openai
openai.gpt-oss-safeguard-20b           | openai
openai.gpt-oss-safeguard-120b          | openai
qwen.qwen3-235b-a22b-2507              | qwen
qwen.qwen3-coder-480b-a35b-instruct    | qwen
writer.palmyra-vision-7b               | writer
zai.glm-4.7                            | zai
zai.glm-5                              | zai
... (完整 40 个见本文 evidence 目录中 weichao_models_list_20260501.json)
```

Mantle 本身就是多厂商 marketplace，这是 Mantle 定位的基线。

### Step 2：对 40 个模型各打一发 `/v1/responses`

```python
def probe_responses(client, model_id):
    try:
        r = client.responses.create(
            model=model_id,
            input="Hi",
            max_output_tokens=8,
        )
        return {"ok": True, "status": 200, "finish_reason": getattr(r, "status", None)}
    except Exception as e:
        status = getattr(e, "status_code", None)
        msg = getattr(e, "message", None) or str(e)
        return {"ok": False, "status": status, "error": msg[:200]}
```

实测只有两款模型返回 200：

```
openai.gpt-oss-120b     → 200 OK
openai.gpt-oss-20b      → 200 OK
```

其他 38 个模型（包括同家族的 `openai.gpt-oss-safeguard-*`）一律 400：

```json
{
  "error": {
    "code": "validation_error",
    "type": "invalid_request_error",
    "message": "The model 'openai.gpt-oss-safeguard-120b' does not support the '/v1/responses' API"
  }
}
```

### Step 3：对 40 个模型各打一发 `/v1/chat/completions`

同样的枚举，把端点换成 `client.chat.completions.create`。实测 **38/40** 成功，失败的两个是：

```
anthropic.claude-haiku-4-5  → 400  does not support the '/v1/chat/completions' API
anthropic.claude-opus-4-7   → 400  does not support the '/v1/chat/completions' API
```

两款 Claude 在 Mantle 上 **既不走 Responses，也不走 Chat Completions**，只保留 Anthropic Messages 一条路径。

`zai.glm-4.6` 首次执行时出现 60s `APITimeoutError`（非路由拒绝）；重试（`zai_glm_4_6_chat_retry.json`）正常返回 200。本文表格以重试后的结果为准。

### Step 4：对 40 个模型各打一发 `/anthropic/v1/messages`

Anthropic 家的 SDK 用法：

```python
import anthropic

client = anthropic.Anthropic(
    base_url="https://bedrock-mantle.us-east-1.api.aws/anthropic",
    api_key="<Bedrock API Key>",
)

def probe_messages(client, model_id):
    try:
        r = client.messages.create(
            model=model_id,
            max_tokens=8,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return {"ok": True, "status": 200}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
```

实测只有两款 Claude 返回 200：

```
anthropic.claude-haiku-4-5   → 200  model_in_response = claude-haiku-4-5-20251001
anthropic.claude-opus-4-7    → 200  model_in_response = claude-opus-4-7-20250929
```

其他 38 个模型一律 400：

```
The model 'openai.gpt-oss-120b' does not support the '/anthropic/v1/messages' API
```

## 测试结果

### 完整矩阵（11 家 × 40 模型 × 3 API）

每格 `a/b` 表示 "支持的模型数 / 该厂商总模型数"，粗体表示该格整列支持。

| 厂商 | 模型数 | `/v1/responses` | `/v1/chat/completions` | `/anthropic/v1/messages` |
|---|---|---|---|---|
| anthropic | 2 | 0/2 | 0/2 | **2/2** |
| deepseek | 2 | 0/2 | **2/2** | 0/2 |
| google | 3 | 0/3 | **3/3** | 0/3 |
| minimax | 3 | 0/3 | **3/3** | 0/3 |
| mistral | 8 | 0/8 | **8/8** | 0/8 |
| moonshotai | 2 | 0/2 | **2/2** | 0/2 |
| nvidia | 4 | 0/4 | **4/4** | 0/4 |
| openai | 4 | **2/4** | **4/4** | 0/4 |
| qwen | 7 | 0/7 | **7/7** | 0/7 |
| writer | 1 | 0/1 | **1/1** | 0/1 |
| zai | 4 | 0/4 | **4/4** | 0/4 |
| **TOTAL** | **40** | **2** | **38** | **2** |

### Responses API 的 2 个模型

| Model ID | 说明 |
|---|---|
| `openai.gpt-oss-120b` | 官方公告明确支持 |
| `openai.gpt-oss-20b` | 官方公告明确支持 |

同家族的 `openai.gpt-oss-safeguard-120b` / `openai.gpt-oss-safeguard-20b` 不在白名单（实测返回 400）。

### Anthropic Messages 的 2 个模型

| Model ID | Response 中的 model 字段 |
|---|---|
| `anthropic.claude-haiku-4-5` | `claude-haiku-4-5-20251001` |
| `anthropic.claude-opus-4-7` | `claude-opus-4-7-20250929` |

### Chat Completions 不支持的 2 个模型

| Model ID | 替代路径 |
|---|---|
| `anthropic.claude-haiku-4-5` | `/anthropic/v1/messages` |
| `anthropic.claude-opus-4-7` | `/anthropic/v1/messages` |

### 路由级错误格式（实测观察，官方未单独记录）

所有未支持 cell 服务端返回统一结构：

```json
{
  "error": {
    "code": "validation_error",
    "type": "invalid_request_error",
    "message": "The model '<MODEL_ID>' does not support the '/<api>' API"
  }
}
```

HTTP 状态码 400。OpenAI SDK 抛 `BadRequestError`，Anthropic SDK 抛 `BadRequestError` / `APIError`。

## 三条 API 的路由规则

把上面的矩阵翻译成路由规则：

- **`/v1/responses`**：白名单极严，当前只覆盖 `openai.gpt-oss-20b` 和 `openai.gpt-oss-120b`。对应 Responses API 的公开节奏（2025-12 公告原话 "with support for other models coming soon"）。
- **`/v1/chat/completions`**：默认全开，仅两款 Claude 不挂载。这意味着把非 Anthropic 模型从 OpenAI/Anthropic/其他第三方推理服务迁到 Bedrock，几乎都可以只改 `base_url` 和 `model` 字符串就跑通。
- **`/anthropic/v1/messages`**：只暴露给当前挂载到 Mantle 的两款 Claude。如果业务已经在用 Anthropic SDK，这是零代码改动的接入点；非 Anthropic 模型在这条路径上一律 400。

值得注意的一点：这三条 API 是**独立挂载的白名单**，不是"一个模型支持 OpenAI 兼容就三条都支持"。目前同时支持 Responses 和 Chat Completions 的只有 `openai.gpt-oss-20b` / `openai.gpt-oss-120b` 两款模型。

## Guardrail header 的生效端点

这一节保留 [旧文](./bedrock-mantle-openai-compatible.md) 的结论，复用其实测（T08/T09/T15）。

- 请求头 `X-Amzn-Bedrock-GuardrailIdentifier` / `X-Amzn-Bedrock-GuardrailVersion` 在 **`bedrock-runtime` 端点**（InvokeModel / Converse / Chat Completions）上生效：触发策略时返回带 `amazon-bedrock-guardrailAction: intervened` 的响应。
- 同一组 header 打到 **`bedrock-mantle` 端点**（任何 API）会被入口静默忽略：请求原样转发给模型，正常计费，响应里没有 Guardrail 元数据。

生产落地建议：

- 要在 OpenAI 兼容端点上强制 Guardrail，需要把请求改走 `bedrock-runtime` 的 Chat Completions 路径（`https://bedrock-runtime.{region}.amazonaws.com/model/{modelId}/chat-completions`）。
- 或者把 Guardrail 配置写到模型调用的 Bedrock Agent / Knowledge Base 这一层，而不是 Mantle 直连调用。

## 选型建议

| 目标 | 推荐 API | 推荐模型 |
|---|---|---|
| 迁移已有 OpenAI SDK 代码，模型不挑 | `/v1/chat/completions` | 38 款模型任选（按业务需求选） |
| 需要 built-in tools、stateful conversation、background job | `/v1/responses` | 仅 `openai.gpt-oss-20b` / `openai.gpt-oss-120b` |
| 已在用 Anthropic SDK，要无改动接入 Bedrock | `/anthropic/v1/messages` | `anthropic.claude-haiku-4-5` / `anthropic.claude-opus-4-7` |
| 需要 Guardrail 强制策略 | `bedrock-runtime` 下的 InvokeModel / Converse / Chat Completions，**不走 Mantle** | 任意 Bedrock 支持模型 |
| 需要 Mantle 暂未覆盖的 Claude（如 Sonnet 4.6 等） | `bedrock-runtime` 的 InvokeModel + [CRIS](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html) | `us.anthropic.claude-sonnet-4-6` 等跨区域推理 profile |

## 踩坑记录

!!! warning "踩坑 1：Responses API 在 `openai.gpt-oss-safeguard-*` 上不可用"
    虽然这两个模型的 ID 前缀是 `openai.`，但 Mantle 的 Responses 白名单只覆盖非 safeguard 版本。枚举时这一点容易被跳过。

    触发：`client.responses.create(model="openai.gpt-oss-safeguard-120b", input="Hi")` → 400。

    来源：实测观察，`bedrock-mantle.html` 官方文档中的代码示例全部使用 `openai.gpt-oss-120b`，未对 safeguard 变体作出说明。

!!! warning "踩坑 2：Mantle 下 Claude 不支持 Chat Completions"
    这两个 Claude 在 Mantle 上只保留 `/anthropic/v1/messages` 一条路径。如果你想用 OpenAI SDK 调 Claude，Mantle 端不行，要走 `bedrock-runtime` 的 OpenAI-compatible Chat Completions 路径（[官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)）。

    触发：`client.chat.completions.create(model="anthropic.claude-haiku-4-5", ...)` 指向 `bedrock-mantle.{region}.api.aws/v1` → 400 `does not support the '/v1/chat/completions' API`。

!!! warning "踩坑 3：路由级拒绝不计 token，但也不给你提示该走哪条路"
    入口识别到不支持的 (model, API) 组合直接返回 400，不进入模型层，所以没有推理费用。但错误 message 只告诉你"不支持"，不会建议替代 API。排查时需要自行对照矩阵。

!!! warning "踩坑 4：`zai.glm-4.6` Chat 列在部分时段 60s 超时"
    2026-05-01 首次执行矩阵时这一格是 `APITimeoutError`，5/2 重试 200 OK 1.0s。如果跑大规模枚举遇到 APITimeoutError，先重试再下结论。

## 勘误说明

本文修正的是 [2026-05-01 发布的旧文](./bedrock-mantle-openai-compatible.md) 的核心结论第 1 条：

> ❌ 旧文原话（摘要）："OpenAI 兼容 API 只给 `openai.*` 家模型 — Claude/Nova/其他一律不给"

经过 5/2 补跑 40 × 3 完整枚举，这一结论被证伪。正确结论是上文的三条 TL;DR。

**错误根因**：旧文 T14 反面测试只对 Anthropic 一家做（`anthropic.claude-haiku-4-5` / `anthropic.claude-opus-4-7`），得到的事实只是"这两款 Claude 在 Mantle 上不走 `/v1/chat/completions`"，但旧文把它推广成"OpenAI 兼容端点只接受 `openai.*` 模型"。实际 38 家厂商的 38 个非 Claude 模型（deepseek / google / minimax / mistral / moonshot / nvidia / openai / qwen / writer / zai）都挂载了 Chat Completions。

**过程教训**：设计测试矩阵时如果直接对 `client.models.list()` 全量枚举，就不会出现这种基于两个数据点推全局结论的错误。以后遇到 "X 只给 Y" 这种封闭结论，必须用 `客户端枚举` 作为铁证。

旧文已在顶部加 deprecation banner，内容保留留档但不应再被引用。

## 复现所需的完整脚本

测试脚本位于本文 evidence 目录：

- `content/evidence/bedrock-mantle-api-support-matrix/mantle_api_matrix.py` — 40 × 3 枚举
- `content/evidence/bedrock-mantle-api-support-matrix/mantle_models_list.py` — `/v1/models` 拉取
- `content/evidence/bedrock-mantle-api-support-matrix/support_matrix_20260501.json` — 主矩阵结果
- `content/evidence/bedrock-mantle-api-support-matrix/zai_glm_4_6_chat_retry.json` — glm-4.6 chat 重试
- `content/evidence/bedrock-mantle-api-support-matrix/weichao_models_list_20260501.json` — 40 模型完整清单

## 费用与清理

- 所有未支持 cell 被入口 400 拒绝，无 token 消耗。
- 两款 gpt-oss 在 Responses / Chat 上的 `max_tokens=8` 请求，以及两款 Claude 在 Messages 上的 `max_tokens=8` 请求，合计实际产出推理不到 100 token。
- 总成本 < $0.10。
- 本实验未创建任何需要清理的托管资源（Guardrail、Agent、Knowledge Base 均未涉及）。

## 参考链接

- [Endpoints supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html)
- [APIs supported by Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html)
- [Generate responses using OpenAI APIs（Mantle 文档）](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [Amazon Bedrock now supports Responses API from OpenAI（What's New 2025-12）](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-responses-api-from-openai/)
- [Amazon Bedrock API compatibility](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html)
- 本仓库旧文（已 deprecated）：[Amazon Bedrock Mantle OpenAI 兼容 API 实测（2026-05-01）](./bedrock-mantle-openai-compatible.md)
