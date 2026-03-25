# Amazon Nova 2 Lite Benchmark — Extended Thinking 推理能力实测对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.50（纯 API 调用）
    - **Region**: us-east-1（通过 cross-region inference）
    - **最后验证**: 2026-03-25

## 背景

2025 年 12 月，AWS 发布了 Amazon Nova 2 — 第二代自研基础模型系列。其中 **Nova 2 Lite** 是首个 GA 的 Nova 2 模型，最大的亮点是新增了 **Extended Thinking**（扩展思考）能力：模型可以在回答前进行 step-by-step 推理，类似 OpenAI o1/o3 的 chain-of-thought reasoning。

核心问题：**Nova 2 Lite 的推理能力相比 Nova v1 系列提升了多少？Extended Thinking 三个档位（low/medium/high）的性价比如何？**

本文通过 5 个维度的实测 benchmark，给出量化答案。

## 前置条件

- AWS 账号，已开通 Amazon Bedrock 模型访问权限（Nova 2 Lite + Nova v1 系列）
- AWS CLI v2 已配置
- Python 3.9+ 及 boto3

## 核心概念

### Nova 2 vs Nova v1 关键变化

| 特性 | Nova Lite v1 | Nova 2 Lite | 变化 |
|------|-------------|-------------|------|
| Context Window | 300K tokens | **1M tokens** | 3.3x ↑ |
| Max Output | 10K tokens | **65K tokens** | 6.5x ↑ |
| Extended Thinking | ❌ | ✅ (low/med/high) | 新增 |
| 内置工具 | ❌ | ✅ Web Grounding + Code Interpreter | 新增 |
| Remote MCP | ❌ | ✅ | 新增 |
| 输入模态 | Text, Image, Video | Text, Image, Video | 不变 |
| 微调 | SFT | SFT + **RFT** | 新增 RFT |

### Extended Thinking 三档详解

| 档位 | 适用场景 | 限制 |
|------|---------|------|
| **low** | 需要结构化思考的复杂任务（代码审查、分析） | 无特殊限制 |
| **medium** | 多步骤任务、编码工作流 | 无特殊限制 |
| **high** | STEM 推理、高级问题解决 | 不能设置 temperature/topP/maxTokens |

!!! warning "重要：推理内容不可见"
    Extended Thinking 的推理过程（reasoning content）显示为 `[REDACTED]`，但**仍然计入 output tokens 费用**。这意味着你无法查看模型的推理过程，但需要为此付费。

### 可用模型清单

截至 2026-03-25，Nova 2 系列在 Bedrock 中的可用状态：

| 模型 | 状态 | Inference Profile ID |
|------|------|---------------------|
| Nova 2 Lite | ✅ GA | `us.amazon.nova-2-lite-v1:0` / `global.amazon.nova-2-lite-v1:0` |
| Nova 2 Pro | ⚠️ Preview（需 Nova Forge 客户资格） | — |
| Nova 2 Sonic | ✅ GA（语音模型） | `us.amazon.nova-2-sonic-v1:0` |

## 动手实践

### Step 1: 确认模型可用性

```bash
# 查看 Nova 2 相关模型
aws bedrock list-foundation-models \
  --region us-east-1 \
  --query "modelSummaries[?contains(modelId, 'nova-2')].{id:modelId,name:modelName,status:modelLifecycle.status}" \
  --output table

# 查看推理 profiles
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'nova')].{id:inferenceProfileId,name:inferenceProfileName}" \
  --output table
```

### Step 2: 基础调用（不开启 Extended Thinking）

```python
import boto3

client = boto3.client("bedrock-runtime", region_name="us-east-1")

response = client.converse(
    modelId="us.amazon.nova-2-lite-v1:0",
    messages=[{
        "role": "user",
        "content": [{"text": "What is 25 * 37 + 128?"}]
    }],
    inferenceConfig={"maxTokens": 4096, "temperature": 0.1},
)

print(response["output"]["message"]["content"][0]["text"])
print(f"Tokens: {response['usage']}")
```

### Step 3: 启用 Extended Thinking

```python
import boto3

client = boto3.client("bedrock-runtime", region_name="us-east-1")

# 启用 Extended Thinking — low 模式
response = client.converse(
    modelId="us.amazon.nova-2-lite-v1:0",
    messages=[{
        "role": "user",
        "content": [{"text": "计算围栏费用：120×80m 田地分割为 15×10m 小块..."}]
    }],
    inferenceConfig={"maxTokens": 4096, "temperature": 0.1},
    additionalModelRequestFields={
        "reasoningConfig": {
            "type": "enabled",
            "maxReasoningEffort": "low"  # low, medium, high
        }
    },
)

# 解析 response
for block in response["output"]["message"]["content"]:
    if "reasoningContent" in block:
        print(f"[Reasoning]: {block['reasoningContent']['reasoningText']['text']}")
    elif "text" in block:
        print(f"[Answer]: {block['text']}")

print(f"Tokens: {response['usage']}")
```

