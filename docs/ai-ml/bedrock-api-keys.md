---
tags:
  - Bedrock
  - API
  - What's New
---

# Amazon Bedrock API Keys：告别繁琐 IAM 配置，一键调用大模型

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 15 分钟
    - **预估费用**: < $0.01（仅模型推理费用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

调用 Amazon Bedrock API 的传统流程需要：创建 IAM 用户/角色 → 配置 IAM Policy → 生成 Access Key → 配置 AWS CLI credentials。对于初次接触 AWS 的开发者，这套流程是不小的门槛。

2025 年 7 月，Amazon Bedrock 推出了 **API Keys** 功能。开发者可以直接在 Bedrock 控制台或通过 API 生成 API Key，使用标准的 `Bearer Token` 方式认证，大幅简化上手流程。

## 前置条件

- AWS 账号，IAM 用户需具备以下权限：
    - `iam:CreateUser`、`iam:AttachUserPolicy`、`iam:CreateServiceSpecificCredential`（创建 Long-term Key）
    - 或 Bedrock 控制台访问权限（Console 一键生成）
- AWS CLI v2 已安装并配置 Profile
- 已开通目标模型的 Model Access（本文使用 Claude 3.5 Haiku）

## 核心概念

### 两种 API Key 类型

| 特性 | Short-term Key | Long-term Key |
|------|---------------|---------------|
| **有效期** | min(12 小时, Session 时长) | 自定义（1 天 ~ 无期限） |
| **权限来源** | 继承当前 IAM Principal 权限 | 自动创建 IAM User + `AmazonBedrockLimitedAccess` |
| **跨 Region** | ❌ 仅限生成 Region | ✅ 可跨 Region 使用 |
| **创建方式** | Console / SDK | Console / CLI / SDK |
| **适用场景** | 生产环境（定期轮换） | 探索和开发（AWS 官方建议仅用于探索） |

### API Key 认证 vs 传统 SigV4

| 对比项 | API Key (Bearer Token) | 传统 SigV4 |
|--------|----------------------|------------|
| **配置步骤** | 1 步（生成 Key） | 4+ 步（IAM User → Policy → Access Key → CLI Config） |
| **认证方式** | `Authorization: Bearer <key>` | AWS Signature V4 |
| **适用范围** | 仅 Bedrock / Bedrock Runtime | 所有 AWS 服务 |
| **CloudTrail** | ✅ 记录 API 调用（Key 本身不记录） | ✅ 完整记录 |

### 不支持的 API

API Key **不能**用于以下 Bedrock API：

- `InvokeModelWithBidirectionalStream`（双向流）
- Agents for Amazon Bedrock（代理）
- Data Automation for Amazon Bedrock（数据自动化）

## 动手实践

### Step 1: 创建 IAM 用户

```bash
# 创建专用 IAM 用户
aws iam create-user \
    --user-name bedrock-api-key-test \
    --region us-east-1 \
    --profile <your-profile>
```

### Step 2: 附加 Bedrock 权限策略

```bash
# 附加 AmazonBedrockLimitedAccess 托管策略
aws iam attach-user-policy \
    --user-name bedrock-api-key-test \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockLimitedAccess \
    --profile <your-profile>
```

### Step 3: 生成 Long-term API Key

```bash
# 生成有效期 30 天的 Bedrock API Key
aws iam create-service-specific-credential \
    --user-name bedrock-api-key-test \
    --service-name bedrock.amazonaws.com \
    --credential-age-days 30 \
    --profile <your-profile>
```

输出示例：

```json
{
    "ServiceSpecificCredential": {
        "CreateDate": "2026-03-27T02:34:20+00:00",
        "ExpirationDate": "2026-04-26T02:34:20+00:00",
        "ServiceName": "bedrock.amazonaws.com",
        "ServiceCredentialAlias": "bedrock-api-key-test-at-595842667825",
        "ServiceCredentialSecret": "ABSK...(Base64 编码的 API Key)...",
        "ServiceSpecificCredentialId": "ACCAYVOX5SUYX5PVDRUM5",
        "UserName": "bedrock-api-key-test",
        "Status": "Active"
    }
}
```

`ServiceCredentialSecret`（以 `ABSK` 开头）就是你的 API Key。

### Step 4: 使用 API Key 调用 Bedrock

#### 方式一：curl 直接调用

```bash
# 设置环境变量
export AWS_BEARER_TOKEN_BEDROCK="<your-api-key>"

# 调用 Converse API
curl -X POST \
  "https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-3-5-haiku-20241022-v1:0/converse" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK" \
  -d '{
    "messages": [
        {
            "role": "user",
            "content": [{"text": "What is the capital of France? Answer in one word."}]
        }
    ]
  }'
```

#### 方式二：Python (Boto3)

