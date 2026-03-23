# 实测 Amazon Bedrock Agents CloudWatch 指标：多模型对比 + 13 个运行时指标全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-23
    - **测试模型**: Claude Sonnet 4.6 / Claude Haiku 4.5

## 背景

你部署了一个 Bedrock Agent，但上线后遇到了问题：**延迟偶尔飙高、Token 消耗超预期、偶发的 5xx 错误**——却没有任何数据可以排查。

之前你只能在应用层自己埋点。现在 Amazon Bedrock Agents 原生支持将 **13 个运行时指标**发布到 CloudWatch，命名空间为 `AWS/Bedrock/Agents`。不需要额外开启，只需确保 Agent 的 IAM Role 有 `cloudwatch:PutMetricData` 权限。

本文通过实际创建 Agent 并调用，验证每个指标的行为，并**对比两个不同模型（Claude Sonnet 4.6 vs Haiku 4.5）的性能差异**，包括几个文档中**没有明确说明的发现**。

## 前置条件

- AWS 账号（Bedrock Agent 访问权限）
- AWS CLI v2 + Python 3 + boto3
- 已开通 Claude Sonnet 4.6 和 Claude Haiku 4.5（或其他 Bedrock 支持的模型）

## 核心概念

### 13 个指标一览

| 指标 | 单位 | 说明 |
|------|------|------|
| **InvocationCount** | Count | Agent API 调用次数 |
| **TotalTime** | ms | 服务端处理请求总时间 |
| **TTFT** | ms | 首 Token 延迟（Time-to-First-Token） |
| **ModelLatency** | ms | 模型推理延迟 |
| **ModelInvocationCount** | Count | Agent 对底层模型的调用次数 |
| **InputTokenCount** | Count | 输入 Token 数 |
| **OutputTokenCount** | Count | 输出 Token 数 |
| **InvocationThrottles** | Count | 被限流的调用次数 |
| **InvocationServerErrors** | Count | 服务端错误次数 |
| **InvocationClientErrors** | Count | 客户端错误次数 |
| **ModelInvocationThrottles** | Count | 模型调用被限流次数 |
| **ModelInvocationServerErrors** | Count | 模型调用服务端错误 |
| **ModelInvocationClientErrors** | Count | 模型调用客户端错误 |

### 3 种维度组合

| 维度 | 可用指标 | 使用场景 |
|------|----------|----------|
| **Operation** | 全部 13 个 | 全局视图：所有 Agent 聚合 |
| **Operation + ModelId** | 不含 InvocationCount/TTFT/Throttles/ClientErrors | 按模型对比性能 |
| **Operation + AgentAliasArn + ModelId** | 全部 13 个 | 精确到单个 Agent 版本 |

## 动手实践

### Step 1: 创建 IAM Role

Agent 需要两组权限：调用模型 + 发布 CloudWatch 指标。

```bash
# 创建信任策略
cat > /tmp/trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

# 创建 Role
aws iam create-role \
  --role-name bedrock-agent-cw-metrics-test-role \
  --assume-role-policy-document file:///tmp/trust-policy.json \
  --region us-east-1

# 添加权限策略
cat > /tmp/agent-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "BedrockModelInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:GetInferenceProfile",
                "bedrock:GetFoundationModel"
            ],
            "Resource": "*"
        },
        {
            "Sid": "CloudWatchPutMetrics",
            "Effect": "Allow",
            "Action": "cloudwatch:PutMetricData",
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "cloudwatch:namespace": "AWS/Bedrock/Agents"
                }
            }
        }
    ]
}
EOF

aws iam put-role-policy \
  --role-name bedrock-agent-cw-metrics-test-role \
  --policy-name bedrock-agent-cw-metrics-policy \
  --policy-document file:///tmp/agent-policy.json
```

!!! tip "关键：cloudwatch:PutMetricData"
    `CloudWatchPutMetrics` 策略是指标发布的前提。没有这个权限，Agent **依然正常工作**，只是 CloudWatch 中看不到指标（我们在后面会验证这一点）。`Condition` 限制了只能向 `AWS/Bedrock/Agents` 命名空间发布，遵循最小权限原则。

### Step 2: 创建 Bedrock Agent（多模型对比）

我们创建两个 Agent，分别使用不同模型：

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region us-east-1)

# Agent 1: Claude Sonnet 4.6（最新旗舰）
aws bedrock-agent create-agent \
  --agent-name cw-metrics-sonnet46-agent \
  --foundation-model us.anthropic.claude-sonnet-4-6 \
  --agent-resource-role-arn arn:aws:iam::${ACCOUNT_ID}:role/bedrock-agent-cw-metrics-test-role \
  --instruction "You are a helpful assistant that answers questions concisely. Keep responses under 100 words." \
  --region us-east-1

