# Amazon Nova 自定义模型 On-Demand Deployment 解析：按需推理的新选择

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟（含 Fine-tuning 等待时间）
    - **预估费用**: < $5（Fine-tuning + 推理测试）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

2025 年 7 月 16 日，AWS 发布了一项重要更新：**Amazon Bedrock 支持对自定义 Nova 模型进行 On-Demand 部署**。在此之前，使用 fine-tuned 或 distilled 的自定义模型进行推理，唯一的选择是 **Provisioned Throughput**——按小时/月预置计算资源，适合稳定高吞吐负载，但对低频使用场景来说成本偏高。

On-Demand Deployment 改变了这一局面：**按用量付费，用多少付多少**，与 base 模型的 on-demand 推理体验一致。这对于开发测试、低频推理、突发负载场景意义重大。

本文将从功能解析、API 流程、定价对比三个维度深入分析这一功能，并分享实际 fine-tuning 测试中的踩坑经历。

## 前置条件

- AWS 账号（需要 Bedrock 模型访问权限）
- AWS CLI v2 已配置
- 一个已完成的 Nova 自定义模型（fine-tuned / distilled / SageMaker AI 导入）
- 模型必须在 **2025-07-16 或之后**完成自定义

## 核心概念

### On-Demand vs Provisioned Throughput

此前，自定义模型只能通过 Provisioned Throughput 进行推理。现在有了两条路径：

| 维度 | Provisioned Throughput | On-Demand Deployment |
|------|----------------------|---------------------|
| **计费模式** | 按小时/月预置，固定成本 | 按 token 使用量付费 |
| **启动方式** | 创建 Provisioned Throughput 单元 | `CreateCustomModelDeployment` API |
| **推理调用** | 使用 Provisioned Throughput ARN | 使用 Deployment ARN 作为 `modelId` |
| **适用场景** | 高吞吐、稳定负载、对延迟敏感 | 低频调用、开发测试、突发负载 |
| **成本特点** | 可预测，持续计费 | 弹性，无使用不计费 |
| **承诺折扣** | 支持 1/6 月承诺 | 不适用 |

**选择建议**：

- **开发/测试阶段** → On-Demand（灵活，用完不花钱）
- **生产环境 + 稳定流量** → Provisioned Throughput（可预测成本和延迟）
- **生产环境 + 突发流量** → On-Demand（自动弹性）

### 支持的模型和 Region

**支持的 Base Models**：

- Amazon Nova Micro
- Amazon Nova Lite
- Amazon Nova Pro
- Meta Llama 3.3 70B Instruct

**支持的 Region**：

- US East (N. Virginia) — `us-east-1`
- US West (Oregon) — `us-west-2`

!!! warning "时间限制"
    仅在 2025-07-16 或之后完成自定义的模型才支持 On-Demand Deployment。之前创建的自定义模型需要重新训练。

### 定价

On-Demand Deployment 的推理定价**与 base 模型的 on-demand 价格相同**。也就是说，你的 fine-tuned Nova Micro 推理成本和直接调用 base Nova Micro 一样。这相比 Provisioned Throughput 的固定成本模式，对低频使用者来说是明显的成本优势。

## 动手实践

### 完整工作流

On-Demand Deployment 的使用流程分为四步：

```
Fine-tune/Distill → 获得 Custom Model → 部署 On-Demand → 使用 Deployment ARN 推理
```

### Step 1: 准备训练数据

使用 Bedrock Conversation Format (JSONL)，每行一个对话样本：

```json
{
  "schemaVersion": "bedrock-conversation-2024",
  "system": [{"text": "You are ArchieBot, an AWS Solutions Architect assistant."}],
  "messages": [
    {"role": "user", "content": [{"text": "What is Amazon S3?"}]},
    {"role": "assistant", "content": [{"text": "Amazon S3 is a scalable object storage service..."}]}
  ]
}
```

```bash
# 创建 S3 bucket（与 Bedrock 同 Region）
aws s3 mb s3://bedrock-nova-ft-${ACCOUNT_ID}-us-east-1 --region us-east-1

# 上传训练数据
aws s3 cp nova-micro-ft.jsonl \
  s3://bedrock-nova-ft-${ACCOUNT_ID}-us-east-1/training-data/ \
  --region us-east-1
```

