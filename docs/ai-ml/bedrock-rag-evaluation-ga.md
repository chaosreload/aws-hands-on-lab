# Amazon Bedrock RAG Evaluation 实测：用 LLM-as-a-Judge 自动评估你的 RAG 应用

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45-60 分钟
    - **预估费用**: $5-15（主要是 OpenSearch Serverless OCU 费用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

构建了 RAG 应用之后，如何系统性地评估它的质量？手动逐条审查不现实，写评估脚本又需要大量工程投入。

2025 年 3 月 20 日，Amazon Bedrock RAG Evaluation 正式 GA。它提供了一个**开箱即用的自动化评估框架**，使用 LLM-as-a-Judge 方法评估 RAG 管道的检索和生成质量。你可以评估基于 Bedrock Knowledge Bases 的 RAG 应用，也可以评估自定义 RAG 系统。

核心价值：

- **无需自建评估框架** — 选择指标、指定 judge 模型、提交数据集，就能获得量化评分
- **多维度评估** — 检索质量（relevance/coverage）+ 生成质量（correctness/faithfulness/completeness）
- **Judge 模型可选** — Nova Pro、Claude 3.5 Haiku 等，不同模型评判标准不同
- **支持跨评估对比** — 调整 chunking 策略、reranker、生成模型后，对比评估结果迭代优化

## 前置条件

- AWS 账号（需要 Bedrock、OpenSearch Serverless、S3、IAM 权限）
- AWS CLI v2 已配置
- Python 3 + `opensearch-py` 库（用于创建 AOSS 向量索引）
- 已安装 `boto3`

## 核心概念

### 两种评估模式

| 模式 | 评估什么 | 适用指标 | 需要生成模型？ |
|------|---------|---------|-------------|
| **Retrieve-only** | 仅检索质量 | ContextRelevance, ContextCoverage | 否 |
| **Retrieve-and-Generate** | 检索 + 生成端到端质量 | Correctness, Completeness, Faithfulness, Harmfulness | 是 |

### 关键指标说明

| 指标 | 含义 | 分值范围 | 方向 |
|------|------|---------|------|
| **ContextRelevance** | 检索到的文档与问题相关程度 | 0-1 | 越高越好 |
| **ContextCoverage** | 检索结果覆盖参考答案的程度 | 0-1 | 越高越好 |
| **Correctness** | 生成答案与参考答案的一致性 | 0-1 | 越高越好 |
| **Completeness** | 生成答案覆盖参考答案要点的程度 | 0-1 | 越高越好 |
| **Faithfulness** | 生成答案是否忠实于检索到的上下文（幻觉检测） | 0-1 | 越高越好 |
| **Harmfulness** | 生成内容是否有害 | 0-1 | 越低越好 |

### 评估工作流

```
评估数据集 (JSONL) → 选择评估模式 → 选择指标 → 选择 Judge 模型 → 创建评估 Job → 获取评分结果
```

## 动手实践

### Step 1: 创建 S3 Bucket 和准备数据

```bash
# 创建 S3 bucket
aws s3 mb s3://bedrock-rag-eval-lab-$(date +%Y%m%d) --region us-east-1

# 上传 KB 源文档（示例：3 个关于 AWS Bedrock 的文档）
cat > /tmp/kb-doc-bedrock.txt << 'EOF'
Amazon Bedrock is a fully managed service that makes high-performing
foundation models (FMs) from leading AI companies available through a
unified API. Key features include: Model Choice, Knowledge Bases for RAG,
Agents, Guardrails, Model Evaluation, Fine-tuning, and Provisioned Throughput.
EOF

cat > /tmp/kb-doc-evaluations.txt << 'EOF'
Amazon Bedrock Evaluations supports RAG evaluation with two modes:
retrieve-only (context relevance, coverage) and retrieve-and-generate
(correctness, completeness, faithfulness, harmfulness). Uses LLM-as-a-judge
with models like Nova Pro, Claude 3.5 Haiku for automated assessment.
EOF

cat > /tmp/kb-doc-knowledge-bases.txt << 'EOF'
Amazon Bedrock Knowledge Bases supports data sources including S3,
web crawlers, Confluence. Chunking strategies: fixed-size, default,
hierarchical, semantic. Vector stores: OpenSearch Serverless, Aurora,
Pinecone, Redis Enterprise Cloud, MongoDB Atlas.
EOF

aws s3 cp /tmp/kb-doc-bedrock.txt s3://YOUR_BUCKET/kb-docs/ --region us-east-1
aws s3 cp /tmp/kb-doc-evaluations.txt s3://YOUR_BUCKET/kb-docs/ --region us-east-1
aws s3 cp /tmp/kb-doc-knowledge-bases.txt s3://YOUR_BUCKET/kb-docs/ --region us-east-1
```

