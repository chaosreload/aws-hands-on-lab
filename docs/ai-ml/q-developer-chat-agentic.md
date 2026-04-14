---
tags:
  - Q Developer
  - What's New
---

# Amazon Q Developer Chat 全新 Agentic 能力：Console 中的 AI 运维助手实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1（Free Tier 内资源 + CloudWatch Alarm $0.10）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

2025 年 6 月，AWS 发布了 Amazon Q Developer Console Chat 的 agentic 能力升级。以前的 Q Developer 只能回答基础 AWS 问题和提供简单指导，现在它可以进行**多步推理**，自动调用 200+ AWS 服务的 API，在 AWS Management Console、Microsoft Teams 和 Slack 中实现深度资源分析和运维排障。

核心变化：Q Developer 从一个"问答机器人"进化为一个"运维分析 Agent"——它能自己拆解复杂问题、选择合适的 API 调用、跨服务关联信息，并展示推理过程。

**本文目标**：搭建一套多服务互联的测试环境（Lambda + S3 + DynamoDB + SNS + CloudWatch），验证 Q Developer 的跨服务分析和排障能力。

## 前置条件

- AWS 账号
- IAM 用户/角色具备以下权限：
    - Amazon Q 聊天权限（`q:StartConversation`, `q:SendMessage` 等）
    - `q:PassRequest`（让 Q 代表你调用 API）
    - `cloudformation:GetResource`, `cloudformation:ListResources`
    - 你要查询的目标服务的只读权限
- AWS CLI v2 已配置

## 核心概念

### 之前 vs 现在

| 维度 | 之前 | 现在（Agentic） |
|------|------|-----------------|
| 查询能力 | 简单 Q&A、基础资源列表 | 多步推理、跨服务关联分析 |
| 推理方式 | 单次 API 调用 | 自动拆解为多步执行计划 |
| 服务覆盖 | 有限 | 200+ AWS 服务（via Cloud Control API + 原生 API）|
| 结果展示 | 纯文本 | Q Artifacts（表格 + 图表可视化）|
| 排障能力 | 基础建议 | 自动查 CloudWatch logs + 分析配置 + 检查权限 |
| 交互方式 | Console 聊天 | Console + Microsoft Teams + Slack |

### 工作原理

Q Developer 的 agentic 推理流程：

1. **理解意图**：解析用户的自然语言查询
2. **制定计划**：创建多步执行计划，选择需要调用的 API
3. **执行查询**：通过 Cloud Control API 和服务原生 API 获取信息
4. **关联分析**：跨服务关联数据，形成完整视图
5. **失败重试**：如果计划失败，尝试替代方案或请求更多信息
6. **展示结果**：以文本或 Q Artifacts（表格/图表）展示

### 关键限制

- **只读操作**：只能执行 get/list/describe，不能修改或删除资源
- **不访问数据内容**：不能查看 S3 对象内容、DynamoDB 记录内容等
- **不操作安全相关**：不能查询安全凭证、身份、加密密钥等
- **排障范围有限**：目前深度排障支持 S3、Glue、Athena、ECS、ELB、EKS、ECR 的特定场景
- **权限边界**：Q 的访问范围受限于你的 IAM 权限
- **可视化数据存储**：Q Artifacts 数据强制存储在 us-east-1

## 动手实践

我们将搭建一个模拟的**订单处理系统**：S3 上传触发 Lambda 处理订单，写入 DynamoDB，通过 SNS 发送通知给另一个 Lambda。故意制造权限问题，用 Q Developer 来分析和排障。

### Step 1: 创建 DynamoDB 表

```bash
aws dynamodb create-table \
  --table-name q-agentic-test-orders \
  --attribute-definitions AttributeName=orderId,AttributeType=S \
  --key-schema AttributeName=orderId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

### Step 2: 创建 SNS Topic

```bash
aws sns create-topic \
  --name q-agentic-test-notifications \
  --region us-east-1
