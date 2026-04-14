---
tags:
  - Serverless
---

# Amazon EventBridge 1MB 事件负载实测：边界探索与 Target 传递踩坑

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 20 分钟
    - **预估费用**: < $0.05（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

2026 年 1 月 29 日，AWS 宣布 Amazon EventBridge Event Bus 事件负载大小上限从 **256 KB 提升到 1 MB**。

在此之前，开发者处理大型事件数据（如 LLM prompt、遥测信号、ML 模型输出）时，不得不：

- 将大 payload 拆分成多个小事件
- 压缩后再发送
- 把数据存到 S3，事件只传引用（Claim-Check 模式）

现在可以直接把完整数据放进一个事件，**架构更简单，延迟更低**。

本文通过 8 组实测，验证 1 MB 边界的精确行为，并揭露一个官方文档未提及的 **Target 传递踩坑**。

## 前置条件

- AWS 账号（需要 EventBridge 和 CloudWatch Logs 权限）
- AWS CLI v2 已配置
- Python 3（用于生成测试 payload）

## 核心概念

### 之前 vs 现在

| 对比项 | 之前 | 现在 |
|--------|------|------|
| PutEvents 请求大小上限 | 256 KB | **1 MB** |
| 每请求最大 entries | 10 | 10（不变） |
| 大小限制级别 | 请求级（所有 entries 总和） | 请求级（不变） |
| 计费单位 | 每 64 KB = 1 event | 每 64 KB = 1 event（不变） |

### Size 计算方式

EventBridge 计算 entry size 的公式：

```
Entry Size = Time(14B, if present) + Source(UTF-8) + DetailType(UTF-8) + Detail(UTF-8) + Resources(UTF-8)
```

注意：这是 **entry size**，不是最终事件大小。EventBridge 在传递到 target 时会包裹一个信封（envelope），增加约 200+ bytes。

### 计费影响

一个 1 MB 事件 = 约 16 个计费事件（1024 KB ÷ 64 KB）。相比 1 KB 的小事件，费用是 **16 倍**。

根据 EventBridge 定价（$1.00/百万自定义事件），一个 1 MB 事件的成本约 $0.000016。大多数场景下这个成本可以忽略，但如果你有高频大事件，值得关注。

## 动手实践

### Step 1: 创建测试基础设施

创建 EventBridge 自定义事件总线：

```bash
aws events create-event-bus \
  --name test-1mb-payload-bus \
  --region us-east-1
```

创建 CloudWatch Logs 日志组作为事件接收端：

```bash
aws logs create-log-group \
  --log-group-name /aws/events/test-1mb-payload \
  --region us-east-1
```

设置 CloudWatch Logs 资源策略，允许 EventBridge 写入：

```bash
cat > /tmp/cw-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EventBridgeToLogs",
      "Effect": "Allow",
      "Principal": {
        "Service": "events.amazonaws.com"
      },
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:YOUR_ACCOUNT_ID:log-group:/aws/events/test-1mb-payload:*"
    }
  ]
}
EOF

aws logs put-resource-policy \
  --policy-name EventBridgeToLogs \
  --policy-document file:///tmp/cw-policy.json \
  --region us-east-1
```

创建 EventBridge 规则，匹配所有 `test.payload` 来源的事件，路由到 CloudWatch Logs：

```bash
aws events put-rule \
  --name test-1mb-payload-rule \
  --event-bus-name test-1mb-payload-bus \
  --event-pattern '{"source":["test.payload"]}' \
  --state ENABLED \
  --region us-east-1

aws events put-targets \
  --rule test-1mb-payload-rule \
  --event-bus-name test-1mb-payload-bus \
  --targets "Id=cw-logs-target,Arn=arn:aws:logs:us-east-1:YOUR_ACCOUNT_ID:log-group:/aws/events/test-1mb-payload" \
  --region us-east-1
```

### Step 2: 生成测试事件

用 Python 生成指定大小的事件文件：

