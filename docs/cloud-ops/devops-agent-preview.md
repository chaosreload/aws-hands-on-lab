---
tags:
  - Cloud Operations
---

# AWS DevOps Agent 深度解析：Frontier Agent 如何革新运维事件响应

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $0（Preview 免费）+ Lambda/CloudWatch 测试费用 < $0.01
    - **Region**: us-east-1（Agent Space 仅此 Region，可监控任意 Region 应用）
    - **最后验证**: 2026-03-25

## 背景

生产环境出事故时，On-call 工程师需要在巨大压力下快速定位根因：翻多个监控工具、查最近部署记录、协调响应团队、同时还要更新 stakeholder。事后，团队又常因人力不足，无法系统性地将 incident learnings 转化为改进措施。

AWS DevOps Agent 是 AWS **Frontier Agent** 品类的首批产品之一 — 一种新型 AI Agent，能自主运行数小时甚至数天而无需持续人工干预。它的定位很明确：**你的 7×24 自动化 On-call 工程师**。

## 前置条件

- AWS 账号（需要 IAM 管理员权限用于创建 Agent Space IAM Roles）
- AWS CLI v2 已配置（用于部署测试基础设施）
- us-east-1 Region 访问权限
- （可选）Slack workspace（用于 incident coordination）
- （可选）GitHub/GitLab 仓库（用于 CI/CD 集成）

## 核心概念

### Frontier Agent 是什么？

Frontier Agent 是 AWS 定义的一类新型 AI Agent：**自主、大规模可扩展、能持续工作数小时到数天**。与传统 Chatbot 或 Copilot 不同，它不需要你一步步提示，而是像一个经验丰富的工程师一样独立完成调查。

### DevOps Agent 架构一览

| 概念 | 说明 |
|------|------|
| **Agent Space** | 逻辑容器，定义 Agent 可访问的 AWS 账号、第三方集成和用户权限 |
| **Topology** | 自动构建的应用拓扑图，映射资源和关系（通过 CloudFormation Stack + Resource Tags 发现） |
| **Investigation** | 自动化事件调查流程，从告警触发到根因分析 |
| **Prevention** | 基于历史事件的主动改进建议，每周自动评估 |
| **Skills** | 模块化指令集，扩展 Agent 的专业能力 |
| **Web App** | 独立于 Console 的操作界面，面向 On-call 工程师 |

### 双控制台架构

DevOps Agent 采用 **Admin Console + Operator Web App** 分离架构：

- **AWS Management Console**：管理员创建/配置 Agent Space、连接 AWS 服务和第三方工具、管理权限
- **DevOps Agent Web App**：运维人员日常使用 — 发起调查、Chat 交互、查看 Topology、审阅 Prevention 建议

Web App 支持两种认证方式：

1. **IAM Identity Center**（推荐）— 支持 OIDC/SAML 联合认证 + MFA
2. **IAM 直接访问链接** — 从 Console 直接进入，但 session 限时 10 分钟

## 动手实践

### Step 1: 部署测试 Lambda（制造事故现场）

首先，我们部署一个会故意出错的 Lambda 函数和对应的 CloudWatch Alarm，为 DevOps Agent 制造一个事故场景。

创建 CloudFormation 模板：

```yaml
# devops-agent-test-stack.yaml
AWSTemplateFormatVersion: "2010-09-09"
Description: "DevOps Agent Test - Error-generating Lambda with CloudWatch Alarm"

Resources:
  LambdaExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: devops-agent-test-lambda-role
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: lambda.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  ErrorGeneratorFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: devops-agent-error-generator
      Runtime: python3.12
      Handler: index.handler
      Role: !GetAtt LambdaExecutionRole.Arn
      Timeout: 30
      Code:
        ZipFile: |
          import json
          import random

          def handler(event, context):
              mode = event.get("mode", "error")
              if mode == "error":
                  raise Exception("Simulated production incident - database connection timeout")
              elif mode == "timeout":
                  import time
                  time.sleep(35)  # 超过 Lambda 30s 超时
              else:
                  return {"statusCode": 200, "body": json.dumps({"message": "OK"})}

  LambdaErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: devops-agent-test-error-alarm
      AlarmDescription: "Lambda error rate alarm for DevOps Agent testing"
      MetricName: Errors
      Namespace: AWS/Lambda
      Statistic: Sum
      Period: 60
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      Dimensions:
        - Name: FunctionName
          Value: !Ref ErrorGeneratorFunction

Outputs:
  FunctionName:
    Value: !Ref ErrorGeneratorFunction
  FunctionArn:
    Value: !GetAtt ErrorGeneratorFunction.Arn
  AlarmName:
    Value: !Ref LambdaErrorAlarm
```

部署 Stack：

```bash
aws cloudformation deploy \
  --template-file devops-agent-test-stack.yaml \
  --stack-name devops-agent-test \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

### Step 2: 触发 Lambda 错误，让 Alarm 进入 ALARM 状态

```bash
# 批量触发错误（10 次）
for i in $(seq 1 10); do
  aws lambda invoke \
    --function-name devops-agent-error-generator \
    --payload $(echo -n '{"mode": "error"}' | base64) \
    --region us-east-1 \
    /tmp/lambda-out-$i.json 2>&1
