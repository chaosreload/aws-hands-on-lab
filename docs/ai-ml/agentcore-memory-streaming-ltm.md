# AgentCore Memory 长期记忆 Streaming 通知实战：告别轮询，拥抱事件驱动

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $2-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

Amazon Bedrock AgentCore Memory 是 Agent 生态中实现"个性化记忆"的核心能力——它能从对话中自动提取长期记忆（Long-Term Memory），让 Agent 在后续交互中记住用户偏好。

但之前有一个痛点：**你怎么知道记忆被创建或修改了？** 只能轮询 `ListMemoryRecords` API。这在事件驱动架构中很不自然，也带来不必要的 API 调用成本。

2026 年 3 月，AgentCore Memory 新增了 **Streaming Notifications** 功能——通过 Kinesis Data Stream 将记忆记录的增删改事件实时推送到你的账户。本文完整实测这个功能，重点对比：

- **FULL_CONTENT vs METADATA_ONLY** 两种内容级别的实际差异
- **直接创建 vs 异步提取** 两条路径的延迟差异
- 三种事件类型（Created / Updated / Deleted）的 Schema 差异

## 前置条件

- AWS 账号，已开通 Amazon Bedrock AgentCore 访问权限
- AWS CLI v2 已配置（需要 `bedrock-agentcore`、`kinesis`、`iam`、`lambda` 权限）
- 了解 Kinesis Data Stream 基本概念

## 核心概念

### 之前 vs 现在

| | 之前（轮询模式） | 现在（Streaming 模式） |
|---|---|---|
| **感知变化** | 周期性调用 ListMemoryRecords | Kinesis 实时推送事件 |
| **延迟** | 取决于轮询间隔（秒~分钟级） | 直接创建 ~1s，异步提取 ~30s |
| **成本** | 持续 API 调用费 | 仅 Kinesis + 实际事件量 |
| **架构风格** | 请求-响应 | 事件驱动 |

### 事件类型

| 事件类型 | 触发场景 |
|---------|---------|
| `MemoryRecordCreated` | 异步提取（CreateEvent + Memory Strategy）或 BatchCreateMemoryRecords |
| `MemoryRecordUpdated` | BatchUpdateMemoryRecords |
| `MemoryRecordDeleted` | DeleteMemoryRecord / BatchDeleteMemoryRecords / 去重合并 |

### 内容级别

- **FULL_CONTENT**：事件包含 `memoryRecordText`（记忆内容全文），适合下游直接消费
- **METADATA_ONLY**：仅元数据（ID、namespace、策略类型等），需二次调用 API 获取内容

## 动手实践

### Step 1: 创建 Kinesis Data Stream

```bash
aws kinesis create-stream \
  --stream-name agentcore-memory-stream \
  --shard-count 1 \
  --region us-east-1
```

等待状态变为 ACTIVE：

```bash
aws kinesis describe-stream \
  --stream-name agentcore-memory-stream \
  --region us-east-1 \
  --query "StreamDescription.StreamStatus"
# 输出: "ACTIVE"
```

### Step 2: 创建 IAM Role

AgentCore 需要一个 IAM Role 来向你的 Kinesis stream 写入事件。

**Trust Policy**（允许 AgentCore 服务 assume）：

```bash
cat > /tmp/trust-policy.json << 'EOF'
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
EOF

aws iam create-role \
  --role-name AgentCoreMemoryStreamRole \
  --assume-role-policy-document file:///tmp/trust-policy.json
```

**Permissions Policy**（授权写入 Kinesis）：

```bash
cat > /tmp/kinesis-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "kinesis:PutRecords",
        "kinesis:DescribeStream"
      ],
      "Resource": "arn:aws:kinesis:us-east-1:<ACCOUNT_ID>:stream/agentcore-memory-stream"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name AgentCoreMemoryStreamRole \
  --policy-name AgentCoreKinesisAccess \
  --policy-document file:///tmp/kinesis-policy.json
```

> ⚠️ 将 `<ACCOUNT_ID>` 替换为你的 AWS 账户 ID。

### Step 3: 创建 Lambda Consumer

创建一个简单的 Lambda 来消费 Kinesis 事件并记录到 CloudWatch：