```python
import json

def generate_event(test_name, target_detail_bytes):
    """生成指定大小的 EventBridge 事件"""
    overhead = len(json.dumps({"test": test_name, "padding": ""}))
    padding = "x" * (target_detail_bytes - overhead)
    detail = json.dumps({"test": test_name, "padding": padding})

    entry = [{
        "Source": "test.payload",
        "DetailType": "PayloadSizeTest",
        "Detail": detail,
        "EventBusName": "test-1mb-payload-bus"
    }]

    # 按 EventBridge 公式计算 entry size
    entry_size = (len("test.payload".encode())
                  + len("PayloadSizeTest".encode())
                  + len(detail.encode()))

    return entry, entry_size

# 生成各种大小的测试事件
tests = {
    "T1-1KB": 1024,
    "T2-256KB": 262144,
    "T3-512KB": 524288,
    "T4-999KB": 1022000,
}

for name, size in tests.items():
    entry, entry_size = generate_event(name, size)
    filename = f"/tmp/event-{name.lower()}.json"
    with open(filename, "w") as f:
        json.dump(entry, f)
    print(f"{name}: Detail={size}B, EntrySize={entry_size}B ({entry_size/1024:.1f}KB)")
```

### Step 3: 发送事件并验证

逐个发送测试事件：

```bash
# T1: 1KB 基线
aws events put-events --entries file:///tmp/event-t1-1kb.json --region us-east-1

# T2: 256KB（旧上限）
aws events put-events --entries file:///tmp/event-t2-256kb.json --region us-east-1

# T3: 512KB（超旧上限）
aws events put-events --entries file:///tmp/event-t3-512kb.json --region us-east-1

# T4: ~999KB（接近新上限）
aws events put-events --entries file:///tmp/event-t4-999kb.json --region us-east-1
```

验证 CloudWatch Logs 接收情况：

```bash
aws logs filter-log-events \
  --log-group-name /aws/events/test-1mb-payload \
  --region us-east-1 \
  --query "events[].{timestamp:timestamp,msgLen:length(message)}" \
  --output table
```

### Step 4: 边界测试 — 精确 1 MB

```python
import json

# 精确构造 1,048,576 bytes (1 MB) 的 entry
target_detail = 1048576 - 12 - 15  # 减去 Source + DetailType
overhead = len(json.dumps({"test": "T5-exact-1MB", "padding": ""}))
padding = "x" * (target_detail - overhead)
detail = json.dumps({"test": "T5-exact-1MB", "padding": padding})

entry = [{
    "Source": "test.payload",
    "DetailType": "PayloadSizeTest",
    "Detail": detail,
    "EventBusName": "test-1mb-payload-bus"
}]

entry_size = len("test.payload".encode()) + len("PayloadSizeTest".encode()) + len(detail.encode())
print(f"Entry size: {entry_size} bytes (== 1MB? {entry_size == 1048576})")

with open("/tmp/event-t5.json", "w") as f:
    json.dump(entry, f)
```

```bash
# 恰好 1MB
aws events put-events --entries file:///tmp/event-t5.json --region us-east-1
# → ✅ 成功！返回 EventId

# 1MB + 1 byte（超过限制）
aws events put-events --entries file:///tmp/event-t6.json --region us-east-1
# → ❌ ValidationException: Total size of the entries in the request is over the limit.
```

### Step 5: 多 Entry Batch 测试

```python
import json

entries = []
for i in range(3):
    padding = "x" * 306000
    detail = json.dumps({"test": f"T7-batch-{i+1}", "padding": padding})
    entries.append({
        "Source": "test.payload",
        "DetailType": "PayloadSizeTest",
        "Detail": detail,
        "EventBusName": "test-1mb-payload-bus"
    })

with open("/tmp/event-t7.json", "w") as f:
    json.dump(entries, f)
# 3 x ~300KB ≈ 900KB total → ✅ 成功
```

## 测试结果

| 测试 | Entry Size | PutEvents | CW Logs 接收 | 备注 |
|------|-----------|-----------|-------------|------|
| T1: 1 KB 基线 | 1,065 B | ✅ | ✅ 1,155 B | Envelope overhead ~213 B |
| T2: 256 KB 旧上限 | 261 KB | ✅ | ✅ 261 KB | 原上限不再是限制 |
| T3: 512 KB | 523 KB | ✅ | ✅ 523 KB | 超旧上限，正常 |
| T4: ~999 KB | 998.0 KB | ✅ | ✅ 998.2 KB | 接近上限，端到端成功 |
| **T5: 恰好 1 MB** | **1,048,576 B** | **✅** | **❌ 未收到** | **⚠️ 见踩坑** |
| T6: 1 MB + 1 B | 1,048,577 B | ❌ | N/A | ValidationException |
| T7: 3×300 KB batch | 896.7 KB 总 | ✅ | ✅ 3 events | 多 entry batch 正常 |

**精确边界**：PutEvents 接受 ≤ 1,048,576 bytes（即 ≤ 1 MB），1 byte 也不能多。

