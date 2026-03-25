# Amazon Bedrock Knowledge Bases 多模态检索实战：图片、音频、视频的统一 RAG 工作流

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $1-2（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

2025 年 11 月，AWS 宣布 Amazon Bedrock Knowledge Bases 正式支持**多模态检索**（GA）。这意味着你可以在一个统一的 RAG 工作流中，同时处理文本、图片、音频和视频——用一句话提问，系统能从所有类型的数据中找到相关内容。

之前 Knowledge Bases 只能处理文本文档和图片。现在，企业可以把会议录音、培训视频、产品图册等多媒体数据全部纳入 RAG 系统，真正实现"一个入口，所有数据"。

## 前置条件

- AWS 账号（需要 Bedrock、OpenSearch Serverless、S3、IAM 权限）
- AWS CLI v2 已配置
- 已启用 Amazon Bedrock 中的 Nova 系列模型和 Titan Embeddings 模型访问权限
- Python 3.x + Pillow 库（用于生成测试图片）

## 核心概念

Bedrock Knowledge Bases 提供**两种多模态处理方式**，适用于不同场景：

| 特性 | Nova Multimodal Embeddings | Bedrock Data Automation (BDA) |
|------|--------------------------|-------------------------------|
| **处理方式** | 原生多模态嵌入，不转换格式 | 将多媒体转为文本后嵌入 |
| **查询类型** | 文本 + 图片查询 | 仅文本查询 |
| **适用场景** | 视觉相似度搜索、以图搜图 | 语音转录、全格式文本搜索 |
| **RAG 支持** | RetrieveAndGenerate 仅限文本 | 完整 RetrieveAndGenerate |
| **Region** | 仅 us-east-1 | 多 Region |
| **存储要求** | 必须配置 multimodal storage | 可选（不配则只处理文本） |

**关键决策点**：

- 需要以图搜图？→ 选 Nova Multimodal Embeddings
- 需要搜索会议录音、培训视频中的语音内容？→ 选 BDA
- 需要完整的 RetrieveAndGenerate（RAG 生成答案）？→ 选 BDA

## 动手实践

本实验创建两个 Knowledge Base 进行对比：

- **KB-A**：Nova Multimodal Embeddings（原生多模态嵌入）
- **KB-B**：BDA + Titan Text Embeddings V2（文本转换方式）

### Step 1: 准备环境和测试数据

#### 1.1 创建 S3 桶

```bash
# 数据源桶
aws s3 mb s3://multimodal-kb-test-${ACCOUNT_ID} --region us-east-1

# Nova multimodal 存储桶（必须独立桶，推荐）
aws s3 mb s3://multimodal-kb-storage-${ACCOUNT_ID} --region us-east-1

# BDA multimodal 存储桶
aws s3 mb s3://multimodal-kb-bda-storage-${ACCOUNT_ID} --region us-east-1
```

#### 1.2 准备测试数据

创建包含多种格式的测试文件：

```bash
# 文本文档
cat > /tmp/aws-architecture-guide.txt << 'EOF'
AWS Well-Architected Framework - Serverless Application Pattern

This guide covers the key components of a serverless architecture on AWS:

1. Amazon API Gateway: Provides RESTful API endpoints with built-in throttling and caching.
2. AWS Lambda: Executes business logic without managing servers.
3. Amazon DynamoDB: NoSQL database for low-latency data access at any scale.
4. Amazon S3: Object storage for static assets and data lake storage.
5. Amazon Bedrock: Managed service for foundation models, enabling generative AI applications.
EOF

cat > /tmp/bedrock-knowledge-bases-overview.txt << 'EOF'
Amazon Bedrock Knowledge Bases - Feature Overview

Amazon Bedrock Knowledge Bases provides fully managed RAG workflows.

Key Features:
- Automatic chunking and embedding of documents
- Multimodal retrieval: search across text, images, audio, and video
- Two processing approaches: Nova Multimodal Embeddings and BDA
- Source attribution with citations in generated responses

Use Cases:
- Enterprise knowledge management
- Customer support automation
- Product catalog search with visual similarity
EOF
```

用 Python 生成测试图片（模拟产品图和架构图）：

