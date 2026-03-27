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

### 认证方式

Bedrock Mantle 端点支持两种认证：

1. **Bedrock API Key**（推荐用于 OpenAI SDK）：通过 Console 或 CLI 生成，分短期（≤12h）和长期（自定义天数）
2. **AWS SigV4**（curl / HTTP 直调）：直接用 IAM 凭证签名

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

实测返回 38 个模型，包括 OpenAI GPT-OSS、Mistral、Qwen、DeepSeek、Google Gemma、NVIDIA NeMo、MiniMax、Moonshot Kimi 等。

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

## 测试结果

### API 响应延迟对比

| 测试场景 | 首次延迟 | 平均延迟 | 备注 |
|---------|---------|---------|------|
| Chat Completions 非流式 (120B) | 1.12s | ~1.5s | 稳定 |
| Chat Completions 流式 (120B) | TTFT 1.29s | ~2.0s | 8 个 chunk |
| Responses API 非流式 (120B) | 2.83s | ~2.5s | 含 reasoning |
| Responses API 流式 (120B) | TTFT 0.86s | ~1.9s | 20 个 event |
| 有状态多轮 Turn 2 (120B) | 0.65s | - | 上下文自动加载 |

### 20B vs 120B 模型对比

| 指标 | GPT-OSS-20B | GPT-OSS-120B |
|------|-------------|--------------|
| 平均延迟 | 2.60s | 2.46s |
| 平均输出 tokens | 431 | 224 |
| 响应风格 | 较冗长 | 更简洁 |
| 首次调用 | 2.39s | 4.26s（疑似冷启动） |

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

    **1. 模型自动输出 reasoning 文本**

    GPT-OSS 模型在 Responses API 中会自动输出 `reasoning_text`（chain-of-thought），在 Chat Completions 中对应 `reasoning` 字段。这会增加输出 token 数。**实测发现，官方文档未记录此行为。**

    **2. 空 input 不报错**

    Responses API 对空 `input` 数组不返回错误，而是生成随机内容。**实测发现，官方未记录。** 建议客户端做输入校验。

    **3. 错误信息清晰**

    无效模型返回 404 + 具体模型名，无效 API Key 返回 401，符合 OpenAI API 错误规范。**已查文档确认。**

    **4. 模型列表远超公告**

    公告原文说 "starting with OpenAI's GPT OSS 20B/120B models"，实测 Models API 返回 38 个模型。这并非错误，而是 Mantle 平台已扩展到更多模型。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| GPT-OSS 模型推理 | 按 token 计费 | ~2000 tokens | < $1 |
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
- **多模型评测**：Mantle 上 38+ 模型统一端点，方便 A/B 测试

### 建议

1. **生产环境用短期密钥**：长期密钥仅用于探索和开发
2. **注意 reasoning tokens**：GPT-OSS 的 reasoning 输出会计入 token 费用，按需提取 `output_text`
3. **优先用 Responses API**：对话场景下，有状态管理减少了每轮请求的 token 传输量

## 参考链接

- [Amazon Bedrock Mantle 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [Bedrock API Keys 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-responses-api-from-openai/)
- [OpenAI Responses API 参考](https://platform.openai.com/docs/api-reference/responses)