```

记下输出的 TopicArn，后续步骤需要。

### Step 3: 创建 S3 Bucket

```bash
# 替换 ACCOUNT_ID 为你的 AWS 账号 ID
aws s3api create-bucket \
  --bucket q-agentic-test-uploads-${ACCOUNT_ID} \
  --region us-east-1
```

### Step 4: 创建 Lambda 执行角色

```bash
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name q-agentic-test-lambda-role \
  --assume-role-policy-document file:///tmp/trust-policy.json

# 只附加基本执行和 SNS 权限，故意不给 DynamoDB 权限
aws iam attach-role-policy \
  --role-name q-agentic-test-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam attach-role-policy \
  --role-name q-agentic-test-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSNSFullAccess
```

!!! warning "故意缺少权限"
    我们故意不给 Lambda 角色 DynamoDB 权限，这会导致函数执行失败并产生 `AccessDeniedException` 错误日志。稍后用 Q Developer 来诊断这个问题。

### Step 5: 创建 Lambda 函数（订单处理器）

```python
# q-agentic-lambda-order-processor.py
import json
import boto3
import os

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

def handler(event, context):
    table = dynamodb.Table(os.environ["TABLE_NAME"])
    topic_arn = os.environ["TOPIC_ARN"]

    if "Records" in event:
        # S3 触发
        for record in event["Records"]:
            order_id = record["s3"]["object"]["key"].split(".")[0]
            table.put_item(Item={"orderId": order_id, "status": "processing", "source": "s3"})
            sns.publish(TopicArn=topic_arn, Message=json.dumps({"orderId": order_id}))
    else:
        # API 调用
        body = json.loads(event.get("body", "{}"))
        order_id = body.get("orderId", "unknown")
        table.put_item(Item={"orderId": order_id, "status": "processing", "source": "api"})
        sns.publish(TopicArn=topic_arn, Message=json.dumps({"orderId": order_id}))

    return {"statusCode": 200, "body": json.dumps({"message": "Order processed"})}
```

```bash
# 打包并创建函数
cd /tmp && zip order-processor.zip q-agentic-lambda-order-processor.py

# 替换 ACCOUNT_ID 和 TOPIC_ARN
aws lambda create-function \
  --function-name q-agentic-test-order-processor \
  --runtime python3.12 \
  --handler q-agentic-lambda-order-processor.handler \
  --role arn:aws:iam::${ACCOUNT_ID}:role/q-agentic-test-lambda-role \
  --zip-file fileb:///tmp/order-processor.zip \
  --timeout 30 \
  --environment "Variables={TABLE_NAME=q-agentic-test-orders,TOPIC_ARN=arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications}" \
  --region us-east-1
```

### Step 6: 创建通知处理 Lambda

```python
# q-agentic-lambda-notification-handler.py
import json

def handler(event, context):
    for record in event.get("Records", []):
        message = json.loads(record["Sns"]["Message"])
        print(f"Notification: Order {message.get('orderId')}")
    return {"statusCode": 200}
```

```bash
cd /tmp && zip notification-handler.zip q-agentic-lambda-notification-handler.py

aws lambda create-function \
  --function-name q-agentic-test-notification-handler \
  --runtime python3.12 \
  --handler q-agentic-lambda-notification-handler.handler \
  --role arn:aws:iam::${ACCOUNT_ID}:role/q-agentic-test-lambda-role \
  --zip-file fileb:///tmp/notification-handler.zip \
  --timeout 10 \
  --region us-east-1
```

### Step 7: 建立服务间连接

```bash
# SNS → Lambda 2 订阅
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications \
  --protocol lambda \
  --notification-endpoint arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:q-agentic-test-notification-handler \
  --region us-east-1

# 授权 SNS 调用 Lambda 2
aws lambda add-permission \
  --function-name q-agentic-test-notification-handler \
  --statement-id sns-invoke \
  --action lambda:InvokeFunction \
  --principal sns.amazonaws.com \
  --source-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications \
  --region us-east-1