```python
from PIL import Image, ImageDraw, ImageFont

def create_product_image(filename, bg_color, title, subtitle):
    img = Image.new('RGB', (400, 300), 'white')
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([50, 50, 350, 250], radius=20, fill=bg_color)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    bbox = draw.textbbox((0,0), title, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((400-tw)//2, 120), title, fill='white', font=font)
    img.save(filename)

create_product_image('/tmp/product-bedrock-kb.png', '#FF9900', 'Amazon Bedrock', 'Knowledge Bases')
create_product_image('/tmp/product-lambda.png', '#232F3E', 'AWS Lambda', 'Serverless Compute')
create_product_image('/tmp/product-s3.png', '#3F8624', 'Amazon S3', 'Object Storage')
```

上传到 S3：

```bash
aws s3 cp /tmp/aws-architecture-guide.txt s3://multimodal-kb-test-${ACCOUNT_ID}/documents/ --region us-east-1
aws s3 cp /tmp/bedrock-knowledge-bases-overview.txt s3://multimodal-kb-test-${ACCOUNT_ID}/documents/ --region us-east-1
aws s3 cp /tmp/product-bedrock-kb.png s3://multimodal-kb-test-${ACCOUNT_ID}/images/ --region us-east-1
aws s3 cp /tmp/product-lambda.png s3://multimodal-kb-test-${ACCOUNT_ID}/images/ --region us-east-1
aws s3 cp /tmp/product-s3.png s3://multimodal-kb-test-${ACCOUNT_ID}/images/ --region us-east-1
```

### Step 2: 创建 IAM 角色

多模态 Knowledge Base 需要额外的权限：

```bash
# 信任策略
cat > /tmp/kb-trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"}}
    }]
}
EOF

aws iam create-role \
    --role-name BedrockKBMultimodalRole \
    --assume-role-policy-document file:///tmp/kb-trust-policy.json \
    --region us-east-1
```

关键权限策略（注意 `bedrock:*` 在生产环境应细化）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelAsync",
        "bedrock:GetAsyncInvoke",
        "bedrock:InvokeDataAutomationAsync",
        "bedrock:GetDataAutomationStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::multimodal-kb-test-${ACCOUNT_ID}",
        "arn:aws:s3:::multimodal-kb-test-${ACCOUNT_ID}/*",
        "arn:aws:s3:::multimodal-kb-storage-${ACCOUNT_ID}",
        "arn:aws:s3:::multimodal-kb-storage-${ACCOUNT_ID}/*",
        "arn:aws:s3:::multimodal-kb-bda-storage-${ACCOUNT_ID}",
        "arn:aws:s3:::multimodal-kb-bda-storage-${ACCOUNT_ID}/*"
      ]
    },
    {
      "Sid": "AOSSAccess",
      "Effect": "Allow",
      "Action": ["aoss:APIAccessAll"],
      "Resource": "*"
    }
  ]
}
```

### Step 3: 创建 OpenSearch Serverless 向量存储

```bash
# 1. 创建加密策略
aws opensearchserverless create-security-policy \
    --name multimodal-kb-encryption \
    --type encryption \
    --policy '{"Rules":[{"ResourceType":"collection","Resource":["collection/multimodal-kb-*"]}],"AWSOwnedKey":true}' \
    --region us-east-1

# 2. 创建网络策略
aws opensearchserverless create-security-policy \
    --name multimodal-kb-network \
    --type network \
    --policy '[{"Rules":[{"ResourceType":"collection","Resource":["collection/multimodal-kb-*"]},{"ResourceType":"dashboard","Resource":["collection/multimodal-kb-*"]}],"AllowFromPublic":true}]' \
    --region us-east-1

# 3. 创建数据访问策略（替换 ROLE_ARN 和 USER_ARN）
aws opensearchserverless create-access-policy \
    --name multimodal-kb-data \
    --type data \
    --policy '[{"Rules":[{"ResourceType":"collection","Resource":["collection/multimodal-kb-*"],"Permission":["aoss:*"]},{"ResourceType":"index","Resource":["index/multimodal-kb-*/*"],"Permission":["aoss:*"]}],"Principal":["<ROLE_ARN>","<USER_ARN>"]}]' \
    --region us-east-1

# 4. 创建集合
aws opensearchserverless create-collection \
    --name multimodal-kb-test \
    --type VECTORSEARCH \
    --region us-east-1
