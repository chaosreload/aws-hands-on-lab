# Amazon Bedrock RAG Evaluation 实战：用 LLM 评估你的 RAG 系统质量

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $5-8（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

RAG（Retrieval-Augmented Generation）是当前企业 GenAI 落地最常见的模式，但一直缺少标准化的评估手段。你的 RAG 系统检索到的内容真的相关吗？生成的回答是否忠实于检索内容？有没有产生幻觉？

Amazon Bedrock RAG Evaluation（2025 年 3 月 GA）提供了一套基于 **LLM-as-a-judge** 的自动化评估框架，支持评估 Bedrock Knowledge Bases 和自定义 RAG pipeline。本文实测了 4 组评估场景，重点对比了不同 judge 模型的评分差异。

## 前置条件

- AWS 账号（需要 Bedrock、S3、OpenSearch Serverless、IAM 权限）
- AWS CLI v2 已配置
- 已开通 Amazon Nova Pro 和 Claude 3.5 Haiku 模型访问权限

## 核心概念

### 两种评估模式

| 模式 | 评估范围 | 可用指标 | 适用场景 |
|------|---------|---------|---------|
| **Retrieve-only** | 仅检索质量 | ContextRelevance, ContextCoverage | 优化 chunking/embedding 策略 |
| **Retrieve-and-Generate** | 检索 + 生成质量 | Correctness, Completeness, Faithfulness, Harmfulness | 端到端 RAG 质量评估 |

### 指标含义

- **ContextRelevance**: 检索到的文档片段中，有多少与问题相关（0-1）
- **ContextCoverage**: 参考答案中的信息点，有多少被检索到的上下文覆盖（0-1）
- **Correctness**: 生成的回答与参考答案的一致程度（0-1）
- **Completeness**: 回答是否涵盖了参考答案的所有关键信息点（0-1）
- **Faithfulness**: 回答是否忠实于检索到的上下文（幻觉检测）（0-1）
- **Harmfulness**: 回答是否包含有害内容（0=安全，1=有害）

### GA 新增能力

- 支持自定义 RAG pipeline 评估（Bring Your Own Inference Responses）
- Citation Precision / Citation Coverage 新指标
- 集成 Bedrock Guardrails
- 跨评估 job 对比

## 动手实践

### Step 1: 创建 S3 Bucket 和测试数据

```bash
# 创建存储桶
aws s3 mb s3://bedrock-rag-eval-test-$(date +%Y%m%d) \
  --region us-east-1

# 准备 KB 数据源文档
mkdir -p /tmp/rag-eval/kb-docs
cat > /tmp/rag-eval/kb-docs/amazon-bedrock-overview.txt << 'EOF'
Amazon Bedrock is a fully managed service that makes high-performing
foundation models (FMs) from leading AI companies available through
a unified API. Key features include Model Choice, Knowledge Bases,
Agents, Guardrails, Model Evaluation, Fine-tuning, and Provisioned
Throughput.
EOF

# 上传到 S3
aws s3 sync /tmp/rag-eval/kb-docs/ \
  s3://bedrock-rag-eval-test-$(date +%Y%m%d)/kb-docs/ \
  --region us-east-1
```

### Step 2: 创建评估数据集

评估数据集是 JSONL 格式，每行包含一个问题和参考答案：

```json
{
  "conversationTurns": [{
    "prompt": {
      "content": [{"text": "What is Amazon Bedrock?"}]
    },
    "referenceResponses": [{
      "content": [{"text": "Amazon Bedrock is a fully managed service..."}]
    }]
  }]
}
```

```bash
# 创建并上传评估数据集
aws s3 cp /tmp/rag-eval/eval-dataset.jsonl \
  s3://bedrock-rag-eval-test-$(date +%Y%m%d)/eval-data/ \
  --region us-east-1
```

### Step 3: 创建 Knowledge Base

需要依次创建：IAM Role → OpenSearch Serverless Collection → 向量索引 → Knowledge Base → Data Source → 数据同步。

```bash
# 创建 Knowledge Base（需要先完成 AOSS 和 IAM 设置）
aws bedrock-agent create-knowledge-base \
  --name rag-eval-test-kb \
  --role-arn arn:aws:iam::<ACCOUNT>:role/BedrockKBRole \
  --knowledge-base-configuration '{"type":"VECTOR","vectorKnowledgeBaseConfiguration":{"embeddingModelArn":"arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"}}' \
  --storage-configuration '{"type":"OPENSEARCH_SERVERLESS","opensearchServerlessConfiguration":{"collectionArn":"<COLLECTION_ARN>","vectorIndexName":"bedrock-kb-index","fieldMapping":{"vectorField":"bedrock-knowledge-base-default-vector","textField":"AMAZON_BEDROCK_TEXT_CHUNK","metadataField":"AMAZON_BEDROCK_METADATA"}}}' \
  --region us-east-1

# 创建数据源并同步
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KB_ID> \
  --data-source-id <DS_ID> \
  --region us-east-1
```

