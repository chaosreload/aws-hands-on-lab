---
tags:
  - Compute
---

# EC2 Image Builder 集成 Lambda 和 Step Functions：工作流自动化实践

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0.50 以内（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

EC2 Image Builder 是 AWS 的 AMI 自动化构建服务。2025 年 11 月，Image Builder 新增了两个关键的工作流步骤动作：

- **ExecuteStateMachine** — 在工作流中直接执行 Step Functions 状态机
- **WaitForAction + Lambda** — 暂停工作流，异步调用 Lambda，等待外部回调

在此之前，要在镜像构建流程中集成 Lambda 或 Step Functions，需要编写自定义代码和多步变通方案。现在可以原生集成，实现自定义合规验证、多阶段安全测试、自定义通知等场景。

**可用性**：所有 AWS 商业区域 + China + GovCloud，无额外费用。

## 前置条件

- AWS 账号（需要 Image Builder、Lambda、Step Functions、IAM、EC2 权限）
- AWS CLI v2 已配置
- 一个可用的 VPC + 子网（需要出站到互联网的能力，用于 SSM Agent）

## 核心概念

### 两种集成模式对比

| 维度 | ExecuteStateMachine | WaitForAction + Lambda |
|------|-------------------|----------------------|
| **执行模式** | 同步：启动状态机 → 等待完成 → 继续 | 异步：触发 Lambda → 等外部回调 |
| **超时** | 默认 6 小时，最大 24 小时 | 默认 3 天，最大 7 天 |
| **输出** | 状态机执行输出（JSON） | RESUME/STOP + reason |
| **适用场景** | 自动化多步验证（合规扫描、配置检查） | 人工审批、外部系统集成、自定义通知 |
| **实现复杂度** | 低（直接等结果） | 高（需实现 `SendWorkflowStepAction` 回调） |
| **IAM 权限** | `states:StartExecution` + `states:DescribeExecution` | `lambda:InvokeFunction` |
| **回滚** | 不支持 | 不支持 |

### 工作流框架

Image Builder 工作流分为三个阶段：

1. **Build 阶段**（pre-snapshot）— 定制 EC2 实例
2. **Test 阶段**（post-snapshot）— 验证生成的镜像
3. **Distribution 阶段**（post-build）— 分发 AMI

Lambda 和 Step Functions 步骤可以用在 **Build** 和 **Test** 阶段。本文在 Test 阶段演示。

## 动手实践

### Step 1: 创建 IAM 角色

#### 1.1 创建 EC2 实例角色（用于构建/测试实例）

```bash
# 创建信任策略
cat > /tmp/ec2-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

# 创建角色并附加策略
aws iam create-role \
  --role-name ib-lab-instance-role \
  --assume-role-policy-document file:///tmp/ec2-trust.json \
  --region us-east-1

aws iam attach-role-policy --role-name ib-lab-instance-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam attach-role-policy --role-name ib-lab-instance-role \
  --policy-arn arn:aws:iam::aws:policy/EC2InstanceProfileForImageBuilder

# 创建实例配置文件
aws iam create-instance-profile \
  --instance-profile-name ib-lab-instance-profile
aws iam add-role-to-instance-profile \
  --instance-profile-name ib-lab-instance-profile \
  --role-name ib-lab-instance-role
```

#### 1.2 创建 Image Builder 工作流执行角色

```bash
cat > /tmp/ib-exec-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "imagebuilder.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name ib-lab-exec-role \
  --assume-role-policy-document file:///tmp/ib-exec-trust.json \
  --region us-east-1
```

关键：执行角色需要以下权限：

