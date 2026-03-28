# Amazon SNS 消息过滤新运算符实战：Wildcard、Anything-but Wildcard 与 Anything-but Prefix

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.01（SNS/SQS 免费额度内）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

Amazon SNS 的消息过滤（Message Filtering）允许订阅者通过 Filter Policy 只接收感兴趣的消息，避免在应用层做额外过滤。2025 年 7 月，AWS 发布了三个新的过滤运算符：

- **Wildcard matching** — 使用 `*` 通配符匹配任意字符序列
- **Anything-but wildcard** — 排除匹配通配符模式的值
- **Anything-but prefix** — 排除匹配特定前缀的值

这些新运算符让过滤策略更加灵活，特别适用于事件路由、日志分级、多环境消息隔离等场景。

## 前置条件

- AWS 账号（需要 SNS 和 SQS 权限）
- AWS CLI v2 已配置
- 基本了解 SNS Topic/Subscription 和 SQS 概念

## 核心概念

### 之前 vs 现在

| 需求 | 之前 | 现在 |
|------|------|------|
| 匹配所有 `*-error` 后缀的事件 | 只能用 suffix 精确匹配，或在订阅端过滤 | `{"wildcard": "*-error"}` 直接过滤 |
| 排除所有 `test-*` 开头的环境 | anything-but 只支持精确值列表 | `{"anything-but": {"wildcard": "test-*"}}` |
| 排除特定前缀的事件 | 需要列举所有要排除的值 | `{"anything-but": {"prefix": "debug-"}}` |
| 匹配复杂路径 `*/src/*.js` | 无法实现，需应用层过滤 | `{"wildcard": "*/src/*.js"}` |

### Wildcard 复杂度规则

SNS 对 wildcard 有专门的复杂度限制：

- 所有字段 wildcard 复杂度总和 ≤ **100 points**
- 每个 pattern 最多 **3 个** `*`
- 计算方式：单 `*` = 1 point，多 `*` = 每个 3 points
- Field complexity = (各 pattern 点数之和) × (pattern 数量)

### Filter Policy Scope

两种 scope 都支持新运算符：

- `MessageAttributes`（默认）— 基于消息属性过滤
- `MessageBody` — 基于消息体 JSON 字段过滤

## 动手实践

### Step 1: 创建 SNS Topic 和 SQS Queue

```bash
# 创建 SNS Topic
aws sns create-topic \
  --name sns-filter-lab \
  --region us-east-1

# 创建 SQS Queue 作为订阅端点
aws sqs create-queue \
  --queue-name sns-filter-lab-queue \
  --region us-east-1
```

记录返回的 Topic ARN 和 Queue URL，后续步骤需要使用。

### Step 2: 配置 SQS 权限

允许 SNS Topic 向 SQS Queue 发送消息：

```bash
# 获取 Queue ARN
QUEUE_ARN=$(aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/sns-filter-lab-queue \
  --attribute-names QueueArn \
  --region us-east-1 \
  --query "Attributes.QueueArn" --output text)

# 设置 SQS Policy（替换 <ACCOUNT_ID> 和 <TOPIC_ARN>）
cat > /tmp/sqs-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "sns.amazonaws.com"},
    "Action": "sqs:SendMessage",
    "Resource": "<QUEUE_ARN>",
    "Condition": {
      "ArnEquals": {"aws:SourceArn": "<TOPIC_ARN>"}
    }
  }]
}
EOF

aws sqs set-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/sns-filter-lab-queue \
  --attributes "{\"Policy\": $(cat /tmp/sqs-policy.json | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" \
  --region us-east-1
```

### Step 3: 测试 Wildcard Matching

创建订阅，使用 wildcard 过滤只接收错误事件：

```bash
# 创建 filter policy 文件
cat > /tmp/sub-attrs.json << 'EOF'
{
  "FilterPolicy": "{\"event\":[{\"wildcard\":\"*-error\"}]}",
  "FilterPolicyScope": "MessageAttributes"
}
EOF

# 创建订阅
aws sns subscribe \
  --topic-arn <TOPIC_ARN> \
  --protocol sqs \
  --notification-endpoint <QUEUE_ARN> \
  --attributes file:///tmp/sub-attrs.json \
  --return-subscription-arn \
  --region us-east-1
```

等待 15 秒让过滤策略生效，然后发送测试消息：

