# 实测 Amazon Bedrock Agents CloudWatch 指标：13 个运行时指标全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-23

## 背景

你部署了一个 Bedrock Agent，但上线后遇到了问题：**延迟偶尔飙高、Token 消耗超预期、偶发的 5xx 错误**——却没有任何数据可以排查。

之前你只能在应用层自己埋点。现在 Amazon Bedrock Agents 原生支持将 **13 个运行时指标**发布到 CloudWatch，命名空间为 `AWS/Bedrock/Agents`。不需要额外开启，只需确保 Agent 的 IAM Role 有 `cloudwatch:PutMetricData` 权限。

本文通过实际创建 Agent 并调用，验证每个指标的行为，包括一个文档中**没有明确说明的发现**。

## 前置条件

- AWS 账号（Bedrock Agent 访问权限）
- AWS CLI v2 + Python 3 + boto3
- 已开通 Claude 3.5 Haiku（或其他 Bedrock 支持的模型）

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

### Step 2: 创建 Bedrock Agent

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region us-east-1)

aws bedrock-agent create-agent \
  --agent-name cw-metrics-test-agent \
  --foundation-model us.anthropic.claude-3-5-haiku-20241022-v1:0 \
  --agent-resource-role-arn arn:aws:iam::${ACCOUNT_ID}:role/bedrock-agent-cw-metrics-test-role \
  --instruction "You are a helpful assistant that answers questions concisely. Keep responses under 100 words." \
  --region us-east-1
```

等待 Agent 状态变为 `PREPARED`：

```bash
aws bedrock-agent get-agent \
  --agent-id <AGENT_ID> \
  --region us-east-1 \
  --query 'agent.agentStatus'
```

!!! warning "模型 ID 注意事项"
    使用推理配置文件 ID（如 `us.anthropic.claude-3-5-haiku-20241022-v1:0`），不要用裸模型 ID（如 `anthropic.claude-3-5-haiku-20241022-v1:0`）。后者在 on-demand 模式下已不被支持，会返回 `validationException`。

### Step 3: 调用 Agent（非流式 + 流式）

使用 Python boto3 调用，因为 `invoke-agent` 是事件流 API：

```python
import boto3, datetime

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

for event in resp['completion']:
    if 'chunk' in event:
        print(event['chunk']['bytes'].decode('utf-8'), end='')
```

### Step 4: 查看 CloudWatch 指标

等待 **3-5 分钟**（指标传播延迟），然后查询：

```bash
# 列出所有已发布的指标
aws cloudwatch list-metrics \
  --namespace "AWS/Bedrock/Agents" \
  --region us-east-1

# 获取具体指标数据
aws cloudwatch get-metric-statistics \
  --namespace "AWS/Bedrock/Agents" \
  --metric-name InvocationCount \
  --dimensions Name=Operation,Value=InvokeAgent \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum \
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

### 指标数据（8 次成功调用）

| 指标 | 值 | 说明 |
|------|-----|------|
| InvocationCount | 8 (成功) + 4 (客户端错误) | 错误调用也被计数 |
| TotalTime | Avg 2218ms, Min 1504ms, Max 3092ms | 端到端延迟 |
| TTFT | Avg 2078ms, Min 1274ms, Max 3092ms | 首 Token 延迟 |
| ModelLatency | Avg 2001ms, Min 1275ms, Max 2884ms | 纯模型推理 |
| **Agent 编排开销** | **~218ms** | TotalTime - ModelLatency |
| InputTokenCount | 总计 2103, 每次 256-271 | 系统提示词占 ~200+ tokens |
| OutputTokenCount | 总计 612, 每次 54-99 | 短回答模式 |

### 维度验证

| 维度组合 | 可见指标数 | 说明 |
|----------|-----------|------|
| Operation | 9/13 | ✅ 全部可见（4 个错误/限流指标未触发是预期的） |
| Operation + ModelId | 部分 | ✅ 符合文档（不含 InvocationCount/TTFT 等） |
| Operation + AgentAliasArn + ModelId | 9/13 | ✅ 最细粒度，全部可见 |

### Alarm 验证