### Step 4: 运行 Retrieve-only 评估

```bash
aws bedrock create-evaluation-job \
  --job-name rag-eval-retrieve-only \
  --role-arn arn:aws:iam::<ACCOUNT>:role/BedrockEvalJobRole \
  --application-type RagEvaluation \
  --evaluation-config '{"automated":{"datasetMetricConfigs":[{"taskType":"QuestionAndAnswer","dataset":{"name":"eval-dataset","datasetLocation":{"s3Uri":"s3://bedrock-rag-eval-test-<DATE>/eval-data/eval-dataset.jsonl"}},"metricNames":["Builtin.ContextRelevance","Builtin.ContextCoverage"]}],"evaluatorModelConfig":{"bedrockEvaluatorModels":[{"modelIdentifier":"amazon.nova-pro-v1:0"}]}}}' \
  --inference-config '{"ragConfigs":[{"knowledgeBaseConfig":{"retrieveConfig":{"knowledgeBaseId":"<KB_ID>","knowledgeBaseRetrievalConfiguration":{"vectorSearchConfiguration":{"numberOfResults":3}}}}}]}' \
  --output-data-config '{"s3Uri":"s3://bedrock-rag-eval-test-<DATE>/eval-output/retrieve-only/"}' \
  --region us-east-1
```

### Step 5: 运行 Retrieve-and-Generate 评估

```bash
# 主要区别：使用 retrieveAndGenerateConfig + 生成质量指标
aws bedrock create-evaluation-job \
  --job-name rag-eval-retrieve-generate \
  --role-arn arn:aws:iam::<ACCOUNT>:role/BedrockEvalJobRole \
  --application-type RagEvaluation \
  --evaluation-config '{"automated":{"datasetMetricConfigs":[{"taskType":"QuestionAndAnswer","dataset":{"name":"eval-dataset","datasetLocation":{"s3Uri":"s3://bedrock-rag-eval-test-<DATE>/eval-data/eval-dataset.jsonl"}},"metricNames":["Builtin.Correctness","Builtin.Completeness","Builtin.Faithfulness","Builtin.Harmfulness"]}],"evaluatorModelConfig":{"bedrockEvaluatorModels":[{"modelIdentifier":"amazon.nova-pro-v1:0"}]}}}' \
  --inference-config '{"ragConfigs":[{"knowledgeBaseConfig":{"retrieveAndGenerateConfig":{"type":"KNOWLEDGE_BASE","knowledgeBaseConfiguration":{"knowledgeBaseId":"<KB_ID>","modelArn":"arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"}}}}]}' \
  --output-data-config '{"s3Uri":"s3://bedrock-rag-eval-test-<DATE>/eval-output/retrieve-generate/"}' \
  --region us-east-1
```

## 测试结果

### Retrieve-only 评估对比（Nova Pro vs Claude 3.5 Haiku 作为 judge）

| 问题 | Nova Pro ContextRelevance | Haiku ContextRelevance | Nova Pro ContextCoverage | Haiku ContextCoverage |
|------|--------------------------|----------------------|------------------------|-----------------------|
| What is Amazon Bedrock? | 0.500 | 0.833 | 1.000 | 1.000 |
| Chunking strategies? | 0.333 | 0.500 | 1.000 | 1.000 |
| Vector databases? | 0.333 | 0.500 | 1.000 | 1.000 |
| RAG eval types? | 0.500 | 0.667 | 1.000 | 1.000 |
| Responsible AI? | 0.667 | N/A | 0.750 | 0.750 |
| **平均** | **0.467** | **0.625** | **0.950** | **0.950** |

**🔑 关键发现**：同一数据集，Claude 3.5 Haiku 的 ContextRelevance 评分比 Nova Pro 高出 **34%**，而 ContextCoverage 评分完全一致。**选择 judge 模型会显著影响评估结果。**

### Retrieve-and-Generate 评估结果（Nova Pro judge + generator）

| 问题 | Correctness | Completeness | Faithfulness | Harmfulness |
|------|------------|-------------|-------------|------------|
| What is Amazon Bedrock? | 1.0 | 1.0 | 1.0 | 0.0 |
| Chunking strategies? | 1.0 | 0.75 | 1.0 | 0.0 |
| Vector databases? | 1.0 | 1.0 | N/A | 0.0 |
| RAG eval types? | 0.5 | 0.75 | 1.0 | 0.0 |
| Responsible AI? | 1.0 | 0.75 | 1.0 | 0.0 |
| **平均** | **0.900** | **0.850** | **1.000** | **0.000** |

### 边界测试

- **最小数据集**：1 条数据即可运行评估 ✅
- **评估耗时**：5 条数据约 8-9 分钟（含 job 启动时间）

## 踩坑记录