```python
# index.py
import json
import base64
from datetime import datetime

def handler(event, context):
    for r in event.get("Records", []):
        payload = base64.b64decode(r["kinesis"]["data"]).decode("utf-8")
        parsed = json.loads(payload)
        event_type = parsed.get("memoryStreamEvent", {}).get("eventType", "unknown")
        memory_id = parsed.get("memoryStreamEvent", {}).get("memoryId", "")
        has_text = "memoryRecordText" in parsed.get("memoryStreamEvent", {})
        print(json.dumps({
            "eventType": event_type,
            "memoryId": memory_id,
            "hasRecordText": has_text,
            "receivedAt": datetime.utcnow().isoformat() + "Z",
            "fullPayload": parsed
        }))
    return {"statusCode": 200}
```

打包部署并添加 Kinesis 触发器：

```bash
# 打包
zip -j /tmp/lambda-consumer.zip index.py

# 创建 Lambda execution role（省略详细步骤）
# 需要 AWSLambdaBasicExecutionRole + AmazonKinesisReadOnlyAccess

# 创建 Lambda
aws lambda create-function \
  --function-name agentcore-memory-consumer \
  --runtime python3.12 \
  --handler index.handler \
  --role arn:aws:iam::<ACCOUNT_ID>:role/agentcore-memory-consumer-role \
  --zip-file fileb:///tmp/lambda-consumer.zip \
  --timeout 60 \
  --region us-east-1

# 添加 Kinesis 触发器
aws lambda create-event-source-mapping \
  --function-name agentcore-memory-consumer \
  --event-source-arn arn:aws:kinesis:us-east-1:<ACCOUNT_ID>:stream/agentcore-memory-stream \
  --starting-position TRIM_HORIZON \
  --batch-size 10 \
  --region us-east-1
```

### Step 4: 创建启用 Streaming 的 Memory

创建两个 Memory，分别使用不同的内容级别进行对比：

**Memory A：FULL_CONTENT 模式**

```bash
cat > /tmp/stream-delivery-full.json << 'EOF'
{
  "resources": [
    {
      "kinesis": {
        "dataStreamArn": "arn:aws:kinesis:us-east-1:<ACCOUNT_ID>:stream/agentcore-memory-stream",
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
EOF

aws bedrock-agentcore-control create-memory \
  --name "streaming_test_full" \
  --description "Memory with FULL_CONTENT streaming" \
  --event-expiry-duration 30 \
  --memory-execution-role-arn "arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreMemoryStreamRole" \
  --stream-delivery-resources file:///tmp/stream-delivery-full.json \
  --region us-east-1
```

**Memory B：METADATA_ONLY 模式**

```bash
cat > /tmp/stream-delivery-meta.json << 'EOF'
{
  "resources": [
    {
      "kinesis": {
        "dataStreamArn": "arn:aws:kinesis:us-east-1:<ACCOUNT_ID>:stream/agentcore-memory-stream",
        "contentConfigurations": [
          {
            "type": "MEMORY_RECORDS",
            "level": "METADATA_ONLY"
          }
        ]
      }
    }
  ]
}
EOF

aws bedrock-agentcore-control create-memory \
  --name "streaming_test_metadata" \
  --description "Memory with METADATA_ONLY streaming" \
  --event-expiry-duration 30 \
  --memory-execution-role-arn "arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreMemoryStreamRole" \
  --stream-delivery-resources file:///tmp/stream-delivery-meta.json \
  --region us-east-1
```

创建成功后，检查 Lambda CloudWatch Logs 应能看到 `StreamingEnabled` 验证事件：

```json
{
  "memoryStreamEvent": {
    "eventType": "StreamingEnabled",
    "memoryId": "streaming_test_full-xxxxx",
    "message": "Streaming enabled for memory resource: streaming_test_full-xxxxx"
  }
}
```

### Step 5: 添加 Memory Strategy

!!! warning "重要发现"
    如果不配置 Memory Strategy，`CreateEvent` 只会写入短期记忆事件，不会触发异步提取为长期记忆，也就不会产生 streaming 事件。

