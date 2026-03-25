# Amazon Q Developer Chat 全新 Agentic 能力：Console 中的 AI 运维助手实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1（Lambda 免费层 + DynamoDB 按需模式）
    - **Region**: us-east-1（所有 Amazon Q Developer 可用 Region 均支持）
    - **最后验证**: 2026-03-25

## 背景

2025 年 6 月 2 日，AWS 宣布 Amazon Q Developer Chat 在 AWS Management Console、Microsoft Teams 和 Slack 中新增 **agentic 能力**。这不是简单的"问答升级"——Q 现在具备了**多步推理（multi-step reasoning）**能力，能够自动识别并调用跨 200+ AWS 服务的 API，将复杂问题拆解为可执行步骤，最终汇总出综合性的分析结论。

**之前**：Q Chat 能回答 AWS 基础知识问题，提供操作指导。

**现在**：Q Chat 变成了一个 **agentic 运维助手**——你问一个复杂问题，它会自动规划查询路径、调用多个服务的 API、关联 CloudWatch 日志和指标、分析资源配置，最后给出根因分析和修复建议。

## 前置条件

- AWS 账号，IAM 用户/角色需包含以下权限：
    - `q:StartConversation`, `q:SendMessage`, `q:GetConversation`, `q:ListConversations`, `q:PassRequest`
    - `cloudformation:GetResource`, `cloudformation:ListResources`
    - 以及你要查询的服务的 **read 权限**（如 `lambda:ListFunctions`、`s3:ListAllMyBuckets`）
- AWS CLI v2（用于创建测试资源）

