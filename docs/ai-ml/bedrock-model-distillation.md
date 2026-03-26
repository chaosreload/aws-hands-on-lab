# Amazon Bedrock Model Distillation 实测：从数据准备到踩坑全记录

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 120 分钟
    - **预估费用**: < $5（teacher 推理 + 数据合成）
    - **Region**: us-east-1（Nova）/ us-west-2（Claude/Llama）
    - **最后验证**: 2026-03-26

## 背景

Amazon Bedrock Model Distillation 于 2025 年 5 月 GA，让你用大模型（teacher）的知识训练小模型（student），使小模型在特定任务上达到接近大模型的效果，同时降低推理成本和延迟。官方宣称蒸馏模型比原模型快 500%，成本低 75%，RAG 场景准确率损失 < 2%。

本文完整记录了我们在 2026 年 3 月对 Bedrock Model Distillation 的实测过程——包括数据准备、Job 创建、Baseline 对比，以及在 Nova、Claude、Llama 三条路线上遇到的所有问题。**Training 阶段最终未能成功完成**（服务端问题），但过程中积累的踩坑发现对其他用户仍有参考价值。

## 前置条件

- AWS 账号（需要 Bedrock、S3、IAM 权限）
- AWS CLI v2 已配置
- 对 Bedrock Model Customization 有基本了解

## 核心概念

### 蒸馏流程

```
选择 Teacher/Student 模型 → 准备训练数据(JSONL) → 上传到 S3
→ 创建 IAM Role → 创建 Distillation Job → 等待训练完成
→ 购买 Provisioned Throughput → 推理验证
```

### 支持的模型组合

| Provider | Teacher | Student | Region |
|----------|---------|---------|--------|
| Amazon | Nova Premier | Nova Pro, Lite, Micro | us-east-1 |
| Amazon | Nova Pro | Nova Lite, Micro | us-east-1 |
| Anthropic | Claude 3.5 Sonnet v1/v2 | Claude 3 Haiku | us-west-2 |
| Meta | Llama 3.1 405B | Llama 3.1 8B/70B, 3.2 1B, 3.3 70B | us-west-2 |
| Meta | Llama 3.1 70B | Llama 3.1 8B, 3.2 1B/3B | us-west-2 |
| Meta | Llama 3.3 70B | Llama 3.1 8B, 3.2 1B/3B | us-west-2 |

### 三种数据准备方式

1. **自己提供 prompts**（Bedrock 自动调用 teacher 生成 responses）
2. **提供 prompt-response pairs**（labeled golden examples）
3. **使用 CloudWatch invocation logs** 中已有的 teacher responses

### 关键限制

- Nova 模型仅 us-east-1，蒸馏后**不能跨 Region 复制**
- Claude/Llama 模型仅 us-west-2，可复制到其他 Region
- 推理**必须购买 Provisioned Throughput**，不支持 on-demand
- 数据合成后最大 15K prompt-response pairs
- Nova 支持多轮对话，Anthropic/Meta 仅单轮

## 动手实践

### Step 1: 准备 IAM Role

创建 Bedrock Model Customization 所需的 Service Role：

```bash
# 创建信任策略
cat > /tmp/bedrock-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "bedrock.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# 创建 IAM Role
aws iam create-role \
  --role-name BedrockDistillationRole \
  --assume-role-policy-document file:///tmp/bedrock-trust-policy.json \
  --profile weichaol-testenv2-awswhatsnewtest

# 附加 S3 和 Bedrock 权限
aws iam attach-role-policy \
  --role-name BedrockDistillationRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess \
  --profile weichaol-testenv2-awswhatsnewtest

aws iam attach-role-policy \
  --role-name BedrockDistillationRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess \
  --profile weichaol-testenv2-awswhatsnewtest
```

### Step 2: 准备训练数据

训练数据使用 JSONL 格式，每行一个 JSON 对象。这是 RAG 问答场景的示例：

```json
{
  "schemaVersion": "bedrock-conversation-2024",
  "system": [{"text": "You are a helpful AWS Solutions Architect. Answer questions accurately and concisely based on the provided context."}],
  "messages": [
    {
      "role": "user",
      "content": [{"text": "<context>Amazon S3 provides 99.999999999% (11 nines) of data durability...</context>\n\n<question>What is the durability guarantee of Amazon S3?</question>"}]
    }
  ]
}
```

