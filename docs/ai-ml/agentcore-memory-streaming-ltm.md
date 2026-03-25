# Amazon Bedrock AgentCore Memory：使用 Streaming Notifications 实时追踪长期记忆变更

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

AI Agent 要做到"记住用户"，需要 Long-Term Memory（LTM）——从对话中自动提取关键信息，跨 session 持久化。Amazon Bedrock AgentCore Memory 提供了这个能力，但之前你只能**轮询** API 才能知道 LTM 是否有变更。

2026 年 3 月，AgentCore Memory 新增了 **Streaming Notifications**：LTM 记录的创建、更新、删除事件会自动推送到你账户的 Kinesis Data Stream，实现真正的事件驱动架构。

**典型场景**：

- Agent 提取用户偏好后，实时同步到 CRM 系统
- LTM 变更触发数据湖更新，构建用户画像
- 审计记忆记录的完整生命周期

## 前置条件

- AWS 账号（需要 Bedrock AgentCore、Kinesis、IAM、Lambda、CloudWatch Logs 权限）
- AWS CLI v2 已配置
- 对 AgentCore Memory 基本概念有了解（短期记忆 vs 长期记忆）

## 核心概念

### Streaming 架构

```
CreateEvent（对话）→ Memory Strategy（异步提取）→ LTM Record 变更
                                                      ↓
                                              Kinesis Data Stream
                                                      ↓
                                              Lambda / 下游消费者
```

### 三种事件类型

| 事件类型 | 触发时机 | 说明 |
|---------|---------|------|
| `MemoryRecordCreated` | LTM 提取完成 / BatchCreate | 新记忆生成 |
| `MemoryRecordUpdated` | BatchUpdate | 记忆内容更新 |
| `MemoryRecordDeleted` | Delete / 去重合并 | 记忆被删除 |

### 两种 Content Level

| Content Level | 事件内容 | 适用场景 |
|--------------|---------|---------|
| `FULL_CONTENT` | 元数据 + `memoryRecordText` 全文 | 直接消费内容的下游处理 |
| `METADATA_ONLY` | 仅元数据（ID、namespace、策略等） | 轻量通知 + 按需 API 查询 |

### Semantic vs Summary 策略

| 维度 | Semantic Memory | Summary Memory |
|------|----------------|----------------|
| 输出数量 | 多条独立记录（每条 = 一个事实） | 1 条合并记录 |
| 输出格式 | 短句 plain text | XML `<topic>` 结构化摘要 |
| Streaming 事件数 | 与事实数量一致（我们的测试中 = 12） | 通常 1 个 |
| 适用场景 | 精确查询单个用户属性 | 快速获取会话概览 |

## 动手实践

### Step 1: 创建 Kinesis Data Stream

```bash
aws kinesis create-stream \
  --stream-name agentcore-memory-stream \
  --stream-mode-details StreamMode=ON_DEMAND \
  --region us-east-1
```

验证状态：

```bash
aws kinesis describe-stream \
  --stream-name agentcore-memory-stream \
  --region us-east-1 \
  --query "StreamDescription.{Status: StreamStatus, ARN: StreamARN}"
```

### Step 2: 创建 IAM Role

AgentCore 需要一个 IAM Role 才能向你的 Kinesis 写入事件。

创建信任策略文件 `trust-policy.json`：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

创建 Role 并附加权限：

```bash
# 创建 Role
aws iam create-role \
  --role-name AgentCoreMemoryStreamRole \
  --assume-role-policy-document file://trust-policy.json

# 附加 Kinesis 权限（替换 ACCOUNT_ID）
aws iam put-role-policy \
  --role-name AgentCoreMemoryStreamRole \
  --policy-name AgentCoreKinesisAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["kinesis:PutRecords", "kinesis:DescribeStream"],
        "Resource": "arn:aws:kinesis:us-east-1:ACCOUNT_ID:stream/agentcore-memory-stream"
      }
    ]
  }'
```

### Step 3: 创建 Lambda Consumer

创建一个简单的 Lambda 来接收和记录 Kinesis 事件。

`index.py`:

```python
import json
import base64
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def handler(event, context):
    for record in event["Records"]:
        payload = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
        parsed = json.loads(payload)
        evt = parsed.get("memoryStreamEvent", {})
        logger.info("EVENT_TYPE=%s | MEMORY_ID=%s | RECORD_ID=%s",
                     evt.get("eventType"), evt.get("memoryId"),
                     evt.get("memoryRecordId", "N/A"))
        if "memoryRecordText" in evt:
            logger.info("RECORD_TEXT=%s", evt["memoryRecordText"][:500])
    return {"statusCode": 200}
```