```bash
cat > /tmp/ib-exec-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EC2AndSSM",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances", "ec2:StopInstances", "ec2:TerminateInstances",
        "ec2:CreateImage", "ec2:CreateTags", "ec2:DescribeImages",
        "ec2:DescribeInstances", "ec2:DescribeInstanceTypes",
        "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSecurityGroups",
        "ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSnapshots",
        "ec2:DescribeVolumes", "ec2:DescribeKeyPairs",
        "iam:PassRole", "iam:GetInstanceProfile",
        "ssm:SendCommand", "ssm:GetCommandInvocation",
        "ssm:DescribeInstanceInformation", "ssm:PutInventory",
        "imagebuilder:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "StepFunctions",
      "Effect": "Allow",
      "Action": ["states:StartExecution", "states:DescribeExecution"],
      "Resource": "arn:aws:states:us-east-1:*:stateMachine:ib-lab-*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:us-east-1:*:function:ib-lab-*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name ib-lab-exec-role \
  --policy-name ib-lab-exec-policy \
  --policy-document file:///tmp/ib-exec-policy.json
```

!!! warning "踩坑：执行角色权限"
    执行角色必须包含 `ec2:DescribeInstanceTypeOfferings`，否则 pipeline 启动后立刻失败。官方文档建议使用 Service-linked Role，但自定义角色更灵活（可限定 Lambda/SFN 资源范围）。

#### 1.3 创建 Lambda 角色

```bash
cat > /tmp/lambda-trust.json << 'EOF'
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
  --role-name ib-lab-lambda-role \
  --assume-role-policy-document file:///tmp/lambda-trust.json \
  --region us-east-1

# Lambda 需要调用 SendWorkflowStepAction 回调
cat > /tmp/lambda-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "imagebuilder:SendWorkflowStepAction",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:*:*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name ib-lab-lambda-role \
  --policy-name ib-lab-lambda-policy \
  --policy-document file:///tmp/lambda-policy.json
```

### Step 2: 创建 Lambda 函数（WaitForAction 回调）

```python
# index.py — Lambda callback for Image Builder WaitForAction
import json
import boto3
import os

def handler(event, context):
    """
    Image Builder WaitForAction 会异步调用此 Lambda。
    
    收到的 event 结构（实测确认）:
    {
      "imageArn": "arn:aws:imagebuilder:...:image/name/ver/build",
      "workflowArn": "arn:aws:imagebuilder:...:workflow/...",
      "workflowExecutionId": "wf-xxx",
      "workflowStepExecutionId": "step-xxx",
      "workflowStepName": "StepName",
      "version": "1.0"
    }
    """
    print("Received event: " + json.dumps(event))
    
    client = boto3.client(
        "imagebuilder",
        region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    
    # 注意字段映射！
    # event 中是 workflowStepExecutionId → API 参数是 stepExecutionId
    # event 中是 imageArn → API 参数是 imageBuildVersionArn
    step_execution_id = event.get("workflowStepExecutionId")
    image_build_version_arn = event.get("imageArn")
    
    if not step_execution_id or not image_build_version_arn:
        print("ERROR: Missing required fields")
        return {"statusCode": 400}
    
    # 执行自定义验证逻辑（这里是示例）
    validation_result = (
        "Lambda validation passed for "
        + event.get("workflowStepName", "unknown")
    )
    
    # 回调 Image Builder — RESUME 继续，STOP 停止
    client.send_workflow_step_action(
        stepExecutionId=step_execution_id,
        imageBuildVersionArn=image_build_version_arn,
        action="RESUME",
        reason=validation_result
    )
    
    return {"statusCode": 200, "body": validation_result}
```

```bash
# 打包并创建 Lambda
mkdir -p /tmp/ib-lambda
# 将上面的 Python 代码保存为 /tmp/ib-lambda/index.py
cd /tmp/ib-lambda && zip -j /tmp/ib-lambda.zip index.py

# 等待 IAM 角色生效（约 10 秒）
sleep 10

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws lambda create-function \
  --function-name ib-lab-callback \
  --runtime python3.12 \
  --handler index.handler \
  --role arn:aws:iam::${ACCOUNT_ID}:role/ib-lab-lambda-role \
  --zip-file fileb:///tmp/ib-lambda.zip \
  --timeout 60 \
  --region us-east-1
```

!!! warning "踩坑：Lambda event 字段名映射"
    Image Builder 发送给 Lambda 的 event 中使用 `workflowStepExecutionId` 和 `imageArn`，
    但 `SendWorkflowStepAction` API 的参数名是 `stepExecutionId` 和 `imageBuildVersionArn`。
    **官方文档未明确记录 Lambda payload 的字段结构**，需要通过实测确认。（实测发现，官方未记录）

