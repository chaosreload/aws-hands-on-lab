---
description: "Benchmark MiniMax M2.5 vs GLM 5 on Amazon Bedrock — agentic tool calling, multi-step reasoning, and token efficiency compared."
---
# Bedrock 新模型实测：MiniMax M2.5 vs GLM 5 — Agentic 能力横评

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.50（纯 API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-19

## 背景

2026 年 3 月 18 日，Amazon Bedrock 同时上线了两个新模型：

- **MiniMax M2.5** — 来自 MiniMax，定位 "agent-native frontier model"，通过 RL 优化 token 效率
- **GLM 5** — 来自 Z.AI（智谱），定位 "frontier-class LLM for complex systems engineering and long-horizon agentic tasks"

两个模型都主打 **Agentic 能力**：多步推理、工具调用、复杂任务分解。

本文通过 **4 个维度的实测对比**，帮你决定哪个模型更适合你的场景。

## 前置条件

- AWS 账号（需要 Bedrock 模型访问权限）
- AWS CLI v2 已配置
- 两个模型都已 **默认 AUTHORIZED**，无需申请 model access

## 核心概念

### 模型基础信息

| | MiniMax M2.5 | GLM 5 |
|---|---|---|
| **Provider** | MiniMax | Z.AI（智谱） |
| **Model ID** | `minimax.minimax-m2.5` | `zai.glm-5` |
| **输入/输出** | TEXT only | TEXT only |
| **Streaming** | ✅ | ✅ |
| **Tool Use** | ✅ | ✅ |
| **Vision** | ❌ | ❌ |
| **内置推理链** | ✅ 自动返回 `reasoningContent` | ❌ |
| **定价（us-east-1）** | $0.30 / $1.20 per 1M tokens | $1.00 / $3.20 per 1M tokens |

**关键差异**：MiniMax M2.5 的每次响应都会自动返回 `reasoningContent`（推理链），类似 Claude 的 extended thinking，但**不需要手动开启**——这直接影响了 output tokens 和延迟。

### 同系列已有模型

Bedrock 上还有这些同家族的模型可选：

- MiniMax：M2、M2.1（前代）
- Z.AI：GLM 4.7、GLM 4.7 Flash

## 动手实践

### Step 1: 确认模型可用性

```bash
# 列出 MiniMax 和 GLM 模型
aws bedrock list-foundation-models \
  --query 'modelSummaries[?contains(modelId, `minimax`) || contains(modelId, `glm`)].{ID:modelId,Name:modelName,Provider:providerName}' \
  --region us-east-1 --output table
```

```bash
# 确认模型已授权
aws bedrock get-foundation-model-availability \
  --model-id minimax.minimax-m2.5 --region us-east-1

aws bedrock get-foundation-model-availability \
  --model-id zai.glm-5 --region us-east-1
```

两个模型的 `authorizationStatus` 都应为 `AUTHORIZED`。

### Step 2: 基础调用测试

```bash
# MiniMax M2.5
aws bedrock-runtime converse \
  --model-id minimax.minimax-m2.5 \
  --messages '[{"role":"user","content":[{"text":"What is 2+2? Reply with just the number."}]}]' \
  --region us-east-1 --output json
```

注意观察 MiniMax M2.5 的返回——即使是如此简单的问题，它也会返回 `reasoningContent`：

```json
{
  "output": {
    "message": {
      "content": [
        {
          "reasoningContent": {
            "reasoningText": {
              "text": "We need to read the conversation... The user wants just the number. So answer \"4\"."
            }
          }
        },
        { "text": "\n\n4" }
      ]
    }
  },
  "usage": { "inputTokens": 51, "outputTokens": 99, "totalTokens": 150 }
}
```

同样的问题，GLM 5：

```json
{
  "output": {
    "message": {
      "content": [{ "text": "4" }]
    }
  },
  "usage": { "inputTokens": 18, "outputTokens": 2, "totalTokens": 20 }
}
```

**Token 差异一目了然**：M2.5 用了 150 tokens（含推理链），GLM 5 只用了 20 tokens。

### Step 3: Tool Use 测试