done
```

实测结果：

```
StatusCode: 200, FunctionError: Unhandled  × 10 次
```

等待约 90 秒后确认 Alarm 状态：

```bash
aws cloudwatch describe-alarms \
  --alarm-names devops-agent-test-error-alarm \
  --region us-east-1 \
  --query "MetricAlarms[0].{State:StateValue,Updated:StateUpdatedTimestamp}" \
  --output json
```

```json
{
    "State": "ALARM",
    "Updated": "2026-03-25T05:28:58.517000+00:00"
}
```

✅ 事故现场已准备好。Lambda 持续出错，CloudWatch Alarm 已触发。

### Step 3: 创建 Agent Space（Console 操作）

!!! note "Console 操作"
    Agent Space 创建必须通过 AWS Console 完成，目前无 CLI/API 支持。

1. 进入 [AWS Console → DevOps Agent](https://console.aws.amazon.com/devops-agent/)（us-east-1）
2. 点击 **Create Agent Space**
3. 输入 Agent Space 名称（如 `my-app-space`）
4. 创建 IAM Roles — 授权 Agent 访问你的 AWS 账号资源
5. 启用 **DevOps Agent Web App**
6. 点击 **Create**

创建完成后，进入 **Topology** 页面。DevOps Agent 会自动发现你账号中的资源：

- 通过 **CloudFormation Stacks** 发现 IaC 部署的资源
- 通过 **Resource Tags** 发现手动创建的资源（需启用 Resource Explorer）

Topology 提供三个视图层级：

| 视图 | 说明 |
|------|------|
| System View | 账号和 Region 边界的高层视图 |
| Container View | CloudFormation Stack 等部署容器级别 |
| Resource View | 所有资源及其关系的完整视图 |

### Step 4: 启动调查

在 DevOps Agent Web App 中：

1. 点击 **Start Investigation**
2. 可选择预置模板快速启动：
    - **Latest alarm** — 调查最近触发的告警
    - **High CPU usage** — 调查 CPU 利用率过高
    - **Error rate spike** — 调查错误率飙升
3. 或手动输入调查详情（incident 描述、时间、账号 ID）

Agent 开始自动调查后会：

- 关联 CloudWatch 指标和日志
- 检查 CloudWatch Logs 和 X-Ray 追踪
- 审查 GitHub/GitLab 中的最近代码变更
- 分析部署历史与错误时间线的相关性
- 在 Slack channel 中实时更新进展（如已配置）

### Step 5: 与 Agent 交互

通过 Web App 的 Chat 界面，你可以：

- 提问：*"你分析了哪些日志？"*
- 引导方向：*"聚焦这些特定的 log groups 重新分析"*
- 查询基础设施：*"显示连接到这个 DynamoDB 表的所有 Lambda 函数"*
- 创建 AWS Support Case（一键填充调查发现）

## 测试结果

### 功能验证总结

| 测试项 | 状态 | 结果 |
|--------|------|------|
| CloudFormation Stack 部署 | ✅ 实测 | Lambda + Alarm 成功创建 |
| Lambda 错误触发 | ✅ 实测 | 10 次调用全部返回 FunctionError: Unhandled |
| CloudWatch Alarm 响应 | ✅ 实测 | ~90 秒内从 INSUFFICIENT_DATA → ALARM |
| CLI/API 可用性 | ✅ 实测 | **无 CLI/API** — `aws devops-agent` 不存在 |
| Agent Space 创建 | 📖 文档 | Console-only，需 IAM 管理员权限 |
| Investigation 流程 | 📖 文档 | 自动关联 CW metrics/logs + 代码变更 |
| Prevention 建议 | 📖 文档 | 每周自动评估，四维改进建议 |

### 集成生态对比

| 集成类型 | 内置支持 | 扩展方式 |
|----------|----------|----------|
| **观测工具** | CloudWatch, Datadog, Dynatrace, New Relic, Splunk | BYO MCP Server（Grafana, Prometheus 等） |
| **CI/CD** | GitHub Actions, GitLab CI/CD | BYO MCP Server |
| **工单系统** | ServiceNow | Webhook（PagerDuty 等） |
| **通信** | Slack | — |
| **代码仓库** | GitHub, GitLab | BYO MCP Server |

### 与现有 AWS 运维工具对比

| 维度 | CloudWatch Investigations (aiops) | DevOps Agent |
|------|-------------------------------------|--------------|
| 定位 | CloudWatch 内的 AI 辅助调查 | 独立 Frontier Agent 产品 |
| 自主性 | 需要人工引导 | 自主调查，可持续数小时 |
| 集成范围 | AWS 观测数据 | AWS + 第三方观测 + CI/CD + 工单 |
| MCP 支持 | ❌ | ✅ BYO MCP Server |
| CLI/API | ✅ `aws aiops` | ❌ Console-only |
| 预防功能 | ❌ | ✅ 四维改进建议 |

## 踩坑记录

!!! warning "注意事项"

    **1. 无 CLI/API 支持（已查文档确认）**

    DevOps Agent 完全依赖 Console UI。AWS CLI v2.34.14 中没有 `devops-agent` service，boto3 也没有 service model。自动化部署和 IaC 管理目前不可能。这对于习惯 Infrastructure as Code 的团队是个明显限制。

    **2. IAM 直接访问 Web App 限时 10 分钟（已查文档确认）**

    如果不配置 IAM Identity Center，通过 Console 直接进入 Web App 的 session 仅有 10 分钟。生产环境强烈建议配置 Identity Center。

    **3. Resource Explorer 依赖（已查文档确认）**

    Topology 通过 Resource Tags 发现资源时，需要目标 AWS 账号已启用 Resource Explorer。如果你的资源主要是手动创建（非 CloudFormation），记得先开启 Resource Explorer。

    **4. Agent 调查范围不限于 Topology（已查文档确认）**

    Agent 可以通过 AWS service APIs 或连接的观测工具调查 Topology 之外的资源。如需限制 Agent 的访问范围，需要在 IAM Role 策略中明确限制。

    **5. `aiops` ≠ DevOps Agent（实测发现）**

    AWS CLI 中有 `aiops` 服务（CloudWatch Investigations），容易与 DevOps Agent 混淆。它们是完全不同的产品。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| DevOps Agent | Preview 免费 | — | $0 |
| Lambda 调用 | $0.20/1M 请求 | 10 请求 | ~$0 |
| CloudWatch Alarm | $0.10/alarm/月 | 1 alarm | $0.10 |
| CloudWatch Logs | $0.50/GB | ~1KB | ~$0 |
| **合计** | | | **< $0.15** |

!!! tip "Preview 定价"
    Preview 期间 DevOps Agent 本身免费，但有月度 agent task hours 上限（具体数值未公布）。GA 后定价模型待定。

## 清理资源

```bash
# 删除 CloudFormation Stack（包含 Lambda + Alarm + IAM Role）
aws cloudformation delete-stack \
  --stack-name devops-agent-test \
  --region us-east-1