### Step 3: 创建 Step Functions 状态机

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Step Functions 执行角色
cat > /tmp/sfn-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "states.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name ib-lab-sfn-role \
  --assume-role-policy-document file:///tmp/sfn-trust.json \
  --region us-east-1
```

创建两个状态机 — 一个成功、一个故意失败（用于测试错误处理）：

```bash
# 成功验证状态机
cat > /tmp/sfn-success.json << 'EOF'
{
  "Comment": "Image compliance validation",
  "StartAt": "ValidateImage",
  "States": {
    "ValidateImage": {
      "Type": "Pass",
      "Result": {
        "validationStatus": "PASSED",
        "checks": ["cve-scan", "compliance", "configuration"],
        "message": "All validation checks passed"
      },
      "Next": "Done"
    },
    "Done": {"Type": "Succeed"}
  }
}
EOF

aws stepfunctions create-state-machine \
  --name ib-lab-validation \
  --role-arn arn:aws:iam::${ACCOUNT_ID}:role/ib-lab-sfn-role \
  --definition file:///tmp/sfn-success.json \
  --region us-east-1

# 故意失败的状态机（用于测试错误处理）
cat > /tmp/sfn-fail.json << 'EOF'
{
  "Comment": "Intentional failure for testing",
  "StartAt": "Validate",
  "States": {
    "Validate": {
      "Type": "Pass",
      "Next": "FailState"
    },
    "FailState": {
      "Type": "Fail",
      "Error": "ValidationFailed",
      "Cause": "Intentional test failure"
    }
  }
}
EOF

aws stepfunctions create-state-machine \
  --name ib-lab-fail \
  --role-arn arn:aws:iam::${ACCOUNT_ID}:role/ib-lab-sfn-role \
  --definition file:///tmp/sfn-fail.json \
  --region us-east-1
```

### Step 4: 创建 Image Builder 工作流

#### 4.1 ExecuteStateMachine 工作流

```yaml
# wf-sfn.yaml — 替换 ACCOUNT_ID 为你的账号 ID
name: test-workflow-with-sfn
description: 使用 Step Functions 进行镜像合规验证
schemaVersion: 1.0
steps:
  - name: LaunchTestInstance
    action: LaunchInstance
    onFailure: Abort
    inputs:
      waitFor: "ssmAgent"

  - name: RunValidationStateMachine
    action: ExecuteStateMachine
    timeoutSeconds: 600
    onFailure: Abort
    inputs:
      stateMachineArn: "arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:ib-lab-validation"
      input: |
        {
          "source": "ImageBuilder",
          "testType": "compliance-validation"
        }

  - name: TerminateTestInstance
    action: TerminateInstance
    onFailure: Continue
    inputs:
      instanceId.$: "$.stepOutputs.LaunchTestInstance.instanceId"
```

#### 4.2 WaitForAction + Lambda 工作流

```yaml
# wf-lambda.yaml
name: test-workflow-with-lambda
description: 使用 Lambda 回调进行自定义验证
schemaVersion: 1.0
steps:
  - name: LaunchTestInstance
    action: LaunchInstance
    onFailure: Abort
    inputs:
      waitFor: "ssmAgent"

  - name: WaitForLambdaCallback
    action: WaitForAction
    timeoutSeconds: 300
    onFailure: Abort
    inputs:
      lambdaFunctionName: "ib-lab-callback"

  - name: TerminateTestInstance
    action: TerminateInstance
    onFailure: Continue
    inputs:
      instanceId.$: "$.stepOutputs.LaunchTestInstance.instanceId"
```

```bash
# 创建工作流资源
aws imagebuilder create-workflow \
  --name ib-lab-wf-sfn \
  --semantic-version 1.0.0 \
  --type TEST \
  --data "$(cat wf-sfn.yaml)" \
  --region us-east-1