## 踩坑记录

!!! warning "⚠️ 重要发现：接近 1 MB 的事件可能在 Target 传递时被静默丢弃"

    **现象**：T5 测试中，PutEvents 成功接受了恰好 1 MB 的事件（返回了 EventId），但 CloudWatch Logs target 始终没有收到这个事件。

    **根因分析**：

    1. EventBridge 在传递事件到 target 时，会在原始数据外包裹一个 **envelope**（含 `version`、`id`、`detail-type`、`source`、`account`、`time`、`region`、`resources` 字段）
    2. 这个 envelope 的 overhead 约为 **~213 bytes**
    3. 1 MB entry + 213 bytes envelope = **超过 CloudWatch Logs 的单事件 1 MB (1,024 KB) 限制**
    4. 事件被 **静默丢弃**，没有任何错误返回到 PutEvents 调用方

    **影响**：不仅仅是 CloudWatch Logs。任何对输入大小有限制的 target（如 Lambda 的同步调用 payload 限制 6 MB、SNS 的 256 KB 消息限制）都可能出现类似问题。

    **建议**：为安全起见，单个 EventBridge entry 的大小应保持在 **~1,023 KB 以内**（预留 ~1 KB 给 envelope overhead），以确保端到端传递成功。

    *实测发现，官方文档未记录此行为。*

!!! note "文档措辞 vs 实际行为"

    AWS 官方文档（PutEvents API Reference 和 User Guide）均使用 **"less than 1 MB"** 描述大小限制。但实测中，**恰好 1,048,576 bytes (= 1 MB) 的 entry 也被成功接受**。

    建议以实测行为为准，但不建议依赖精确 1 MB 边界 — 留出 envelope 余量更安全。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EventBridge 自定义事件 | $1.00/百万 events | ~128 events (按 64KB 分块) | < $0.01 |
| CloudWatch Logs 采集 | $0.50/GB | ~5 MB | < $0.01 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 1. 删除 EventBridge Rule Target
aws events remove-targets \
  --rule test-1mb-payload-rule \
  --event-bus-name test-1mb-payload-bus \
  --ids cw-logs-target \
  --region us-east-1

# 2. 删除 EventBridge Rule
aws events delete-rule \
  --name test-1mb-payload-rule \
  --event-bus-name test-1mb-payload-bus \
  --region us-east-1

# 3. 删除 EventBridge Event Bus
aws events delete-event-bus \
  --name test-1mb-payload-bus \
  --region us-east-1

# 4. 删除 CloudWatch Log Group
aws logs delete-log-group \
  --log-group-name /aws/events/test-1mb-payload \
  --region us-east-1

# 5. 删除 CloudWatch Logs Resource Policy
aws logs delete-resource-policy \
  --policy-name EventBridgeToLogs \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 EventBridge 和 CloudWatch Logs 的费用很低，但养成好习惯。

## 结论与建议

### 适用场景

1 MB payload 最适合以下场景：

- **LLM/GenAI 工作流** — prompt + context 数据直接放入事件，无需拆分或外存
- **IoT 遥测** — 批量传感器数据一次打包
- **ML Pipeline** — 模型推理结果直接通过事件传递
- **复杂业务事件** — 告别 Claim-Check 模式的额外复杂度

### 生产环境建议

1. **预留 Envelope 余量** — 实际 payload 控制在 ~1,023 KB 以内，为 EventBridge 信封预留空间
2. **关注计费** — 1 MB 事件按 16 个事件计费，高频场景做好成本估算
3. **验证 Target 限制** — 不同 target 有各自的大小限制（如 SNS 256 KB、SQS 256 KB），确认端到端链路无阻塞
4. **检查 Region 支持** — 部分 Region 暂不支持（New Zealand, Thailand, Malaysia, Taipei, Mexico Central）
5. **超大 Payload 仍用 Claim-Check** — 超过 1 MB 的数据继续使用 S3 存储 + 事件引用模式

## 参考链接

- [AWS What's New: Amazon EventBridge increases event payload size from 256 KB to 1 MB](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-eventbridge-increases-event-payload-size-256-kb-1-mb/)
- [Amazon EventBridge PutEvents API Reference](https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutEvents.html)
- [Calculating PutEvents event entry size](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-putevents.html#eb-putevent-size)
- [Amazon EventBridge Quotas](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-quota.html)
- [Amazon EventBridge Pricing](https://aws.amazon.com/eventbridge/pricing/)