# Agent 2: Claude Haiku 4.5（轻量快速）
aws bedrock-agent create-agent \
  --agent-name cw-metrics-haiku45-agent \
  --foundation-model us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --agent-resource-role-arn arn:aws:iam::${ACCOUNT_ID}:role/bedrock-agent-cw-metrics-test-role \
  --instruction "You are a helpful assistant that answers questions concisely. Keep responses under 100 words." \
  --region us-east-1
```

等待 Agent 状态变为 `PREPARED`：

```bash
# 准备 Agent
aws bedrock-agent prepare-agent --agent-id <AGENT_ID> --region us-east-1

# 检查状态
aws bedrock-agent get-agent \
  --agent-id <AGENT_ID> \
  --region us-east-1 \
  --query 'agent.agentStatus'
```

!!! warning "模型 ID 注意事项"
    使用推理配置文件 ID（如 `us.anthropic.claude-sonnet-4-6`），不要用裸模型 ID（如 `anthropic.claude-sonnet-4-6`）。后者在 on-demand 模式下可能不被支持，会返回 `validationException`。

### Step 3: 调用 Agent（非流式 + 流式）

使用 Python boto3 调用，因为 `invoke-agent` 是事件流 API：

```python
import boto3, time

session = boto3.Session(region_name='us-east-1')
client = session.client('bedrock-agent-runtime')

# 非流式调用
resp = client.invoke_agent(
    agentId='<AGENT_ID>',
    agentAliasId='TSTALIASID',  # 测试别名，指向 DRAFT 版本
    sessionId='test-001',
    inputText='What is Amazon S3? Answer in one sentence.',
    enableTrace=True
)

for event in resp['completion']:
    if 'chunk' in event:
        print(event['chunk']['bytes'].decode('utf-8'))

# 流式调用
resp = client.invoke_agent(
    agentId='<AGENT_ID>',
    agentAliasId='TSTALIASID',
    sessionId='test-002',
    inputText='What are the benefits of using AWS? List 3 benefits briefly.',
    enableTrace=True,
    streamingConfigurations={'streamFinalResponse': True}
)

first_chunk_time = None
start = time.time()
for event in resp['completion']:
    if 'chunk' in event:
        if first_chunk_time is None:
            first_chunk_time = time.time() - start
            print(f"TTFT (client-side): {first_chunk_time:.2f}s")
        print(event['chunk']['bytes'].decode('utf-8'), end='')
```

### Step 4: 查看 CloudWatch 指标

等待 **3-5 分钟**（指标传播延迟），然后查询：

```bash
# 列出所有已发布的指标
aws cloudwatch list-metrics \
  --namespace "AWS/Bedrock/Agents" \
  --region us-east-1

# 获取具体指标数据（按模型维度）
aws cloudwatch get-metric-statistics \
  --namespace "AWS/Bedrock/Agents" \
  --metric-name TotalTime \
  --dimensions Name=Operation,Value=InvokeAgent \
               Name=ModelId,Value=us.anthropic.claude-sonnet-4-6 \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Average Minimum Maximum \
  --region us-east-1
```

### Step 5: 设置 CloudWatch Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name bedrock-agent-high-invocation-count \
  --alarm-description "Alert when Bedrock Agent invocations exceed 50 per 5 min" \
  --metric-name InvocationCount \
  --namespace "AWS/Bedrock/Agents" \
  --statistic Sum \
  --period 300 \
  --threshold 50 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --dimensions Name=Operation,Value=InvokeAgent \
  --treat-missing-data notBreaching \
  --region us-east-1
```

## 测试结果

### 🆚 模型性能对比（核心数据）

我们对两个模型各执行了 **5 次调用**（4 次非流式 + 1 次流式），使用相同的 prompt，以下是 CloudWatch 实际报告的指标：

| 指标 | Claude Sonnet 4.6 | Claude Haiku 4.5 | 差异 |
|------|--------------------|--------------------|----|
| **TotalTime (Avg)** | 3321 ms | 2075 ms | Haiku 快 **37.5%** |
| **ModelLatency (Avg)** | 3043 ms | 1790 ms | Haiku 快 **41.2%** |
| **Agent 编排开销** | ~278 ms | ~285 ms | 基本一致 |
| **InputTokenCount (Avg)** | 266 tokens | 231 tokens | Sonnet 系统开销略高 |
| **OutputTokenCount (Avg)** | 91 tokens | 67 tokens | Sonnet 回答更详细 |
| **ModelInvocationCount** | 5（1:1） | 5（1:1） | 相同 |
| **TTFT (Streaming, client-side)** | 2.17s | 1.31s | Haiku 快 **39.6%** |