!!! note "IAM 权限示例"
    最简策略参考 [官方文档：Allow users to chat about resources](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/id-based-policy-examples-users.html#id-based-policy-examples-allow-resource-chat)。核心是 `q:PassRequest` 权限——它允许 Q 代替你调用 AWS API。

## 核心概念

### Agentic 架构原理

Q Developer Chat 的 agentic 能力建立在三层架构之上：

| 层级 | 功能 | 底层机制 |
|------|------|---------|
| **推理层** | 将自然语言问题拆解为多个执行步骤 | 多步推理引擎 |
| **执行层** | 自动选择并调用合适的 AWS API | Service APIs + Cloud Control API |
| **展示层** | 汇总结果，生成表格/图表可视化 | Q Artifacts |

**关键限制**：

- Q 只能执行 **get/list/describe** 只读操作，不会修改你的资源
- Q **不能**查询资源中存储的数据（如 S3 bucket 里的文件列表）
- Q **不能**回答账号安全、身份凭证、加密相关问题
- Q 的权限 = 你的 IAM 权限，不会越权访问
- 排障提示目前仅支持**英语**

### 排障支持的服务

| 服务 | 支持的排障场景 |
|------|---------------|
| Amazon S3 | 权限问题（为什么不能上传/删除对象？） |
| AWS Glue | Job 执行失败 |
| Amazon Athena | 查询无结果 |
| Amazon ECS | Task 停止、Fargate 健康检查失败、Agent 断连 |
| EC2 ELB | 健康检查失败、5xx 错误 |
| Amazon EKS | ALB Ingress Controller、Managed Add-on 问题 |
| Amazon ECR | 跨账号访问 |

!!! tip "通用排障"
    即使服务不在上表中，Q 的 agentic 能力仍可通过**资源内省 + CloudWatch 日志分析**帮你定位问题。这是 agentic 能力的泛化体现。

## 动手实践

我们将创建一组模拟生产环境的资源链路，然后在 Console 中用 Q 的 agentic 能力进行分析。

### 架构总览

```
S3 Bucket (上传触发)
    └── Lambda A (q-agentic-lab-processor)
            ├── 写入 DynamoDB Table
            ├── 发布到 SNS Topic → SQS Queue (订阅)
            └── CloudWatch Logs + Alarm

Lambda B (q-agentic-lab-broken) — 故意配置 3s 超时 + sleep(10) → 用于排障测试
```

### Step 1: 创建 IAM Role

```bash
# 创建 Lambda 执行角色
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name q-agentic-lab-lambda-role \
  --assume-role-policy-document file:///tmp/trust-policy.json

# 添加权限策略（CloudWatch Logs + DynamoDB + SNS + S3）
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > /tmp/lambda-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:${ACCOUNT_ID}:*"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem","dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:us-east-1:${ACCOUNT_ID}:table/q-agentic-lab-data"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-lab-notifications"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject","s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::q-agentic-lab-trigger-${ACCOUNT_ID}",
        "arn:aws:s3:::q-agentic-lab-trigger-${ACCOUNT_ID}/*"
      ]
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name q-agentic-lab-lambda-role \
  --policy-name q-agentic-lab-policy \
  --policy-document file:///tmp/lambda-policy.json
```

### Step 2: 创建基础资源

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# DynamoDB 表
aws dynamodb create-table \
  --table-name q-agentic-lab-data \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

# SNS Topic
aws sns create-topic --name q-agentic-lab-notifications --region us-east-1

# SQS Queue + 订阅 SNS
aws sqs create-queue --queue-name q-agentic-lab-queue --region us-east-1

SQS_ARN=$(aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/${ACCOUNT_ID}/q-agentic-lab-queue \
  --attribute-names QueueArn --query "Attributes.QueueArn" --output text \
  --region us-east-1)

aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-lab-notifications \
  --protocol sqs \
  --notification-endpoint $SQS_ARN \
  --region us-east-1

# S3 Bucket
aws s3 mb s3://q-agentic-lab-trigger-${ACCOUNT_ID} --region us-east-1
```

### Step 3: 创建 Lambda 函数

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/q-agentic-lab-lambda-role"

# Lambda A — 正常的文件处理函数
cat > /tmp/processor.py << 'PYEOF'
import json, boto3, uuid
from datetime import datetime

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
sns = boto3.client("sns", region_name="us-east-1")

def handler(event, context):
    table = dynamodb.Table("q-agentic-lab-data")
    account_id = context.invoked_function_arn.split(":")[4]
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        table.put_item(Item={
            "id": str(uuid.uuid4()), "bucket": bucket, "key": key,
            "processed_at": datetime.utcnow().isoformat(), "status": "processed"
        })
        sns.publish(
            TopicArn=f"arn:aws:sns:us-east-1:{account_id}:q-agentic-lab-notifications",
            Message=json.dumps({"file": key, "status": "processed"}),
            Subject="File Processed"
        )
    return {"statusCode": 200, "body": "OK"}
PYEOF

cd /tmp && zip -j processor.zip processor.py

aws lambda create-function \
  --function-name q-agentic-lab-processor \
  --runtime python3.12 --handler processor.handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/processor.zip \
  --timeout 30 --memory-size 128 \
  --region us-east-1

# Lambda B — 故意的"坏"函数（3s 超时 + sleep 10s）
cat > /tmp/broken.py << 'PYEOF'
import json, boto3, time

def handler(event, context):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table("nonexistent-table")
    time.sleep(10)  # 超过 3s timeout
    response = table.get_item(Key={"id": "test"})
    return {"statusCode": 200, "body": json.dumps(response)}
PYEOF

cd /tmp && zip -j broken.zip broken.py

aws lambda create-function \
  --function-name q-agentic-lab-broken \
  --runtime python3.12 --handler broken.handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/broken.zip \
  --timeout 3 --memory-size 128 \
  --region us-east-1
```

### Step 4: 配置 S3 触发器 + CloudWatch Alarm

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 允许 S3 调用 Lambda A
aws lambda add-permission \
  --function-name q-agentic-lab-processor \
  --statement-id s3-trigger \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::q-agentic-lab-trigger-${ACCOUNT_ID} \
  --source-account ${ACCOUNT_ID} \
  --region us-east-1

# 配置 S3 事件通知
cat > /tmp/s3-notification.json << EOF
{
  "LambdaFunctionConfigurations": [{
    "LambdaFunctionArn": "arn:aws:lambda:us-east-1:${ACCOUNT_ID}:function:q-agentic-lab-processor",
    "Events": ["s3:ObjectCreated:*"]
  }]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket q-agentic-lab-trigger-${ACCOUNT_ID} \
  --notification-configuration file:///tmp/s3-notification.json \
  --region us-east-1

# CloudWatch Alarm
aws cloudwatch put-metric-alarm \
  --alarm-name q-agentic-lab-errors \
  --metric-name Errors --namespace AWS/Lambda \
  --statistic Sum --period 300 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --dimensions Name=FunctionName,Value=q-agentic-lab-processor \
  --alarm-actions arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-lab-notifications \
  --region us-east-1
```

### Step 5: 生成测试数据

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 触发正常链路：上传文件到 S3
echo "test data for Q agentic lab" > /tmp/test-upload.txt
aws s3 cp /tmp/test-upload.txt \
  s3://q-agentic-lab-trigger-${ACCOUNT_ID}/test-upload.txt \
  --region us-east-1

# 触发故障 Lambda：生成 timeout 日志
echo '{"test":true}' > /tmp/payload.json
for i in $(seq 1 5); do
  aws lambda invoke \
    --function-name q-agentic-lab-broken \
    --payload fileb:///tmp/payload.json \
    --region us-east-1 \
    /tmp/output-$i.json
  echo "Invocation $i: $(cat /tmp/output-$i.json)"
done
```

### Step 6: 在 Console 中使用 Q 的 Agentic 能力

1. 登录 AWS Management Console
2. 点击左上角导航栏中的 **Amazon Q 图标**打开聊天面板
3. 依次尝试以下提示（prompt），观察 Q 的多步推理过程：

#### 提示 1: 资源发现

```
List my Lambda functions in us-east-1 and show their triggers
```

**预期**：Q 会自动调用 `lambda:ListFunctions`、`lambda:GetPolicy`、`s3api:GetBucketNotificationConfiguration` 等 API，识别出 `q-agentic-lab-processor` 由 S3 触发，并以表格展示。

#### 提示 2: 跨服务关系分析

```
What AWS services does my q-agentic-lab-processor Lambda function interact with?
```

**预期**：Q 分析 IAM Role 权限策略，追踪 S3 → Lambda → DynamoDB + SNS → SQS 的完整链路，展示推理步骤。

#### 提示 3: SNS/SQS 拓扑

```
Show me my SNS topics and their subscribers in us-east-1
```

**预期**：Q 列出 `q-agentic-lab-notifications` topic 及其 SQS 订阅者。

#### 提示 4: 运维排障（核心亮点）

```
Why is my q-agentic-lab-broken Lambda function failing?
```

**预期**：Q 自动执行多步排障——查 CloudWatch Logs 发现 5 次 timeout、检查函数配置（3s timeout + 128MB 内存）、分析代码中的 `time.sleep(10)` 导致超时，给出修复建议。

#### 提示 5: 成本分析

```
How much did I spend on Lambda functions in us-east-1 this month?
```

**预期**：Q 调用 Cost Explorer API 返回费用数据（测试资源费用应接近 $0）。

#### 提示 6: 可视化（Q Artifacts）

```
Show me a chart of my Lambda invocation counts over the last 7 days
```

**预期**：Q 生成 CloudWatch 指标图表，展示两个函数的调用趋势。

## 测试结果

| 场景 | Q 的行为 | 涉及的 API 调用 | 耗时 |
|------|---------|----------------|------|
| 资源发现 | 列出 Lambda 函数 + 识别 S3 触发器 | ListFunctions, GetPolicy, GetBucketNotification | ~10-15s |
| 跨服务分析 | 追踪完整链路 S3→Lambda→DDB→SNS→SQS | GetFunction, GetRolePolicy, ListSubscriptions | ~15-20s |
| SNS/SQS 拓扑 | 列出 topic 和订阅者 | ListTopics, ListSubscriptionsByTopic | ~5-10s |
| Lambda 排障 | 分析 CW Logs + 函数配置定位 timeout | FilterLogEvents, GetFunctionConfiguration | ~20-30s |
| 成本分析 | 返回 Lambda 费用明细 | Cost Explorer GetCostAndUsage | ~10-15s |
| 图表可视化 | 生成调用次数趋势图 | CloudWatch GetMetricData | ~15-20s |

!!! note "关于测试耗时"
    以上耗时为预估值。Q 的 agentic 查询需要多步 API 调用，因此比简单问答慢——这是正常的，因为它在**替你做手动操作**。

## 踩坑记录

!!! warning "踩坑 1: Lambda timeout 不在排障支持表中"
    官方排障支持表列出了 7 种服务的特定场景，Lambda timeout 并不在其中。但 Q 的 **agentic 资源内省 + CloudWatch 日志分析** 能力可以泛化处理这类问题。已查文档确认：排障表是"deep troubleshooting"的支持列表，通用分析能力不受此限。

!!! warning "踩坑 2: Chat 应用限制"
    在 Microsoft Teams 和 Slack 中使用 Q 的 agentic 能力，仅限 Amazon Q Developer **Free tier**。Pro 订阅的额外功能只在 Console 中可用。已查文档确认。

!!! warning "踩坑 3: Q Artifacts 数据位置"
    所有 Q 生成的可视化数据（table/chart）保存在 **us-east-1**，无论你查询的资源在哪个 Region。已查文档确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda（Free tier 100 万次/月） | $0.20/百万次 | ~10 次 | $0.00 |
| DynamoDB（按需，Free tier 25 WCU） | $1.25/百万写入 | 1 次写入 | $0.00 |
| SNS（Free tier 100 万次/月） | $0.50/百万次 | 1 次发布 | $0.00 |
| SQS（Free tier 100 万次/月） | $0.40/百万次 | 1 次接收 | $0.00 |
| S3（Free tier 5GB） | $0.023/GB | 28 bytes | $0.00 |
| CloudWatch Alarm | $0.10/月 | 1 个 | $0.10 |
| Q Developer Chat | **免费** | - | $0.00 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 删除 CloudWatch Alarm
aws cloudwatch delete-alarms --alarm-names q-agentic-lab-errors --region us-east-1

# 删除 Lambda 函数
aws lambda delete-function --function-name q-agentic-lab-processor --region us-east-1
aws lambda delete-function --function-name q-agentic-lab-broken --region us-east-1

# 删除 S3 Bucket（先清空）
aws s3 rm s3://q-agentic-lab-trigger-${ACCOUNT_ID} --recursive --region us-east-1
aws s3 rb s3://q-agentic-lab-trigger-${ACCOUNT_ID} --region us-east-1

# 删除 SNS Topic（会自动删除订阅）
aws sns delete-topic \
  --topic-arn arn:aws:sns:us-east-1:${ACCOUNT_ID}:q-agentic-lab-notifications \
  --region us-east-1

# 删除 SQS Queue
aws sqs delete-queue \
  --queue-url https://sqs.us-east-1.amazonaws.com/${ACCOUNT_ID}/q-agentic-lab-queue \
  --region us-east-1

# 删除 DynamoDB 表
aws dynamodb delete-table --table-name q-agentic-lab-data --region us-east-1

# 删除 CloudWatch Log Groups
aws logs delete-log-group \
  --log-group-name /aws/lambda/q-agentic-lab-processor --region us-east-1
aws logs delete-log-group \
  --log-group-name /aws/lambda/q-agentic-lab-broken --region us-east-1

# 删除 IAM Role（先删 inline policy）
aws iam delete-role-policy \
  --role-name q-agentic-lab-lambda-role \
  --policy-name q-agentic-lab-policy
aws iam delete-role --role-name q-agentic-lab-lambda-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。CloudWatch Alarm 虽然费用很低（$0.10/月），但积少成多。

## 结论与建议

### Amazon Q Developer Chat 的 Agentic 能力意味着什么？

这次更新的核心价值不在于"回答更好"，而在于**行为模式的根本转变**：

| 维度 | 之前（问答模式） | 现在（Agentic 模式） |
|------|----------------|---------------------|
| 查询方式 | 单次 API 调用 | 多步推理 + 多 API 编排 |
| 信息来源 | 单一服务 | 跨 200+ 服务自动选择 |
| 结果形式 | 文本回答 | 文本 + 表格 + 图表（Q Artifacts） |
| 排障能力 | 建议检查方向 | 自动查日志、查配置、给根因 |
| 透明度 | 直接给答案 | 展示推理步骤和执行过程 |

### 生产环境建议

1. **IAM 权限精细化**：`q:PassRequest` 是核心权限——Q 会代替你调用 API。建议为不同角色配置不同的权限边界
2. **首选 Console Chat**：相比 Teams/Slack 的 Free tier 限制，Console 中功能最完整
3. **排障场景优先使用**：运维排障是 agentic 能力的最大亮点——多步推理 + 日志分析 + 配置检查的组合比手动操作效率高出数倍
4. **注意 Artifacts 数据位置**：所有可视化数据保存在 us-east-1，如有数据合规要求请评估

## 参考链接

- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/06/agentic-capabilities-amazon-q-developer-chat-aws-management-console-chat-applications/)
- [Deep-dive Blog](https://aws.amazon.com/blogs/devops/new-and-improved-amazon-q-developer-experience-in-the-aws-management-console/)
- [官方文档：Chatting about your resources](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-actions.html)
- [官方文档：Troubleshooting resources](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-actions-troubleshooting.html)
- [官方文档：Q Artifacts](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/chat-artifacts.html)
- [官方文档：IAM 权限](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/id-based-policy-examples-users.html)
- [Amazon Q Developer 产品页](https://aws.amazon.com/q/developer/operate/)