aws imagebuilder create-workflow \
  --name ib-lab-wf-lambda \
  --semantic-version 1.0.0 \
  --type TEST \
  --data "$(cat wf-lambda.yaml)" \
  --region us-east-1
```

### Step 5: 创建 Pipeline 并执行

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 创建 Image Recipe
aws imagebuilder create-image-recipe \
  --name ib-lab-recipe \
  --semantic-version 1.0.0 \
  --parent-image "arn:aws:imagebuilder:us-east-1:aws:image/amazon-linux-2023-x86/x.x.x" \
  --components '[{"componentArn":"arn:aws:imagebuilder:us-east-1:aws:component/update-linux/x.x.x"}]' \
  --region us-east-1

# 创建 Security Group（仅出站，无入站规则）
SG_ID=$(aws ec2 create-security-group \
  --group-name ib-lab-sg \
  --description "Image Builder lab - outbound only" \
  --vpc-id YOUR_VPC_ID \
  --region us-east-1 \
  --query GroupId --output text)

# 创建 Infrastructure Configuration
aws imagebuilder create-infrastructure-configuration \
  --name ib-lab-infra \
  --instance-types t3.micro \
  --instance-profile-name ib-lab-instance-profile \
  --security-group-ids $SG_ID \
  --subnet-id YOUR_SUBNET_ID \
  --terminate-instance-on-failure \
  --region us-east-1

# 创建 Distribution Configuration
aws imagebuilder create-distribution-configuration \
  --name ib-lab-dist \
  --distributions '[{"region":"us-east-1","amiDistributionConfiguration":{"name":"ib-lab-{{imagebuilder:buildDate}}"}}]' \
  --region us-east-1

# 创建 Pipeline（使用 SFN 工作流）
aws imagebuilder create-image-pipeline \
  --name ib-lab-pipeline-sfn \
  --image-recipe-arn arn:aws:imagebuilder:us-east-1:${ACCOUNT_ID}:image-recipe/ib-lab-recipe/1.0.0 \
  --infrastructure-configuration-arn arn:aws:imagebuilder:us-east-1:${ACCOUNT_ID}:infrastructure-configuration/ib-lab-infra \
  --distribution-configuration-arn arn:aws:imagebuilder:us-east-1:${ACCOUNT_ID}:distribution-configuration/ib-lab-dist \
  --workflows "[{\"workflowArn\":\"arn:aws:imagebuilder:us-east-1:${ACCOUNT_ID}:workflow/test/ib-lab-wf-sfn/1.0.0/1\"}]" \
  --execution-role arn:aws:iam::${ACCOUNT_ID}:role/ib-lab-exec-role \
  --region us-east-1

# 启动构建
aws imagebuilder start-image-pipeline-execution \
  --image-pipeline-arn arn:aws:imagebuilder:us-east-1:${ACCOUNT_ID}:image-pipeline/ib-lab-pipeline-sfn \
  --region us-east-1
```

### Step 6: 监控和验证

```bash
# 查看构建状态（替换 IMAGE_BUILD_VERSION_ARN）
aws imagebuilder get-image \
  --image-build-version-arn "IMAGE_BUILD_VERSION_ARN" \
  --region us-east-1 \
  --query "image.state"

# 查看工作流执行详情
aws imagebuilder list-workflow-executions \
  --image-build-version-arn "IMAGE_BUILD_VERSION_ARN" \
  --region us-east-1

# 查看每个步骤的执行结果（包含 SFN/Lambda 的输入输出）
aws imagebuilder list-workflow-step-executions \
  --workflow-execution-id "WORKFLOW_EXECUTION_ID" \
  --region us-east-1
```

## 测试结果

### ExecuteStateMachine（同步模式）

| 步骤 | 耗时 | 状态 |
|------|------|------|
| LaunchInstance | 3 分 25 秒 | COMPLETED |
| **ExecuteStateMachine** | **10 秒** | **COMPLETED** |
| TerminateInstance | 52 秒 | COMPLETED |
| **Total** | **4 分 18 秒** | |

ExecuteStateMachine 步骤输出：