**关键洞察：**

- **编排开销稳定在 ~280ms**：不受模型选择影响，这是 Bedrock Agent 框架的固定开销
- **模型延迟差异显著**：Sonnet 4.6 的平均延迟比 Haiku 4.5 高 ~70%，但回答质量更高（更详细、更有结构）
- **Token 消耗差异**：相同 prompt 下，Sonnet 4.6 的系统 prompt tokens 略多（可能是模型内部优化不同），输出也更丰富

### 每模型详细指标

#### Claude Sonnet 4.6 — 旗舰级推理

| 指标 | 值 | 说明 |
|------|-----|------|
| TotalTime | Avg 3321ms, Min 2275ms, Max 4417ms | 端到端延迟 |
| ModelLatency | Avg 3043ms, Min 2055ms, Max 3973ms | 纯模型推理 |
| InputTokenCount | 总计 1331, 平均 266/次 | 系统提示词占大头 |
| OutputTokenCount | 总计 457, 平均 91/次 | 回答详细 |
| Streaming TTFT | 2.17s（客户端） | 首 Token 延迟 |

#### Claude Haiku 4.5 — 轻量快速

| 指标 | 值 | 说明 |
|------|-----|------|
| TotalTime | Avg 2075ms, Min 1504ms, Max 2800ms | 端到端延迟 |
| ModelLatency | Avg 1790ms, Min 1275ms, Max 2400ms | 纯模型推理 |
| InputTokenCount | 总计 1156, 平均 231/次 | 系统 prompt 更轻 |
| OutputTokenCount | 总计 337, 平均 67/次 | 简洁回答 |
| Streaming TTFT | 1.31s（客户端） | 首 Token 延迟 |

### 维度验证

| 维度组合 | 可见指标数 | 说明 |
|----------|-----------|------|
| Operation | 9/13 | ✅ 全部可见（4 个错误/限流指标未触发是预期的） |
| Operation + ModelId | 部分 | ✅ 符合文档（不含 InvocationCount/TTFT 等） |
| Operation + AgentAliasArn + ModelId | 9/13 | ✅ 最细粒度，可区分不同 Agent/模型 |

!!! success "多模型对比的价值"
    通过 `Operation + ModelId` 维度，可以在 **同一个 CloudWatch 命名空间**下直接对比不同模型的性能。这对于 A/B 测试模型选型非常有价值——不需要额外的埋点代码。

### 无 CW 权限测试

移除 `cloudwatch:PutMetricData` 后调用 Agent：

- ✅ Agent 正常返回结果
- ❌ CloudWatch 中无对应指标
- **结论**：CW 权限是可观测性的前提，但不影响 Agent 功能

## 踩坑记录

!!! warning "踩坑 1：推理配置文件 ID"
    **现象**：使用裸模型 ID（如 `anthropic.claude-sonnet-4-6`）创建 Agent 后调用报错 `validationException: Invocation of model ID ... with on-demand throughput isn't supported`。

    **原因**：Bedrock 新一代模型通过推理配置文件（Inference Profile）访问。必须使用带区域前缀的 ID，如 `us.anthropic.claude-sonnet-4-6`。

    **解决**：用 `aws bedrock list-inference-profiles` 查找正确的推理配置文件 ID。

!!! warning "踩坑 2：IAM Resource 范围"
    **现象**：将 IAM 策略中 `bedrock:InvokeModel` 的 Resource 限制为特定模型 ARN 时，使用推理配置文件的调用报 `accessDeniedException`。

    **原因**：推理配置文件的 ARN 格式 (`arn:aws:bedrock:us:ACCOUNT:inference-profile/...`) 与模型 ARN 不同，需要额外允许。

    **建议**：使用 `Resource: "*"` 或同时允许模型 ARN 和推理配置文件 ARN。

!!! danger "踩坑 3：GLM 模型不兼容 Bedrock Agents"
    **现象**：使用 Z.AI 的 GLM 5 或 GLM 4.7 创建 Agent 后，调用报错 `validationException: This model doesn't support the stopSequences field. Remove stopSequences and try again.`

    **原因**：Bedrock Agent 框架在编排时会自动向模型请求注入 `stopSequences` 参数。GLM 系列模型的 Bedrock API 实现不支持该参数，导致所有 Agent 调用失败。

    **状态**：截至 2026-03-23，GLM 5 / GLM 4.7 / GLM 4.7 Flash **均无法用于 Bedrock Agent**。可以通过 `InvokeModel` API 直接调用，但不能作为 Agent 的基础模型。

    **CloudWatch 表现**：失败的调用仍然产生 `InvocationClientErrors` 和 `ModelInvocationClientErrors` 指标——这证明了 CW 指标对错误监控的价值。