# 确认删除完成
aws cloudformation wait stack-delete-complete \
  --stack-name devops-agent-test \
  --region us-east-1

# Agent Space 需要在 Console 中手动删除
# Console → DevOps Agent → 选择 Agent Space → Delete
```

!!! danger "务必清理"
    虽然 Preview 期间 DevOps Agent 免费，但关联的 Lambda 和 CloudWatch 资源会产生微量费用。Lab 完成后请执行清理。

## 结论与建议

### 适合场景

- **中大型团队的事件响应**：多服务、多工具的环境下，Agent 自动关联数据的能力最有价值
- **7×24 运维**：凌晨 2 点告警不再需要人工值守，Agent 先行调查
- **DevOps 成熟度提升**：Prevention 功能推动从"救火"到"预防"的转变
- **多云/混合环境**：通过 MCP Server 扩展，不局限于 AWS 原生工具

### 生产环境建议

1. **Authentication**：配置 IAM Identity Center，不要依赖 10 分钟限时的 IAM 直接访问
2. **Agent Space 规划**：按团队/应用/环境分 Agent Space，利用数据隔离保障安全
3. **Resource Explorer**：先启用 Resource Explorer，确保 Topology 能发现所有资源
4. **IAM 最小权限**：限制 Agent Role 的访问范围，避免过度授权
5. **Slack 集成**：配置 Slack 集成获取实时调查更新，加速团队协作
6. **Prevention 闭环**：定期审阅 Prevention 建议，接受或拒绝并提供反馈，帮助 Agent 学习

### 当前限制

- 无 CLI/API（无法 IaC 管理 Agent Space）
- 仅 us-east-1 可创建 Agent Space
- Preview 阶段有 task hours 上限
- GA 定价待定

### 展望

作为 Frontier Agent 品类的早期产品，DevOps Agent 展示了 AWS 对"自主 AI Agent"的愿景：不只是辅助人类决策（Copilot 模式），而是独立完成复杂、长时间的任务。MCP Server 支持也意味着它能融入现有工具链，而非替换。随着 GA 版本的发布和 CLI/API 的增加，这将成为 DevOps 团队的重要工具。

## 参考链接

- [AWS DevOps Agent 产品页](https://aws.amazon.com/devops-agent/)
- [AWS DevOps Agent 文档](https://docs.aws.amazon.com/devopsagent/latest/userguide/)
- [AWS News Blog：AWS DevOps Agent 发布公告](https://aws.amazon.com/blogs/aws/aws-devops-agent-helps-you-accelerate-incident-response-and-improve-system-reliability-preview/)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/devops-agent-preview-frontier-agent-operational-excellence/)
- [AWS Frontier Agents](https://aws.amazon.com/ai/frontier-agents/)