```bash
# 消息 1: 应该被接收（匹配 *-error）
echo '{"event":{"DataType":"String","StringValue":"system-error"}}' > /tmp/attrs.json
aws sns publish \
  --topic-arn <TOPIC_ARN> \
  --message "Disk usage exceeded 90%" \
  --message-attributes file:///tmp/attrs.json \
  --region us-east-1

# 消息 2: 应该被过滤（不匹配 *-error）
echo '{"event":{"DataType":"String","StringValue":"system-warning"}}' > /tmp/attrs.json
aws sns publish \
  --topic-arn <TOPIC_ARN> \
  --message "Disk usage at 70%" \
  --message-attributes file:///tmp/attrs.json \
  --region us-east-1
```

检查 SQS Queue，只有消息 1 被接收：

```bash
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/sns-filter-lab-queue \
  --max-number-of-messages 10 \
  --wait-time-seconds 5 \
  --region us-east-1
```

### Step 4: 测试 Anything-but Wildcard

排除所有测试环境的消息，只接收非测试环境：

```bash
# 先删除旧订阅，创建新订阅
cat > /tmp/sub-attrs.json << 'EOF'
{
  "FilterPolicy": "{\"env\":[{\"anything-but\":{\"wildcard\":\"test-*\"}}]}",
  "FilterPolicyScope": "MessageAttributes"
}
EOF

aws sns subscribe \
  --topic-arn <TOPIC_ARN> \
  --protocol sqs \
  --notification-endpoint <QUEUE_ARN> \
  --attributes file:///tmp/sub-attrs.json \
  --return-subscription-arn \
  --region us-east-1
```

```bash
# 消息: env=prod-us → 应该被接收
echo '{"env":{"DataType":"String","StringValue":"prod-us"}}' > /tmp/attrs.json
aws sns publish --topic-arn <TOPIC_ARN> --message "Deploy success" \
  --message-attributes file:///tmp/attrs.json --region us-east-1

# 消息: env=test-staging → 应该被过滤
echo '{"env":{"DataType":"String","StringValue":"test-staging"}}' > /tmp/attrs.json
aws sns publish --topic-arn <TOPIC_ARN> --message "Test passed" \
  --message-attributes file:///tmp/attrs.json --region us-east-1
```

### Step 5: 测试 Anything-but Prefix

排除所有 debug 级别的事件：

```bash
cat > /tmp/sub-attrs.json << 'EOF'
{
  "FilterPolicy": "{\"event\":[{\"anything-but\":{\"prefix\":\"debug-\"}}]}",
  "FilterPolicyScope": "MessageAttributes"
}
EOF

aws sns subscribe \
  --topic-arn <TOPIC_ARN> \
  --protocol sqs \
  --notification-endpoint <QUEUE_ARN> \
  --attributes file:///tmp/sub-attrs.json \
  --return-subscription-arn \
  --region us-east-1
```

### Step 6: 测试 MessageBody Scope

Wildcard 也支持基于消息体过滤：

```bash
cat > /tmp/sub-attrs.json << 'EOF'
{
  "FilterPolicy": "{\"type\":[{\"wildcard\":\"*_event\"}]}",
  "FilterPolicyScope": "MessageBody"
}
EOF

aws sns subscribe \
  --topic-arn <TOPIC_ARN> \
  --protocol sqs \
  --notification-endpoint <QUEUE_ARN> \
  --attributes file:///tmp/sub-attrs.json \
  --return-subscription-arn \
  --region us-east-1
```

发送包含 JSON body 的消息：

```bash
aws sns publish \
  --topic-arn <TOPIC_ARN> \
  --message '{"type":"click_event","user":"alice"}' \
  --region us-east-1
```

## 测试结果

| 测试场景 | 运算符 | Filter Policy | 消息属性值 | 预期 | 结果 |
|---------|--------|---------------|-----------|------|------|
| Wildcard 后缀匹配 | wildcard | `*-error` | `system-error` | ✅ 收到 | ✅ |
| Wildcard 不匹配 | wildcard | `*-error` | `system-warning` | ❌ 过滤 | ✅ |
| Wildcard 中间匹配 | wildcard | `log-*-2025.txt` | `log-app-2025.txt` | ✅ 收到 | ✅ |
| 多通配符 | wildcard | `*/src/*.js` | `app/src/index.js` | ✅ 收到 | ✅ |
| Anything-but wildcard 放行 | anything-but wildcard | `test-*` | `prod-us` | ✅ 收到 | ✅ |
| Anything-but wildcard 排除 | anything-but wildcard | `test-*` | `test-staging` | ❌ 过滤 | ✅ |
| Anything-but prefix 放行 | anything-but prefix | `debug-` | `info-login` | ✅ 收到 | ✅ |
| Anything-but prefix 排除 | anything-but prefix | `debug-` | `debug-trace` | ❌ 过滤 | ✅ |
| MessageBody + wildcard | wildcard (body) | `*_event` | body: `click_event` | ✅ 收到 | ✅ |
| 全匹配 `*` | wildcard | `*` | `anything` | ✅ 收到 | ✅ |
| 组合 exact + wildcard | mixed | `prod` OR `staging-*` | `staging-us` | ✅ 收到 | ✅ |

