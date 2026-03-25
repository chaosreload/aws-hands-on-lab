# Amazon Bedrock Knowledge Bases GraphRAG 实战：用图增强 RAG 实现跨文档关联推理

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $1-2（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

传统 RAG（Retrieval-Augmented Generation）通过向量相似度检索文档片段来增强 LLM 回答。但当答案需要**跨多个文档**连接信息时，纯向量搜索往往力不从心——它擅长找到"相似"的内容，却难以理解实体之间的"关系"。

**GraphRAG** 是 Amazon Bedrock Knowledge Bases 的一项 GA 功能（2025年3月发布），它在向量搜索基础上引入了**知识图谱**：自动从文档中提取实体和关系，存储在 Amazon Neptune Analytics 图数据库中。查询时，先通过向量搜索找到相关块，再通过图遍历扩展关联实体，最终为 LLM 提供更完整的上下文。

**核心价值**：当你的知识库包含多个相互关联的文档（如组织架构 + 产品文档 + 项目报告），GraphRAG 能自动发现并利用文档间的实体关系，显著提升跨文档问答的准确性。

## 前置条件

- AWS 账号，具备 Bedrock、Neptune Analytics、S3、IAM 权限
- AWS CLI v2 已配置
- Bedrock 模型访问权限：Amazon Titan Embed Text v2、Anthropic Claude 3 Haiku

## 核心概念

### GraphRAG vs 传统 RAG

| 维度 | 传统向量 RAG | GraphRAG |
|------|-------------|----------|
| 检索方式 | 向量相似度搜索 | 向量搜索 + 图遍历 |
| 跨文档能力 | 弱（依赖 chunk 相似度） | 强（通过实体关系连接） |
| 存储后端 | OpenSearch / Pinecone 等 | Neptune Analytics |
| 图构建 | 无 | 自动（Claude 3 Haiku 提取实体） |
| 额外费用 | 无 | 无额外费用（仅收底层服务费） |
| 配置复杂度 | 低 | 中（需创建 Neptune graph） |

### 工作流程

```
文档 → S3 → Bedrock KB 同步
                ↓
        1. 分块 + 向量化（Titan Embed v2）
        2. 实体提取（Claude 3 Haiku）
        3. 构建知识图谱（Neptune Analytics）
                ↓
查询 → 向量搜索 → 图遍历扩展 → 丰富上下文 → LLM 生成回答
```

### 关键限制

- 仅支持 **S3** 作为数据源
- 图构建使用 **Claude 3 Haiku**（不可更换）— 官方文档明确指出 "Configuration options to customize the graph build are not supported"，因此无法换用 Claude 3.5/4.x Haiku 或 Nova 等更新模型
- **Embedding 模型**仅支持 4 家：Amazon Titan Embed Text v1/v2、Cohere Embed English/Multilingual、Titan Multimodal Embeddings G1、Cohere Embed v3 Multimodal。**不支持 Nova Multimodal Embeddings**
- Neptune Analytics graph **不支持自动扩缩**，需要手动通过 `update-graph --provisioned-memory` 调整容量（范围 16~24576 m-NCU）
- 每个数据源最多 **1000 文件**（可申请增加到 10000）
- 删除 KB **不会自动删除** Neptune graph（需手动删除，否则持续计费！）
- 层级分块策略下只检索子块，不替换为父块

## 动手实践

### Step 1: 准备测试数据

创建 3 个相互关联的文档，模拟企业知识库场景（公司概况、产品技术、团队项目）：

