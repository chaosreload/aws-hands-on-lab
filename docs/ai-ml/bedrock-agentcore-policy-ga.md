# Amazon Bedrock AgentCore Policy 实战：用 Cedar 策略语言精准控制 Agent 工具调用

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60-90 分钟
    - **预估费用**: $5-10（AgentCore Gateway + Lambda 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-24

## 背景

AI Agent 最大的风险之一是**工具滥用**——agent 可能误解业务规则，调用不该调用的工具，或传入超出权限范围的参数。传统方案是在 agent 代码里硬编码安全检查，但这有两个致命缺陷：

1. **可绕过性**：策略在代码内部，agent 可能通过 prompt injection 绕过
2. **不可审计**：每个 agent 实现自己的安全逻辑，无法统一管控

Amazon Bedrock AgentCore Policy（2026 年 3 月 GA）将安全策略从 agent 代码中剥离，放到 **Gateway 边界**执行：

```
Agent → Gateway → [Policy Engine 评估] → Tool
                   ↑
              Cedar 策略集合
```

策略引擎基于 [Cedar](https://www.cedarpolicy.com/)——AWS 开源的策略语言（也用于 Amazon Verified Permissions）。每次 agent 调用工具时，Gateway 拦截请求，Cedar 引擎逐条评估策略，决定 allow 或 deny。**策略执行在代码外部，不可被 agent 操纵。**

## 前置条件

- AWS 账号（需要 `bedrock-agentcore:*`, `lambda:*`, `iam:*`, `cognito-idp:*` 权限）
- AWS CLI v2 已配置
- Python 3.10+
- `bedrock-agentcore-starter-toolkit` (`pip install bedrock-agentcore-starter-toolkit`)

## 核心概念

### 架构组件

| 组件 | 作用 | 类比 |
|------|------|------|
| **Policy Engine** | 存储和管理 Cedar 策略集合 | IAM Policy Store |
| **Cedar Policy** | 定义 permit/forbid 规则 | IAM Policy Statement |
| **Gateway** | 流量拦截点，连接 Policy Engine | API Gateway |
| **OAuth Authorizer** | 识别调用者身份（principal） | Cognito Auth |

### Cedar 授权请求结构

当 agent 通过 Gateway 调用工具时，Gateway 构造一个 Cedar 授权请求：

```json
{
  "principal": "AgentCore::OAuthUser::\"client-id\"",
  "action": "AgentCore::Action::\"RefundTool___process_refund\"",
  "resource": "AgentCore::Gateway::\"gateway-arn\"",
  "context": {
    "input": {
      "amount": 500.0,
      "customer_id": "C001"
    }
  }
}
```

### 类型映射（关键！）

JSON Schema 到 Cedar 的类型映射：

| JSON Schema type | Cedar type | 示例值 |
|-----------------|------------|--------|
| `string` | String | `"hello"` |
| `integer` | Long | `42` |
| `number` | **Decimal** | `42.0` |
| `boolean` | Bool | `true` |

!!! warning "踩坑预警：integer vs number"
    如果工具 schema 声明参数为 `number`（映射到 Cedar Decimal），那么 JSON 传入的**整数值**（如 `500`）会被解析为 Long 类型，与 Decimal 类型不兼容。必须传入浮点值（如 `500.0`）。这是 Cedar 类型系统的严格性导致的，**官方文档未明确记录此行为**。

## 动手实践

### Step 1: 创建 Lambda 工具（退款处理）

```bash
# 创建 Lambda 执行角色
cat > /tmp/lambda-trust.json << EOF
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
  --role-name AgentCorePolicyTestLambdaRole \
  --assume-role-policy-document file:///tmp/lambda-trust.json \
  --region us-east-1

aws iam attach-role-policy \
  --role-name AgentCorePolicyTestLambdaRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

```python
# refund-tool.py — 退款工具 Lambda
import json

def lambda_handler(event, context):
    body = event
    if isinstance(event.get("body"), str):
        body = json.loads(event["body"])

    amount = body.get("amount", 0)
    customer_id = body.get("customer_id", "unknown")
    reason = body.get("reason", "no reason")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "approved",
            "refund_id": f"REF-{customer_id}-{int(amount)}",
            "amount": amount,
            "customer_id": customer_id,
            "message": f"Refund of ${amount} processed for {customer_id}"
        })
    }
```

```bash
# 打包并创建 Lambda
cd /tmp && zip refund-tool.zip refund-tool.py
aws lambda create-function \
  --function-name AgentCorePolicyRefundTool \
  --runtime python3.12 \
  --handler refund-tool.lambda_handler \
  --role arn:aws:iam::YOUR_ACCOUNT:role/AgentCorePolicyTestLambdaRole \
  --zip-file fileb:///tmp/refund-tool.zip \
  --timeout 30 \
  --region us-east-1