!!! warning "schemaVersion 拼写注意"
    官方文档中存在拼写不一致：格式说明处写的是 `bedrock-conversion-2024`（少了 "sa"），但示例代码中用的是 `bedrock-conversation-2024`（正确值）。**请使用 `bedrock-conversation-2024`**。

我们准备了 110 条覆盖 22 个 AWS 服务的 RAG 问答 prompts，上传到 S3：

```bash
# 创建 S3 bucket
aws s3 mb s3://bedrock-distillation-test-595842667825 \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest

# 上传训练数据
aws s3 cp distillation-training-110.jsonl \
  s3://bedrock-distillation-test-595842667825/training/distillation-training-110.jsonl \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest
```

### Step 3: 创建 Distillation Job

```bash
aws bedrock create-model-customization-job \
  --job-name bedrock-distill-nova-pro-to-lite \
  --custom-model-name distilled-nova-lite-rag \
  --role-arn arn:aws:iam::595842667825:role/BedrockDistillationRole \
  --base-model-identifier amazon.nova-lite-v1:0:300k \
  --training-data-config '{"s3Uri":"s3://bedrock-distillation-test-595842667825/training/distillation-training-110.jsonl"}' \
  --output-data-config '{"s3Uri":"s3://bedrock-distillation-test-595842667825/output/"}' \
  --customization-config '{"distillationConfig":{"teacherModelConfig":{"teacherModelIdentifier":"amazon.nova-pro-v1:0","maxResponseLengthForInference":2048}}}' \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest
```

!!! warning "API 参数结构"
    CLI 参数是 `--customization-config`（不是 `--distillation-config`），distillation 配置嵌套在 `customizationConfig.distillationConfig` 下。这一点在官方文档中不够直观，容易搞错。

### Step 4: 监控 Job 进度

```bash
aws bedrock get-model-customization-job \
  --job-identifier <job-arn> \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest \
  --query '{status:status,stages:trainingMetrics}'
```

Distillation Job 包含以下阶段：

| 阶段 | 说明 | 我们的实测耗时 |
|------|------|---------------|
| Validation | 校验训练数据格式 | ~1 分钟 |
| DataProcessing | Teacher 推理 + 数据合成增强 | ~50 分钟（110 prompts） |
| Training | 训练 Student 模型 | ⚠️ 未完成（见下文） |

## Baseline 对比：Nova Pro vs Nova Lite

在开始蒸馏之前，我们先测试了 teacher（Nova Pro）和 student（Nova Lite）在 RAG 问答上的基线表现。

### 测试 1: 简单 RAG（S3 durability / storage classes）

| 指标 | Nova Pro（Teacher） | Nova Lite（Student） | 差异 |
|------|---------------------|----------------------|------|
| Bedrock latencyMs | 1,132 ms | 578 ms | Lite 快 49% |
| E2E 延迟 | 2,381 ms | 1,837 ms | Lite 快 23% |
| Input tokens | 160 | 160 | 相同 |
| Output tokens | 117 | 121 | 接近 |
| 回答质量 | 准确，结构清晰 | 准确，结构清晰 | 相当 |

### 测试 2: 复杂 RAG（Bedrock Distillation tradeoffs）

| 指标 | Nova Pro（Teacher） | Nova Lite（Student） | 差异 |
|------|---------------------|----------------------|------|
| Bedrock latencyMs | 2,937 ms | 2,269 ms | Lite 快 23% |
| Input tokens | 223 | 223 | 相同 |
| Output tokens | 224 | 370 | Lite 多 65% |
| 回答质量 | 简洁精确，结构清晰 | 详细但冗长，有重复 | Pro 更好 |

**Baseline 发现**：Nova Lite 速度更快但倾向于冗长回答。Nova Pro 更简洁高效。这正是蒸馏的理想场景——让 Lite 学到 Pro 的简洁回答风格。

## 三条路线的完整尝试记录

### 路线 1: Nova Pro → Nova Lite（us-east-1）

**结果**：⚠️ 可创建 Job，Training 阶段卡住

| 尝试 | 数据量 | Validation | DataProcessing | Training | 结果 |
|------|--------|-----------|----------------|----------|------|
| v1（20 prompts） | 20 条 | ✅ 通过 | ❌ 失败 | - | 需至少 100 prompts |
| 边界测试（3 prompts） | 3 条 | ✅ 通过 | ❌ 失败 | - | 需至少 100 prompts |
| v2（110 prompts） | 110 条 | ✅ 通过 | ✅ ~50min | ⚠️ 运行 11h 无产物 | 手动 Stop |
| v3（110 prompts 重试） | 110 条 | ✅ 通过 | ✅ | ⚠️ 运行 6h+ 无产物 | 手动 Stop |

