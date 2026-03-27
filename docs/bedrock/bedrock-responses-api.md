# Amazon Bedrock Responses API 实战：OpenAI 兼容端点动手指南

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $1-5（按 token 计费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

Amazon Bedrock 推出了 **Project Mantle** —— 一个全新的分布式推理引擎，提供与 OpenAI API 完全兼容的服务端点。这意味着你可以用 OpenAI 的 Python SDK，仅改一行 `base_url`，就能调用 Bedrock 上的模型。

核心亮点是 **Responses API**：与传统的 Chat Completions 不同，它支持**有状态的对话管理** —— 服务端自动记住对话历史，你不需要每次请求都传完整的 `messages` 数组。对于多轮对话和 Agent 场景，这大幅简化了客户端代码。

## 前置条件

- AWS 账号（需要 IAM 权限创建用户和 Service Specific Credential）
- AWS CLI v2 已配置
- Python 3.8+
- `pip install openai`

## 核心概念

### Bedrock 推理 API 对比

| 特性 | Responses API (Mantle) | Chat Completions (Mantle) | Converse API (原生) |
|------|----------------------|--------------------------|-------------------|
| **端点** | `bedrock-mantle.{region}.api.aws` | `bedrock-mantle.{region}.api.aws` | `bedrock-runtime.{region}.amazonaws.com` |
| **SDK** | OpenAI SDK | OpenAI SDK | AWS SDK (boto3) |
| **有状态对话** | ✅ 自动管理 | ❌ 手动传历史 | ❌ 手动传历史 |
| **认证** | API Key / AWS SigV4 | API Key / AWS SigV4 | AWS SigV4 |
| **流式** | ✅ | ✅ | ✅ |
| **异步推理** | ✅ | ❌ | ❌ |
| **支持模型数** | 2（仅 GPT-OSS） | 38 | 所有 Bedrock 模型 |

### 认证方式

Bedrock Mantle 端点支持两种认证：

1. **Bedrock API Key**（推荐用于 OpenAI SDK）：通过 Console 或 CLI 生成，分短期（≤12h）和长期（自定义天数）
2. **AWS SigV4**（curl / HTTP 直调）：直接用 IAM 凭证签名

### 可用模型完整列表

通过 Models API 实测（2026-03-27），Mantle 端点共提供 **38 个模型**，远超公告中仅提到的 GPT-OSS 系列：

