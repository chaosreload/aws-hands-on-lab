---
tags:
  - Bedrock
  - API
  - What's New
---

# Amazon Bedrock Count Tokens API 实测：推理前精确预估 Token 用量与成本

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 15 分钟
    - **预估费用**: $0（CountTokens API 免费）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-27

## 背景

在使用大语言模型时，Token 用量直接决定成本和是否会触发限流。但在发送推理请求之前，你通常只能"估算"输入会消耗多少 Token——不同模型的分词策略不同，多语言文本的 Token 效率差异显著，这让成本预估变成了一门玄学。

Amazon Bedrock 推出的 **Count Tokens API** 解决了这个问题：在推理前精确返回输入的 Token 数量，与实际推理计费完全一致，且**调用完全免费**。

目前支持 Anthropic Claude 系列模型，包括 Claude 3.5 Haiku、Claude 3.5 Sonnet v1/v2、Claude 3.7 Sonnet、Claude Opus 4 和 Claude Sonnet 4。

## 前置条件

- AWS 账号，IAM 权限需包含 `bedrock:CountTokens`
- AWS CLI v2（已支持 `aws bedrock-runtime count-tokens` 命令）
- 目标 Region 已启用对应的 Claude 模型

## 核心概念

Count Tokens API 的设计非常简洁：

| 特性 | 说明 |
|------|------|
| **端点** | `POST /model/{modelId}/count-tokens` |
| **输入格式** | 支持 `invokeModel`（InvokeModel body 格式）和 `converse`（Converse messages 格式） |
| **返回** | `{ "inputTokens": number }` |
| **计费** | 免费，不产生任何费用 |
| **精度** | 返回值与实际推理计费的 inputTokens 完全一致 |
| **权限** | 需要 `bedrock:CountTokens` IAM action |

!!! tip "两种输入格式的区别"
    - **invokeModel 格式**：body 是 JSON 字符串（Base64 编码的 Blob），包含 `anthropic_version`、`max_tokens`、`messages` 等模型特定参数
    - **converse 格式**：直接传 `messages` 和 `system` JSON 对象，使用 Bedrock 统一的 Converse API 格式

## 动手实践

### Step 1: 使用 Converse 格式计算 Token 数

Converse 格式更简洁，推荐优先使用：

```bash
aws bedrock-runtime count-tokens \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --input '{
    "converse": {
      "messages": [
        {
          "role": "user",
          "content": [{"text": "What is the capital of France?"}]
        }
      ]
    }
  }' \
  --region us-west-2 \
  --output json
```

返回结果：

```json
{
    "inputTokens": 14
}
```

### Step 2: 使用 InvokeModel 格式计算 Token 数

InvokeModel 格式的 body 字段是 Blob 类型，在 CLI 中需要 Base64 编码：

```bash
# 先构造请求体 JSON，然后 Base64 编码
BODY=$(echo -n '{"anthropic_version":"bedrock-2023-05-31","max_tokens":500,"messages":[{"role":"user","content":"What is the capital of France?"}]}' | base64 -w0)

aws bedrock-runtime count-tokens \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --input "{\"invokeModel\":{\"body\":\"$BODY\"}}" \
  --region us-west-2 \
  --output json
```

返回结果同样是 14 个 Token。

!!! warning "CLI 踩坑：body 必须 Base64 编码"
    InvokeModel 格式的 `body` 字段在 API 层面是 Blob 类型，AWS CLI 会尝试将其当作 Base64 解码。如果直接传 JSON 字符串，会报 `Invalid base64` 错误。解决方案：先将 JSON body Base64 编码后再传入。（已查文档确认：API Reference 中 body 的类型为 Blob）

### Step 3: 验证与实际推理的一致性

先用 CountTokens 计算 Token 数，再实际调用 Converse 对比：

```bash
# CountTokens 返回 14
aws bedrock-runtime count-tokens \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --input '{"converse":{"messages":[{"role":"user","content":[{"text":"What is the capital of France?"}]}]}}' \
  --region us-west-2 --output json

# Converse 实际推理
aws bedrock-runtime converse \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --messages '[{"role":"user","content":[{"text":"What is the capital of France?"}]}]' \
  --region us-west-2 --output json
```

Converse 返回的 `usage.inputTokens` 也是 **14**，完全一致。

### Step 4: 验证 System Prompt 的 Token 影响

