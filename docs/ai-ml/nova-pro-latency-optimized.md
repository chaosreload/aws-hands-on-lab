# Amazon Nova Pro Latency-Optimized Inference 实测：一个参数降低 33% 推理延迟

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 20 分钟
    - **预估费用**: < $1（API 调用费用）
    - **Region**: us-east-1（Cross-Region Inference）
    - **最后验证**: 2026-03-24

## 背景

对于延迟敏感的 GenAI 应用（聊天机器人、实时助手、代码补全），模型推理延迟直接影响用户体验。Amazon Bedrock 推出了 **Latency-Optimized Inference**（预览），只需在 API 请求中加一个参数，即可获得更快的响应时间——无需微调，无需额外部署。

目前支持的模型包括 Amazon Nova Pro、Anthropic Claude 3.5 Haiku、Meta Llama 3.1 70B/405B。本文以 **Amazon Nova Pro** 为例，实测对比 Standard 和 Optimized 模式的延迟差异。

## 前置条件

- AWS 账号，已开通 Amazon Bedrock 访问权限
- AWS CLI v2 已安装并配置
- 确认 Nova Pro Inference Profile 可用：

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `nova-pro`)].{id:inferenceProfileId, name:inferenceProfileName, status:status}' \
  --output table
```

预期输出：`us.amazon.nova-pro-v1:0` 状态为 `ACTIVE`。

## 核心概念

### 一句话说清楚

在调用 Bedrock Converse / InvokeModel API 时，增加 `performanceConfig.latency` 参数即可切换推理模式：

```json
{
  "performanceConfig": {
    "latency": "optimized"   // 或 "standard"（默认）
  }
}
```

### 关键限制

| 项目 | 说明 |
|------|------|
| **状态** | Preview（可能变更） |
| **调用方式** | 必须通过 Cross-Region Inference Profile（`us.amazon.nova-pro-v1:0`），不支持直接模型 ID |
| **可用 Region** | us-east-1, us-east-2, us-west-2 |
| **Quota 回退** | 达到 optimized 配额后自动回退 standard，按 standard 计费 |
| **默认模式** | 所有请求默认走 standard |

## 动手实践

### Step 1: 准备测试 Payload

创建两个 JSON 文件，分别用于 Standard 和 Optimized 模式：

```bash
# Standard 模式
cat > /tmp/nova-pro-standard.json << 'EOF'
{
  "modelId": "us.amazon.nova-pro-v1:0",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "What are the three main benefits of serverless computing? Answer concisely in 3 bullet points."
        }
      ]
    }
  ],
  "inferenceConfig": {
    "maxTokens": 200,
    "temperature": 0.1
  },
  "performanceConfig": {
    "latency": "standard"
  }
}
EOF

# Optimized 模式（仅改 latency 参数）
cat > /tmp/nova-pro-optimized.json << 'EOF'
{
  "modelId": "us.amazon.nova-pro-v1:0",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "What are the three main benefits of serverless computing? Answer concisely in 3 bullet points."
        }
      ]
    }
  ],
  "inferenceConfig": {
    "maxTokens": 200,
    "temperature": 0.1
  },
  "performanceConfig": {
    "latency": "optimized"
  }
}
EOF
```

### Step 2: 单次调用对比

```bash
# Standard 模式
time aws bedrock-runtime converse --region us-east-1 \
  --cli-input-json file:///tmp/nova-pro-standard.json --output json

# Optimized 模式
time aws bedrock-runtime converse --region us-east-1 \
  --cli-input-json file:///tmp/nova-pro-optimized.json --output json
```

观察响应中的关键字段：

- `metrics.latencyMs`：服务端处理延迟
- `performanceConfig.latency`：实际使用的模式（确认 optimized 生效）
- `usage`：token 消耗统计

### Step 3: 批量延迟测试（10 次采样）

使用以下脚本进行批量测试：

```bash
#!/bin/bash
REGION=us-east-1
PROFILE_ID="us.amazon.nova-pro-v1:0"
RUNS=10
PROMPT="$1"
MAX_TOKENS="${2:-200}"

echo "mode,run,latencyMs,inputTokens,outputTokens"