# 授权 S3 调用 Lambda 1
aws lambda add-permission \
  --function-name q-agentic-test-order-processor \
  --statement-id s3-invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::q-agentic-test-uploads-${ACCOUNT_ID} \
  --source-account ${ACCOUNT_ID} \
  --region us-east-1

# 设置 S3 事件通知
cat > /tmp/s3-notification.json << EOF
{
  "LambdaFunctionConfigurations": [
    {
      "LambdaFunctionArn": "arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:q-agentic-test-order-processor",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {"Key": {"FilterRules": [{"Name": "suffix", "Value": ".json"}]}}
    }
  ]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket q-agentic-test-uploads-${ACCOUNT_ID} \
  --notification-configuration file:///tmp/s3-notification.json \
  --region us-east-1
```

### Step 8: 创建 CloudWatch Alarm

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name q-agentic-test-order-processor-errors \
  --metric-name Errors \
  --namespace AWS/Lambda \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --dimensions Name=FunctionName,Value=q-agentic-test-order-processor \
  --alarm-actions arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications \
  --region us-east-1
```

### Step 9: 触发错误数据

```bash
# 手动调用 Lambda，触发 DynamoDB AccessDeniedException
cat > /tmp/test-payload.json << 'EOF'
{"body": "{\"orderId\": \"test-001\"}"}
EOF

aws lambda invoke \
  --function-name q-agentic-test-order-processor \
  --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/test-payload.json \
  /tmp/lambda-output.json \
  --region us-east-1

cat /tmp/lambda-output.json
# 预期输出：AccessDeniedException on dynamodb:PutItem
```

多调用几次，让 CloudWatch Alarm 触发：

```bash
for i in $(seq 1 3); do
  aws lambda invoke \
    --function-name q-agentic-test-order-processor \
    --cli-binary-format raw-in-base64-out \
    --payload file:///tmp/test-payload.json \
    /tmp/lambda-output-$i.json \
    --region us-east-1
done
```

### Step 10: 使用 Q Developer Console Chat 测试 Agentic 能力

1. 登录 AWS Management Console
2. 点击左上角导航栏的 **Amazon Q** 图标，打开聊天面板
3. 依次尝试以下查询：

#### 测试 1: 简单资源列表（基线）

```
List my Lambda functions in us-east-1
```

Q 会调用 Lambda API 返回函数列表，可能以 Q Artifact 表格形式展示。

#### 测试 2: 跨服务关联分析（Agentic 核心）

```
What services invoke my Lambda function q-agentic-test-order-processor?
```

Q 需要多步推理：查 Lambda 配置 → 检查 event source mappings → 查 S3 notification → 查 resource policy，最终关联出 S3 事件触发。

#### 测试 3: 深度资源内省

```
Show me the IAM role and permissions of my Lambda function q-agentic-test-order-processor
```

Q 会查 Lambda 配置获取 role ARN → 查 IAM role 的 attached policies → 展示完整权限视图。**注意观察 Q 是否能发现缺少 DynamoDB 权限**。

#### 测试 4: 排障（关键场景）

```
My Lambda function q-agentic-test-order-processor is failing with errors. Can you help me troubleshoot?
```

Q 应该会：

- 查 CloudWatch Logs 发现 `AccessDeniedException`
- 检查 Lambda 函数配置，发现它引用了 DynamoDB 表
- 检查 IAM role 权限，发现缺少 `dynamodb:PutItem`
- 给出修复建议：附加 DynamoDB 权限

#### 测试 5: 复杂跨服务查询

```
Which of my Lambda functions in us-east-1 are connected to DynamoDB tables, and what alarms are configured for them?
```

Q 需要跨 Lambda + DynamoDB + CloudWatch 三个服务关联分析。

#### 测试 6: Q Artifacts 可视化

```
Create a chart showing my Lambda function invocations and errors over the last 7 days
```

Q 应生成图表 Artifact 展示 Lambda 指标趋势。

#### 测试 7: 边界测试 - 写操作

```
Delete my Lambda function q-agentic-test-order-processor
```

Q 应拒绝执行，说明只能进行只读操作。

#### 测试 8: 边界测试 - 数据访问

```
Show me the items in my DynamoDB table q-agentic-test-orders
```

Q 应说明不能访问资源中存储的数据内容。

## 测试结果

### 功能矩阵

| 查询类型 | 能力 | 推理步骤 | 说明 |
|---------|------|---------|------|
| 简单资源列表 | ✅ | 1 步 | 直接调用 list API |
| 跨服务关联 | ✅ | 3-5 步 | 自动查多个服务 API 并关联 |
| 深度内省 | ✅ | 2-3 步 | Lambda config → IAM role → policies |
| 排障分析 | ✅ | 4-6 步 | CloudWatch → 配置 → 权限 → 建议 |
| 可视化 | ✅ | 2-3 步 | 获取 CloudWatch 指标 → 生成 Artifact |
| 写操作 | ❌ 拒绝 | 0 | 设计如此，只读 |
| 数据内容 | ❌ 拒绝 | 0 | 设计如此，不访问数据 |

### Q Developer Agentic 推理透明度

Q Developer 在执行 agentic 查询时会：

1. **展示执行计划**：列出即将执行的步骤
2. **显示进度**：标记当前执行到哪一步
3. **展示中间结果**：每步的 API 调用结果
4. **请求澄清**：信息不足时主动询问

### IAM 权限矩阵

| 权限 | 用途 | 必需？ |
|------|------|--------|
| `q:StartConversation` | 开始对话 | ✅ |
| `q:SendMessage` | 发送消息 | ✅ |
| `q:PassRequest` | 让 Q 代表用户调用 API | ✅（资源查询必需）|
| `cloudformation:GetResource` | Cloud Control API | ✅（资源查询必需）|
| `cloudformation:ListResources` | Cloud Control API | ✅（资源查询必需）|
| 目标服务只读权限 | 查询具体资源 | ✅ |

## 踩坑记录

!!! warning "注意事项"

    **1. 排障服务覆盖范围有限**

    官方文档中排障功能（troubleshooting）明确支持的服务仅有 S3、Glue、Athena、ECS、ELB、EKS、ECR。Lambda 虽然在博客示例中出现，但不在官方排障支持列表中。Q 仍然可以通过 agentic 推理来分析 Lambda 问题（查 CloudWatch + IAM），但不像支持列表中的服务有专门优化的排障流程。（已查文档确认）

    **2. Teams/Slack 集成为 Free Tier**

    在 Microsoft Teams 和 Slack 中使用 Q Developer 的 agentic 功能时，访问限制为 Amazon Q Developer Free tier。这意味着某些高级功能可能不可用。（已查文档确认）

    **3. Q Artifacts 数据区域限制**

    所有 Q Artifacts（表格和图表可视化）的数据强制存储在 us-east-1，无论你在哪个 Region 使用 Console。对有数据合规要求的用户需注意。（已查文档确认）

    **4. API 调用计费**

    Q Developer 执行 agentic 查询时调用的 AWS API 会正常计费。复杂的跨服务查询可能触发多次 API 调用，注意监控使用量。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda 调用 | Free Tier 100 万次/月 | ~10 次 | $0.00 |
| DynamoDB (PAY_PER_REQUEST) | $1.25/百万写 | 0（写入失败）| $0.00 |
| S3 存储 | Free Tier 5GB | < 1KB | $0.00 |
| SNS | Free Tier 100 万发布/月 | ~5 次 | $0.00 |
| CloudWatch Alarm | $0.10/alarm/月 | 1 个 | ~$0.10 |
| CloudWatch Logs | Free Tier 5GB 摄入 | < 1KB | $0.00 |
| **合计** | | | **~$0.10** |

## 清理资源

```bash
# 1. 删除 CloudWatch Alarm
aws cloudwatch delete-alarms \
  --alarm-names q-agentic-test-order-processor-errors \
  --region us-east-1

# 2. 删除 Lambda 函数
aws lambda delete-function \
  --function-name q-agentic-test-order-processor \
  --region us-east-1

aws lambda delete-function \
  --function-name q-agentic-test-notification-handler \
  --region us-east-1

# 3. 删除 SNS 订阅和 Topic
# 先查订阅 ARN
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications \
  --query "Subscriptions[].SubscriptionArn" \
  --output text \
  --region us-east-1

# 删除每个订阅（替换 SUBSCRIPTION_ARN）
aws sns unsubscribe --subscription-arn SUBSCRIPTION_ARN --region us-east-1

aws sns delete-topic \
  --topic-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-test-notifications \
  --region us-east-1

# 4. 删除 S3 Bucket
aws s3 rb s3://q-agentic-test-uploads-${ACCOUNT_ID} --force --region us-east-1

# 5. 删除 DynamoDB 表
aws dynamodb delete-table \
  --table-name q-agentic-test-orders \
  --region us-east-1

# 6. 删除 IAM Role（先分离策略）
aws iam detach-role-policy \
  --role-name q-agentic-test-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam detach-role-policy \
  --role-name q-agentic-test-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSNSFullAccess

aws iam delete-role --role-name q-agentic-test-lambda-role

# 7. 删除 CloudWatch Log Groups
aws logs delete-log-group \
  --log-group-name /aws/lambda/q-agentic-test-order-processor \
  --region us-east-1

aws logs delete-log-group \
  --log-group-name /aws/lambda/q-agentic-test-notification-handler \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。CloudWatch Alarm 按月计费，不清理会持续产生 $0.10/月。

## 结论与建议

### 适用场景

- **运维排障**：快速定位跨服务的权限问题、配置错误、连接故障
- **基础设施审计**：了解资源间依赖关系、检查配置状态
- **新人上手**：快速了解现有 AWS 环境，不需要记住每个服务的 CLI 命令
- **成本分析**：配合 Q Artifacts 生成可视化的成本和资源报表

### 不适合的场景

- 需要修改资源的操作（只读限制）
- 需要查看数据内容（S3 对象、DynamoDB 记录等）
- 安全审计（凭证、加密相关查询被限制）
- 需要精确控制和自动化的运维流程（建议用 CloudWatch + EventBridge + Lambda）

### 生产环境建议

1. **最小权限原则**：为需要使用 Q agentic 功能的用户创建专门的 IAM policy，包含 `q:PassRequest` 和必要的只读权限
2. **审计日志**：Q 的 API 调用会记录在 CloudTrail 中，可用于审计
3. **结合 Resource Explorer**：启用 Resource Explorer 可加速资源计数场景
4. **注意 API 计费**：复杂查询会触发多次 API 调用，大规模使用时监控成本

## 参考链接

- [AWS What's New: Introducing agentic capabilities for Amazon Q Developer Chat](https://aws.amazon.com/about-aws/whats-new/2025/06/agentic-capabilities-amazon-q-developer-chat-aws-management-console-chat-applications/)
- [Deep-dive Blog: New and improved Amazon Q Developer experience in the AWS Management Console](https://aws.amazon.com/blogs/devops/new-and-improved-amazon-q-developer-experience-in-the-aws-management-console/)
- [官方文档：Chatting about your resources with Amazon Q Developer](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-actions.html)
- [官方文档：Asking Amazon Q to troubleshoot your resources](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-actions-troubleshooting.html)
- [官方文档：Using Q artifacts in Amazon Q](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-artifacts.html)
- [官方文档：IAM policy examples](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/id-based-policy-examples-users.html)
