# 实测 Amazon Bedrock 新 CloudWatch Metrics：TimeToFirstToken 和 EstimatedTPMQuotaUsage

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 15 分钟
    - **预估费用**: < $0.50（Bedrock inference + CloudWatch 免费层）
    - **Region**: ap-southeast-1（通过 cross-region inference profile 调用）
    - **最后验证**: 2026-03-19

## 背景

跑 GenAI 生产负载，两个数字最让人抓瞎：

1. **第一个 token 什么时候出来？** — `InvocationLatency` 只给端到端延迟，streaming 场景下用户感知的"响应速度"取决于第一个 token 的延迟（TTFT），之前只能自己在客户端埋点
2. **我的 quota 到底还剩多少？** — `InputTokenCount` / `OutputTokenCount` 是原始 token 数，但 Bedrock 对某些模型有 **output token burndown multiplier**（例如 Claude 系列 5x），100 个 output token 实际吃掉 500 的 TPM quota，不知道真实消耗就会被突然限流

2026 年 3 月 12 日，Bedrock 新增了两个 CloudWatch metrics 来填补这两个空白：

| Metric | 含义 | 适用 API |
|--------|------|---------|
| `TimeToFirstToken` | 服务端测量的 TTFT（ms） | ConverseStream, InvokeModelWithResponseStream |
| `EstimatedTPMQuotaUsage` | 考虑 burndown 后的实际 quota 消耗 | 所有 inference API |

**零配置、零成本、自动上报。** 这篇文章用 7 个测试验证它们是否靠谱。

## 前置条件

- AWS 账号，有 Bedrock inference 权限
- AWS CLI v2 + Python 3 + boto3
- 已开通至少一个 Bedrock 模型的 on-demand 访问

## 核心概念

### TimeToFirstToken

```
┌──────────┐    请求     ┌──────────────┐    第一个 token    ┌──────┐
│  Client   │ ──────────> │   Bedrock    │ ─────────────────> │ User │
└──────────┘             └──────────────┘                    └──────┘
                         |<-- TTFT(ms) -->|
                         |<----------- InvocationLatency ----------->|
```

- **服务端测量** — 不受网络延迟影响，比客户端打时间戳更准
- **仅 streaming API** — 非 streaming 请求没有"第一个 token"的概念
- Dimension：`ModelId`（使用 inference profile ID，如 `global.anthropic.claude-sonnet-4-6`）

### EstimatedTPMQuotaUsage

On-demand 计算公式：

```
EstimatedTPMQuotaUsage = InputTokenCount + CacheWriteInputTokens + (OutputTokenCount × burndown_rate)
```

| 模型 | Output Burndown Rate |
|------|---------------------|
| Claude Sonnet/Opus 4.5/4.6 | **5x** |
| Claude 3.x 系列 | 各不相同 |
| Nova 系列 | 1x（无 burndown） |

**举例**：一个 Claude 请求用了 100 input + 100 output tokens：

- 你看到的 token 数：200
- 你实际消耗的 quota：100 + (100 × 5) = **600 TPM** 🤯
- 你的账单：按 200 tokens 计费（billing ≠ quota）

## 动手实践

### Step 1: 发起 Inference 请求

我们用两个模型、两种 API 来测试：

**Streaming 请求（Claude Sonnet 4.6）— 会产生 TTFT + Quota 两个 metrics：**

```python
import boto3
import time

bedrock = boto3.client('bedrock-runtime', region_name='ap-southeast-1')

t0 = time.time()
first_token_time = None

response = bedrock.converse_stream(
    modelId='global.anthropic.claude-sonnet-4-6',
    messages=[{'role': 'user', 'content': [{'text': 'What is the capital of France? Answer in one sentence.'}]}],
    inferenceConfig={'maxTokens': 200}
)

for event in response['stream']:
    if 'contentBlockDelta' in event:
        if first_token_time is None:
            first_token_time = time.time()
        print(event['contentBlockDelta']['delta'].get('text', ''), end='')
    if 'metadata' in event:
        usage = event['metadata'].get('usage', {})
        print(f"\nInput: {usage.get('inputTokens')}, Output: {usage.get('outputTokens')}")

print(f"Client-side TTFT: {(first_token_time - t0) * 1000:.0f}ms")
```