```

等待集合变为 ACTIVE（约 3-5 分钟），然后创建向量索引：

```bash
# Nova Multimodal Embeddings 使用 3072 维向量
awscurl --service aoss --region us-east-1 \
    -X PUT "${COLLECTION_ENDPOINT}/bedrock-multimodal-nova-index" \
    -H "Content-Type: application/json" \
    -d '{
        "settings": {"index": {"knn": true}},
        "mappings": {
            "properties": {
                "bedrock-knowledge-base-default-vector": {
                    "type": "knn_vector",
                    "dimension": 3072,
                    "method": {"engine": "faiss", "name": "hnsw"}
                },
                "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
                "AMAZON_BEDROCK_METADATA": {"type": "text", "index": false}
            }
        }
    }'

# BDA 使用 Titan Text Embeddings V2（1024 维）
awscurl --service aoss --region us-east-1 \
    -X PUT "${COLLECTION_ENDPOINT}/bedrock-multimodal-bda-index" \
    -H "Content-Type: application/json" \
    -d '{
        "settings": {"index": {"knn": true}},
        "mappings": {
            "properties": {
                "bedrock-knowledge-base-default-vector": {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {"engine": "faiss", "name": "hnsw"}
                },
                "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
                "AMAZON_BEDROCK_METADATA": {"type": "text", "index": false}
            }
        }
    }'
```

!!! warning "注意向量维度"
    Nova Multimodal Embeddings V1 的向量维度是 **3072**，不是常见的 1024。如果维度不匹配，`create-knowledge-base` 会返回 ValidationException。

### Step 4: 创建 Knowledge Base（BDA 方式）

```bash
# 创建 KB 配置文件
cat > /tmp/kb-bda-create.json << EOF
{
    "knowledgeBaseConfiguration": {
        "vectorKnowledgeBaseConfiguration": {
            "embeddingModelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
            "supplementalDataStorageConfiguration": {
                "storageLocations": [{
                    "type": "S3",
                    "s3Location": {"uri": "s3://multimodal-kb-bda-storage-${ACCOUNT_ID}/"}
                }]
            }
        },
        "type": "VECTOR"
    },
    "storageConfiguration": {
        "opensearchServerlessConfiguration": {
            "collectionArn": "${COLLECTION_ARN}",
            "vectorIndexName": "bedrock-multimodal-bda-index",
            "fieldMapping": {
                "vectorField": "bedrock-knowledge-base-default-vector",
                "textField": "AMAZON_BEDROCK_TEXT_CHUNK",
                "metadataField": "AMAZON_BEDROCK_METADATA"
            }
        },
        "type": "OPENSEARCH_SERVERLESS"
    },
    "name": "multimodal-kb-bda",
    "description": "Multimodal KB with BDA processing",
    "roleArn": "${ROLE_ARN}"
}
EOF

aws bedrock-agent create-knowledge-base \
    --cli-input-json file:///tmp/kb-bda-create.json \
    --region us-east-1
```

### Step 5: 添加数据源并同步（关键：BDA 解析器）

```bash
cat > /tmp/kb-bda-ds.json << EOF
{
    "knowledgeBaseId": "${KB_ID}",
    "name": "multimodal-test-data",
    "dataSourceConfiguration": {
        "type": "S3",
        "s3Configuration": {
            "bucketArn": "arn:aws:s3:::multimodal-kb-test-${ACCOUNT_ID}"
        }
    },
    "vectorIngestionConfiguration": {
        "parsingConfiguration": {
            "parsingStrategy": "BEDROCK_DATA_AUTOMATION",
            "bedrockDataAutomationConfiguration": {
                "parsingModality": "MULTIMODAL"
            }
        }
    }
}
EOF

aws bedrock-agent create-data-source \
    --cli-input-json file:///tmp/kb-bda-ds.json \
    --region us-east-1

# 启动同步
aws bedrock-agent start-ingestion-job \
    --knowledge-base-id ${KB_ID} \
    --data-source-id ${DS_ID} \
    --region us-east-1
```

!!! tip "BDA 解析器是关键"
    如果不配置 `parsingStrategy: BEDROCK_DATA_AUTOMATION`，默认解析器会跳过多模态文件。配置 `parsingModality: MULTIMODAL` 确保图片、音频、视频都会被处理。

### Step 6: 验证多模态检索

**文本查询（跨模态搜索）**：

```bash
aws bedrock-agent-runtime retrieve \
    --knowledge-base-id ${KB_ID} \
    --retrieval-query text="Amazon Bedrock Knowledge Bases product" \
    --region us-east-1