**详细症状**（v2 & v3 一致）：

- `lastModifiedTime` 在 DataProcessing 完成后停止更新
- S3 output 目录仅有 `input_data_report/manifest.report.csv`，无训练中间产物
- CloudWatch 无 log group、无训练指标
- 两次独立尝试，间隔数小时，表现完全一致

### 路线 2: Claude 3.5 Sonnet v2 → Claude 3 Haiku（us-west-2）

**结果**：❌ 无法创建 Job

```
Access denied. This Model is marked by provider as Legacy and you have not
been actively using the model in the last 15 days. Please upgrade to an
active model on Amazon Bedrock
```

Bedrock Distillation 支持的 Claude 模型（3.5 Sonnet v1/v2 和 3 Haiku）目前均为 **Legacy** 状态。新账号或近期未使用这些模型的账号无法直接创建 distillation job。

更糟糕的是，`create-model-customization-job` API 返回的是 `InternalServerException`（HTTP 500），而不是上面那条有意义的错误信息——那条信息是我们通过 Console 的 Model Access 页面才看到的。

### 路线 3: Llama 3.1 70B → Llama 3.1 8B（us-west-2）

**结果**：❌ API 返回 500

前置验证：

```bash
# 确认模型可正常调用
aws bedrock-runtime invoke-model \
  --model-id meta.llama3-1-70b-instruct-v1:0 \
  --body '{"prompt":"<|begin_of_text|>Hello","max_gen_len":10}' \
  --region us-west-2 \
  --profile weichaol-testenv2-awswhatsnewtest \
  output.json
# ✅ 成功
```

但创建 distillation job 时：

```
An error occurred (InternalServerException) when calling the
CreateModelCustomizationJob operation
```

连续 3 次重试均失败。额外尝试 Llama 3.3 70B 也是同样的 500 错误。us-west-2 的 `CreateModelCustomizationJob` API 疑似全面故障。

## 踩坑记录

!!! warning "踩坑 1：最少 100 prompts 是硬性要求"
    官方文档措辞是 "we highly recommend... at least 100 prompt-response pairs"，听起来像是建议而非强制。但实测中，20 条和 3 条 prompts 的 Job 都在 **DataProcessing 阶段报错失败**，错误信息明确写着 `Input data must have at minimum 100 valid prompts`。**这是硬性最低要求，不是建议。** （实测发现，官方文档措辞具误导性）

!!! warning "踩坑 2：API 参数嵌套结构"
    CLI 的参数名是 `--customization-config`，不是 `--distillation-config`。distillation 配置需要嵌套在里面：`{"distillationConfig":{"teacherModelConfig":{...}}}`。第一次用很容易搞错。（实测发现，官方文档未充分说明 CLI 用法）

!!! warning "踩坑 3：schemaVersion 文档拼写错误"
    官方文档的格式说明部分写的是 `bedrock-conversion-2024`，但所有示例代码用的是 `bedrock-conversation-2024`。正确值是后者（conversation，含 "sa"）。（已查文档确认，文档内部不一致）

!!! warning "踩坑 4：Legacy 模型 API 返回 500"
    对 Legacy 状态的模型调用 `CreateModelCustomizationJob`，API 返回 `InternalServerException`（500），没有任何有用的错误信息指向 Legacy 访问限制。期望行为应是返回 `ValidationException` 并明确说明模型已 Legacy 且缺少访问权限。（实测发现，属于 API UX 问题）

!!! warning "踩坑 5：数据验证报告会"骗人""
    Job 创建后生成的 `input_data_report` 显示所有 prompts 都 accepted（20/20、3/3），**但 Job 仍然在 DataProcessing 阶段因数量不足而失败**。验证报告只校验格式，不校验数量。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| Nova Pro Teacher 推理（数据合成） | 110 prompts × ~500 tokens | < $0.10 |
| DataProcessing（数据增强） | 合成扩展训练集 | < $1.00 |
| Training（未完成） | 两次 Job 均手动 Stop | < $2.00（估） |
| S3 存储 | 训练数据 + 输出 | < $0.01 |
| **合计** | | **< $5.00** |

!!! note "省钱提示"
    因为 Training 未完成，我们没有产生 Provisioned Throughput 费用（这通常是最大的成本项）。如果 Training 成功，推理验证阶段的 Provisioned Throughput 按小时计费，最少 1 小时承诺。