| 厂商 | 模型 ID | 类型 | Responses API | Chat Completions |
|------|---------|------|:---:|:---:|
| **OpenAI** | `openai.gpt-oss-120b` | 通用 | ✅ | ✅ |
| | `openai.gpt-oss-20b` | 通用 | ✅ | ✅ |
| | `openai.gpt-oss-safeguard-120b` | 安全过滤 | ❌ | ✅ |
| | `openai.gpt-oss-safeguard-20b` | 安全过滤 | ❌ | ✅ |
| **Mistral** | `mistral.mistral-large-3-675b-instruct` | 通用（大） | ❌ | ✅ |
| | `mistral.devstral-2-123b` | 代码 | ❌ | ✅ |
| | `mistral.magistral-small-2509` | 通用 | ❌ | ✅ |
| | `mistral.ministral-3-14b-instruct` | 通用（小） | ❌ | ✅ |
| | `mistral.ministral-3-8b-instruct` | 通用（小） | ❌ | ✅ |
| | `mistral.ministral-3-3b-instruct` | 通用（小） | ❌ | ✅ |
| | `mistral.voxtral-small-24b-2507` | 语音 | ❌ | ✅ |
| | `mistral.voxtral-mini-3b-2507` | 语音 | ❌ | ✅ |
| **Qwen** | `qwen.qwen3-235b-a22b-2507` | 通用（大） | ❌ | ✅ |
| | `qwen.qwen3-next-80b-a3b-instruct` | 通用 | ❌ | ✅ |
| | `qwen.qwen3-32b` | 通用 | ❌ | ✅ |
| | `qwen.qwen3-coder-480b-a35b-instruct` | 代码（大） | ❌ | ✅ |
| | `qwen.qwen3-coder-30b-a3b-instruct` | 代码 | ❌ | ✅ |
| | `qwen.qwen3-coder-next` | 代码 | ❌ | ✅ |
| | `qwen.qwen3-vl-235b-a22b-instruct` | 多模态 | ❌ | ✅ |
| **DeepSeek** | `deepseek.v3.1` | 通用 | ❌ | ✅ |
| | `deepseek.v3.2` | 通用 | ❌ | ✅ |
| **Google** | `google.gemma-3-27b-it` | 通用 | ❌ | ✅ |
| | `google.gemma-3-12b-it` | 通用 | ❌ | ✅ |
| | `google.gemma-3-4b-it` | 通用（小） | ❌ | ✅ |
| **NVIDIA** | `nvidia.nemotron-super-3-120b` | 通用（大） | ❌ | ✅ |
| | `nvidia.nemotron-nano-3-30b` | 通用 | ❌ | ✅ |
| | `nvidia.nemotron-nano-12b-v2` | 通用（小） | ❌ | ✅ |
| | `nvidia.nemotron-nano-9b-v2` | 通用（小） | ❌ | ✅ |
| **MiniMax** | `minimax.minimax-m2.5` | 通用 | ❌ | ✅ |
| | `minimax.minimax-m2.1` | 通用 | ❌ | ✅ |
| | `minimax.minimax-m2` | 通用 | ❌ | ✅ |
| **Moonshot** | `moonshotai.kimi-k2.5` | 通用 | ❌ | ✅ |
| | `moonshotai.kimi-k2-thinking` | 推理 | ❌ | ✅ |
| **智谱 (ZAI)** | `zai.glm-5` | 通用 | ❌ | ✅ |
| | `zai.glm-4.7` | 通用 | ❌ | ✅ |
| | `zai.glm-4.7-flash` | 通用（快） | ❌ | ✅ |
| | `zai.glm-4.6` | 通用 | ❌ | ✅ |
| **Writer** | `writer.palmyra-vision-7b` | 多模态 | ❌ | ✅ |

!!! warning "Responses API 支持范围"
    目前只有 **`openai.gpt-oss-120b`** 和 **`openai.gpt-oss-20b`** 支持 Responses API（`/v1/responses`）。其他 36 个模型仅支持 Chat Completions API（`/v1/chat/completions`）。调用不支持的模型会返回 400 错误。

## 动手实践

### Step 1: 创建 Bedrock API Key

```bash
# 创建 IAM 用户
aws iam create-user --user-name bedrock-api-user

# 附加 Bedrock 权限
aws iam attach-user-policy \
  --user-name bedrock-api-user \
  --policy-arn arn:aws:iam::aws:policy/AmazonBedrockLimitedAccess

# 生成 30 天有效的 API Key
aws iam create-service-specific-credential \
  --user-name bedrock-api-user \
  --service-name bedrock.amazonaws.com \
  --credential-age-days 30
```

记录返回的 `ServiceApiKeyValue`，这就是你的 API Key。

### Step 2: 配置环境变量

```bash
export OPENAI_API_KEY="<你的 Bedrock API Key>"
export OPENAI_BASE_URL="https://bedrock-mantle.us-east-1.api.aws/v1"
```

### Step 3: 查看可用模型

```python
from openai import OpenAI

client = OpenAI()

models = client.models.list()
for model in models.data:
    print(model.id)
```

实测返回 38 个模型，完整列表见上方"可用模型"表格。

### Step 4: Chat Completions API（无状态）

```python
from openai import OpenAI

client = OpenAI()

# 非流式
completion = client.chat.completions.create(
    model="openai.gpt-oss-120b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Amazon Bedrock in one sentence?"}
    ]
)
print(completion.choices[0].message.content)
```

??? example "实测返回（点击展开）"
    ```json
    {
      "id": "chatcmpl-8bbde7d0-436d-4781-8fea-7d51ddf58f52",
      "choices": [
        {
          "finish_reason": "stop",
          "index": 0,
          "message": {
            "content": "Amazon Bedrock is AWS's fully managed service that provides on‑demand access to a suite of foundation‑model APIs (including text, image, and embedding models from Amazon, Anthropic, Meta, and Stability AI) for building generative AI applications without handling infrastructure, model training, or scaling complexities.",
            "role": "assistant",
            "reasoning": "We need to answer succinctly: one sentence describing Amazon Bedrock. Provide clear definition."
          }
        }
      ],
      "model": "openai.gpt-oss-120b",
      "usage": {
        "completion_tokens": 90,
        "prompt_tokens": 74,
        "total_tokens": 164
      }
    }
    ```
    注意 `reasoning` 字段：GPT-OSS 模型会自动输出 chain-of-thought 推理过程。