```

**RetrieveAndGenerate（多模态 RAG）**：

```bash
cat > /tmp/rag-query.json << EOF
{
    "input": {"text": "What AWS services are mentioned in the images and documents?"},
    "retrieveAndGenerateConfiguration": {
        "type": "KNOWLEDGE_BASE",
        "knowledgeBaseConfiguration": {
            "knowledgeBaseId": "${KB_ID}",
            "modelArn": "arn:aws:bedrock:us-east-1:${ACCOUNT_ID}:inference-profile/us.amazon.nova-premier-v1:0"
        }
    }
}
EOF

aws bedrock-agent-runtime retrieve-and-generate \
    --cli-input-json file:///tmp/rag-query.json \
    --region us-east-1
```

## 测试结果

### BDA 多模态检索 — 跨模态搜索效果

| 查询文本 | 第一结果 | 模态 | 相似度 |
|---------|---------|------|--------|
| "serverless architecture Lambda" | product-lambda.png | IMAGE | 0.678 |
| "Amazon Bedrock Knowledge Bases product" | product-bedrock-kb.png | IMAGE | 0.849 |
| "RAG architecture foundation model" | architecture-rag.png | IMAGE | 0.631 |
| "object storage S3" | product-s3.png | IMAGE | 0.808 |

**核心发现**：BDA 将图片内容转为文本描述后嵌入，文本查询可以精准匹配到图片内容。例如查询 "Amazon Bedrock Knowledge Bases product" 直接返回了包含 "Amazon Bedrock Knowledge Bases" 文字的产品图，相似度高达 0.849。

### Nova vs BDA 对比

| 维度 | KB-A (Nova Multimodal) | KB-B (BDA) |
|------|----------------------|------------|
| 索引结果 | 仅 2 文本（多模态索引失败） | 8/10 成功（5 图 + 1 视频 + 2 文本） |
| 查询结果数 | 2（仅文本） | 5（图片 + 文本混合） |
| 跨模态搜索 | ❌ 未生效 | ✅ 文本查询返回图片 |
| RAG 生成 | N/A | ✅ Nova Premier 成功 |
| 同步时间 | ~3 min | ~3 min |

### RetrieveAndGenerate 模型兼容性

| 模型 | 多模态 RAG 支持 | 备注 |
|------|----------------|------|
| Nova Premier | ✅ 支持 | 成功生成含多模态引用的答案 |
| Claude 3.5 Haiku | ❌ 不支持 | "doesn't support image content block" |
| Claude 3.5 Sonnet v2 | ❌ | 标记为 Legacy |

### 同步统计

| 文件类型 | BDA 结果 | 说明 |
|---------|---------|------|
| .txt (2 个) | ✅ 全部索引 | 标准文本处理 |
| .png (5 个) | ✅ 全部索引 | OCR + 视觉描述转文本 |
| .mp4 (1 个) | ✅ 索引成功 | 视频帧描述 + 音频转录 |
| .mp3 (1 个) | ❌ 失败 | 纯音调无语音，"no text content found" |
| .tiff (1 个) | ❌ 失败 | 不支持的格式（预期行为） |

## 踩坑记录

!!! warning "踩坑 1: Nova Multimodal Embeddings 向量维度是 3072"
    **现象**：使用 1024 维创建 OpenSearch 索引后，`create-knowledge-base` 返回 `ValidationException: Query vector has invalid dimension: 3072`。
    
    **原因**：Nova Multimodal Embeddings V1 (`amazon.nova-2-multimodal-embeddings-v1:0`) 输出 3072 维向量，而非常见的 1024。官方文档未明确标注此维度。
    
    **解决**：创建 OpenSearch 索引时指定 `"dimension": 3072`。

!!! warning "踩坑 2: Multimodal Storage 桶必须独立（不能用子目录）"
    **现象**：尝试在 supplementalDataStorageConfiguration 中指定 `s3://bucket/subfolder/` 路径时，返回 `ValidationException: The S3 URI contains a sub-folder which is not supported`。
    
    **解决**：为每个 KB 使用独立的 S3 桶作为 multimodal storage destination。（已查文档确认：推荐使用独立桶）