!!! tip "HIGH 模式注意事项"
    使用 `maxReasoningEffort: "high"` 时：

    1. **不能**设置 `temperature`、`topP` 或 `maxTokens`
    2. 建议设置 `read_timeout=3600`（boto3 默认 60 秒可能不够）
    3. 输出可能超过 65K tokens（文档提到最高 128K）

    ```python
    from botocore.config import Config
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        config=Config(read_timeout=3600)
    )
    ```

## 测试结果

### 测试 1: 数学推理

**题目**：多步骤围栏费用计算（120×80m 田地，15×10m 小块，共享边界只建一道围栏，每米 $12.50）

**正确答案**：$22,500

| 模型 | 答案 | 正确？ | 延迟 | Output Tokens |
|------|------|--------|------|---------------|
| **Nova 2 Lite (OFF)** | $22,500 | ✅ | 5.2s | 591 |
| **Nova 2 Lite (LOW)** | $22,500 | ✅ | 9.1s | 1,849 |
| **Nova 2 Lite (MED)** | $22,500 | ✅ | 5.9s | 1,226 |
| **Nova 2 Lite (HIGH)** | $22,500 | ✅ | 94.3s | 7,335 |
| Nova Lite v1 | $12,000 | ❌ | 0.6s | 7 |
| Nova Pro v1 | $63,000 | ❌ | 0.6s | 8 |
| Nova Micro v1 | $12,000 | ❌ | 1.8s | 560 |

🔥 **关键发现**：Nova 2 Lite 所有四档全部正确！Nova v1 全系列（包括 Pro）全部错误！

### 测试 2: 代码生成

**题目**：实现 `merge_intervals` 函数（合并重叠区间）

| 模型 | 代码正确？ | 延迟 | Output Tokens |
|------|-----------|------|---------------|
| Nova 2 Lite (OFF) | ✅ | 0.9s | 100 |
| Nova 2 Lite (LOW) | ✅ | 4.3s | 916 |
| Nova 2 Lite (HIGH) | ✅ | 32.0s | 8,075 |
| Nova Lite v1 | ✅ | 0.8s | 111 |
| Nova Pro v1 | ✅ | 1.2s | 100 |
| Nova Micro v1 | ✅ | 0.8s | 95 |

所有模型均正确生成。对于简单编码任务，开启 Extended Thinking 没有收益但有巨大成本开销。

### 测试 3: Tool Use (Function Calling)

**题目**：查询东京天气并转换温度（3 个工具可选，含一个干扰工具 send_email）

| 模型 | 工具选择 | 参数 | 延迟 |
|------|----------|------|------|
| Nova 2 Lite (OFF) | ✅ get_weather | `{city: "Tokyo"}` | 0.7s |
| Nova 2 Lite (LOW) | ✅ get_weather | `{city: "Tokyo"}` | 20.5s |
| Nova Lite v1 | ✅ get_weather | `{city: "Tokyo", country: "JP"}` | 0.9s |
| Nova Pro v1 | ✅ get_weather | `{city: "Tokyo"}` | 1.1s |

所有模型都正确选择了工具并忽略了干扰工具。Nova 2 Lite (OFF) 最快。

### 测试 4: Vision（图像理解）

**测试图片**：Nova 2 官方 benchmark 对比图表

| 模型 | 描述质量 | 延迟 | Input Tokens |
|------|----------|------|-------------|
| Nova 2 Lite | ✅ 详细识别四个模型名、类别分组、具体数据 | 7.0s | 2,653 |
| Nova Lite v1 | ⚠️ 识别模型名但细节较少 | 3.9s | 1,768 |
| Nova Pro v1 | ✅ 详细描述，识别具体分数 | 12.4s | 1,768 |

Nova 2 Lite 的 Vision 能力明显强于 Lite v1，接近 Pro v1 水平。

### Extended Thinking 代价分析

这是最重要的实测数据——Extended Thinking 的「隐性成本」：

| 指标 | OFF | LOW | MEDIUM | HIGH |
|------|-----|-----|--------|------|
| 数学推理延迟 | 5.2s | 9.1s (**1.8x**) | 5.9s (1.1x) | 94.3s (**18x**) |
| 数学推理 tokens | 591 | 1,849 (**3.1x**) | 1,226 (2.1x) | 7,335 (**12.4x**) |
| 代码生成延迟 | 0.9s | 4.3s (4.8x) | — | 32.0s (**35x**) |
| 代码生成 tokens | 100 | 916 (**9.2x**) | — | 8,075 (**80.8x**) |
| Tool Use 延迟 | 0.7s | 20.5s (**31x**) | — | — |