## 清理资源

```bash
# 1. 停止运行中的 customization jobs
aws bedrock stop-model-customization-job \
  --job-identifier <job-arn> \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest

# 2. 删除 S3 buckets
aws s3 rb s3://bedrock-distillation-test-595842667825 --force \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest

aws s3 rb s3://bedrock-distill-claude-595842667825 --force \
  --region us-west-2 \
  --profile weichaol-testenv2-awswhatsnewtest

# 3. 删除 IAM Role（先 detach 策略）
aws iam detach-role-policy \
  --role-name BedrockDistillationRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess \
  --profile weichaol-testenv2-awswhatsnewtest

aws iam detach-role-policy \
  --role-name BedrockDistillationRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess \
  --profile weichaol-testenv2-awswhatsnewtest

aws iam delete-role \
  --role-name BedrockDistillationRole \
  --profile weichaol-testenv2-awswhatsnewtest

# 4. 检查是否有残留 custom models
aws bedrock list-custom-models \
  --region us-east-1 \
  --profile weichaol-testenv2-awswhatsnewtest
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。特别注意：如果 Training 成功产生了 custom model，且你购买了 Provisioned Throughput，记得一并删除。

## 当前服务状态总结（2026-03-26）

| 路线 | Region | 能否创建 Job | Training 状态 | 问题 |
|------|--------|-------------|--------------|------|
| Nova Pro → Lite | us-east-1 | ✅ | ⚠️ 卡住（两次尝试均无产物） | 疑似训练资源排队或后端 bug |
| Claude 3.5v2 → Haiku | us-west-2 | ❌ 500 | N/A | Legacy 模型 + API 返回 500 |
| Llama 70B → 8B | us-west-2 | ❌ 500 | N/A | API 全面 500 |
| Llama 3.3 70B → 8B | us-west-2 | ❌ 500 | N/A | 同上 |

## 结论与建议

### 这个功能适合什么场景？

Bedrock Model Distillation 的设计理念很好：用大模型的知识训练小模型，在特定任务上达到接近效果的同时降低成本和延迟。特别适合：

- **RAG 问答**：让小模型学到大模型的回答风格和准确性
- **Agent function calling**：蒸馏工具调用能力到小模型
- **高 QPS 低延迟场景**：用小模型服务，保持大模型质量

### 当前建议

1. **如果你计划使用 Distillation**：建议先通过 AWS Support 确认目标 Region 的服务状态，尤其是 us-west-2
2. **训练数据准备**：直接准备 100+ 条高质量 prompts，不要指望少于 100 条能工作
3. **选择 Nova 路线**（如果可行）：Nova 是 Amazon 原生模型，支持多轮对话，且 us-east-1 比 us-west-2 状态更稳定
4. **注意 schemaVersion**：使用 `bedrock-conversation-2024`（不是 `bedrock-conversion-2024`）
5. **预留充足时间**：DataProcessing 阶段（teacher 推理 + 数据合成）对 110 条 prompts 需要约 50 分钟，Training 阶段时间未知

### 诚实评价

截至 2026 年 3 月，Bedrock Model Distillation 在我们的测试中**未能成功完成 Training**。us-east-1 的 Nova 路线可以走到 Training 阶段但无法完成，us-west-2 则完全无法创建 Job。这可能是暂时的服务问题，但对于计划使用此功能的团队，建议做好：(1) 提前与 AWS Support 确认服务可用性，(2) 准备备选方案（如 Fine-tuning 或 Prompt Engineering），(3) 预留额外时间应对可能的服务端问题。

## 参考链接

- [AWS What's New: Bedrock Model Distillation GA](https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-bedrock-model-distillation-generally-available/)
- [官方文档: Model Distillation 概述](https://docs.aws.amazon.com/bedrock/latest/userguide/model-distillation.html)
- [官方文档: 数据准备](https://docs.aws.amazon.com/bedrock/latest/userguide/distillation-prepare-datasets.html)
- [官方文档: 前置条件与支持的模型](https://docs.aws.amazon.com/bedrock/latest/userguide/prequisites-model-distillation.html)
- [官方文档: 提交 Distillation Job](https://docs.aws.amazon.com/bedrock/latest/userguide/submit-model-distillation-job.html)
- [数据集验证脚本 (aws-samples)](https://github.com/aws-samples/amazon-bedrock-samples/blob/main/custom-models/model_distillation/dataset-validation/README.md)