### Step 2: 准备评估数据集

评估数据集是 JSONL 格式，每行包含一个问答对：

```json
{
  "conversationTurns": [{
    "prompt": {"content": [{"text": "你的问题"}]},
    "referenceResponses": [{"content": [{"text": "参考答案（ground truth）"}]}]
  }]
}
```

```bash
cat > /tmp/eval-dataset.jsonl << 'EVALEOF'
{"conversationTurns": [{"referenceResponses": [{"content": [{"text": "Amazon Bedrock is a fully managed service..."}]}], "prompt": {"content": [{"text": "What is Amazon Bedrock and what are its key features?"}]}}]}
{"conversationTurns": [{"referenceResponses": [{"content": [{"text": "Fixed-size, default, hierarchical, and semantic chunking."}]}], "prompt": {"content": [{"text": "What chunking strategies does Amazon Bedrock Knowledge Bases support?"}]}}]}
{"conversationTurns": [{"referenceResponses": [{"content": [{"text": "OpenSearch Serverless, Aurora, Pinecone, Redis Enterprise Cloud, MongoDB Atlas."}]}], "prompt": {"content": [{"text": "What vector databases can I use with Bedrock Knowledge Bases?"}]}}]}
{"conversationTurns": [{"referenceResponses": [{"content": [{"text": "Retrieve-only (context relevance, coverage) and retrieve-and-generate (correctness, completeness, faithfulness)."}]}], "prompt": {"content": [{"text": "What types of RAG evaluation does Amazon Bedrock support?"}]}}]}
{"conversationTurns": [{"referenceResponses": [{"content": [{"text": "Content filtering, PII detection, Guardrails, harmfulness evaluation."}]}], "prompt": {"content": [{"text": "How does Amazon Bedrock support responsible AI?"}]}}]}
EVALEOF

aws s3 cp /tmp/eval-dataset.jsonl s3://YOUR_BUCKET/eval-data/ --region us-east-1
```

### Step 3: 创建 Knowledge Base

完整步骤包括：创建 IAM Role → AOSS 安全策略 → AOSS 集合 → 向量索引 → KB → Data Source → Sync

```bash
# 3.1 创建 KB IAM Role（信任 bedrock.amazonaws.com）
cat > /tmp/kb-trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": "YOUR_ACCOUNT_ID"}}
    }]
}
EOF
aws iam create-role --role-name BedrockKBRole \
    --assume-role-policy-document file:///tmp/kb-trust-policy.json

# 3.2 附加 S3 + Bedrock + AOSS 权限（略，见 GitHub 完整版本）

# 3.3 创建 AOSS 集合（安全策略 + 集合本身，约需 3-5 分钟变为 ACTIVE）
aws opensearchserverless create-collection \
    --name rag-eval-kb --type VECTORSEARCH --region us-east-1

# 3.4 Python 创建向量索引（1024 维，匹配 Titan Embed V2）
# 3.5 创建 KB + Data Source + Sync
aws bedrock-agent create-knowledge-base --name rag-eval-test-kb \
    --role-arn arn:aws:iam::ACCOUNT:role/BedrockKBRole \
    --knowledge-base-configuration '{...}' \
    --storage-configuration '{...}' \
    --region us-east-1
```

> 完整的 Step 3 命令较长，核心要点是 AOSS 需要三种安全策略（encryption、network、data access），Data Access Policy 必须同时授权 KB Role 和你的 IAM 用户。

### Step 4: 创建 Evaluation Job Role

评估 Job 需要独立的 IAM Role，需要 S3 读写、Bedrock 模型调用、KB Retrieve 权限。

```bash
aws iam create-role --role-name BedrockEvalJobRole \
    --assume-role-policy-document file:///tmp/kb-trust-policy.json
```

!!! warning "跨区域推理模型需要额外权限"
    使用 `us.anthropic.claude-3-5-haiku` 等跨区域推理模型作为 judge 时，IAM Resource 需包含 `arn:aws:bedrock:*:ACCOUNT:inference-profile/*`。

### Step 5: 运行 Retrieve-only 评估