```python
import os
import boto3

# 设置 API Key（也可通过环境变量 AWS_BEARER_TOKEN_BEDROCK）
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "<your-api-key>"

# 创建 Bedrock Runtime 客户端（SDK 自动检测环境变量）
client = boto3.client(
    service_name="bedrock-runtime",
    region_name="us-east-1"
)

# 调用 Converse API
response = client.converse(
    modelId="us.anthropic.claude-3-5-haiku-20241022-v1:0",
    messages=[{"role": "user", "content": [{"text": "Hello"}]}],
)

print(response["output"]["message"]["content"][0]["text"])
```

## 测试结果

### 认证方式延迟对比

使用相同 prompt 对 Claude 3.5 Haiku 发起 5 次请求：

| 认证方式 | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | 平均延迟 (Server) |
|---------|-------|-------|-------|-------|-------|--------------------|
| API Key (Bearer) | 801ms | 791ms | 1289ms | 848ms | 851ms | **916ms** |
| SigV4 (传统) | 635ms | 687ms | 534ms | 578ms | 694ms | **626ms** |

API Key 认证的 Server-side 延迟比 SigV4 高约 46%。这可能与 Bearer Token 的额外验证步骤有关。不过对于大部分 AI 应用场景，几百毫秒的差异影响不大。

### 跨 Region 测试

| Key 生成 Region | 调用 Region | 结果 |
|----------------|------------|------|
| us-east-1 | us-east-1 | ✅ 成功 |
| us-east-1 | us-west-2 | ✅ 成功（Long-term Key 可跨 Region） |

### 不支持的 API 测试

| API | 结果 | 错误信息 |
|-----|------|---------|
| Bedrock Runtime (Converse) | ✅ 成功 | — |
| Agents Runtime | ❌ 403 | `Authorization header requires 'Credential' parameter`（要求 SigV4） |
| 无效 API Key | ❌ 403 | `Invalid API Key format: Base64 decoding failed` |

## 踩坑记录

!!! warning "注意事项"

    **1. 必须使用 Inference Profile ID**
    
    调用模型时不能使用 base model ID（如 `anthropic.claude-3-5-haiku-20241022-v1:0`），必须使用 cross-region inference profile ID（如 `us.anthropic.claude-3-5-haiku-20241022-v1:0`），否则返回 400 错误。实测发现，官方文档示例中使用了正确的 ID 但未明确说明此限制。
    
    **2. `ServiceCredentialSecret` = API Key**
    
    文档中提到 `ServiceApiKeyValue` 是 API Key，但 CLI 返回字段名为 `ServiceCredentialSecret`。两者是同一值，以 `ABSK` 前缀开头。
    
    **3. Long-term Key 的安全建议**
    
    AWS 官方**强烈建议**仅将 Long-term Key 用于探索目的。生产环境应使用 Short-term Key 或传统 IAM 临时凭证。已查文档确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| IAM User / API Key | 免费 | — | $0 |
| Claude 3.5 Haiku 推理 | $0.80/1M input, $4.00/1M output | ~200 input + ~150 output tokens | < $0.01 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 删除 Service-Specific Credential（API Key）
aws iam delete-service-specific-credential \
    --user-name bedrock-api-key-test \
    --service-specific-credential-id <credential-id> \
    --profile <your-profile>

# 2. 卸载策略
aws iam detach-user-policy \
    --user-name bedrock-api-key-test \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockLimitedAccess \
    --profile <your-profile>

# 3. 删除 IAM 用户
aws iam delete-user \
    --user-name bedrock-api-key-test \
    --profile <your-profile>
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 API Key 本身不产生费用，但 Long-term Key 关联的 IAM User 如果被滥用可能导致意外的模型调用费用。

## 结论与建议

**Amazon Bedrock API Keys 大幅降低了调用大模型 API 的入门门槛**。开发者可以跳过复杂的 IAM 配置，几分钟内就能开始调用模型。

### 适用场景

| 场景 | 推荐方式 |
|------|---------|
| 快速原型 / Demo | ✅ Long-term Key（设置合理过期时间） |
| 开发测试 | ✅ Short-term Key |
| 生产环境 | ⚠️ Short-term Key（需实现自动刷新）或传统 IAM Role |
| 第三方框架集成（OpenAI SDK 等） | ✅ API Key（天然兼容 Bearer Token） |
| 企业安全合规 | 使用 SCP + Condition Key 控制 Key 的生成和使用 |

### 企业安全控制

管理员可通过以下方式控制 API Key：

- **`bedrock:CallWithBearerToken`** — 控制 API Key 的使用
- **`bedrock:bearerTokenType`** — 区分控制 Short-term / Long-term Key
- **`iam:CreateServiceSpecificCredential`** — 控制 Long-term Key 的生成
- **`iam:ServiceSpecificCredentialAgeDays`** — 限制 Key 最大有效期

## 参考链接

- [Amazon Bedrock API Keys 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
- [API Key 生成指南](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html)
- [API Key 使用方式](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html)
- [权限控制](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-permissions.html)
- [AWS Blog: Accelerate AI development with Amazon Bedrock API keys](https://aws.amazon.com/blogs/machine-learning/accelerate-ai-development-with-amazon-bedrock-api-keys/)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-bedrock-api-keys-for-streamlined-development/)