```bash
# 创建 tool 定义
cat > /tmp/toolconfig.json << 'EOF'
{
  "tools": [{
    "toolSpec": {
      "name": "get_weather",
      "description": "Get current weather for a location",
      "inputSchema": {
        "json": {
          "type": "object",
          "properties": {
            "location": { "type": "string", "description": "City name" },
            "unit": { "type": "string", "enum": ["celsius", "fahrenheit"] }
          },
          "required": ["location"]
        }
      }
    }
  }]
}
EOF

# 测试多工具调用
aws bedrock-runtime converse \
  --model-id minimax.minimax-m2.5 \
  --messages '[{"role":"user","content":[{"text":"What is the weather in Tokyo and New York?"}]}]' \
  --tool-config file:///tmp/toolconfig.json \
  --region us-east-1 --output json
```

两个模型都成功返回了 **2 个并行的 `toolUse` 调用**（`stopReason: tool_use`），正确识别需要分别查询两个城市。

### Step 4: 完整 Benchmark

我们设计了 4 个测试场景 + 1 个速度测试，覆盖 Agentic 关键能力：

| 测试 | 场景 | 考察能力 |
|------|------|---------|
| Math Reasoning | 2^2026 mod 7（Fermat 定理） | 多步推理、数学能力 |
| Code Generation | Thread-safe LRU Cache + TTL | 代码生成质量 |
| Tool Use | 同时查询两个城市天气 | 工具调用、并行决策 |
| Agentic Diagnosis | ECS 503 故障诊断 | 复杂分析、结构化输出 |
| Speed Test | 简单 Q&A × 5 轮 | 延迟稳定性 |

## 实测数据

### 核心对比

| 测试场景 | 模型 | API Latency | Input Tokens | Output Tokens | 内置推理 |
|---------|------|:-----------:|:-----------:|:------------:|:-------:|
| **Math Reasoning** | M2.5 | 28,116ms | 66 | 1,065 | ✅ |
| | GLM 5 | 12,110ms | 33 | 493 | ❌ |
| **Code Generation** | M2.5 | 45,457ms | 61 | 3,234 | ✅ |
| | GLM 5 | 40,894ms | 28 | 1,733 | ❌ |
| **Tool Use** | M2.5 | 3,468ms | 207 | 86 | ✅ |
| | GLM 5 | **1,146ms** | 177 | 39 | ❌ |
| **Agentic Diagnosis** | M2.5 | 50,562ms | 124 | 3,504 | ✅ |
| | GLM 5 | **20,603ms** | 94 | 926 | ❌ |

### Speed Test（5 轮平均）

| 模型 | 平均 Latency | 平均 Output Tokens | 延迟稳定性 |
|------|:-----------:|:-----------------:|:--------:|
| MiniMax M2.5 | 3,985ms | 299 | 稳定（3.3-5.3s） |
| GLM 5 | ~1,700ms* | 73 | 偶有抖动 |

*GLM 5 有一次异常值 15,158ms，剔除后平均约 1,700ms。

### Token 效率 vs 单价：谁更便宜？

虽然 GLM 5 每次请求的 token 消耗远低于 M2.5，但 **GLM 5 的单价显著更高**：

| 指标 | MiniMax M2.5 | GLM 5 |
|------|:-----------:|:-----:|
| **Input 单价** | $0.30 / 1M | $1.00 / 1M |
| **Output 单价** | $1.20 / 1M | $3.20 / 1M |
| 简单问答 Input | 51 tokens | 18 tokens |
| 简单问答 Output | 99 tokens | 2 tokens |
| 复杂任务 Output | 3,504 tokens | 926 tokens |

**实际成本对比（以 Agentic Diagnosis 为例）：**

| 模型 | Input Cost | Output Cost | **单次总成本** |
|------|:---------:|:----------:|:-------------:|
| M2.5 | 124 × $0.30/1M = $0.000037 | 3,504 × $1.20/1M = $0.004205 | **$0.004242** |
| GLM 5 | 94 × $1.00/1M = $0.000094 | 926 × $3.20/1M = $0.002963 | **$0.003057** |