打包部署：

```bash
# 创建 Lambda 执行 Role（需要 AWSLambdaBasicExecutionRole + AWSLambdaKinesisExecutionRole）
aws lambda create-function \
  --function-name agentcore-stream-consumer \
  --runtime python3.12 \
  --handler index.handler \
  --role arn:aws:iam::ACCOUNT_ID:role/AgentCoreLambdaConsumerRole \
  --zip-file fileb://function.zip \
  --timeout 30 \
  --region us-east-1

# 添加 Kinesis 触发器
aws lambda create-event-source-mapping \
  --function-name agentcore-stream-consumer \
  --event-source-arn arn:aws:kinesis:us-east-1:ACCOUNT_ID:stream/agentcore-memory-stream \
  --starting-position TRIM_HORIZON \
  --batch-size 10 \
  --region us-east-1
```

### Step 4: 创建启用 Streaming 的 Memory

创建配置文件 `create-memory.json`：

```json
{
  "name": "StreamingSemanticMemory",
  "description": "Memory with semantic strategy and FULL_CONTENT streaming",
  "eventExpiryDuration": 30,
  "memoryExecutionRoleArn": "arn:aws:iam::ACCOUNT_ID:role/AgentCoreMemoryStreamRole",
  "memoryStrategies": [
    {
      "semanticMemoryStrategy": {
        "name": "semantic_facts",
        "description": "Extract semantic facts from conversations"
      }
    }
  ],
  "streamDeliveryResources": {
    "resources": [
      {
        "kinesis": {
          "dataStreamArn": "arn:aws:kinesis:us-east-1:ACCOUNT_ID:stream/agentcore-memory-stream",
          "contentConfigurations": [
            {
              "type": "MEMORY_RECORDS",
              "level": "FULL_CONTENT"
            }
          ]
        }
      }
    ]
  }
}
```

```bash
aws bedrock-agentcore-control create-memory \
  --cli-input-json file://create-memory.json \
  --region us-east-1
```

创建完成后（~2 分钟），你的 Lambda 应该收到第一个事件——`StreamingEnabled`：

```json
{
  "memoryStreamEvent": {
    "eventType": "StreamingEnabled",
    "eventTime": "2026-03-25T08:43:31.337Z",
    "memoryId": "StreamingSemanticMemory-e8u5Cd3egX",
    "message": "Streaming enabled for memory resource: StreamingSemanticMemory-e8u5Cd3egX"
  }
}
```

### Step 5: 注入对话，触发 LTM 提取

```bash
aws bedrock-agentcore create-event \
  --memory-id "YOUR_MEMORY_ID" \
  --actor-id "test-user" \
  --session-id "test-session-1" \
  --event-timestamp "$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")" \
  --payload '[
    {
      "conversational": {
        "content": {"text": "I am a software engineer at a fintech startup in Singapore. I use Python and Go."},
        "role": "USER"
      }
    },
    {
      "conversational": {
        "content": {"text": "Great! What kind of projects are you working on?"},
        "role": "ASSISTANT"
      }
    },
    {
      "conversational": {
        "content": {"text": "We are building a real-time fraud detection system using Apache Kafka and Amazon Bedrock. I prefer Claude models for text analysis."},
        "role": "USER"
      }
    },
    {
      "conversational": {
        "content": {"text": "That sounds like a powerful combination for fraud detection!"},
        "role": "ASSISTANT"
      }
    }
  ]' \
  --region us-east-1
```

约 **48 秒**后，Lambda 收到一批 `MemoryRecordCreated` 事件。Semantic 策略从对话中提取出独立事实：

```
RECORD_TEXT=The user is a software engineer working at a fintech startup in Singapore.
RECORD_TEXT=The user prefers using Claude models for text analysis tasks.
RECORD_TEXT=The user is building a real-time fraud detection system.
RECORD_TEXT=The user's system uses Apache Kafka for streaming.
...
```

### Step 6: 验证三种事件类型

**Update 事件**：

```bash
aws bedrock-agentcore batch-update-memory-records \
  --memory-id "YOUR_MEMORY_ID" \
  --records '[{
    "memoryRecordId": "RECORD_ID",
    "content": {"text": "Updated: user now also uses TypeScript for frontend development."},
    "namespaces": ["YOUR_NAMESPACE"],
    "timestamp": "EPOCH_SECONDS"
  }]' \
  --region us-east-1
```