```bash
cat > /tmp/eval-retrieve-only.json << 'EOF'
{
    "jobName": "rag-eval-retrieve-nova-pro",
    "jobDescription": "Retrieve-only evaluation with Nova Pro judge",
    "roleArn": "arn:aws:iam::ACCOUNT:role/BedrockEvalJobRole",
    "applicationType": "RagEvaluation",
    "evaluationConfig": {
        "automated": {
            "datasetMetricConfigs": [{
                "taskType": "QuestionAndAnswer",
                "dataset": {
                    "name": "eval-5q",
                    "datasetLocation": {"s3Uri": "s3://YOUR_BUCKET/eval-data/eval-dataset.jsonl"}
                },
                "metricNames": ["Builtin.ContextRelevance", "Builtin.ContextCoverage"]
            }],
            "evaluatorModelConfig": {
                "bedrockEvaluatorModels": [{"modelIdentifier": "amazon.nova-pro-v1:0"}]
            }
        }
    },
    "inferenceConfig": {
        "ragConfigs": [{
            "knowledgeBaseConfig": {
                "retrieveConfig": {
                    "knowledgeBaseId": "YOUR_KB_ID",
                    "knowledgeBaseRetrievalConfiguration": {
                        "vectorSearchConfiguration": {"numberOfResults": 3}
                    }
                }
            }
        }]
    },
    "outputDataConfig": {"s3Uri": "s3://YOUR_BUCKET/eval-output/retrieve-only/"}
}
EOF

aws bedrock create-evaluation-job --cli-input-json file:///tmp/eval-retrieve-only.json --region us-east-1
```

### Step 6: 运行 Retrieve-and-Generate 评估

与 Step 5 类似，关键区别：

- `inferenceConfig` 使用 `retrieveAndGenerateConfig`（需指定生成模型）
- `metricNames` 使用 `Correctness`, `Completeness`, `Faithfulness`, `Harmfulness`

### Step 7: 换 Judge 模型对比

```bash
# 修改 evaluatorModelConfig:
"modelIdentifier": "us.anthropic.claude-3-5-haiku-20241022-v1:0"
# 其余配置完全相同
```

### Step 8: 查看结果

```bash
# 检查状态（约 8-10 分钟完成）
aws bedrock list-evaluation-jobs --region us-east-1 \
    --query "jobSummaries[].{name:jobName,status:status}" --output table

# 下载结果
aws s3 cp s3://YOUR_BUCKET/eval-output/ /tmp/results/ --recursive --region us-east-1

# 解析结果（每行 JSONL 包含评分和 judge 解释）
python3 -c "
import json, sys
for line in open('/tmp/results/output.jsonl'):
    obj = json.loads(line)
    for t in obj['conversationTurns']:
        q = t['inputRecord']['prompt']['content'][0]['text'][:50]
        scores = {r['metricName'].split('.')[1]: r['result'] for r in t['results']}
        print(f'{q}: {scores}')
"
```

## 测试结果

### 实验 1：Judge 模型对比（Retrieve-only）

同一 KB、同一数据集，分别用 **Nova Pro** 和 **Claude 3.5 Haiku** 作为 judge：

| 问题 | Nova Pro CR | Haiku CR | 差异 |
|------|:---:|:---:|:---:|
| What is Amazon Bedrock? | 0.500 | 0.833 | **+0.333** |
| What chunking strategies? | 0.500 | 0.500 | 0.000 |
| What vector databases? | 0.333 | 0.500 | +0.167 |
| What RAG evaluation types? | 0.500 | 0.667 | +0.167 |
| How support responsible AI? | 0.667 | 0.667 | 0.000 |
| **平均** | **0.500** | **0.633** | **+0.133** |

**ContextCoverage** 两个模型均接近 1.0，差异主要在 **ContextRelevance**。

**关键发现**：Claude 3.5 Haiku 比 Nova Pro **宽松约 27%**。意味着：

- 换 judge 模型可能显著影响评估结果
- A/B 测试时必须保持 judge 一致
- 建议用宽松模型筛选，严格模型精评

### 实验 2：Retrieve-and-Generate 端到端评估

| 问题 | Correctness | Completeness | Faithfulness | Harmfulness |
|------|:---:|:---:|:---:|:---:|
| What is Amazon Bedrock? | 1.000 | 1.000 | 1.000 | 0.000 |
| What chunking strategies? | 1.000 | 0.750 | 1.000 | 0.000 |
| What vector databases? | 1.000 | 1.000 | 1.000 | 0.000 |
| What RAG evaluation types? | 1.000 | 0.750 | 1.000 | 0.000 |
| How support responsible AI? | 1.000 | 0.750 | 1.000 | 0.000 |
| **平均** | **1.000** | **0.850** | **1.000** | **0.000** |

- **Correctness 满分、Faithfulness 满分** — 零幻觉
- **Completeness 有差异**（0.75-1.0）— 部分回答未覆盖所有参考要点

### 实验 3：边界测试

| 测试 | 结果 |
|------|------|
| 最小数据集（1 行） | ✅ 正常工作 |
| 混用指标（ContextRelevance + R&G 模式） | ❌ 创建时报错：`metric not available for retrieveAndGenerate` |
| 自定义 RAG BYO (precomputedRagSourceConfig) | ❌ 数据集格式不明确，多种格式均报错 |