**Non-streaming 请求（Claude Sonnet 4.6）— 仅产生 Quota metric，无 TTFT：**

```python
response = bedrock.converse(
    modelId='global.anthropic.claude-sonnet-4-6',
    messages=[{'role': 'user', 'content': [{'text': 'What is the capital of France? Answer in one sentence.'}]}],
    inferenceConfig={'maxTokens': 200}
)

usage = response['usage']
print(f"Input: {usage['inputTokens']}, Output: {usage['outputTokens']}")
```

!!! note "必须用 inference profile"
    Claude Sonnet 4.6 不支持直接用 base model ID 调用 on-demand inference，必须用 inference profile（如 `global.anthropic.claude-sonnet-4-6`），否则报 `ValidationException`。

### Step 2: 在 CloudWatch 查看 Metrics

等待约 2 分钟后，用 CLI 确认 metrics 已上报：

```bash
# 列出可用的 TimeToFirstToken metrics
aws cloudwatch list-metrics \
  --namespace AWS/Bedrock \
  --metric-name TimeToFirstToken \
  --region ap-southeast-1

# 查询最近 10 分钟的 TTFT 数据
aws cloudwatch get-metric-statistics \
  --namespace AWS/Bedrock \
  --metric-name TimeToFirstToken \
  --dimensions Name=ModelId,Value=global.anthropic.claude-sonnet-4-6 \
  --start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 60 \
  --statistics Average Minimum Maximum SampleCount \
  --region ap-southeast-1
```

### Step 3: 创建 CloudWatch Alarm

**TTFT 延迟告警** — 当平均 TTFT 超过 1000ms 时触发：

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name bedrock-ttft-alarm \
  --metric-name TimeToFirstToken \
  --namespace AWS/Bedrock \
  --dimensions Name=ModelId,Value=global.anthropic.claude-sonnet-4-6 \
  --statistic Average \
  --period 60 \
  --evaluation-periods 1 \
  --threshold 1000 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --region ap-southeast-1
```

**Quota 消耗告警** — 当每分钟 quota 消耗超过 50K TPM 时预警：

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name bedrock-quota-alarm \
  --metric-name EstimatedTPMQuotaUsage \
  --namespace AWS/Bedrock \
  --dimensions Name=ModelId,Value=global.anthropic.claude-sonnet-4-6 \
  --statistic Sum \
  --period 60 \
  --evaluation-periods 1 \
  --threshold 50000 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --region ap-southeast-1
```

## 实测数据

我们跑了 7 个测试，结果如下：

### TTFT 对比：Claude Sonnet 4.6 vs Nova Lite

| Model | Server-side TTFT (Avg) | Server-side TTFT (Min) | Server-side TTFT (Max) | Requests |
|-------|----------------------|----------------------|----------------------|----------|
| Claude Sonnet 4.6 | **2,683ms** | 972ms | 4,646ms | 4 |
| Nova Lite | **320ms** | 320ms | 320ms | 1 |

> Nova Lite 的 TTFT 比 Claude Sonnet 4.6 快约 **8 倍**。这是模型本身的推理特性差异，TTFT metric 让你能精确量化这个差距并据此做模型选型。

### Prompt 长度对 TTFT 的影响

| Prompt | Input Tokens | Client TTFT | 
|--------|-------------|-------------|
| "Say hello." | 10 | 2,743ms |
| 长分析 prompt | 246 | 4,649ms |

> 更长的 prompt → 更高的 TTFT。246 tokens 的 prompt 比 10 tokens 的慢约 **1.7 倍**。

### Burndown 公式验证（Claude Sonnet 4.6, 5x multiplier）

| 请求 | Input Tokens | Output Tokens | 预期 Quota (input + output×5) |
|------|-------------|---------------|-------------------------------|
| Streaming #1 | 19 | 10 | 69 |
| Non-streaming #1 | 19 | 10 | 69 |
| Non-streaming #2 | 18 | 93 | 483 |
| Streaming #2 | 19 | 10 | 69 |
| Streaming #3 | 10 | 24 | 130 |
| Streaming #4 | 246 | 89 | 691 |
| **合计** | | | **1,511** |

