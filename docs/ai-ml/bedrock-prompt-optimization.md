# Amazon Bedrock Prompt Optimization 实测：一键优化 Prompt 的效果到底如何？

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（API 调用费用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

Prompt 工程是使用大语言模型的核心环节，但不同模型有不同的最佳实践——Claude 偏好 XML 标签，Llama 适合简洁指令，Mistral 有自己的格式偏好。手动为每个模型优化 prompt 既耗时又容易遗漏关键技巧。

2024 年 11 月，Amazon Bedrock 推出了 **Prompt Optimization** 预览版。2025 年 4 月正式 GA，支持 Anthropic、Meta、Amazon、Mistral、DeepSeek 等主流模型。它能自动分析你的 prompt 并重写为针对目标模型优化的版本。

**核心问题**：自动优化到底有多大效果？优化后的 prompt 结构有什么规律？不同模型的优化策略有何差异？本文通过 7 组实测对比给出答案。

## 前置条件

- AWS 账号（需要 `bedrock:InvokeModel` 和 `bedrock-agent-runtime:OptimizePrompt` 权限）
- AWS CLI v2 已配置
- Python 3.8+ 和 boto3
- 目标 Region 已启用 Bedrock 模型访问（Claude 3 Haiku、Nova Lite、Llama 3 70B、Mistral Large）

## 核心概念

### Prompt Optimization 是什么？

Prompt Optimization 是 Amazon Bedrock 内置的 prompt 自动优化工具。你提供一段原始 prompt 和目标模型 ID，它会：

1. **分析（Analyze）**：识别 prompt 的任务类型、意图和改进空间
2. **重写（Optimize）**：基于目标模型的最佳实践，生成结构化的优化版本

### 关键特性

| 特性 | 说明 |
|------|------|
| 支持模型 | Claude 3/3.5/3.7/4, Nova Lite/Micro/Pro/Premier, Llama 3/3.1/3.2/3.3/4, Mistral Large, DeepSeek-R1 |
| 输入格式 | 仅文本（textPrompt），不支持多模态 |
| API | `bedrock-agent-runtime` 服务的 `OptimizePrompt` 端点 |
| 响应格式 | 流式（先返回分析事件，再返回优化后 prompt） |
| 推荐语言 | 英文效果最佳 |
| GA Regions | us-east-1, us-west-2, ap-south-1, ap-southeast-2, ca-central-1, eu-central-1, eu-west-1, eu-west-2, eu-west-3, sa-east-1 |

## 动手实践

### Step 1: 准备环境

```bash
# 确保 boto3 已安装
pip install boto3

# 配置 AWS CLI（如已配置可跳过）
aws configure --profile your-profile
```

### Step 2: 调用 Prompt Optimization API

```python
import boto3
import json

# 配置
REGION = "us-east-1"
client = boto3.client('bedrock-agent-runtime', region_name=REGION)

def optimize_prompt(prompt_text, target_model_id):
    """调用 OptimizePrompt API，返回分析结果和优化后 prompt"""
    response = client.optimize_prompt(
        input={"textPrompt": {"text": prompt_text}},
        targetModelId=target_model_id
    )
    
    analysis = None
    optimized = None
    for event in response['optimizedPrompt']:
        if 'analyzePromptEvent' in event:
            analysis = event['analyzePromptEvent'].get('message', '')
        elif 'optimizedPromptEvent' in event:
            opt_data = event['optimizedPromptEvent'].get('optimizedPrompt', {})
            if 'textPrompt' in opt_data:
                optimized = opt_data['textPrompt'].get('text', '')
    
    return analysis, optimized

# 示例：优化一个简单的摘要 prompt
original = "Summarize the key points of cloud computing"
analysis, optimized = optimize_prompt(
    original, 
    "anthropic.claude-3-haiku-20240307-v1:0"
)

print(f"原始 prompt ({len(original)} 字符):")
print(original)
print(f"\n优化后 prompt ({len(optimized)} 字符):")
print(optimized)
```

### Step 3: 对比优化效果