```bash
# 创建 S3 bucket
aws s3 mb s3://graphrag-test-$(date +%Y%m%d) \
  --region us-east-1

# 创建测试文档 1 — 公司概况（人物 + 组织架构）
cat > /tmp/doc1-company-overview.txt << 'EOF'
TechNova Inc. Company Overview
TechNova Inc. is a technology company founded in 2019 by Dr. Sarah Chen and Marcus Williams.
Organization Structure:
- CEO: Dr. Sarah Chen (co-founder, formerly VP of Engineering at CloudScale)
- CTO: Marcus Williams (co-founder, formerly Lead Architect at DataStream)
- VP of Engineering: James Rodriguez (joined 2021, formerly at AWS)
- VP of Product: Emily Zhang (joined 2020, formerly at Microsoft Azure)
- Head of AI Research: Dr. Priya Patel (joined 2022, formerly professor at MIT)
Product Lines:
1. NovaPlatform - Enterprise data integration platform
2. NovaInsight - AI-powered analytics dashboard
3. NovaAgent - Autonomous AI agent framework
EOF

# 创建测试文档 2 — 产品技术细节
cat > /tmp/doc2-product-details.txt << 'EOF'
TechNova Product Technical Documentation
NovaPlatform - Enterprise Data Integration
- Lead Engineer: James Rodriguez
- Team: Platform Engineering (35 engineers)
- Architecture: Microservices on Kubernetes (EKS)
NovaInsight - AI Analytics Dashboard
- Lead Engineer: Dr. Priya Patel
- Dependencies: NovaPlatform for data access, Amazon Bedrock for AI inference
NovaAgent - AI Agent Framework
- Lead Engineer: Emily Zhang with technical lead from Dr. Priya Patel
- Dependencies: NovaInsight for data analysis, Amazon Bedrock for LLM inference
Integration: NovaPlatform (data) -> NovaInsight (analysis) -> NovaAgent (action)
EOF

# 创建测试文档 3 — 团队项目（依赖关系）
cat > /tmp/doc3-team-projects.txt << 'EOF'
TechNova Projects Report - Q1 2025
Project Meridian (Platform Team, Lead: James Rodriguez):
- Migration of NovaPlatform from ECS to EKS, Budget: $500K
Project Phoenix (Agent Team, Lead: Emily Zhang, Tech Lead: Dr. Priya Patel):
- NovaAgent GA release, Budget: $600K
- Blocked by: Project Meridian infrastructure changes
Project Oracle (Agent Team):
- Autonomous compliance agent, depends on Phoenix GA
- Stakeholder: Dr. Sarah Chen (CEO personally sponsors)
Cross-Team Dependencies:
1. Meridian blocks Phoenix - infrastructure stability needed
2. Dr. Priya Patel spans AI/ML and Agent teams - resource contention risk
EOF

# 上传文档
aws s3 cp /tmp/doc1-company-overview.txt s3://graphrag-test-$(date +%Y%m%d)/docs/ --region us-east-1
aws s3 cp /tmp/doc2-product-details.txt s3://graphrag-test-$(date +%Y%m%d)/docs/ --region us-east-1
aws s3 cp /tmp/doc3-team-projects.txt s3://graphrag-test-$(date +%Y%m%d)/docs/ --region us-east-1
```

### Step 2: 创建 Neptune Analytics Graph

```bash
# 创建 Neptune Analytics graph（向量维度 1024，匹配 Titan Embed v2）
aws neptune-graph create-graph \
  --graph-name graphrag-test-$(date +%Y%m%d) \
  --provisioned-memory 32 \
  --no-public-connectivity \
  --vector-search-configuration dimension=1024 \
  --no-deletion-protection \
  --region us-east-1 \
  --output json

# 记录 graph ID 和 ARN（后续步骤需要）
# 等待 graph 状态变为 AVAILABLE（约 4-5 分钟）
aws neptune-graph get-graph \
  --graph-identifier <graph-id> \
  --region us-east-1 \
  --query 'status' --output text
```

### Step 3: 创建 IAM Role

```bash
# 创建信任策略
cat > /tmp/kb-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "<YOUR_ACCOUNT_ID>"}
    }
  }]
}
EOF

# 创建权限策略（替换 bucket 名称和 graph ARN）
cat > /tmp/kb-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::<YOUR_BUCKET>",
        "arn:aws:s3:::<YOUR_BUCKET>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "neptune-graph:GetGraph",
        "neptune-graph:ReadDataViaQuery",
        "neptune-graph:WriteDataViaQuery",
        "neptune-graph:DeleteDataViaQuery",
        "neptune-graph:GetQueryStatus",
        "neptune-graph:CancelQuery"
      ],
      "Resource": "<YOUR_GRAPH_ARN>"
    }
  ]
}
EOF

aws iam create-role \
  --role-name BedrockKBGraphRAGRole \
  --assume-role-policy-document file:///tmp/kb-trust-policy.json \
  --region us-east-1

aws iam put-role-policy \
  --role-name BedrockKBGraphRAGRole \
  --policy-name BedrockKBGraphRAGPolicy \
  --policy-document file:///tmp/kb-policy.json
```

### Step 4: 创建 Knowledge Base（使用 Neptune Analytics）