### Step 2: 配置 IAM Role

Bedrock Fine-tuning 需要一个 IAM Role 来访问 S3 训练数据：

```bash
# 创建信任策略（限定 Bedrock 服务 + 源账号）
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"}
    }
  }]
}
EOF

# 创建 Role
aws iam create-role \
  --role-name BedrockFinetuningRole \
  --assume-role-policy-document file://trust-policy.json

# 附加 S3 访问策略
aws iam put-role-policy \
  --role-name BedrockFinetuningRole \
  --policy-name S3Access \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::bedrock-nova-ft-*",
        "arn:aws:s3:::bedrock-nova-ft-*/*"
      ]
    }]
  }'
```

### Step 3: 提交 Fine-tuning Job

```bash
aws bedrock create-model-customization-job \
  --job-name "nova-micro-ft-lab" \
  --custom-model-name "nova-micro-custom-lab" \
  --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/BedrockFinetuningRole" \
  --base-model-identifier "amazon.nova-micro-v1:0:128k" \
  --customization-type "FINE_TUNING" \
  --training-data-config '{"s3Uri": "s3://bedrock-nova-ft-${ACCOUNT_ID}-us-east-1/training-data/nova-micro-ft.jsonl"}' \
  --output-data-config '{"s3Uri": "s3://bedrock-nova-ft-${ACCOUNT_ID}-us-east-1/output/"}' \
  --hyper-parameters '{"epochCount": "2", "learningRate": "0.00001", "learningRateWarmupSteps": "5", "batchSize": "1"}' \
  --region us-east-1
```

Nova Micro Fine-tuning 超参数：

| 参数 | 范围 | 默认值 | 说明 |
|------|------|--------|------|
| epochCount | 1-5 | 2 | 训练轮次 |
| learningRate | 1e-6 ~ 1e-4 | 1e-5 | 学习率 |
| learningRateWarmupSteps | 0-100 | 10 | 预热步数 |
| batchSize | 1 | 1 | 批量大小 |

### Step 4: 部署 On-Demand（训练完成后）

```bash
# 部署自定义模型
aws bedrock create-custom-model-deployment \
  --model-deployment-name "nova-micro-ondemand-lab" \
  --model-arn "arn:aws:bedrock:us-east-1:${ACCOUNT_ID}:custom-model/amazon.nova-micro-v1:0:128k/your-model-id" \
  --region us-east-1

# 查询部署状态
aws bedrock get-custom-model-deployment \
  --custom-model-deployment-identifier "deployment-arn" \
  --region us-east-1
```

部署状态变为 `Active` 后，即可使用 Deployment ARN 进行推理：

```bash
# 使用 Deployment ARN 调用推理
aws bedrock-runtime converse \
  --model-id "deployment-arn" \
  --messages '[{"role": "user", "content": [{"text": "What is Amazon S3?"}]}]' \
  --region us-east-1
```

### Step 5: 清理

```bash
# 删除部署（不影响底层 custom model）
aws bedrock delete-custom-model-deployment \
  --custom-model-deployment-identifier "deployment-arn" \
  --region us-east-1

# 删除自定义模型（可选）
aws bedrock delete-custom-model \
  --model-identifier "custom-model-arn" \
  --region us-east-1

# 清理 S3 数据
aws s3 rb s3://bedrock-nova-ft-${ACCOUNT_ID}-us-east-1 --force
```

!!! danger "务必清理"
    部署 On-Demand 后如不再使用，请及时删除部署。虽然 On-Demand 不收取空闲费用，删除部署是不可逆操作——如需恢复需重新创建。

## 测试结果

### 实测执行情况

我们使用 Nova Micro 进行了完整的 fine-tuning 流程测试：

| 步骤 | 操作 | 结果 | 状态 |
|------|------|------|------|
| 数据准备 | 54 条 AWS Q&A JSONL 数据 | 格式正确，成功上传 S3 | ✅ |
| IAM 配置 | BedrockFinetuningRole + S3 策略 | 权限配置正确 | ✅ |
| Job 提交 | `create-model-customization-job` | 成功创建，返回 Job ARN | ✅ |
| Validation | 自动校验训练数据格式 | ~25 秒完成，通过 | ✅ |
| Training | 模型训练 | **6+ 小时 NotStarted，未启动** | ❌ |
| On-Demand 部署 | 需要 custom model | 因训练未完成，无法测试 | ⏸️ |