for MODE in standard optimized; do
  cat > /tmp/bench-payload.json << EOF
{
  "modelId": "$PROFILE_ID",
  "messages": [{"role":"user","content":[{"text":"$PROMPT"}]}],
  "inferenceConfig": {"maxTokens": $MAX_TOKENS, "temperature": 0.1},
  "performanceConfig": {"latency": "$MODE"}
}
EOF
  for i in $(seq 1 $RUNS); do
    RESULT=$(aws bedrock-runtime converse --region $REGION \
      --cli-input-json file:///tmp/bench-payload.json --output json 2>&1)
    PARSED=$(python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
print(d['metrics']['latencyMs'], d['usage']['inputTokens'], d['usage']['outputTokens'])
" <<< "$RESULT")
    echo "$MODE,$i,$PARSED"
    sleep 1
  done
done
```

运行：

```bash
bash bench.sh "What are the three main benefits of serverless computing? Answer concisely in 3 bullet points." 200
```

### Step 4: 边界条件验证

```bash
# 测试 1：无效参数值 → 预期 ValidationException
cat > /tmp/test-invalid.json << 'EOF'
{
  "modelId": "us.amazon.nova-pro-v1:0",
  "messages": [{"role":"user","content":[{"text":"Hello"}]}],
  "inferenceConfig": {"maxTokens": 50},
  "performanceConfig": {"latency": "invalid_value"}
}
EOF
aws bedrock-runtime converse --region us-east-1 \
  --cli-input-json file:///tmp/test-invalid.json 2>&1

# 测试 2：直接模型 ID（非 Inference Profile）→ 预期 ValidationException
cat > /tmp/test-direct.json << 'EOF'
{
  "modelId": "amazon.nova-pro-v1:0",
  "messages": [{"role":"user","content":[{"text":"Hello"}]}],
  "inferenceConfig": {"maxTokens": 50},
  "performanceConfig": {"latency": "optimized"}
}
EOF
aws bedrock-runtime converse --region us-east-1 \
  --cli-input-json file:///tmp/test-direct.json 2>&1
```

## 测试结果

### 短 Prompt 延迟对比（20 input tokens, ~65 output tokens）

| Run | Standard (ms) | Optimized (ms) |
|-----|--------------|----------------|
| 1 | 1392 | 746 |
| 2 | 1301 | 737 |
| 3 | 960 | 716 |
| 4 | 910 | 737 |
| 5 | 818 | 719 |
| 6 | 596 | 715 |
| 7 | 882 | 1039 |
| 8 | 948 | 988 |
| 9 | 1245 | 705 |
| 10 | 1026 | 711 |
| **平均** | **1007.8** | **781.3** |
| **p50** | **954** | **728** |

**短 Prompt 延迟改善：约 22-24%**

### 长 Prompt 延迟对比（145 input tokens, 500 output tokens）

| Run | Standard (ms) | Optimized (ms) |
|-----|--------------|----------------|
| 1 | 5178 | 3338 |
| 2 | 2817 | 3129 |
| 3 | 5756 | 3155 |
| 4 | 5879 | 3591 |
| 5 | 4666 | 3137 |
| 6 | 6277 | 3194 |
| 7 | 5454 | 3043 |
| 8 | 4634 | 3654 |
| 9 | 4903 | 3914 |
| 10 | 5380 | 3846 |
| **平均** | **5094.4** | **3400.1** |
| **p50** | **5279** | **3266** |

**长 Prompt 延迟改善：约 33-38%**

### 关键发现

1. **输出越多，优化效果越显著**：短输出场景改善 ~22%，长输出场景改善 ~33%
2. **Optimized 模式方差更小**：延迟更稳定、更可预测
3. **响应质量一致**：相同 prompt 下 standard 和 optimized 输出语义一致
4. **API 响应可区分模式**：`performanceConfig.latency` 字段明确标识实际使用的模式

### 边界条件测试结果

| 测试 | 输入 | 结果 |
|------|------|------|
| 无效 latency 值 | `"invalid_value"` | `ValidationException: Member must satisfy enum value set: [optimized, standard]` ✅ |
| 直接模型 ID + optimized | `amazon.nova-pro-v1:0` | `ValidationException: Latency performance configuration is not supported for amazon.nova-pro-v1:0 in us-east-1` ✅ |

!!! warning "必须使用 Cross-Region Inference Profile"
    Latency-optimized inference **只能**通过 Cross-Region Inference Profile（如 `us.amazon.nova-pro-v1:0`）调用，直接使用模型 ID（如 `amazon.nova-pro-v1:0`）会返回 ValidationException。这是已查文档确认的设计限制。

## 踩坑记录

!!! warning "CloudTrail 中的额外发现"
    从 CloudTrail 事件中发现，即使在 us-east-1 发起请求，实际推理可能路由到 **us-east-2**（`additionalEventData.inferenceRegion: "us-east-2"`）。这是 Cross-Region Inference 的正常行为——Bedrock 自动选择最优 Region 处理请求。已查文档确认。

!!! tip "Streaming 模式的延迟测量"
    CLI 的 `converse-stream` 命令在收到第一个 chunk 后即返回，wall time 差异不明显。要精确测量 Streaming 模式下的 TTFB（Time to First Token）改善，建议使用 SDK 并记录首个 `contentBlockDelta` 事件的时间戳。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Nova Pro Input Tokens | $0.80/M tokens | ~6K tokens | $0.005 |
| Nova Pro Output Tokens | $3.20/M tokens | ~15K tokens | $0.048 |
| **合计** | | | **< $0.10** |

!!! note "关于 Latency-Optimized 定价"
    Preview 期间，latency-optimized 请求按标准 on-demand 费率计费。超出 optimized 配额自动回退 standard 模式，同样按标准费率计费。

## 清理资源

本实验仅使用 Bedrock API 按调用计费，**无需清理任何持久化资源**。

如需清理临时文件：

```bash
rm -f /tmp/nova-pro-standard.json /tmp/nova-pro-optimized.json \
      /tmp/bench-payload.json /tmp/test-invalid.json /tmp/test-direct.json
```

## 结论与建议

### 适用场景

- **聊天机器人 / 实时助手**：用户感知延迟敏感，22-33% 的改善直接提升体验
- **长文本生成**：报告、代码生成等场景，改善效果更显著（33%+）
- **低成本升级**：只改一个参数，零迁移成本

### 不适用场景

- 对延迟不敏感的批处理任务（标准模式更稳定）
- 需要严格 Region 控制的合规场景（Cross-Region Inference 会跨 Region 路由）

### 生产环境建议

1. **渐进式启用**：先在非关键路径启用 optimized，观察 CloudWatch 指标
2. **监控 quota 回退**：关注 CloudWatch 中 `model-id+latency-optimized` 维度的指标
3. **CloudTrail 审计**：检查 `additionalEventData.performanceConfig.latency` 确认实际模式
4. **注意 Preview 状态**：功能可能变更，建议保留 standard 模式的回退逻辑

## 参考链接

- [Latency-Optimized Inference 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/latency-optimized-inference.html)
- [Cross-Region Inference 用户指南](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
- [Amazon Nova 产品页](https://aws.amazon.com/nova/)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/latency-optimized-inference-amazon-nova-pro-foundation-model-bedrock/)