```python
bedrock = boto3.client('bedrock-runtime', region_name=REGION)

def invoke_model(model_id, prompt_text, max_tokens=500):
    """调用 Converse API 获取模型响应"""
    response = bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt_text}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3}
    )
    output = response['output']['message']['content'][0]['text']
    usage = response.get('usage', {})
    return output, usage

# 分类任务对比
original_prompt = "Classify this text as positive or negative: I love this product"
_, optimized_prompt = optimize_prompt(
    original_prompt, 
    "anthropic.claude-3-haiku-20240307-v1:0"
)

# 用原始 prompt 调用模型
output_original, usage_original = invoke_model(
    "anthropic.claude-3-haiku-20240307-v1:0", original_prompt
)
print(f"原始 prompt 输出 ({usage_original.get('outputTokens')} tokens): {output_original}")

# 用优化后 prompt 调用模型
output_optimized, usage_optimized = invoke_model(
    "anthropic.claude-3-haiku-20240307-v1:0", optimized_prompt
)
print(f"优化后输出 ({usage_optimized.get('outputTokens')} tokens): {output_optimized}")
```

### Step 4: 多模型对比优化

```python
# 同一 prompt 针对不同模型优化
base_prompt = "Write a detailed analysis of the pros and cons of microservices architecture"

models = {
    "Claude 3 Haiku": "anthropic.claude-3-haiku-20240307-v1:0",
    "Nova Lite": "amazon.nova-lite-v1:0",
    "Llama 3 70B": "meta.llama3-70b-instruct-v1:0",
    "Mistral Large": "mistral.mistral-large-2402-v1:0"
}

for name, model_id in models.items():
    _, optimized = optimize_prompt(base_prompt, model_id)
    print(f"\n{name}: {len(optimized)} 字符")
    # 查看前 200 字符了解结构差异
    print(optimized[:200])
```

## 测试结果

### 实验 1：Prompt 膨胀度分析

不同任务类型的原始 prompt 经优化后的长度变化：

| 任务类型 | 原始长度 | 优化后长度 | 扩展倍数 | 优化策略特征 |
|---------|---------|-----------|---------|------------|
| 摘要 | 43 字符 | 1,802 字符 | **42x** | 8 维度分析框架 + 输出格式要求 |
| 分类 | 63 字符 | 1,716 字符 | **27x** | 分类标准定义 + 4 个 Few-shot 示例 |
| 代码生成 | 38 字符 | 1,269 字符 | **33x** | 需求列表 + 示例用法 + 输出格式 |
| 推理 | 56 字符 | 794 字符 | **14x** | 结构化指令 + 输出格式（最简洁） |
| 极短（write poem） | 10 字符 | 1,706 字符 | **170x** | 完整诗歌创作要求 |

**关键发现**：系统能智能识别任务类型——分类任务自动添加 few-shot 示例，推理任务保持简洁聚焦，代码生成任务补充了 edge case 处理要求。

### 实验 2：多模型优化策略对比

同一 prompt 针对 4 个模型优化的结构差异：

| 目标模型 | 优化长度 | 耗时 | 结构特征 |
|---------|---------|------|---------|
| Claude 3 Haiku | 2,766 字符 | 24.4s | XML 标签（task_objective/analysis_framework/instructions） |
| Nova Lite | 2,140 字符 | 13.2s | Markdown ## 标题 + 编号 guidelines |
| Llama 3 70B | 1,516 字符 | 14.2s | Markdown ## 标题，更简洁直接 |
| Mistral Large | 1,526 字符 | 15.6s | Markdown ### 子标题，分 Pros/Cons 框架 |

**关键发现**：Claude 模型优化结果使用 XML 标签结构（这是 Anthropic 推荐的最佳实践），而 Nova/Llama/Mistral 统一使用 Markdown 格式但各有差异，证实系统会根据目标模型特点定制优化策略。

### 实验 3：输出质量对比

原始 prompt vs 优化后 prompt 在同一模型上的输出质量：

**分类任务**（Claude 3 Haiku）：

| 指标 | 原始 prompt | 优化后 prompt |
|------|-----------|-------------|
| 输出内容 | "The given text is a positive classification..." (138 字符) | "Positive" (8 字符) |
| 输出 tokens | 32 | **5** |
| 输入 tokens | 20 | 257 |
| 响应时间 | 1.26s | **0.80s** |

优化后的分类任务输出 tokens 减少 **84%**，响应更精准、零冗余。

**推理任务**（Claude 3 Haiku）：

| 指标 | 原始 prompt | 优化后 prompt |
|------|-----------|-------------|
| 答案正确 | ✅ 9 只 | ✅ 9 只 |
| 输出 tokens | 145 | **79** |
| 输出格式 | 冗长分步推导 | 结构化 XML 标签包裹 |