```python
# 流式
stream = client.chat.completions.create(
    model="openai.gpt-oss-120b",
    messages=[{"role": "user", "content": "List 3 benefits of serverless."}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Step 5: Responses API（有状态）

```python
from openai import OpenAI

client = OpenAI()

# 第一轮对话
response = client.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "My name is Alice and I like cats."}]
)
print(f"Response ID: {response.id}")

# 第二轮 —— 用 previous_response_id 引用上一轮
response2 = client.responses.create(
    model="openai.gpt-oss-120b",
    input=[{"role": "user", "content": "What is my name and what do I like?"}],
    previous_response_id=response.id
)

# 提取回复文本
for item in response2.output:
    if hasattr(item, "content"):
        for c in item.content:
            if c.type == "output_text":
                print(c.text)
```

注意：不需要在第二轮请求中传入完整的对话历史。`previous_response_id` 让服务端自动重建上下文。

??? example "Responses API 实测返回（点击展开）"
    ```json
    {
      "id": "resp_hwo3zxb5ls7m5ztdhnntfqn6ovgnl7sak2qlz2we4sjwosmoowyq",
      "model": "openai.gpt-oss-120b",
      "object": "response",
      "status": "completed",
      "output": [
        {
          "type": "reasoning",
          "content": [
            {
              "type": "reasoning_text",
              "text": "We need to answer succinctly: define Amazon Bedrock in one sentence. Should mention it's a fully managed service that provides foundation model APIs from various providers, for generative AI. One sentence."
            }
          ],
          "status": "completed"
        },
        {
          "type": "message",
          "role": "assistant",
          "content": [
            {
              "type": "output_text",
              "text": "Amazon Bedrock is a fully managed AWS service that gives developers on‑demand access via simple APIs to a curated portfolio of high‑performing foundation models (from AWS and leading AI startups) for building, customizing, and scaling generative AI applications without managing underlying infrastructure."
            }
          ],
          "status": "completed"
        }
      ],
      "usage": {
        "input_tokens": 74,
        "output_tokens": 104,
        "input_tokens_details": { "cached_tokens": 0 },
        "output_tokens_details": { "reasoning_tokens": 0 }
      }
    }
    ```
    Responses API 返回结构化输出：先 `reasoning`（思考过程），再 `message`（最终回答）。

### Step 6: 用 curl 直接调用（不用 SDK）

**方式一：API Key**

```bash
curl -X POST "https://bedrock-mantle.us-east-1.api.aws/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{
    "model": "openai.gpt-oss-20b",
    "input": [{"role": "user", "content": "Hello"}]
  }'
```

**方式二：AWS SigV4（无需 API Key）**

```bash
curl -s --aws-sigv4 "aws:amz:us-east-1:bedrock" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "Content-Type: application/json" \
  -X POST "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions" \
  -d '{
    "model": "openai.gpt-oss-20b",
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

### Step 7: 测试开源模型（GLM 5 / Kimi K2.5）

除了 GPT-OSS，Mantle 端点上的开源模型同样可以通过 Chat Completions API 调用。以下测试 GLM 5 和 Kimi K2.5：

```python
from openai import OpenAI

client = OpenAI()

# GLM 5
resp_glm = client.chat.completions.create(
    model="zai.glm-5",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain what Amazon S3 is in 2 sentences."}
    ]
)
print(f"[GLM 5] {resp_glm.choices[0].message.content}")

# Kimi K2.5
resp_kimi = client.chat.completions.create(
    model="moonshotai.kimi-k2.5",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain what Amazon S3 is in 2 sentences."}
    ]
)
print(f"[Kimi K2.5] {resp_kimi.choices[0].message.content}")

# Kimi K2 Thinking（带推理链）
resp_kimi_think = client.chat.completions.create(
    model="moonshotai.kimi-k2-thinking",
    messages=[
        {"role": "user", "content": "Explain what Amazon S3 is in 2 sentences."}
    ]
)
print(f"[Kimi K2 Thinking] {resp_kimi_think.choices[0].message.content}")
if hasattr(resp_kimi_think.choices[0].message, 'reasoning'):
    print(f"[Reasoning] {resp_kimi_think.choices[0].message.reasoning}")
```