```json
{
  "output": "{\"validationStatus\":\"PASSED\",\"checks\":[\"cve-scan\",\"compliance\",\"configuration\"]}",
  "executionArn": "arn:aws:states:us-east-1:595842667825:execution:ib-test-validation:aea87837-...",
  "status": "SUCCEEDED"
}
```

### WaitForAction + Lambda（异步回调模式）

| 步骤 | 耗时 | 状态 |
|------|------|------|
| LaunchInstance | 3 分 24 秒 | COMPLETED |
| **WaitForAction** | **13 秒** | **COMPLETED** |
| TerminateInstance | 41 秒 | COMPLETED |
| **Total** | **4 分 20 秒** | |

WaitForAction 步骤输出：

```json
{
  "action": "RESUME",
  "reason": "Lambda validation passed for step WaitForLambdaCallback"
}
```

### 错误处理测试（SFN 失败 + onFailure:Continue）

| 步骤 | 耗时 | 状态 |
|------|------|------|
| LaunchInstance | 3 分 25 秒 | COMPLETED |
| **ExecuteStateMachine** | **10 秒** | **FAILED** |
| TerminateInstance | 42 秒 | COMPLETED |
| **AMI 构建结果** | | **AVAILABLE ✅** |

**关键发现**：即使 ExecuteStateMachine 步骤失败，设置 `onFailure: Continue` 后工作流仍继续执行，AMI 照常生成。失败步骤的 error 和 cause 从 Step Functions Fail state 完整透传：

```json
{
  "errorMessage": "ExpectationNotMet. stepfunctions:DescribeExecution returned terminal state FAILED",
  "error": "ValidationFailed",
  "cause": "Intentional test failure",
  "status": "FAILED"
}
```

## 踩坑记录

!!! warning "踩坑 1：Lambda event 字段名不匹配 API 参数"
    Image Builder 发给 Lambda 的 event 使用 `workflowStepExecutionId` 和 `imageArn`，
    但 `SendWorkflowStepAction` API 要求的参数名是 `stepExecutionId` 和 `imageBuildVersionArn`。
    Lambda 代码中需要做字段映射。（实测发现，官方未记录）

!!! warning "踩坑 2：执行角色需要 ec2:DescribeInstanceTypeOfferings"
    自定义执行角色必须包含 `ec2:DescribeInstanceTypeOfferings` 权限，否则 pipeline 一启动就会失败，
    错误信息会明确提示缺少该权限。建议使用 `AmazonEC2FullAccess` 托管策略作为起点，再逐步收紧。

!!! warning "踩坑 3：WaitForAction Lambda 异步调用会重试"
    如果 Lambda 执行出错，WaitForAction 会自动重试调用（实测观察到 3 次重试）。
    确保 Lambda 是幂等的，避免重复执行副作用操作。（已查文档确认：Lambda 是异步调用模式）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.micro | $0.0104/hr | ~0.3 hr（每次构建约 5 分钟） | ~$0.003 |
| Lambda | 免费层 | < 10 次调用 | $0.00 |
| Step Functions | 免费层 | < 10 次转换 | $0.00 |
| Image Builder | 免费 | — | $0.00 |
| EBS Snapshots | $0.05/GB/月 | ~8 GB（生成的 AMI） | ~$0.40/月 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
REGION=us-east-1
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 1. 删除 Image Builder 资源（按依赖顺序）
aws imagebuilder delete-image-pipeline \
  --image-pipeline-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:image-pipeline/ib-lab-pipeline-sfn \
  --region $REGION

# 删 AMI 和关联 snapshot
for build in 1 2 3; do
  aws imagebuilder delete-image \
    --image-build-version-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:image/ib-lab-recipe/1.0.0/$build \
    --region $REGION 2>/dev/null
done

# 删配置
aws imagebuilder delete-distribution-configuration \
  --distribution-configuration-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:distribution-configuration/ib-lab-dist \
  --region $REGION
aws imagebuilder delete-infrastructure-configuration \
  --infrastructure-configuration-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:infrastructure-configuration/ib-lab-infra \
  --region $REGION
aws imagebuilder delete-image-recipe \
  --image-recipe-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:image-recipe/ib-lab-recipe/1.0.0 \
  --region $REGION