**CloudWatch EstimatedTPMQuotaUsage Sum = 1,511** ✅ 完美匹配公式计算。

### Streaming vs Non-streaming

| API | 上报 TTFT? | 上报 Quota? |
|-----|-----------|------------|
| ConverseStream | ✅ Yes | ✅ Yes |
| Converse | ❌ No（正确行为） | ✅ Yes |

### Cross-Region Inference Profile

| 验证项 | 结果 |
|--------|------|
| ModelId dimension 使用 inference profile ID | ✅ `global.anthropic.claude-sonnet-4-6` |
| 不同 profile 各自独立统计 | ✅ global / apac 分开 |

## 踩坑记录

!!! warning "三个注意事项"

    1. **必须用 inference profile ID** — Claude Sonnet 4.6 / Opus 4.6 等新模型不支持直接用 base model ID (`anthropic.claude-sonnet-4-6`) 调用 on-demand inference，必须用 inference profile（如 `global.anthropic.claude-sonnet-4-6`）。

    2. **不是所有 profile 都有所有模型** — `apac.anthropic.claude-sonnet-4-6` 不存在（APAC 区域 profile 未包含 Sonnet 4.6），需要用 `global` profile。

    3. **Metrics 有 1-2 分钟延迟** — 调用完成后需要等约 2 分钟才能在 CloudWatch 看到数据点，这是 CloudWatch 的标准聚合延迟。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Bedrock inference (Claude Sonnet 4.6) | ~$3/$15 per 1M input/output tokens | ~331 input + 236 output tokens | < $0.01 |
| Bedrock inference (Nova Lite) | ~$0.06/$0.24 per 1M input/output tokens | ~12 input + 8 output tokens | < $0.01 |
| CloudWatch metrics | 免费（自动上报） | - | $0.00 |
| CloudWatch alarms | 免费层（10 个） | 2 个 | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 删除 CloudWatch alarms
aws cloudwatch delete-alarms \
  --alarm-names bedrock-ttft-test-alarm bedrock-quota-test-alarm \
  --region ap-southeast-1
```

!!! tip "无持久资源"
    本 Lab 只使用了 on-demand Bedrock inference 和 CloudWatch（自动上报），除了 CloudWatch alarms 外没有创建任何持久资源。删除 alarms 后即完成清理。

## 结论与建议

### 这两个 Metrics 解决了什么

| 之前 | 现在 |
|------|------|
| 想知道 TTFT → 自己埋点、受网络干扰 | 服务端精确测量，开箱即用 |
| 想知道 quota 消耗 → 自己算 burndown | CloudWatch 直接给出 burndown 后的真实值 |
| 被限流了才知道 quota 不够 | 设 alarm 提前预警 |

### 生产环境建议

1. **TTFT Alarm 必设** — 对延迟敏感的 chatbot / coding assistant，建议设 P95 TTFT alarm（如 > 3000ms），配合 SNS 通知
2. **Quota Alarm 公式**：建议阈值设为 TPM quota 的 **80%**，给自己留 buffer
3. **模型选型要看 TTFT** — Claude Sonnet 4.6 的 TTFT（~2.7s avg）远高于 Nova Lite（~0.3s），如果业务对首字延迟敏感，考虑用更轻量的模型
4. **注意 burndown multiplier** — Claude 系列的 5x output burndown 意味着同样的 output，quota 消耗是 Nova 的 5 倍。高吞吐场景下这个差异很显著
5. **Dashboard 建议**：把 TTFT（P50/P95/P99）+ EstimatedTPMQuotaUsage（Sum/1min）+ InvocationLatency 放在同一个 dashboard，一眼看出性能和容量

## 参考链接

- [官方博客：Improve operational visibility for inference workloads on Amazon Bedrock](https://aws.amazon.com/blogs/machine-learning/improve-operational-visibility-for-inference-workloads-on-amazon-bedrock-with-new-cloudwatch-metrics-for-ttft-and-estimated-quota-consumption/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-bedrock-observability-ttft-quota/)
- [Bedrock Monitoring 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring.html)
- [Token Burndown Multipliers](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html)