??? example "GLM 5 实测返回（点击展开）"
    ```json
    {
      "id": "chatcmpl-379bb127-b7d4-47c7-b744-12e421694fae",
      "choices": [
        {
          "finish_reason": "stop",
          "message": {
            "content": "Amazon S3 (Simple Storage Service) is a scalable cloud storage service designed to store and retrieve any amount of data from anywhere on the web. It offers industry-leading durability, availability, and security for a wide range of use cases, such as data lakes, backups, and application hosting.",
            "role": "assistant"
          }
        }
      ],
      "model": "zai.glm-5",
      "usage": {
        "completion_tokens": 59,
        "prompt_tokens": 24,
        "total_tokens": 83
      }
    }
    ```
    GLM 5 响应简洁，无 reasoning 字段，延迟 2.51s。

??? example "Kimi K2.5 实测返回（点击展开）"
    ```json
    {
      "id": "chatcmpl-e3e3aaf9-cc99-4e9c-a6fb-7f8eca793d3f",
      "choices": [
        {
          "finish_reason": "stop",
          "message": {
            "content": "Amazon S3 (Simple Storage Service) is a cloud-based object storage service that allows you to store and retrieve any amount of data from anywhere on the web. It's designed for durability, scalability, and security, making it ideal for backups, data lakes, website hosting, and application data storage.",
            "role": "assistant"
          }
        }
      ],
      "model": "moonshotai.kimi-k2.5",
      "usage": {
        "completion_tokens": 60,
        "prompt_tokens": 30,
        "total_tokens": 90
      }
    }
    ```
    Kimi K2.5 响应快（1.41s），质量与 GLM 5 相当。

??? example "Kimi K2 Thinking 实测返回（点击展开）"
    Kimi K2 Thinking 是推理模型，与 GPT-OSS 类似会输出 `reasoning` 字段：

    - **延迟**: 3.73s（含推理时间）
    - **Token 用量**: 18 prompt + 306 completion = 324 total（推理链占大量 output tokens）
    - **输出**: 先在 `reasoning` 字段输出思考过程，再在 `content` 中给出最终回答
    - 注意 completion_tokens 显著高于非推理模型（306 vs 59-60），因为包含了推理链

## 测试结果

### API 响应延迟对比

| 测试场景 | 首次延迟 | 平均延迟 | 备注 |
|---------|---------|---------|------|
| Chat Completions 非流式 (GPT-OSS 120B) | 1.12s | ~1.5s | 稳定 |
| Chat Completions 流式 (GPT-OSS 120B) | TTFT 1.29s | ~2.0s | 8 个 chunk |
| Responses API 非流式 (GPT-OSS 120B) | 2.83s | ~2.5s | 含 reasoning |
| Responses API 流式 (GPT-OSS 120B) | TTFT 0.86s | ~1.9s | 20 个 event |
| 有状态多轮 Turn 2 (GPT-OSS 120B) | 0.65s | - | 上下文自动加载 |
| Chat Completions (GLM 5) | 2.51s | - | 无 reasoning |
| Chat Completions (Kimi K2.5) | 1.41s | - | 无 reasoning |
| Chat Completions (Kimi K2 Thinking) | 3.73s | - | 含推理链 |

### 20B vs 120B 模型对比

| 指标 | GPT-OSS-20B | GPT-OSS-120B |
|------|-------------|--------------|
| 平均延迟 | 2.60s | 2.46s |
| 平均输出 tokens | 431 | 224 |
| 响应风格 | 较冗长 | 更简洁 |
| 首次调用 | 2.39s | 4.26s（疑似冷启动） |

### 开源模型对比（Chat Completions，同一 prompt）