# 删 workflow
aws imagebuilder delete-workflow \
  --workflow-build-version-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:workflow/test/ib-lab-wf-sfn/1.0.0/1 \
  --region $REGION
aws imagebuilder delete-workflow \
  --workflow-build-version-arn arn:aws:imagebuilder:$REGION:$ACCOUNT_ID:workflow/test/ib-lab-wf-lambda/1.0.0/1 \
  --region $REGION

# 2. 删除 Lambda
aws lambda delete-function --function-name ib-lab-callback --region $REGION

# 3. 删除 Step Functions
aws stepfunctions delete-state-machine \
  --state-machine-arn arn:aws:states:$REGION:$ACCOUNT_ID:stateMachine:ib-lab-validation \
  --region $REGION
aws stepfunctions delete-state-machine \
  --state-machine-arn arn:aws:states:$REGION:$ACCOUNT_ID:stateMachine:ib-lab-fail \
  --region $REGION

# 4. 删除 Security Group
aws ec2 delete-security-group --group-id $SG_ID --region $REGION

# 5. 删除 IAM（最后删，其他资源可能依赖）
aws iam remove-role-from-instance-profile \
  --instance-profile-name ib-lab-instance-profile \
  --role-name ib-lab-instance-role
aws iam delete-instance-profile --instance-profile-name ib-lab-instance-profile

for ROLE in ib-lab-instance-role ib-lab-exec-role ib-lab-lambda-role ib-lab-sfn-role; do
  for POLICY in $(aws iam list-attached-role-policies --role-name $ROLE \
    --query "AttachedPolicies[].PolicyArn" --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name $ROLE --policy-arn $POLICY
  done
  for POLICY in $(aws iam list-role-policies --role-name $ROLE \
    --query "PolicyNames[]" --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name $ROLE --policy-name $POLICY
  done
  aws iam delete-role --role-name $ROLE
done

# 6. 删除 CloudWatch Logs
aws logs delete-log-group \
  --log-group-name /aws/lambda/ib-lab-callback \
  --region $REGION
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。主要费用来自 EBS Snapshots（AMI 底层存储），如不清理会持续计费。

## 结论与建议

### 选择建议

- **需要自动化多步验证**（合规扫描、安全测试）→ 用 **ExecuteStateMachine**
    - 更简单：同步等待，无需实现回调
    - 状态机输出可供后续步骤引用（`$.stepOutputs.StepName.output`）
    - 适合：CI/CD 流程中的自动化质量门禁

- **需要人工审批或外部系统集成** → 用 **WaitForAction + Lambda**
    - 可以暂停等待人工确认（通过外部调用 `SendWorkflowStepAction`）
    - Lambda 作为桥梁连接任意外部系统（Jira、Slack、自定义 API）
    - 适合：生产环境发布前的手动审批、第三方安全工具集成

### 生产建议

1. **错误处理策略**：对关键验证步骤用 `onFailure: Abort`（阻止不合规镜像发布）；对可选检查用 `onFailure: Continue`
2. **Lambda 幂等性**：WaitForAction 会重试 Lambda，确保回调逻辑是幂等的
3. **超时设置**：根据实际验证耗时合理设置 `timeoutSeconds`，避免使用默认值（SFN 默认 6 小时、WaitForAction 默认 3 天太长了）
4. **IAM 最小权限**：在执行角色中限定 Lambda/SFN 资源 ARN，不要使用通配符

## 参考链接

- [EC2 Image Builder Workflow Step Actions](https://docs.aws.amazon.com/imagebuilder/latest/userguide/wfdoc-step-actions.html)
- [Create a YAML Workflow Document](https://docs.aws.amazon.com/imagebuilder/latest/userguide/image-workflow-create-document.html)
- [SendWorkflowStepAction API Reference](https://docs.aws.amazon.com/imagebuilder/latest/APIReference/API_SendWorkflowStepAction.html)
- [AWS What's New Announcement](https://aws.amazon.com/about-aws/whats-new/2025/11/ec2-image-builder-lambda-step-functions/)