```bash
cat > /tmp/create-kb.json << 'EOF'
{
  "name": "graphrag-test-kb",
  "description": "GraphRAG test knowledge base with Neptune Analytics",
  "roleArn": "<YOUR_ROLE_ARN>",
  "knowledgeBaseConfiguration": {
    "type": "VECTOR",
    "vectorKnowledgeBaseConfiguration": {
      "embeddingModelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
      "embeddingModelConfiguration": {
        "bedrockEmbeddingModelConfiguration": {
          "dimensions": 1024
        }
      }
    }
  },
  "storageConfiguration": {
    "type": "NEPTUNE_ANALYTICS",
    "neptuneAnalyticsConfiguration": {
      "graphArn": "<YOUR_GRAPH_ARN>",
      "fieldMapping": {
        "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
        "metadataField": "AMAZON_BEDROCK_METADATA"
      }
    }
  }
}
EOF

aws bedrock-agent create-knowledge-base \
  --cli-input-json file:///tmp/create-kb.json \
  --region us-east-1 --output json
```

### Step 5: 创建数据源并启用 GraphRAG

这是关键步骤——通过 `contextEnrichmentConfiguration` 启用图实体提取：

```bash
cat > /tmp/create-ds.json << 'EOF'
{
  "knowledgeBaseId": "<YOUR_KB_ID>",
  "name": "graphrag-test-s3-source",
  "dataSourceConfiguration": {
    "type": "S3",
    "s3Configuration": {
      "bucketArn": "arn:aws:s3:::<YOUR_BUCKET>",
      "inclusionPrefixes": ["docs/"]
    }
  },
  "vectorIngestionConfiguration": {
    "chunkingConfiguration": {
      "chunkingStrategy": "FIXED_SIZE",
      "fixedSizeChunkingConfiguration": {
        "maxTokens": 300,
        "overlapPercentage": 20
      }
    },
    "contextEnrichmentConfiguration": {
      "type": "BEDROCK_FOUNDATION_MODEL",
      "bedrockFoundationModelConfiguration": {
        "enrichmentStrategyConfiguration": {
          "method": "CHUNK_ENTITY_EXTRACTION"
        },
        "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
      }
    }
  }
}
EOF

aws bedrock-agent create-data-source \
  --cli-input-json file:///tmp/create-ds.json \
  --region us-east-1 --output json
```

### Step 6: 同步数据并测试

```bash
# 启动同步任务
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <YOUR_KB_ID> \
  --data-source-id <YOUR_DS_ID> \
  --region us-east-1 --output json

# 检查同步状态（约 30 秒完成）
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id <YOUR_KB_ID> \
  --data-source-id <YOUR_DS_ID> \
  --ingestion-job-id <YOUR_JOB_ID> \
  --region us-east-1 \
  --query 'ingestionJob.{status:status, stats:statistics}' \
  --output table
```

### Step 7: 测试跨文档关联推理

```bash
# 测试 1: 简单的跨文档检索
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <YOUR_KB_ID> \
  --retrieval-query '{"text": "Who is responsible for Project Phoenix and what is their background?"}' \
  --retrieval-configuration '{"vectorSearchConfiguration": {"numberOfResults": 5}}' \
  --region us-east-1 --output json

# 测试 2: 复杂的跨文档关联推理（使用 Retrieve and Generate）
cat > /tmp/rag-query.json << 'EOF'
{
  "input": {
    "text": "If Project Meridian is delayed by 2 months, what is the cascading impact on all other projects and which executives should be notified?"
  },
  "retrieveAndGenerateConfiguration": {
    "type": "KNOWLEDGE_BASE",
    "knowledgeBaseConfiguration": {
      "knowledgeBaseId": "<YOUR_KB_ID>",
      "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0",
      "retrievalConfiguration": {
        "vectorSearchConfiguration": {
          "numberOfResults": 5
        }
      }
    }
  }
}
EOF

aws bedrock-agent-runtime retrieve-and-generate \
  --cli-input-json file:///tmp/rag-query.json \
  --region us-east-1 --output json
```

## 测试结果

### 跨文档关联推理效果

| 测试查询 | 涉及文档数 | 回答质量 | 关键发现 |
|----------|-----------|---------|---------|
| "Who is responsible for Project Phoenix?" | 3 | ✅ 优秀 | 正确关联人物背景 + 技术角色 + 项目职责 |
| "Complete data flow architecture + team leads" | 2 | ✅ 优秀 | 准确描述产品数据流 + 负责人 |
| "Meridian delay cascading impact" | 3 | ✅ 优秀 | 识别 4 层级联依赖 + 正确列出需通知的 4 位高管 |

### 关键数据

| 指标 | 数值 |
|------|------|
| Neptune graph 创建时间 | ~4 分钟 |
| KB 创建时间 | ~1 分钟 |
| 数据同步时间（3 文档） | ~30 秒 |
| Retrieve API 响应时间 | <2 秒 |
| Retrieve and Generate 响应时间 | ~5 秒 |
| 检索得分范围 | 1.30 - 2.17 |