!!! warning "taskType 必须指定但被忽略"
    RAG 评估虽然不依赖 taskType，但 `Custom` 值会导致 ValidationException。必须使用 `QuestionAndAnswer` 或其他标准值。**已查文档确认：文档说 "ignored for knowledge base evaluation jobs" 但验证逻辑仍会检查。**

!!! warning "Legacy 模型需要 Cross-Region Inference Profile"
    Claude 3.5 Sonnet v2 已标记为 LEGACY，直接使用 model ID 会报 Access Denied。需要使用 inference profile ID（如 `us.anthropic.claude-3-5-haiku-20241022-v1:0`）。**已查文档确认。**

!!! warning "Custom RAG Pipeline (BYO) 数据集格式文档缺失"
    GA 功能号称支持自定义 RAG pipeline 评估（precomputedRagSourceConfig），但数据集格式文档严重缺失。测试了 6 种不同格式均返回 "prompt not in expected format" 错误，且错误信息无法指导修正。**实测发现，官方未记录期望的数据格式。**

!!! warning "Faithfulness 可能返回 None"
    当回答未引用 Knowledge Base 内容时，Faithfulness 指标会返回 None 而非数值。需要在评估结果解析时处理此边界情况。**实测发现，官方未记录。**

!!! warning "ContextRelevance 仅适用 Retrieve-only"
    在 Retrieve-and-Generate 模式下使用 ContextRelevance 指标会报 ValidationException。需要根据评估模式选择正确的指标集。**已查文档确认。**

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch Serverless | $0.24/OCU/hr × 2 OCU | ~1 hr | ~$0.48 |
| Titan Embed V2（KB 嵌入） | $0.02/1M tokens | ~5K tokens | < $0.01 |
| Nova Pro（judge + generator） | $0.80/$3.20 per 1M in/out tokens | 4 jobs | ~$2-4 |
| Claude 3.5 Haiku（judge） | $0.80/$4.00 per 1M in/out tokens | 1 job | ~$0.50 |
| S3 存储 | $0.023/GB/月 | < 1 MB | < $0.01 |
| **合计** | | | **~$5-8** |

## 清理资源

```bash
# 1. 删除 Knowledge Base
aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id <KB_ID> --region us-east-1

# 2. 删除 OpenSearch Serverless Collection
aws opensearchserverless delete-collection \
  --id <COLLECTION_ID> --region us-east-1

# 3. 删除 AOSS 安全策略
aws opensearchserverless delete-security-policy \
  --name rag-eval-test-enc --type encryption --region us-east-1
aws opensearchserverless delete-security-policy \
  --name rag-eval-test-net --type network --region us-east-1
aws opensearchserverless delete-access-policy \
  --name rag-eval-test-access --type data --region us-east-1

# 4. 清空并删除 S3 Bucket
aws s3 rb s3://bedrock-rag-eval-test-<DATE> --force --region us-east-1

# 5. 删除 IAM Roles
aws iam delete-role-policy --role-name BedrockKBRagEvalTestRole \
  --policy-name BedrockKBRagEvalPolicy
aws iam delete-role --role-name BedrockKBRagEvalTestRole
aws iam delete-role-policy --role-name BedrockEvalJobRole \
  --policy-name BedrockEvalPolicy
aws iam delete-role --role-name BedrockEvalJobRole
```

!!! danger "务必清理"
    OpenSearch Serverless 按 OCU 持续计费（$0.24/OCU/hr），即使没有流量。Lab 完成后请立即清理。

## 结论与建议

### 适用场景

1. **RAG 系统迭代优化**：对比不同 chunking 策略、embedding 模型、reranker 配置的效果
2. **生产质量监控**：定期评估 RAG 系统的 faithfulness（幻觉检测）
3. **模型选型**：比较不同生成模型在 RAG 场景下的表现

### 最佳实践

1. **固定 judge 模型**：不同 judge 模型评分差异显著（实测高达 34%），对比实验务必固定同一 judge
2. **合理选择指标**：Retrieve-only 用于调优检索层；Retrieve-and-Generate 用于端到端评估
3. **关注 Faithfulness**：这是 RAG 最关键的指标——回答是否忠实于上下文，是否产生幻觉
4. **数据集设计**：确保参考答案涵盖期望的信息点，这直接影响 Coverage 和 Completeness 的计算
5. **成本控制**：评估成本主要来自 judge 模型调用，大规模评估考虑使用较便宜的 judge（如 Nova Pro 或 Haiku）

### 当前限制

- Custom RAG Pipeline (BYO) 功能虽已 GA，但数据集格式文档缺失，实际使用有障碍
- Faithfulness 在特定条件下返回 None，需要额外处理
- 评估结果目前只能通过 S3 下载 JSONL 分析，缺少内置的可视化对比工具

## 参考链接

- [Amazon Bedrock RAG Evaluation 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/evaluation-kb.html)
- [Amazon Bedrock Evaluations 产品页](https://aws.amazon.com/bedrock/evaluations/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-rag-evaluation-generally-available/)
- [CreateEvaluationJob API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateEvaluationJob.html)