**全部 11 项测试通过。**

## 踩坑记录

!!! warning "Filter Policy 生效延迟"
    官方文档声明 "additions or changes to a subscription filter policy require up to 15 minutes to fully take effect"。实测中，新建订阅后 5 秒内发消息可能丢失（过滤策略尚未生效），等待 15 秒后稳定。**建议在生产环境中，变更过滤策略后预留至少 30 秒的缓冲时间。**（已查文档确认：eventual consistency 机制）

!!! warning "多 Wildcard 的复杂度成本"
    单个 pattern 中使用多个 `*`（如 `*/src/*.js`）虽然有效，但复杂度从 1 point 跳到 3×N points。两个 `*` = 6 points，三个 `*` = 9 points。在复杂过滤场景下容易触及 100 points 上限。**建议尽量用单 `*` 配合 prefix/suffix 组合替代多 `*` pattern。**（已查文档确认：subscription-filter-policy-constraints.html）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| SNS Publish | $0.50/百万请求 | ~20 请求 | < $0.01 |
| SQS 请求 | $0.40/百万请求 | ~50 请求 | < $0.01 |
| **合计** | | | **< $0.01** |

SNS 和 SQS 均在免费额度内（每月 100 万请求）。

## 清理资源

```bash
# 1. 删除所有订阅
SUBS=$(aws sns list-subscriptions-by-topic \
  --topic-arn <TOPIC_ARN> \
  --region us-east-1 \
  --query "Subscriptions[].SubscriptionArn" --output text)
for sub in $SUBS; do
  aws sns unsubscribe --subscription-arn "$sub" --region us-east-1
done

# 2. 删除 SNS Topic
aws sns delete-topic --topic-arn <TOPIC_ARN> --region us-east-1

# 3. 删除 SQS Queue
aws sqs delete-queue \
  --queue-url https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/sns-filter-lab-queue \
  --region us-east-1
```

!!! danger "务必清理"
    虽然 SNS/SQS 免费额度充足，Lab 完成后仍建议清理，保持账号整洁。

## 结论与建议

### 适用场景

- **事件驱动架构**：用 wildcard 按事件名称模式路由消息，如 `order-*-completed`
- **多环境隔离**：用 anything-but wildcard 排除测试/开发环境消息，如排除 `dev-*`、`test-*`
- **日志分级**：用 anything-but prefix 排除低级别日志，只订阅 `error-`、`critical-` 前缀的事件
- **文件处理管道**：用 wildcard 匹配文件路径模式，如 `uploads/*/images/*.jpg`

### 生产建议

1. **优先用 prefix/suffix 替代简单 wildcard** — 复杂度更低，性能更好
2. **监控 wildcard 复杂度** — 总上限 100 points，多 `*` pattern 成本高（每个 3 points）
3. **变更过滤策略后预留缓冲** — 至少 30 秒，关键业务建议 1-2 分钟
4. **组合运算符实现复杂逻辑** — exact + wildcard + anything-but 可以在同一 key 中 OR 组合

### 与已有运算符对比

| 运算符 | 引入时间 | 典型场景 |
|--------|---------|---------|
| Exact match | 原始功能 | 精确值匹配 |
| Prefix | 较早 | 前缀匹配（如 `order-`） |
| Suffix | 较早 | 后缀匹配（如 `.json`） |
| **Wildcard** | **2025-07** | **灵活模式匹配（`*` 通配）** |
| Anything-but (exact) | 较早 | 排除特定值 |
| **Anything-but wildcard** | **2025-07** | **排除匹配模式的值** |
| **Anything-but prefix** | **2025-07** | **排除匹配前缀的值** |

## 参考链接

- [Amazon SNS Message Filtering](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html)
- [String Value Matching](https://docs.aws.amazon.com/sns/latest/dg/string-value-matching.html)
- [Filter Policy Constraints](https://docs.aws.amazon.com/sns/latest/dg/subscription-filter-policy-constraints.html)
- [AWS What's New: Amazon SNS launches additional message filtering operators](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-sns-message-filtering-operators/)