| 模型 | 延迟 | Output Tokens | 特点 |
|------|------|:---:|------|
| GLM 5 | 2.51s | 59 | 简洁，无推理链 |
| Kimi K2.5 | 1.41s | 60 | 最快，质量好 |
| Kimi K2 Thinking | 3.73s | 306 | 带推理链，token 消耗 5x |
| GPT-OSS 120B | 2.46s | 90 | 自带 reasoning 字段 |

### Responses API 响应结构

Responses API 的 JSON 结构比 Chat Completions 更丰富：

```json
{
  "output": [
    {
      "type": "reasoning",
      "content": [{"type": "reasoning_text", "text": "思考过程..."}]
    },
    {
      "type": "message",
      "role": "assistant",
      "content": [{"type": "output_text", "text": "最终回答"}]
    }
  ]
}
```

## 踩坑记录

!!! warning "注意事项"

    **1. Responses API 仅支持 GPT-OSS（非 safeguard）**

    38 个模型中，只有 `openai.gpt-oss-120b` 和 `openai.gpt-oss-20b` 支持 `/v1/responses`。其他模型（包括 safeguard 变体）调用会返回 400：`The model 'xxx' does not support the '/v1/responses' API`。

    **2. 模型自动输出 reasoning 文本**

    GPT-OSS 模型在 Responses API 中会自动输出 `reasoning_text`（chain-of-thought），在 Chat Completions 中对应 `reasoning` 字段。Kimi K2 Thinking 也有类似行为。这会增加输出 token 数（实测比非推理模型多 3-5x）。**实测发现，GPT-OSS 的此行为官方文档未记录。**

    **3. 空 input 不报错**

    Responses API 对空 `input` 数组不返回错误，而是生成随机内容。**实测发现，官方未记录。** 建议客户端做输入校验。

    **4. 错误信息清晰**

    无效模型返回 404 + 具体模型名，无效 API Key 返回 401，符合 OpenAI API 错误规范。**已查文档确认。**

    **5. 模型列表远超公告**

    公告原文说 "starting with OpenAI's GPT OSS 20B/120B models"，实测 Models API 返回 38 个模型，覆盖 8 个厂商。这并非错误，而是 Mantle 平台已扩展到更多模型。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| GPT-OSS 模型推理 | 按 token 计费 | ~2000 tokens | < $1 |
| 开源模型推理 | 按 token 计费 | ~800 tokens | < $0.5 |
| IAM 用户/API Key | 免费 | - | $0 |
| **合计** | | | **< $1** |

## 清理资源

```bash
# 1. 删除 API Key
aws iam delete-service-specific-credential \
  --user-name bedrock-api-user \
  --service-specific-credential-id <CREDENTIAL_ID>

# 2. 解除策略
aws iam detach-user-policy \
  --user-name bedrock-api-user \
  --policy-arn arn:aws:iam::aws:policy/AmazonBedrockLimitedAccess

# 3. 删除 IAM 用户
aws iam delete-user --user-name bedrock-api-user
```

!!! danger "务必清理"
    长期 API Key 如果不清理会持续有效直到过期。Lab 完成后请执行清理步骤。

## 结论与建议

### 适用场景

- **从 OpenAI 迁移**：仅改 `base_url` 和 API Key 即可迁移，零代码重构
- **多轮对话应用**：Responses API 的有状态管理大幅简化客户端，特别适合 chatbot 和 Agent
- **多模型评测**：Mantle 上 38 个模型统一端点，方便 A/B 测试
- **开源模型试用**：GLM 5、Kimi K2.5、DeepSeek 等无需额外配置即可调用

### 建议

1. **生产环境用短期密钥**：长期密钥仅用于探索和开发
2. **注意 reasoning tokens**：GPT-OSS 和 Kimi K2 Thinking 的推理链会计入 token 费用，按需提取 `output_text`
3. **优先用 Responses API**：对话场景下，有状态管理减少了每轮请求的 token 传输量（前提是使用 GPT-OSS 模型）
4. **开源模型选型**：快速响应选 Kimi K2.5（1.41s），需要推理链选 Kimi K2 Thinking，中文场景可试 GLM 5

## 参考链接

- [Amazon Bedrock Mantle 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [Bedrock API Keys 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-responses-api-from-openai/)
- [OpenAI Responses API 参考](https://platform.openai.com/docs/api-reference/responses)