```

### Step 2: 创建 OAuth 认证

Policy Engine 需要 JWT 来识别 principal（**不支持 NONE auth**）。使用 starter toolkit 创建 Cognito OAuth：

```python
from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient

client = GatewayClient(region_name="us-east-1")
cognito_response = client.create_oauth_authorizer_with_cognito("PolicyTest")
# 保存 cognito_response，后续创建 Gateway 时使用
```

这会自动创建：
- Cognito User Pool + Domain
- Resource Server（scope: `PolicyTest/invoke`）
- App Client（client_credentials flow）

### Step 3: 创建 Policy Engine + Gateway

```bash
# 创建 Policy Engine
aws bedrock-agentcore-control create-policy-engine \
  --name PolicyTestEngine \
  --description "Policy engine for refund control" \
  --region us-east-1
# 记录返回的 policyEngineId

# 创建 Gateway（附加 Policy Engine，ENFORCE 模式）
cat > /tmp/create-gateway.json << EOF
{
    "name": "RefundPolicyGateway",
    "roleArn": "arn:aws:iam::YOUR_ACCOUNT:role/AgentCoreGatewayExecutionRole",
    "protocolType": "MCP",
    "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-03-26"]}},
    "authorizerType": "CUSTOM_JWT",
    "authorizerConfiguration": {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/YOUR_POOL_ID/.well-known/openid-configuration",
            "allowedClients": ["YOUR_CLIENT_ID"],
            "allowedScopes": ["PolicyTest/invoke"]
        }
    },
    "policyEngineConfiguration": {
        "arn": "arn:aws:bedrock-agentcore:us-east-1:YOUR_ACCOUNT:policy-engine/YOUR_ENGINE_ID",
        "mode": "ENFORCE"
    },
    "exceptionLevel": "DEBUG"
}
EOF

aws bedrock-agentcore-control create-gateway \
  --cli-input-json file:///tmp/create-gateway.json \
  --region us-east-1
```

### Step 4: 添加 Gateway Target

```bash
cat > /tmp/create-target.json << EOF
{
    "gatewayIdentifier": "YOUR_GATEWAY_ID",
    "name": "RefundTool",
    "description": "Process customer refunds",
    "targetConfiguration": {
        "mcp": {
            "lambda": {
                "lambdaArn": "arn:aws:lambda:us-east-1:YOUR_ACCOUNT:function:AgentCorePolicyRefundTool",
                "toolSchema": {
                    "inlinePayload": [{
                        "name": "process_refund",
                        "description": "Process a customer refund",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "amount": {"type": "number", "description": "Refund amount in USD"},
                                "customer_id": {"type": "string", "description": "Customer ID"},
                                "reason": {"type": "string", "description": "Refund reason"}
                            },
                            "required": ["amount", "customer_id"]
                        }
                    }]
                }
            }
        }
    },
    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
}
EOF

aws bedrock-agentcore-control create-gateway-target \
  --cli-input-json file:///tmp/create-target.json \
  --region us-east-1
```

### Step 5: 创建 Cedar 策略

**方式一：自然语言生成**

```bash
aws bedrock-agentcore-control start-policy-generation \
  --policy-engine-id YOUR_ENGINE_ID \
  --resource {arn:YOUR_GATEWAY_ARN} \
  --content {rawText:Allow the process_refund tool from RefundTool to be called only when the refund amount is less than 1000} \
  --name RefundLimit \
  --region us-east-1
```

约 17 秒后，生成的 Cedar 策略：

```cedar
permit(
  principal,
  action == AgentCore::Action::"RefundTool___process_refund",
  resource == AgentCore::Gateway::"YOUR_GATEWAY_ARN"
) when {
  ((context.input).amount).lessThan(decimal("1000.0"))
};
```

**方式二：直接写 Cedar**

```bash
cat > /tmp/refund-policy.json << EOF
{
    "name": "RefundLimitPolicy",
    "policyEngineId": "YOUR_ENGINE_ID",
    "description": "Permit refunds under 1000 USD",
    "definition": {
        "cedar": {
            "statement": "permit(\n  principal,\n  action == AgentCore::Action::\"RefundTool___process_refund\",\n  resource == AgentCore::Gateway::\"YOUR_GATEWAY_ARN\"\n) when {\n  ((context.input).amount).lessThan(decimal(\"1000.0\"))\n};"
        }
    }
}
EOF