**结论**：GLM 5 单价贵 3x，但因为 token 消耗低（无推理链），复杂任务实际每请求成本仍略低于 M2.5。**简单任务差距更大**——M2.5 简单问答 99 output tokens × $1.20/1M = $0.000119，GLM 5 仅 2 tokens × $3.20/1M = $0.000006，差 20 倍。

M2.5 的 output tokens 显著偏高，主因是**内置推理链始终开启**——推理部分也计入 output tokens 计费。

## 踩坑记录

!!! warning "MiniMax M2.5 的推理链始终开启"
    M2.5 的 `reasoningContent` 是**默认行为**，无法通过 Converse API 关闭。即使是 "2+2=?" 也会产生推理过程。这意味着：
    
    1. Output tokens **始终偏高**，简单场景下成本可能是 GLM 5 的 10-50x
    2. 延迟也会因推理过程而增加
    3. 如果你的应用不需要推理链透明度，这些 tokens 纯属浪费
    
    **建议**：如果需要控制推理 tokens，考虑使用 `inferenceConfig` 的 `maxTokens` 限制。

!!! warning "GLM 5 的 tokenizer 差异"
    同样的 prompt，GLM 5 的 input tokens 只有 M2.5 的约 50%（如 18 vs 51）。这不是因为 GLM 5 "更高效"，而是**不同 tokenizer 的编码方式不同**。在比较成本时，必须同时考虑 token 数量和单价。

!!! warning "GLM 5 偶发延迟抖动"
    5 轮速度测试中，GLM 5 有一次延迟飙到 15,158ms（其余 4 次平均 1,700ms）。可能是冷启动或后端路由波动。生产环境建议设置合理的 timeout 和 retry。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| MiniMax M2.5 API 调用 | ~13,000 output tokens × $1.20/1M | ~$0.016 |
| GLM 5 API 调用 | ~4,500 output tokens × $3.20/1M | ~$0.014 |
| **合计** | | **< $0.05** |

## 清理资源

本 Lab 仅使用 Bedrock On-Demand API 调用，**无需清理任何资源**。不会产生持续费用。

## 结论与建议

### 一句话总结

> **MiniMax M2.5 = 内置 Deep Thinking 的重型推理模型；GLM 5 = 快速高效的轻量 Agent 引擎。**

### 选型指南

| 场景 | 推荐模型 | 原因 |
|------|---------|------|
| **高吞吐 Agent/Chatbot** | GLM 5 | Tool Use 仅 1.1s，延迟低 3x |
| **需要推理透明度** | MiniMax M2.5 | 自动返回推理链，适合审计场景 |
| **成本敏感应用** | 看场景 | M2.5 单价低但 token 多，GLM 5 单价高但 token 省；简单任务 GLM 5 更省，复杂任务两者接近 |
| **复杂分析报告** | MiniMax M2.5 | 输出更详细，格式化更好 |
| **实时交互（< 2s）** | GLM 5 | 简单 Q&A 平均 1.7s |
| **数学/代码竞赛** | 两个都行 | 都答对了，看你更在意速度还是详细度 |

### 与 Bedrock 已有模型的定位对比

```
                  推理深度 →
  速度 ↑  ┌────────────────────────┐
          │ GLM 5       │ M2.5     │
          │ (快+省)     │ (详+透明) │
          ├─────────────┼──────────┤
          │ Nova Pro    │ Claude   │
          │ DeepSeek    │ Sonnet   │
          └─────────────┴──────────┘
```

### 生产环境建议

1. **单价 vs 实际成本要分开看** — M2.5 单价便宜（$0.30/$1.20）但推理链导致 token 多，GLM 5 单价贵（$1.00/$3.20）但 token 省。按实际请求算，简单任务 GLM 5 便宜 20 倍，复杂任务两者接近
2. **GLM 5 适合做 Agent 的 "fast path"** — 工具选择、简单判断用 GLM 5，复杂推理切换到更强模型
3. **两个模型都不支持 Vision** — 如果需要图像输入，仍需 Claude/Nova
4. **建议 A/B 测试** — 在你的实际 prompt 上跑两个模型，看哪个的输出质量/成本比更适合

## 参考链接

- [What's New: MiniMax M2.5 and GLM 5 on Bedrock](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-minimax-glm/)
- [Bedrock Model Regions](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html)
- [Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)