!!! warning "踩坑 3: BDA RAG 需要支持图片的模型"
    **现象**：使用 Claude 3.5 Haiku 进行 RetrieveAndGenerate 时报错 "This model doesn't support the image content block"。
    
    **原因**：BDA 处理后的内容包含图片 content block，需要支持多模态输入的模型。
    
    **解决**：使用 Nova Premier 或其他支持图片输入的模型。（已查 troubleshooting 文档确认）

!!! warning "踩坑 4: 音频必须包含语音内容"
    **现象**：纯音调 MP3 文件（无语音）被 BDA 跳过，报 "no text content found"。
    
    **原因**：BDA 通过 ASR（自动语音识别）处理音频，纯音调无法提取文本。
    
    **解决**：确保音频文件包含实际语音内容。背景音乐或纯音效无法被有效索引。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch Serverless | $0.24/OCU/hr × 2 OCU | ~1 hr | ~$0.48 |
| BDA 处理 | ~$0.02/文件 | 10 文件 | ~$0.20 |
| Nova Embeddings | ~$0.01/调用 | ~10 调用 | ~$0.10 |
| S3 存储 | $0.023/GB | <1 MB | <$0.01 |
| Bedrock API | 按 token | ~1000 tokens | ~$0.05 |
| **合计** | | | **~$1.00** |

!!! tip "成本优化"
    OpenSearch Serverless 是主要成本来源（最低 2 OCU），测试完成后务必立即删除集合。

## 清理资源

```bash
# 1. 删除 Knowledge Bases
aws bedrock-agent delete-knowledge-base --knowledge-base-id ${KB_A_ID} --region us-east-1
aws bedrock-agent delete-knowledge-base --knowledge-base-id ${KB_B_ID} --region us-east-1

# 2. 删除 OpenSearch Serverless 集合（最重要！按小时计费）
aws opensearchserverless delete-collection --id ${COLLECTION_ID} --region us-east-1

# 3. 删除 AOSS 策略
aws opensearchserverless delete-security-policy --name multimodal-kb-encryption --type encryption --region us-east-1
aws opensearchserverless delete-security-policy --name multimodal-kb-network --type network --region us-east-1
aws opensearchserverless delete-access-policy --name multimodal-kb-data --type data --region us-east-1

# 4. 删除 S3 桶
aws s3 rb s3://multimodal-kb-test-${ACCOUNT_ID} --force --region us-east-1
aws s3 rb s3://multimodal-kb-storage-${ACCOUNT_ID} --force --region us-east-1
aws s3 rb s3://multimodal-kb-bda-storage-${ACCOUNT_ID} --force --region us-east-1

# 5. 删除 IAM 角色和策略
aws iam detach-role-policy --role-name BedrockKBMultimodalRole --policy-arn ${POLICY_ARN}
aws iam delete-role --role-name BedrockKBMultimodalRole
aws iam delete-policy --policy-arn ${POLICY_ARN}
```

!!! danger "务必清理"
    OpenSearch Serverless 按 OCU 小时计费（最低 2 OCU = $0.48/hr），Lab 完成后请立即执行清理步骤。

## 结论与建议

### 选型建议

| 场景 | 推荐方式 | 原因 |
|------|---------|------|
| 产品图册搜索（以图搜图） | Nova Multimodal Embeddings | 原生视觉相似度匹配 |
| 会议录音/培训视频搜索 | BDA | 完整语音转录能力 |
| 企业全格式知识库 | BDA | 支持 RetrieveAndGenerate，更稳定 |
| 技术文档 + 架构图混合 | BDA | 图片 OCR + 文本混合检索 |

### 生产环境建议

1. **选 BDA 起步**：BDA 方式更成熟稳定，支持完整 RAG 流程，多 Region 可用
2. **独立存储桶**：multimodal storage destination 使用独立 S3 桶，避免冲突
3. **模型选择**：RAG 生成使用 Nova Premier 或 Claude Sonnet 4 等支持多模态的模型
4. **内容要求**：音频文件需包含语音内容，纯音效/音乐无法被有效索引
5. **成本控制**：OpenSearch Serverless 是主要成本，考虑使用 Amazon S3 Vectors 作为更经济的向量存储

## 参考链接

- [官方文档 - 多模态 Knowledge Bases](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-multimodal.html)
- [选择多模态处理方式](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-multimodal-choose-approach.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/multimodal-retrieval-bedrock-knowledge-bases/)
- [Troubleshooting 多模态 KB](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-multimodal-troubleshooting.html)