```bash
cat > /tmp/add-strategy.json << 'EOF'
{
  "addMemoryStrategies": [
    {
      "semanticMemoryStrategy": {
        "name": "SemanticExtraction",
        "description": "Extract semantic memories from conversations",
        "namespaceTemplates": ["{actorId}"]
      }
    }
  ]
}
EOF

aws bedrock-agentcore-control update-memory \
  --memory-id "<MEMORY_FULL_ID>" \
  --memory-strategies file:///tmp/add-strategy.json \
  --region us-east-1

aws bedrock-agentcore-control update-memory \
  --memory-id "<MEMORY_META_ID>" \
  --memory-strategies file:///tmp/add-strategy.json \
  --region us-east-1
```

### Step 6: 注入对话，触发 Streaming 事件

**路径 A：通过 CreateEvent 触发异步提取**

```bash
aws bedrock-agentcore create-event \
  --memory-id "<MEMORY_FULL_ID>" \
  --actor-id "test-user" \
  --session-id "test-session-1" \
  --event-timestamp "$(date -u +'%Y-%m-%dT%H:%M:%S.000Z')" \
  --payload '[
    {
      "conversational": {
        "content": {"text": "My favorite programming language is Python and I use VS Code"},
        "role": "USER"
      }
    },
    {
      "conversational": {
        "content": {"text": "Got it! I will remember your preferences."},
        "role": "ASSISTANT"
      }
    }
  ]' \
  --region us-east-1
```

**路径 B：通过 BatchCreateMemoryRecords 直接创建**

```bash
aws bedrock-agentcore batch-create-memory-records \
  --memory-id "<MEMORY_FULL_ID>" \
  --records '[
    {
      "requestIdentifier": "test-1",
      "content": {"text": "User prefers hiking in autumn season"},
      "namespaces": ["hobbies/test-user"],
      "timestamp": "'$(date +%s)'"
    }
  ]' \
  --region us-east-1
```

### Step 7: 验证 Update 和 Delete 事件

```bash
# Update
aws bedrock-agentcore batch-update-memory-records \
  --memory-id "<MEMORY_FULL_ID>" \
  --records '[
    {
      "memoryRecordId": "<RECORD_ID>",
      "content": {"text": "User prefers hiking in autumn and spring seasons"},
      "namespaces": ["hobbies/test-user"],
      "timestamp": "'$(date +%s)'"
    }
  ]' \
  --region us-east-1

# Delete
aws bedrock-agentcore delete-memory-record \
  --memory-id "<MEMORY_FULL_ID>" \
  --memory-record-id "<RECORD_ID>" \
  --region us-east-1
```

## 测试结果

### FULL_CONTENT vs METADATA_ONLY 对比

同一对话（"My favorite programming language is Python and I use VS Code"）分别注入两个 Memory，语义提取后生成的 streaming 事件对比：

| 字段 | FULL_CONTENT | METADATA_ONLY |
|------|-------------|--------------|
| eventType | MemoryRecordCreated | MemoryRecordCreated |
| memoryId | ✅ | ✅ |
| memoryRecordId | ✅ | ✅ |
| namespaces | ✅ | ✅ |
| memoryStrategyType | SEMANTIC | SEMANTIC |
| **memoryRecordText** | **"The user's favorite programming language is Python."** | **❌ 不包含** |

**结论**：两种模式的元数据完全相同，唯一区别是 FULL_CONTENT 包含 `memoryRecordText` 字段。

### 三种事件类型 Schema 差异

| 事件类型 | 包含字段 | hasRecordText (FULL_CONTENT) |
|---------|---------|-----|
| MemoryRecordCreated | memoryId, memoryRecordId, namespaces, createdAt, memoryStrategyId, memoryStrategyType, memoryRecordText* | ✅ |
| MemoryRecordUpdated | 同 Created（含更新后文本） | ✅ |
| MemoryRecordDeleted | **仅** memoryId + memoryRecordId | ❌（不受 content level 影响） |

### 端到端延迟对比

| 路径 | 描述 | 样本 | p50 | p90 | 说明 |
|------|------|------|-----|-----|------|
| BatchCreateMemoryRecords → Kinesis | 直接创建记录 | 10 | **1.05s** | **2.02s** | 几乎实时 |
| CreateEvent → 语义提取 → Kinesis | 对话注入 → LLM 提取 → 推送 | 4 | **~30s** | **~32s** | 主要延迟来自 LLM 推理 |

**关键发现**：BatchCreate 路径延迟亚秒级到 2 秒，几乎实时。CreateEvent 路径因需要 LLM 语义提取，延迟约 30 秒，但 streaming 推送本身的延迟仍 < 2 秒。