!!! tip "实测发现：TTFT 不仅限于 Streaming"
    官方文档说 TTFT "Emitted when Streaming configuration is enabled"，但**实测发现非流式调用也产生了 TTFT 数据**（SampleCount 与总成功调用数一致）。这可能是 AWS 已更新行为但文档尚未同步。实际使用时可以不区分调用模式来监控 TTFT。

    ⚠️ *实测发现，官方文档与实际行为不完全一致*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Claude Sonnet 4.6 Input | $0.003/1K tokens | ~1.3K tokens | ~$0.004 |
| Claude Sonnet 4.6 Output | $0.015/1K tokens | ~0.5K tokens | ~$0.008 |
| Claude Haiku 4.5 Input | $0.001/1K tokens | ~1.2K tokens | ~$0.001 |
| Claude Haiku 4.5 Output | $0.005/1K tokens | ~0.3K tokens | ~$0.002 |
| CloudWatch Alarm | $0.10/alarm/month | 2 alarms (几分钟) | ~$0.00 |
| CloudWatch Metrics | 免费 | AWS 服务指标 | $0.00 |
| **合计** | | | **< $0.02** |

## 清理资源

```bash
# 1. 删除 CloudWatch Alarm
aws cloudwatch delete-alarms \
  --alarm-names bedrock-agent-high-invocation-count \
  --region us-east-1

# 2. 删除 Agent
aws bedrock-agent delete-agent \
  --agent-id <SONNET_AGENT_ID> \
  --region us-east-1

aws bedrock-agent delete-agent \
  --agent-id <HAIKU_AGENT_ID> \
  --region us-east-1

# 3. 删除 IAM 策略和 Role
aws iam delete-role-policy \
  --role-name bedrock-agent-cw-metrics-test-role \
  --policy-name bedrock-agent-cw-metrics-policy

aws iam delete-role \
  --role-name bedrock-agent-cw-metrics-test-role
```

!!! danger "务必清理"
    虽然 Bedrock Agent 本身不产生闲置费用，但建议删除不再使用的资源以保持账户整洁。CloudWatch Alarm 按月计费。

## 结论与建议

**这个功能适合谁？**

- 所有在生产环境运行 Bedrock Agent 的团队
- 需要监控 Agent 性能 SLA 的运维人员
- 想要优化 Token 消耗和成本的开发者

**模型选型建议（基于 CW 指标）：**

| 场景 | 推荐模型 | 理由 |
|------|----------|------|
| 延迟敏感（聊天机器人） | Haiku 4.5 | TotalTime 低 37%，TTFT 低 40% |
| 质量优先（复杂推理） | Sonnet 4.6 | 回答更详细、更有结构 |
| 成本敏感 | Haiku 4.5 | Token 消耗少，单价低，双重优势 |
| A/B 测试 | 两者同时部署 | 通过 ModelId 维度直接对比 |

**生产环境建议：**

1. **必设告警**：InvocationClientErrors > 0、TotalTime P99 > 阈值
2. **Token 监控**：InputTokenCount 趋势监控，检测 prompt 膨胀
3. **多维度分析**：用 `Operation + ModelId` 维度对比不同模型的性能
4. **Agent 编排开销**：TotalTime - ModelLatency ≈ 280ms，这是 Agent 框架的固定开销，不受模型选择影响
5. **模型兼容性**：选择 Agent 基础模型前，确认模型支持 `stopSequences` 参数（目前 GLM 系列不支持）

**关键发现总结：**

- 指标从调用到可查约 **3 分钟延迟**
- Agent 编排开销约 **280ms**，稳定不受模型影响
- **Sonnet 4.6 vs Haiku 4.5**：延迟差 37-41%，token 差 15-36%，编排开销一致
- **GLM 5/4.7 不兼容 Bedrock Agents**（stopSequences 限制），但错误仍产生 CW 指标
- TTFT 实测**在所有调用模式下均有数据**（与文档描述有差异）
- 无 CW 权限不影响 Agent 功能，只影响可观测性

## 参考链接

- [官方文档：Monitor Amazon Bedrock Agents using CloudWatch Metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-agents-cw-metrics.html)
- [AWS What's New: Amazon Bedrock Agents Metrics in CloudWatch](https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-bedrock-agents-metrics-cloudwatch/)
- [Bedrock Supported Models](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html)
- [CloudWatch User Guide](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/)