```bash
# 不带 system prompt：14 tokens
aws bedrock-runtime count-tokens \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --input '{"converse":{"messages":[{"role":"user","content":[{"text":"What is the capital of France?"}]}]}}' \
  --region us-west-2 --output json

# 带 system prompt：27 tokens（+13）
aws bedrock-runtime count-tokens \
  --model-id anthropic.claude-3-5-haiku-20241022-v1:0 \
  --input '{"converse":{"messages":[{"role":"user","content":[{"text":"What is the capital of France?"}]}],"system":[{"text":"You are a helpful geography expert. Always answer in one sentence."}]}}' \
  --region us-west-2 --output json
```

System Prompt 的 Token 被正确计入总数，这对预估包含长 System Prompt 的应用成本非常关键。

## 测试结果

### 跨模型 Token 数对比

同一输入 "What is the capital of France?" 在不同 Claude 模型上的 Token 数：

| 模型 | Model ID | inputTokens |
|------|----------|-------------|
| Claude 3.5 Haiku | anthropic.claude-3-5-haiku-20241022-v1:0 | 14 |
| Claude 3.5 Sonnet v2 | anthropic.claude-3-5-sonnet-20241022-v2:0 | 14 |
| Claude 3.5 Sonnet v1 | anthropic.claude-3-5-sonnet-20240620-v1:0 | 14 |

实测发现 Claude 3.5 系列模型对同一输入返回完全相同的 Token 数，说明它们使用了相同的 Tokenizer。

!!! note "注意"
    官方文档指出 "Token counting is model-specific because different models use different tokenization strategies"，所以不同代际的 Claude 模型（如未来的新版本）可能使用不同的分词策略。建议始终针对实际使用的模型 ID 调用 CountTokens。

### 多语言 Token 效率对比

| 语言 | 输入文本 | inputTokens | 相对英文比率 |
|------|----------|-------------|-------------|
| English | "What is the capital of France?" | 14 | 1.0x |
| 日本語 | "フランスの首都はどこですか？" | 19 | 1.36x |
| 中文 + Emoji | "法国的首都是哪里？🇫🇷" | 24 | 1.71x |

**关键发现**：同语义的中文输入约消耗英文 1.7 倍的 Token。对于多语言应用，这个差异会显著影响成本预估。

### 边界条件测试

| 测试场景 | 输入 | 结果 |
|----------|------|------|
| 单字符 | "A" | 8 tokens（含 ~7 tokens 系统开销） |
| 超长文本 | "Hello world. " × 10,000（130K 字符） | 30,008 tokens |
| 空内容 | "" | `ValidationException: user messages must have non-empty content` |
| 非 Claude 模型 | meta.llama3-8b-instruct-v1:0 | `ValidationException: The provided model doesn't support counting tokens.` |

## 踩坑记录

!!! warning "InvokeModel 格式的 body 必须 Base64 编码"
    在 CLI 中使用 InvokeModel 格式时，`body` 字段是 Blob 类型。直接传 JSON 字符串会报 `Invalid base64` 错误。需要先将 JSON 内容 Base64 编码。Converse 格式没有这个问题，在 CLI 场景下更方便。（已查文档确认：API Reference 中 InvokeModel 的 body 类型为 Blob）

!!! warning "仅支持 Claude 模型"
    目前 CountTokens API 仅支持 Anthropic Claude 系列模型。对非 Claude 模型调用会返回明确的 `ValidationException: The provided model doesn't support counting tokens.`。未来可能扩展到更多模型。（已查文档确认：官方文档模型表格仅列出 Anthropic Claude）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CountTokens API | 免费 | 多次 | $0.00 |
| Converse 验证调用 | 按 Token 计费 | 1 次（14 input + 10 output tokens） | < $0.01 |
| **合计** | | | **< $0.01** |

## 清理资源

本 Lab 无需创建任何持久化资源，无需清理。所有操作都是无状态的 API 调用。

## 结论与建议

**Count Tokens API 适合这些场景**：

1. **成本预估**：在批量推理前精确计算 Token 用量，预估总成本
2. **Prompt 优化**：确保输入不超过模型的 Context Window 限制
3. **限流管理**：结合 Token 配额（TPM）做客户端限流，避免 429 错误
4. **多语言应用**：不同语言 Token 效率差异大（中文约 1.7x 英文），预估时必须实测

**生产环境使用建议**：

- 优先使用 **Converse 格式**，更简洁且不需要处理 Base64 编码
- CountTokens 免费，可以大量调用而不用担心成本
- 始终指定实际使用的 **Model ID** 调用，不要假设所有模型的 Token 数相同
- 对于包含 System Prompt 的应用，别忘了将 System Prompt 也纳入 Token 计算

## 参考链接

- [Count Tokens API 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/count-tokens.html)
- [CountTokens API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CountTokens.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/08/count-tokens-api-anthropics-claude-models-bedrock/)
- [Bedrock 模型 Region 可用性](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html)