### 实验 4：跨模型使用

为 Claude 优化的 prompt 用在 Nova 上是否可行？

| 调用方式 | 输出 tokens | 输出长度 |
|---------|-----------|---------|
| 原始 prompt → Nova | 373 | 2,070 字符 |
| Claude 优化 prompt → Nova（跨模型） | 291 | 1,423 字符 |
| Nova 优化 prompt → Nova（匹配） | 500 | 2,709 字符 |

**发现**：跨模型使用可行但非最优。匹配优化的 prompt 能更好地发挥目标模型的能力。

### 实验 5：边界测试

| 场景 | 输入 | 结果 |
|------|------|------|
| 极短 prompt | "write poem" (10 字符) | ✅ 成功，生成 1,706 字符的完整诗歌创作指令 |
| 不支持的模型 | titan-text-express-v1 | ❌ `ValidationException: Model not found`（0.3s 快速失败） |

## 踩坑记录

!!! warning "注意事项"
    1. **优化耗时 13-25 秒**：Prompt Optimization 不适合放在实时调用链中。建议在开发/迭代阶段使用，优化好后保存到 Prompt Management 复用。（已查文档确认：设计为开发时使用，非实时推理路径）
    
    2. **输入 tokens 大幅增加**：优化后 prompt 可达原始的 14-170 倍。虽然输出更精准（输出 tokens 减少），但输入成本会上升。需要在精准度和成本间做权衡。
    
    3. **优化格式因模型而异**：Claude 模型优化结果使用 XML 标签，其他模型使用 Markdown。跨模型复用优化后的 prompt 效果会打折扣。（实测发现，官方未明确记录各模型的优化格式差异）
    
    4. **仅支持英文最佳**：官方明确推荐英文 prompt。中文或其他语言的优化效果可能不理想。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OptimizePrompt API | 按 token 计费 | ~15 次调用 | < $0.25 |
| Converse API（Claude 3 Haiku） | $0.25/1M input, $1.25/1M output | ~5 次调用 | < $0.10 |
| Converse API（Nova Lite） | $0.06/1M input, $0.24/1M output | ~3 次调用 | < $0.05 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
# 本 Lab 仅使用 API 调用，无需清理持久化资源
# 如果在 Prompt Management 中保存了优化后的 prompt，可按需删除：
aws bedrock-agent delete-prompt \
    --prompt-identifier YOUR_PROMPT_ID \
    --region us-east-1
```

!!! tip "无需担心"
    本 Lab 不创建任何持久化 AWS 资源，完成后不会产生持续费用。

## 结论与建议

### 核心发现

1. **Prompt Optimization 确实有效**：优化后的 prompt 让模型输出更精准、更结构化，输出 tokens 减少 45-84%
2. **智能任务识别**：系统能自动识别摘要/分类/代码生成/推理等任务类型，采用差异化优化策略
3. **模型特异性优化**：针对不同模型使用不同格式（Claude→XML，Others→Markdown），这是真正的模型适配，不是简单的 prompt 膨胀
4. **Token 经济性权衡**：输入成本上升但输出成本下降，对需要精准输出的场景（分类、提取）收益最大

### 适用场景

| 推荐使用 ✅ | 不推荐 ❌ |
|------------|----------|
| 开发阶段优化 prompt | 实时推理链中调用（13-25s 延迟） |
| 分类/提取等需要精准输出的任务 | 已经精心手工调优过的 prompt |
| 多模型适配（一个 prompt 需要跑多个模型） | 非英文 prompt |
| Prompt 工程新手快速上手 | 简单的一次性查询 |

### 生产环境建议

1. **开发时优化，运行时复用**：在 Prompt Management 中保存优化后的 prompt，运行时直接引用
2. **分类/提取任务优先使用**：这类任务从优化中获益最大（输出 tokens 降幅可达 84%）
3. **为每个目标模型单独优化**：跨模型使用优化 prompt 效果会打折扣
4. **关注总成本**：输入 tokens 增加 + 输出 tokens 减少，算总账决定是否值得

## 参考链接

- [Amazon Bedrock Prompt Optimization 用户指南](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-optimize.html)
- [OptimizePrompt API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_OptimizePrompt.html)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/04/prompt-optimization-amazon-bedrock-generally-available/)
- [Prompt Management 文档](https://aws.amazon.com/bedrock/prompt-management/)