## 踩坑记录

!!! warning "跨区域推理模型的 IAM 权限"
    使用 `us.anthropic.claude-3-5-haiku-20241022-v1:0` 作为 judge 时，IAM Role 的 Resource 必须包含 `arn:aws:bedrock:*:ACCOUNT_ID:inference-profile/*`，仅有 `foundation-model/*` 不够。

!!! warning "指标与评估模式必须匹配"
    ContextRelevance/ContextCoverage **仅适用于 Retrieve-only 模式**。Correctness/Completeness/Faithfulness 仅适用于 Retrieve-and-Generate 模式。API 在创建 Job 时校验。已查文档确认。

!!! warning "自定义 RAG 管道评估 (BYO) 格式不明确"
    What's New 宣传的 BYO inference 功能（precomputedRagSourceConfig），通过 CLI 创建时多种数据集格式均报错。该功能可能需要 Console 或 SDK 配合特定格式。实测发现，官方未提供 CLI 示例。

!!! tip "评估耗时预估"
    5 行数据集约 8-10 分钟。大数据集预期线性增长。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch Serverless | $0.24/OCU/hr × 2 OCU | ~1 小时 | ~$0.48 |
| Titan Embeddings V2 | $0.00002/1K tokens | ~3K tokens | ~$0.00 |
| Nova Pro (生成+judge) | $0.80/M input + $3.20/M output | ~50K tokens | ~$0.20 |
| Claude 3.5 Haiku (judge) | $0.80/M input + $4.00/M output | ~30K tokens | ~$0.15 |
| S3 | 标准存储 | negligible | ~$0.00 |
| **合计** | | | **~$0.83** |

> AOSS 是最大成本项。Lab 完成后务必立即清理。

## 清理资源

```bash
# 1. 删除评估 Jobs
aws bedrock batch-delete-evaluation-job \
    --job-identifiers "arn:aws:bedrock:us-east-1:ACCOUNT:evaluation-job/JOB_ID" \
    --region us-east-1

# 2. 删除 Knowledge Base
aws bedrock-agent delete-knowledge-base --knowledge-base-id KB_ID --region us-east-1

# 3. 删除 AOSS 集合（停止计费的关键步骤！）
aws opensearchserverless delete-collection --id COLLECTION_ID --region us-east-1

# 4. 删除 AOSS 策略
aws opensearchserverless delete-security-policy --name rag-eval-kb-enc --type encryption --region us-east-1
aws opensearchserverless delete-security-policy --name rag-eval-kb-net --type network --region us-east-1
aws opensearchserverless delete-access-policy --name rag-eval-kb-data --type data --region us-east-1

# 5. 删除 S3 Bucket
aws s3 rb s3://YOUR_BUCKET --force --region us-east-1

# 6. 删除 IAM Roles
aws iam delete-role-policy --role-name BedrockKBRole --policy-name BedrockKBPolicy
aws iam delete-role --role-name BedrockKBRole
aws iam delete-role-policy --role-name BedrockEvalJobRole --policy-name BedrockEvalPolicy
aws iam delete-role --role-name BedrockEvalJobRole
```

!!! danger "务必清理"
    AOSS 按时间计费（$0.24/OCU/h × 2 OCU 起），不使用也会持续产生费用。

## 结论与建议

### 适合场景

- **RAG 管道迭代优化** — 修改 chunking 策略/reranker 后量化评估效果变化
- **生产前质量门禁** — 部署新版本前确保关键指标不低于基线
- **模型选型对比** — 对比不同生成模型的 Faithfulness 和 Correctness

### 使用建议

1. **Judge 模型选择很重要** — Claude Haiku 比 Nova Pro 宽松 27%，A/B 测试必须保持 judge 一致
2. **先评 Retrieve，再评 R&G** — Retrieve-only 更快更便宜，先确保检索质量
3. **评估数据集质量是关键** — 参考答案质量直接影响评分准确性
4. **控制 AOSS 成本** — 评估完立即删除 AOSS 集合

### 当前限制

- BYO 自定义 RAG 管道评估的 CLI 使用体验有待改进
- 部分指标可能返回 `None`（无法评估）
- 评估 Job 创建后不可修改

## 参考链接

- [Amazon Bedrock Evaluations 产品页](https://aws.amazon.com/bedrock/evaluations/)
- [Amazon Bedrock RAG Evaluation GA 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-rag-evaluation-generally-available/)
- [Amazon Bedrock 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)
- [CreateEvaluationJob API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateEvaluationJob.html)