| Alarm | 阈值 | 状态 | 说明 |
|-------|------|------|------|
| high-invocation-count | >50/5min | OK | ✅ 正确，调用量未超阈值 |
| test-trigger-alarm | >1/5min | ALARM | ✅ 正确触发 |

### 无 CW 权限测试

移除 `cloudwatch:PutMetricData` 后调用 Agent：

- ✅ Agent 正常返回结果
- ❌ CloudWatch 中无对应指标
- **结论**：CW 权限是可观测性的前提，但不影响 Agent 功能

## 踩坑记录

!!! warning "踩坑 1：推理配置文件 ID"
    **现象**：使用 `anthropic.claude-3-5-haiku-20241022-v1:0` 创建 Agent 后调用报错 `validationException: Invocation of model ID ... with on-demand throughput isn't supported`。

    **原因**：Bedrock 某些模型已切换为通过推理配置文件（Inference Profile）访问。必须使用 `us.anthropic.claude-3-5-haiku-20241022-v1:0`（带 `us.` 前缀的推理配置文件 ID）。

    **已查文档确认**：这是平台层变更，非 Agent 特定问题。

!!! warning "踩坑 2：IAM Resource 范围"
    **现象**：将 IAM 策略中 `bedrock:InvokeModel` 的 Resource 限制为特定模型 ARN 时，使用推理配置文件的调用报 `accessDeniedException`。

    **原因**：推理配置文件的 ARN 格式 (`arn:aws:bedrock:us:ACCOUNT:inference-profile/...`) 与模型 ARN 不同，需要额外允许。

    **建议**：使用 `Resource: "*"` 或同时允许模型 ARN 和推理配置文件 ARN。

!!! tip "实测发现：TTFT 不仅限于 Streaming"
    官方文档说 TTFT "Emitted when Streaming configuration is enabled"，但**实测发现非流式调用也产生了 TTFT 数据**（SampleCount 与总成功调用数一致）。这可能是 AWS 已更新行为但文档尚未同步。实际使用时可以不区分调用模式来监控 TTFT。

    ⚠️ *实测发现，官方文档与实际行为不完全一致*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Claude 3.5 Haiku Input | $0.001/1K tokens | ~2.1K tokens | ~$0.002 |
| Claude 3.5 Haiku Output | $0.005/1K tokens | ~0.6K tokens | ~$0.003 |
| CloudWatch Alarm | $0.10/alarm/month | 2 alarms (几分钟) | ~$0.00 |
| CloudWatch Metrics | 免费 | AWS 服务指标 | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 删除 CloudWatch Alarm
aws cloudwatch delete-alarms \
  --alarm-names bedrock-agent-high-invocation-count bedrock-agent-test-trigger-alarm \
  --region us-east-1

# 2. 删除 Agent Alias（如有自定义 alias）
aws bedrock-agent delete-agent-alias \
  --agent-id <AGENT_ID> \
  --agent-alias-id <ALIAS_ID> \
  --region us-east-1

# 3. 删除 Agent
aws bedrock-agent delete-agent \
  --agent-id <AGENT_ID> \
  --region us-east-1

# 4. 删除 IAM 策略和 Role
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

**生产环境建议：**

1. **必设告警**：InvocationClientErrors > 0、TotalTime P99 > 阈值
2. **Token 监控**：InputTokenCount 趋势监控，检测 prompt 膨胀
3. **多维度分析**：用 AgentAliasArn 维度区分不同版本的性能
4. **Agent 编排开销**：TotalTime - ModelLatency ≈ 200ms（本次测试），这是 Agent 框架的固定开销

**关键发现总结：**

- 指标从调用到可查约 **3 分钟延迟**
- Agent 编排开销约 **200ms**（不含模型推理）
- 系统提示词每次消耗 **200+ input tokens**，是成本大头
- TTFT 实测**在所有调用模式下均有数据**（与文档描述有差异）
- 无 CW 权限不影响 Agent 功能，只影响可观测性

## 参考链接

- [官方文档：Monitor Amazon Bedrock Agents using CloudWatch Metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-agents-cw-metrics.html)
- [AWS What's New: Amazon Bedrock Agents Metrics in CloudWatch](https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-bedrock-agents-metrics-cloudwatch/)
- [CloudWatch User Guide](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/)