## 踩坑记录

!!! warning "Neptune Graph 删除顺序"
    **必须先删除 Knowledge Base，再删除 Neptune graph。** 反过来会导致 KB 删除失败。而且删除 KB 不会自动删除 Neptune graph，遗忘清理会持续产生费用。已查文档确认。

!!! warning "Neptune no-public-connectivity 限制"
    如果创建 graph 时选择 `--no-public-connectivity`（推荐的安全做法），则无法从外部直接查询图实体。Bedrock KB 可正常使用（通过 AWS 内部网络访问）。如需直接查询图数据做调试，需要开启 public connectivity 或通过 VPC 内的实例访问。实测发现，官方未明确记录。

!!! warning "向量维度必须匹配"
    Neptune graph 的 `dimension` 参数必须与 embedding 模型输出维度一致。Titan Embed Text v2 默认 1024 维，创建 graph 时需指定 `dimension=1024`。实测发现，官方未明确说明此要求。

!!! tip "IAM 权限配置"
    KB Role 需要同时包含 `bedrock:InvokeModel`（embedding + 图构建）、`s3:GetObject`（数据源）和 `neptune-graph:*`（图操作）三组权限。遗漏任何一组都会导致同步失败。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Neptune Analytics (32 mNCU) | $0.204/hr | ~1 hr | ~$0.20 |
| Titan Embed Text v2 | $0.00002/1K tokens | ~5K tokens | ~$0.0001 |
| Claude 3 Haiku (图构建) | $0.00025/1K input | ~10K tokens | ~$0.003 |
| Claude 3 Haiku (推理) | $0.00125/1K output | ~3K tokens | ~$0.004 |
| S3 存储 | - | <1MB | ~$0 |
| **合计** | | | **~$0.21** |

!!! note "成本提示"
    Neptune Analytics 的最低配置为 32 mNCU（$0.204/hr ≈ $4.90/天）。即使不使用也会持续计费，务必在测试完成后立即删除。

## 清理资源

⚠️ **严格按照以下顺序清理**，避免删除失败或资源残留：

```bash
# 1. 删除 Data Source
aws bedrock-agent delete-data-source \
  --knowledge-base-id <YOUR_KB_ID> \
  --data-source-id <YOUR_DS_ID> \
  --region us-east-1

# 2. 删除 Knowledge Base
aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id <YOUR_KB_ID> \
  --region us-east-1

# 3. 删除 Neptune Analytics graph（KB 删除后再执行！）
aws neptune-graph delete-graph \
  --graph-identifier <GRAPH_ID> \
  --skip-snapshot \
  --region us-east-1

# 4. 删除 S3 bucket
aws s3 rb s3://<YOUR_BUCKET> --force --region us-east-1

# 5. 删除 IAM Role
aws iam delete-role-policy \
  --role-name BedrockKBGraphRAGRole \
  --policy-name BedrockKBGraphRAGPolicy
aws iam delete-role --role-name BedrockKBGraphRAGRole
```

!!! danger "务必清理"
    Neptune Analytics graph 持续计费（$0.204/hr），Lab 完成后请立即执行清理步骤。

## 结论与建议

### GraphRAG 适合什么场景？

- ✅ **知识库文档间有强实体关联**（如企业组织架构 + 项目文档 + 产品手册）
- ✅ **需要跨文档推理**的问答场景（"谁负责什么"、"影响链分析"）
- ✅ **合规/审计类问答**（需要追踪实体关系链）
- ❌ 文档间无明显实体关联的场景（普通向量 RAG 即可）
- ❌ 对延迟极度敏感的实时场景（图遍历会增加些许延迟）

### 生产环境建议

1. **容量规划**：根据文档量和实体复杂度选择 Neptune graph 大小（最小 32 mNCU）
2. **成本控制**：Neptune Analytics 持续计费，评估是否值得长期运行
3. **安全最佳实践**：使用 `--no-public-connectivity` + VPC 内访问
4. **监控**：关注 ingestion job 的 `numberOfDocumentsFailed` 指标
5. **删除流程**：建立 SOP，确保先删 KB 再删 graph

## 参考链接

- [Amazon Bedrock Knowledge Bases GraphRAG 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html)
- [AWS What's New: GraphRAG GA](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-knowledge-bases-graphrag-generally-available/)
- [Neptune Analytics 用户指南](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/what-is-neptune-analytics.html)
- [删除数据源文档](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-ds-delete.html)