FULL_CONTENT 模式下，`MemoryRecordUpdated` 事件包含更新后的完整文本：

```json
{
  "memoryStreamEvent": {
    "eventType": "MemoryRecordUpdated",
    "memoryRecordText": "Updated: user now also uses TypeScript for frontend development.",
    "memoryStrategyType": "SEMANTIC"
  }
}
```

**Delete 事件**：

```bash
aws bedrock-agentcore delete-memory-record \
  --memory-id "YOUR_MEMORY_ID" \
  --memory-record-id "RECORD_ID" \
  --region us-east-1
```

Delete 事件始终只包含 ID，不受 content level 影响：

```json
{
  "memoryStreamEvent": {
    "eventType": "MemoryRecordDeleted",
    "memoryId": "YOUR_MEMORY_ID",
    "memoryRecordId": "RECORD_ID"
  }
}
```

## 测试结果

### FULL_CONTENT vs METADATA_ONLY 对比

我们创建两个 Memory 用相同对话测试，唯一差异是 content level：

| 字段 | FULL_CONTENT | METADATA_ONLY |
|------|:-----------:|:------------:|
| eventType | ✅ | ✅ |
| eventTime | ✅ | ✅ |
| memoryId | ✅ | ✅ |
| memoryRecordId | ✅ | ✅ |
| namespaces | ✅ | ✅ |
| memoryStrategyId | ✅ | ✅ |
| memoryStrategyType | ✅ | ✅ |
| createdAt | ✅ | ✅ |
| **memoryRecordText** | **✅ 包含完整文本** | **❌ 不包含** |

**关键结论**：`METADATA_ONLY` 适合用作触发器（"知道变了就行"），配合 `GetMemoryRecord` API 按需查询内容；`FULL_CONTENT` 适合直接消费内容的下游处理，省去额外 API 调用。

### Semantic vs Summary 策略 Streaming 事件对比

同一段对话（6 轮，涉及个人信息、技术栈、团队、预算），分别用两种策略处理：

| 维度 | Semantic Memory | Summary Memory |
|------|:--------------:|:-------------:|
| 产出记录数 | **12** | **1** |
| Streaming 事件数 | **12 × MemoryRecordCreated** | **1 × MemoryRecordCreated** |
| 输出格式 | 短句事实 | XML `<topic>` 结构化摘要 |
| 示例内容 | "The user's infrastructure budget is around $15K per month." | `<topic name="Team and Infrastructure">Team consists of 8 engineers...` |
| Namespace 模式 | `/strategies/{id}/actors/{actorId}/` | `/strategies/{id}/actors/{actorId}/sessions/{sessionId}/` |

Semantic 策略的 **12 条事实**包括：

1. 用户是新加坡 fintech startup 的软件工程师
2. 使用 Python 和 Go 已有 5 年
3. 正在构建实时欺诈检测系统
4. 使用 Apache Kafka 做流处理
5. 使用 PostgreSQL 做交易数据库
6. 最近开始用 Amazon Bedrock
7. 偏好 Claude 模型做文本分析
8. 团队 8 人
9. 部署目标 AWS ap-southeast-1
10. 使用 Terraform 部署
11. CI/CD 用 GitHub Actions
12. 月预算约 $15K

Summary 策略则生成 **1 条 XML 摘要**，按 "User Background"、"Fraud Detection System Architecture"、"Team and Infrastructure" 三个主题组织。

### 延迟数据

| 路径 | 延迟 | 说明 |
|------|------|------|
| BatchCreateMemoryRecords → Kinesis 事件 | p50: 1.05s, p90: 2.02s | 直接创建，无 LLM 推理 |
| CreateEvent → 异步提取 → Kinesis 事件 | ~48s | 包含 LLM 语义提取（~46s）+ streaming 发布（~2s） |
| BatchUpdateMemoryRecords → Kinesis 事件 | ~30s | 更新操作 |
| DeleteMemoryRecord → Kinesis 事件 | ~31s | 删除操作 |

## 踩坑记录

!!! warning "Memory name 不支持连字符"
    Memory name 只接受 `[a-zA-Z][a-zA-Z0-9_]{0,47}`，使用 `my-memory` 会报 ValidationException。用下划线替代：`my_memory`。（已查 API Reference 确认）