### 训练阶段详细记录

```
Job 提交时间: 2026-03-26 16:19 UTC
Validation 完成: 2026-03-26 16:19 UTC (~25s)
Training 状态: NotStarted
等待时间: 6+ 小时
最终操作: 手动 StopModelCustomizationJob
```

## 踩坑记录

!!! warning "Fine-tuning Training 阶段无限等待（第二次遇到）"
    **现象**：Validation 阶段正常完成（~25s），但 Training 阶段 `trainingDetails.status` 一直停在 `NotStarted`，等待 6+ 小时无任何进展。

    **背景**：这是同一账号第二次遇到此问题。Task #44 (Bedrock Model Distillation) 也出现完全相同的症状——Validation 通过后 Training 卡住不动。

    **可能原因**：

    1. **Region 训练资源紧张** — us-east-1 是最热门的 Region，训练 GPU 资源可能排队严重
    2. **账号级配额限制** — 测试账号可能有未明确文档化的训练并发/优先级限制
    3. **训练容量规划** — Bedrock fine-tuning 后端可能基于需求动态分配训练资源

    **建议**：

    - 尝试 us-west-2（第二个支持 Region）
    - 提交 Service Quota increase request
    - 联系 AWS Support 确认账号级训练资源配额
    - 考虑在非高峰时段提交训练任务

    **状态**：已查文档确认——官方文档未提及训练等待时间 SLA 或资源排队机制，属于未文档化的运营限制。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 存储（训练数据） | $0.023/GB·月 | 65 KiB | ~$0.00 |
| Fine-tuning Job（未完成） | 按训练 token 计 | 未消耗 | $0.00 |
| On-Demand 推理（未测试） | 与 base 相同 | 未消耗 | $0.00 |
| **合计** | | | **$0.00** |

由于训练未实际启动，本次测试几乎零成本。

## 结论与建议

### 功能评价

On-Demand Deployment 是 Bedrock 自定义模型推理方式的重要补充：

1. **降低使用门槛**：不再需要为 fine-tuned 模型预置 Provisioned Throughput，适合低频和开发场景
2. **定价友好**：与 base 模型 on-demand 价格相同，真正的按需付费
3. **API 设计清晰**：`create-custom-model-deployment` → 获取 Deployment ARN → 作为 `modelId` 推理，流程简洁
4. **支持范围合理**：覆盖 Nova 全系列（Micro/Lite/Pro）+ Meta Llama 3.3 70B

### 实测受限说明

本次测试 fine-tuning 训练阶段未能启动（6+ 小时 NotStarted），这是同一账号第二次遇到此问题（首次为 Task #44 Distillation）。从功能角度看，On-Demand Deployment 的 API 设计和文档都很完善，但实际使用受限于训练资源分配。

### 生产环境使用建议

| 场景 | 推荐方式 | 理由 |
|------|---------|------|
| 开发/测试自定义模型 | On-Demand | 灵活，不闲置资源 |
| 生产 + 稳定 QPS | Provisioned Throughput | 可预测延迟和成本 |
| 生产 + 突发流量 | On-Demand | 自动弹性，按需付费 |
| A/B 测试多个模型版本 | On-Demand | 低成本并行部署多版本 |
| 批量推理 | Provisioned Throughput | 大量 token 时固定成本更优 |

## 参考链接

- [AWS What's New: On-demand deployment for custom Amazon Nova models in Bedrock](https://aws.amazon.com/about-aws/whats-new/2025/07/on-demand-deployment-custom-amazon-nova-models-bedrock/)
- [官方文档: Deploy a custom model for on-demand inference](https://docs.aws.amazon.com/bedrock/latest/userguide/deploy-custom-model-on-demand.html)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [Amazon Bedrock Model Customization](https://docs.aws.amazon.com/bedrock/latest/userguide/custom-models.html)
- [同系列踩坑: Bedrock Model Distillation 实测](https://chaosreload.github.io/aws-hands-on-lab/ai-ml/bedrock-model-distillation/)