aws bedrock-agentcore-control create-policy \
  --cli-input-json file:///tmp/refund-policy.json \
  --region us-east-1
```

### Step 6: 测试策略效果

```python
import json, requests

GATEWAY_URL = "https://YOUR_GATEWAY_ID.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
TOKEN_URL = "https://YOUR_DOMAIN.auth.us-east-1.amazoncognito.com/oauth2/token"

# 获取 OAuth token
token_resp = requests.post(TOKEN_URL,
    data={"grant_type": "client_credentials",
          "client_id": "YOUR_CLIENT_ID",
          "client_secret": "YOUR_CLIENT_SECRET",
          "scope": "PolicyTest/invoke"},
    headers={"Content-Type": "application/x-www-form-urlencoded"})
access_token = token_resp.json()["access_token"]

def call_refund(amount):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "RefundTool___process_refund",
                   "arguments": {"amount": amount, "customer_id": "C001", "reason": "test"}}
    })
    resp = requests.post(GATEWAY_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {access_token}"})
    return resp.json()

# 测试！
print(call_refund(500.0))   # ✅ ALLOWED
print(call_refund(2000.0))  # ❌ DENIED by policy
```

## 测试结果

### 基线测试（ENFORCE 模式）

| 场景 | 金额 | JSON 类型 | 预期 | 实际 | 延迟 |
|------|------|-----------|------|------|------|
| 正常允许 | $500.0 | float | ALLOW | ✅ ALLOW | 984ms |
| 正常拒绝 | $2000.0 | float | DENY | ✅ DENY | 907ms |
| 边界允许 | $999.99 | float | ALLOW | ✅ ALLOW | 973ms |
| 边界拒绝 | $1000.0 | float | DENY | ✅ DENY | 918ms |
| 零值 | $0.01 | float | ALLOW | ✅ ALLOW | 973ms |
| 负值 | $-100.0 | float | ALLOW | ✅ ALLOW | 1014ms |

### 类型不匹配实验

| 金额值 | JSON 传入类型 | Cedar 期望 | 结果 |
|--------|-------------|-----------|------|
| `500` | Long (integer) | Decimal | ❌ 评估错误 |
| `500.0` | Decimal (float) | Decimal | ✅ 正常允许 |
| `"500"` | String | Decimal | ❌ 评估错误 |

**关键发现**：Cedar 类型系统严格，`number` 类型参数在 JSON 中必须以浮点格式传入（`500.0`），不能是整数（`500`）。

### ENFORCE vs LOG_ONLY 模式

| 模式 | $500.0 | $2000.0 | 行为 |
|------|--------|---------|------|
| ENFORCE | ✅ ALLOW | ❌ DENY | 策略生效，违规请求被阻断 |
| LOG_ONLY | ✅ ALLOW | ✅ ALLOW | 策略只记录到 CloudWatch，不阻断 |

**建议**：上线前先用 LOG_ONLY 观察策略效果，确认无误后再切换到 ENFORCE。

### 多策略组合（permit + forbid）

同时存在两条策略：
- **permit**: 允许 `amount < 1000`
- **forbid**: 禁止 `amount >= 500`

| 金额 | permit 匹配? | forbid 匹配? | 最终结果 |
|------|-------------|-------------|----------|
| $100.0 | ✅ | ❌ | ALLOWED |
| $499.99 | ✅ | ❌ | ALLOWED |
| $500.0 | ✅ | ✅ | **DENIED** (forbid wins) |
| $750.0 | ✅ | ✅ | **DENIED** (forbid wins) |
| $1000.0 | ❌ | ✅ | DENIED |

**关键发现**：Cedar 的 "explicit deny wins" 模型与 AWS IAM 一致——**forbid 永远覆盖 permit**。

### 自然语言策略生成

| 指标 | 值 |
|------|-----|
| 生成时间 | ~17 秒 |
| 准确性 | 生成的 Cedar 与手写等效 |
| 验证 | 过度宽松/过度限制策略被自动检测 |

## 踩坑记录

!!! warning "踩坑 1: NONE authorizer 不支持 Policy"
    Gateway 使用 `authorizerType: NONE` 时，Policy Engine 会返回 "Policy Evaluation Internal Failure"。原因：Cedar 策略的 principal 必须是 `AgentCore::OAuthUser`，需要 JWT token 提供身份信息。**已查文档确认**：principal 必须是 OAuthUser。

!!! warning "踩坑 2: Gateway authorizer 创建后不可更改"
    已创建的 Gateway 不能从 NONE 改为 CUSTOM_JWT（返回 "Authorizer type cannot be updated"）。如果一开始选错了 auth 方式，只能删除重建。**已查文档确认**：这是 API 限制。

!!! warning "踩坑 3: integer vs number 类型严格"
    JSON 的 `500`（integer/Long）和 `500.0`（float/Decimal）在 Cedar 中是**不同类型**。如果工具 schema 将参数声明为 `number`（映射到 Decimal），必须确保 JSON 传入浮点值。**实测发现，官方未明确记录此行为**。

!!! warning "踩坑 4: 必须使用 client_credentials 流"
    Cognito User Password Auth 返回的 token 会报 "insufficient_scope"。必须使用 OAuth 2.0 client_credentials 流，并在 Cognito Resource Server 中配置自定义 scope。**已查文档确认**。

!!! warning "踩坑 5: 无条件 permit 被验证器拒绝"
    创建无任何 when 条件的 permit 策略，会被标记为 "Overly Permissive" 并拒绝创建（除非设置 `validationMode: IGNORE_ALL_FINDINGS`）。这是一个**安全护栏**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AgentCore Gateway | 按请求计费 | ~50 requests | ~$0.01 |
| Lambda (128MB) | $0.20/1M requests | ~50 invocations | ~$0 (Free Tier) |
| Cognito User Pool | Free Tier (50K MAU) | 1 user | $0 |
| **合计** | | | **< $1** |

## 清理资源

```bash
REGION=us-east-1
PROFILE=your-profile

# 1. 删除策略
aws bedrock-agentcore-control delete-policy \
  --policy-id YOUR_POLICY_ID \
  --policy-engine-id YOUR_ENGINE_ID \
  --region $REGION --profile $PROFILE

# 2. 删除 Gateway Target
aws bedrock-agentcore-control delete-gateway-target \
  --gateway-identifier YOUR_GATEWAY_ID \
  --target-id YOUR_TARGET_ID \
  --region $REGION --profile $PROFILE

# 3. 删除 Gateway
aws bedrock-agentcore-control delete-gateway \
  --gateway-identifier YOUR_GATEWAY_ID \
  --region $REGION --profile $PROFILE

# 4. 删除 Policy Engine
aws bedrock-agentcore-control delete-policy-engine \
  --policy-engine-id YOUR_ENGINE_ID \
  --region $REGION --profile $PROFILE

# 5. 删除 Lambda
aws lambda delete-function \
  --function-name AgentCorePolicyRefundTool \
  --region $REGION --profile $PROFILE

# 6. 删除 Cognito User Pool
aws cognito-idp delete-user-pool \
  --user-pool-id YOUR_POOL_ID \
  --region $REGION --profile $PROFILE

# 7. 删除 IAM Role
aws iam detach-role-policy \
  --role-name AgentCorePolicyTestLambdaRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name AgentCorePolicyTestLambdaRole
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。

## 结论与建议

### 适用场景

- **金融合规**：限制退款/转账金额上限
- **多租户 Agent**：不同用户只能访问自己权限范围的工具
- **安全运维**：紧急关闭某个工具（forbid + no condition = emergency shutdown）
- **灰度发布**：LOG_ONLY 模式观察新策略效果

### 生产环境建议

1. **先 LOG_ONLY 后 ENFORCE**：新策略上线先用日志模式观察，确认无误再切换
2. **注意 Cedar 类型匹配**：JSON Schema 用 `number` 类型时，确保 agent 发送浮点值
3. **利用自然语言生成 + 人工审核**：自然语言生成策略效果不错，但建议生成后人工审核 Cedar
4. **利用 forbid 做安全兜底**：permit 定义允许范围，forbid 做硬性上限
5. **策略验证器是好东西**：过度宽松/限制/不可满足策略都会被检测

### 与现有方案对比

| 维度 | AgentCore Policy | 代码内策略 | AWS IAM |
|------|-----------------|-----------|---------|
| 执行位置 | Gateway 边界 | Agent 内部 | API 层 |
| 可绕过性 | ❌ 不可 | ⚠️ 可被 agent 操纵 | ❌ 不可 |
| 粒度 | 工具 + 参数级 | 自定义 | API 操作级 |
| 审计 | CloudWatch 自动 | 需自行实现 | CloudTrail |
| 动态调整 | 改策略不改代码 | 需改代码重部署 | 改 IAM Policy |

## 参考链接

- [Policy in Amazon Bedrock AgentCore 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Cedar Policy 语言官网](https://www.cedarpolicy.com/)
- [AgentCore Gateway 快速入门](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-quick-start.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/)
- [Policy Schema Constraints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-schema-constraints.html)
- [Common Policy Patterns](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-common-patterns.html)