!!! danger "HIGH 模式的代价"
    HIGH 模式的 token 消耗是 OFF 的 **12-81 倍**，延迟增加 **18-35 倍**。且推理内容显示为 `[REDACTED]`，你付费但看不到思考过程。仅在真正需要深度推理的 STEM 难题中使用 HIGH 模式。

## 踩坑记录

!!! warning "踩坑 1：HIGH 模式可能超时"
    Nova 2 Lite 在 HIGH 模式下处理复杂逻辑题时超时（>5 分钟）。
    
    **原因**：boto3 默认 `read_timeout=60` 秒，但 Nova 2 推理请求最长可达 60 分钟。（已查文档确认：aws-knowledge `core-inference.html`）
    
    **解决**：设置 `Config(read_timeout=3600)`。

!!! warning "踩坑 2：推理内容不可见但计费"
    Extended Thinking 的 reasoning tokens 显示为 `[REDACTED]`，但仍计入 output tokens 费用。HIGH 模式下一次调用可能产生 7,000+ output tokens（其中绝大部分是不可见的推理内容）。
    
    **建议**：在控制成本时，优先使用 OFF 或 LOW 模式。（已查文档确认：aws-knowledge `extended-thinking.html`）

!!! warning "踩坑 3：复杂约束推理仍是弱点"
    Nova 2 Lite 在复杂逻辑约束推理（如 Einstein puzzle）上表现不佳，即使开启 HIGH 模式也可能超时或产生矛盾结果。这是当前 Nova 系列的能力边界。
    
    **建议**：对于复杂逻辑推理任务，考虑使用 Claude 或 GPT 系列模型。

## 费用明细

| 资源 | 费用 |
|------|------|
| Nova 2 Lite API 调用（~42K output tokens） | < $0.10 |
| Nova v1 API 调用（~5K output tokens） | < $0.05 |
| **合计** | **< $0.50** |

## 清理资源

本 Lab 为纯 API 调用，无需清理任何 AWS 资源。

## 结论与建议

### 三个核心发现

1. **Nova 2 Lite 推理能力大幅超越 v1 全系列**
   - 数学推理：v1 全军覆没，Nova 2 Lite 即使不开 thinking 也能正确解题
   - 这不是小幅提升，而是**质的飞跃**

2. **Extended Thinking 是双刃剑**
   - 对需要深度推理的任务（数学、STEM），效果显著
   - 对简单任务（编码、工具调用），纯属浪费
   - HIGH 模式代价极高（12-81x token 消耗），且推理不可见

3. **Nova 2 Lite OFF 模式是性价比之王**
   - 推理能力已经远超 v1 Pro，无需开启 thinking
   - 延迟和 v1 模型相当（<5s）
   - 适合绝大多数生产场景

### 使用建议

| 场景 | 推荐模式 | 理由 |
|------|---------|------|
| 日常对话/客服 | Nova 2 Lite (OFF) | 够用，最快最便宜 |
| 代码生成/审查 | Nova 2 Lite (OFF 或 LOW) | OFF 已足够，LOW 对复杂代码有帮助 |
| 数学/STEM 问题 | Nova 2 Lite (LOW 或 MED) | 显著提升准确率，性价比最优 |
| 顶级难题 | Nova 2 Lite (HIGH) | 仅在确实需要时使用，注意成本和超时 |
| Tool Use/Agent | Nova 2 Lite (OFF) | 最快，thinking 对工具调用无帮助 |
| 图像理解 | Nova 2 Lite (OFF) | 接近 Pro v1 水平，性价比更高 |

### Nova 2 系列定位图

```
                    推理能力 →
                    
  ┌──────────────────────────────────────────────┐
  │                                              │
  │  Nova Micro v1    Nova 2 Lite     Nova 2 Lite│
  │  (text-only)      (OFF)           (HIGH)     │
  │  ⚡最便宜          ⭐性价比之王     🧠最强推理 │
  │                                              │
  │  Nova Lite v1     Nova Pro v1     Nova 2 Pro │
  │  (多模态入门)      (v1 旗舰)       (Preview)  │
  │                                              │
  │                   Nova Premier v1             │
  │                   (1M context)                │
  │                                              │
  └──────────────────────────────────────────────┘
  ↑ 成本
```

**核心观点**：Nova 2 Lite (OFF) 已经取代 Nova Pro v1 成为新的默认选择。如果你还在用 Nova v1 系列，现在是升级的最佳时机。

## 参考链接

- [Amazon Nova 2 What's New](https://aws.amazon.com/about-aws/whats-new/2025/12/nova-2-foundation-models-amazon-bedrock/)
- [AWS Blog: Introducing Amazon Nova 2 Lite](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-2-lite-a-fast-cost-effective-reasoning-model/)
- [Amazon Nova 2 User Guide](https://docs.aws.amazon.com/nova/latest/nova2-userguide/what-is-nova-2.html)
- [Extended Thinking Documentation](https://docs.aws.amazon.com/nova/latest/nova2-userguide/extended-thinking.html)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