!!! warning "没有 Memory Strategy = 没有异步 LTM 提取"
    如果创建 Memory 时不配置 `memoryStrategies`，`CreateEvent` 只写入短期记忆，**不会**触发长期记忆提取，也就不会产生 streaming 事件。必须至少配置一个策略（Semantic / Summary / Episodic / User Preference）。（已查文档确认："If no strategies are specified, long-term memory records will not be extracted for that memory."）

!!! warning "BatchUpdateMemoryRecords 需要 timestamp 字段"
    即使是更新操作，`timestamp` 仍为必填参数，遗漏会返回 ParamValidation 错误。（已查 API Reference 确认）

!!! warning "Lambda consumer 注意 f-string 转义"
    通过 SSH heredoc 创建 Lambda 代码时，f-string 中的反斜杠转义容易出问题。建议本地写好 zip 再 SCP 上传。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Kinesis Data Stream (ON_DEMAND) | $0.08/shard-hr | ~0.5 hr | $0.04 |
| Kinesis PUT | $0.014/百万 PUT | ~50 PUT | < $0.01 |
| Lambda 调用 | $0.20/百万 | ~30 次 | < $0.01 |
| AgentCore Memory API | 按调用计费 | ~20 次 | < $0.50 |
| **合计** | | | **< $1.00** |

## 清理资源

按以下顺序清理（先解除依赖，再删资源）：

```bash
REGION=us-east-1

# 1. 删除 AgentCore Memory
aws bedrock-agentcore-control delete-memory \
  --memory-id YOUR_SEMANTIC_MEMORY_ID --region $REGION
aws bedrock-agentcore-control delete-memory \
  --memory-id YOUR_SUMMARY_MEMORY_ID --region $REGION

# 2. 删除 Lambda event source mapping
aws lambda delete-event-source-mapping \
  --uuid YOUR_ESM_UUID --region $REGION

# 3. 删除 Lambda
aws lambda delete-function \
  --function-name agentcore-stream-consumer --region $REGION

# 4. 删除 Kinesis Data Stream
aws kinesis delete-stream \
  --stream-name agentcore-memory-stream --region $REGION

# 5. 删除 IAM Role + Policy
aws iam delete-role-policy \
  --role-name AgentCoreMemoryStreamRole \
  --policy-name AgentCoreKinesisAccess
aws iam delete-role --role-name AgentCoreMemoryStreamRole

aws iam detach-role-policy \
  --role-name AgentCoreLambdaConsumerRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam detach-role-policy \
  --role-name AgentCoreLambdaConsumerRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaKinesisExecutionRole
aws iam delete-role --role-name AgentCoreLambdaConsumerRole

# 6. 删除 CloudWatch Log Group
aws logs delete-log-group \
  --log-group-name /aws/lambda/agentcore-stream-consumer --region $REGION
```

!!! danger "务必清理"
    Kinesis Data Stream 按 shard-hour 持续计费，即使没有数据写入。Lab 完成后请立即清理。

## 结论与建议

### 这个功能解决了什么

AgentCore Memory Streaming Notifications 把"轮询检查记忆变化"变成了"记忆变化主动通知你"。对于需要实时响应 LTM 变更的场景（用户画像同步、审计、数据湖更新），这是架构上的显著简化。

### 生产环境建议

1. **Content Level 选择**：大多数场景用 `METADATA_ONLY` + 按需查询就够了，减少 Kinesis 数据传输量。只有需要直接消费 LTM 全文的下游（如搜索索引更新）才用 `FULL_CONTENT`
2. **策略选择影响事件量**：Semantic 策略产出细粒度事实（每条对话可能 10+ 条），Summary 策略产出 1 条聚合摘要。事件量差异显著，影响 Kinesis 吞吐和 Lambda 调用成本
3. **延迟预期**：异步 LTM 提取含 LLM 推理，端到端 ~48s；直接 API 创建 ~1s。设计下游系统时需考虑这个延迟
4. **错误处理**：建议 Lambda consumer 配置 DLQ（死信队列），处理解析失败的事件
5. **观测性**：AgentCore 提供 CloudWatch Metrics 和 Logs，建议配置告警监控 streaming 投递失败

### 与 AgentCore 系列的关系

这是我们 AgentCore 系列的第四篇。Streaming Notifications 补全了 Memory 服务的"可观测"拼图——你不只能给 Agent 加记忆，还能实时知道 Agent 记住了什么。

## 参考链接

- [AgentCore Memory Streaming 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-record-streaming.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-memory-streaming-ltm/)
- [AgentCore Memory 概览](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
- [Memory 策略文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html)
- [AgentCore Region 可用性](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