## 踩坑记录

!!! warning "Memory 名称不能用连字符"
    `create-memory --name "streaming-test"` 会报 `ValidationException`。名称必须匹配 `[a-zA-Z][a-zA-Z0-9_]{0,47}`，只能用字母、数字和下划线。**已查 API Reference 确认。**

!!! warning "没有 Memory Strategy = 没有异步提取"
    如果 Memory 未配置 Strategy（semantic / summary / episodic 等），`CreateEvent` 只会写入短期记忆事件，不会触发 LLM 提取生成长期记忆记录，自然也不会产生 streaming 事件。这不是 Bug，是 by design——文档说 "via CreateEvent **and memory strategies**"。**已查文档确认。**

!!! warning "BatchUpdateMemoryRecords 的 timestamp 是必填字段"
    即使是更新操作，`timestamp` 仍然是必填参数，否则会报 `ParamValidation` 错误。**实测发现。**

!!! warning "namespaceTemplates 模板变量格式"
    使用 `{actorId}` 而非 `{{actor_id}}`。支持的变量：`{actorId}`、`{sessionId}`、`{memoryStrategyId}`。**已查 API Reference 确认。**

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Kinesis Data Stream (1 shard) | $0.015/hr | ~2 hr | $0.03 |
| Kinesis PUT Units | $0.014/million | < 100 | < $0.01 |
| Lambda 调用 | $0.20/million | < 50 | < $0.01 |
| AgentCore Memory API | 按调用计费 | ~30 calls | < $1.00 |
| **合计** | | | **< $2.00** |

## 清理资源

```bash
# 1. 删除 AgentCore Memories
aws bedrock-agentcore-control delete-memory \
  --memory-id "<MEMORY_FULL_ID>" --region us-east-1
aws bedrock-agentcore-control delete-memory \
  --memory-id "<MEMORY_META_ID>" --region us-east-1

# 2. 删除 Lambda event source mapping
aws lambda delete-event-source-mapping \
  --uuid "<EVENT_SOURCE_MAPPING_UUID>" --region us-east-1

# 3. 删除 Lambda function
aws lambda delete-function \
  --function-name agentcore-memory-consumer --region us-east-1

# 4. 删除 Kinesis Data Stream
aws kinesis delete-stream \
  --stream-name agentcore-memory-stream --region us-east-1

# 5. 删除 IAM Roles
aws iam delete-role-policy \
  --role-name AgentCoreMemoryStreamRole \
  --policy-name AgentCoreKinesisAccess
aws iam delete-role --role-name AgentCoreMemoryStreamRole

aws iam detach-role-policy \
  --role-name agentcore-memory-consumer-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam detach-role-policy \
  --role-name agentcore-memory-consumer-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonKinesisReadOnlyAccess
aws iam delete-role --role-name agentcore-memory-consumer-role
```

!!! danger "务必清理"
    Kinesis Data Stream 按 shard-hour 持续计费（$0.015/hr/shard），Lab 完成后请立即清理。

## 结论与建议

### 适用场景

| 场景 | 推荐模式 | 理由 |
|------|---------|------|
| 用户画像实时更新 | FULL_CONTENT | 下游直接消费记忆内容，无需二次 API 调用 |
| 审计日志 / 合规追踪 | METADATA_ONLY | 只需知道"发生了什么变化"，按需查详情 |
| 数据湖汇聚 | FULL_CONTENT | 直接将记忆流式写入 S3/数据仓库 |
| 跨 Agent 记忆同步 | METADATA_ONLY + API | 轻量通知 + 按需拉取，减少带宽 |

### 生产环境建议

1. **始终配置 Memory Strategy** — 否则 CreateEvent 不会触发 LTM 提取
2. **选择合适的内容级别** — FULL_CONTENT 适合数据管道，METADATA_ONLY 适合轻量触发器
3. **监控 Kinesis 延迟** — 关注 `IteratorAgeMilliseconds` 指标
4. **如果使用 KMS 加密 Kinesis** — 记得在 IAM Policy 中添加 `kms:GenerateDataKey` 权限

## 参考链接

- [Memory record streaming 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-record-streaming.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-memory-streaming-ltm/)
- [AgentCore Memory 概述](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
- [Region 可用性](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
