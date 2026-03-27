# S3 Vectors vs OpenSearch Serverless：向量检索选型实测对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $2-3（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

GenAI 应用（RAG、语义搜索、AI Agent）的核心需求是**向量检索**。AWS 现在有两个 Serverless 向量检索方案：

- **Amazon S3 Vectors** — 2025 年 12 月 GA，S3 原生向量存储，定位"低成本、零运维"
- **OpenSearch Serverless (AOSS)** — Vector Search Collection，定位"全功能搜索引擎"

客户常问：**该选哪个？** 本文通过实测对比，帮你做出选型决策。

## 前置条件

- AWS 账号（需要 `s3vectors:*` 和 `aoss:*` 权限）
- AWS CLI v2 已配置
- Python 3.10+，安装 `boto3` 和 `opensearch-py`

```bash
pip install boto3 opensearch-py
```

## 一句话结论

| 你的场景 | 推荐方案 |
|---------|---------|
| 纯向量存储 + 简单 kNN 检索 | **S3 Vectors**（成本低 60-175 倍） |
| 需要全文搜索 + 向量混合检索 | **OpenSearch Serverless** |
| 需要聚合分析 + 复杂过滤 | **OpenSearch Serverless** |
| 预算敏感、向量量级 < 1000 万 | **S3 Vectors** |
| 已有 OpenSearch 生态 | **OpenSearch Serverless** |

## 核心概念对比

| 维度 | S3 Vectors | OpenSearch Serverless |
|------|-----------|---------------------|
| **架构** | S3 原生 vector bucket + index | Collection + OpenSearch index |
| **搜索能力** | kNN only | kNN + BM25 全文 + 混合搜索 |
| **距离度量** | Cosine, Euclidean | Cosine, Euclidean, Dot Product |
| **最大维度** | 4,096 | 16,000 |
| **向量上限** | 20 亿/索引 | 1 TiB/索引 |
| **过滤** | 简单 metadata key-value | keyword, numeric, geo, boolean, date, nested |
| **聚合查询** | ❌ | ✅ |
| **量化压缩** | ❌ | ✅ Faiss 16-bit |
| **计费模型** | 按用量（PUT + 存储 + 查询） | OCU 时间 + 存储 |
| **最低月费** | ~$0 | ~$350（2 OCU） |
| **API 风格** | AWS SDK (s3vectors) | OpenSearch REST API |
| **部署复杂度** | 1 条命令 | 3 个策略 + Collection |

## 动手实践

### Step 1: 部署 S3 Vectors

```bash
# 创建 Vector Bucket
aws s3vectors create-vector-bucket \
  --vector-bucket-name s3v-benchmark \
  --region us-east-1

# 创建 Vector Index（1024 维，cosine 距离）
aws s3vectors create-index \
  --vector-bucket-name s3v-benchmark \
  --index-name idx-benchmark \
  --dimension 1024 \
  --distance-metric cosine \
  --data-type float32 \
  --region us-east-1
```

总耗时：**< 5 秒**，立即可写入。

### Step 2: 部署 OpenSearch Serverless

```bash
# 1. 创建加密策略
aws opensearchserverless create-security-policy \
  --name benchmark-enc \
  --type encryption \
  --policy '{"Rules":[{"ResourceType":"collection","Resource":["collection/vs-benchmark"]}],"AWSOwnedKey":true}' \
  --region us-east-1

# 2. 创建网络策略（公网访问，测试用）
aws opensearchserverless create-security-policy \
  --name benchmark-net \
  --type network \
  --policy '[{"Rules":[{"ResourceType":"collection","Resource":["collection/vs-benchmark"]},{"ResourceType":"dashboard","Resource":["collection/vs-benchmark"]}],"AllowFromPublic":true}]' \
  --region us-east-1

# 3. 创建数据访问策略
aws opensearchserverless create-access-policy \
  --name benchmark-access \
  --type data \
  --policy '[{"Rules":[{"ResourceType":"index","Resource":["index/vs-benchmark/*"],"Permission":["aoss:CreateIndex","aoss:UpdateIndex","aoss:DescribeIndex","aoss:ReadDocument","aoss:WriteDocument"]},{"ResourceType":"collection","Resource":["collection/vs-benchmark"],"Permission":["aoss:CreateCollectionItems","aoss:DescribeCollectionItems","aoss:UpdateCollectionItems"]}],"Principal":["arn:aws:iam::YOUR_ACCOUNT:user/YOUR_USER"]}]' \
  --region us-east-1

# 4. 创建 Collection（禁用冗余，降低测试成本）
aws opensearchserverless create-collection \
  --name vs-benchmark \
  --type VECTORSEARCH \
  --standby-replicas DISABLED \
  --region us-east-1
```

总耗时：**~5 分钟**（等待 Collection 变为 Active 状态）。

```bash
# 等待 Collection 就绪
aws opensearchserverless batch-get-collection \
  --names vs-benchmark \
  --region us-east-1 \
  --query "collectionDetails[0].{status:status,endpoint:collectionEndpoint}"
```

Collection Active 后，创建向量索引：

```python
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import boto3

session = boto3.Session(region_name="us-east-1")
auth = AWSV4SignerAuth(session.get_credentials(), "us-east-1", "aoss")

client = OpenSearch(
    hosts=[{"host": "YOUR_COLLECTION_ID.us-east-1.aoss.amazonaws.com", "port": 443}],
    http_auth=auth, use_ssl=True, verify_certs=True,
    connection_class=RequestsHttpConnection, timeout=60,
)

# 创建索引
client.indices.create("benchmark-vectors", body={
    "settings": {"index.knn": True},
    "mappings": {"properties": {
        "embedding": {
            "type": "knn_vector", "dimension": 1024,
            "method": {"name": "hnsw", "engine": "faiss", "space_type": "cosinesimil",
                       "parameters": {"m": 16, "ef_construction": 256}}
        },
        "category": {"type": "keyword"},
        "price": {"type": "float"},
        "title": {"type": "text"},
    }}
})
```

### Step 3: 写入测试数据

使用统一数据集：1024 维归一化随机向量，带 `category`（8 类）和 `price` 元数据。

**S3 Vectors**:
```bash
# 批量写入 500 向量/次
aws s3vectors put-vectors \
  --vector-bucket-name s3v-benchmark \
  --index-name idx-benchmark \
  --vectors file:///tmp/batch_0.json \
  --region us-east-1
```

**OpenSearch Serverless**:
```python
# 批量写入 50 向量/次（1024 维 payload 较大，控制 batch size）
actions = []
for vec_data in vectors:
    actions.append({"index": {"_index": "benchmark-vectors"}})
    actions.append({"embedding": vec_data["embedding"], "category": vec_data["category"], "price": vec_data["price"]})
client.bulk(body="\n".join(json.dumps(a) for a in actions) + "\n")
```

### Step 4: 查询性能基准测试

**S3 Vectors 查询**:
```python
import boto3, time

s3v = boto3.client("s3vectors", region_name="us-east-1")

start = time.time()
result = s3v.query_vectors(
    vectorBucketName="s3v-benchmark",
    indexName="idx-benchmark",
    queryVector={"float32": query_vector},
    topK=5,
)
print(f"Latency: {(time.time()-start)*1000:.0f}ms, hits: {len(result['vectors'])}")
```

**OpenSearch Serverless 查询**:
```python
start = time.time()
result = client.search(index="benchmark-vectors", body={
    "size": 5,
    "query": {"knn": {"embedding": {"vector": query_vector, "k": 5}}}
})
print(f"Client: {(time.time()-start)*1000:.0f}ms, server took: {result['took']}ms")
```

## 测试结果

### 查询延迟对比（1024 维，cosine 距离）

| 查询类型 | S3 Vectors | AOSS (客户端) | AOSS (服务端) |
|---------|-----------|--------------|-------------|
| **冷查询 top-5** | 271ms | 735ms | 70ms |
| **热查询 top-5 P50** | **274ms** | **262ms** | 26ms |
| **热查询 top-5 avg** | 274ms | 261ms | 25ms |
| **Top-K=100 P50** | 274ms | 301ms | 34ms |
| **元数据过滤 P50** | ~280ms | 263ms | 21ms |

!!! note "延迟说明"
    - **S3 Vectors 延迟** = 端到端（无法拆分服务端/网络），且非常稳定（269-287ms 区间）
    - **AOSS 客户端延迟** ≈ 网络 + 签名（~230ms） + 服务端处理（~25ms）
    - **AOSS 服务端 took** = 纯搜索引擎处理时间，极快（16-70ms）
    - 两者客户端总延迟几乎相同（~260-280ms），差距 < 10%

### 关键发现

1. **客户端总延迟相当** — S3 Vectors ~274ms vs AOSS ~262ms，日常使用体感一致
2. **AOSS 冷启动慢** — 首次查询 735ms（需加载索引到 OCU），S3 Vectors 冷启动仅 271ms
3. **S3 Vectors 延迟极其稳定** — 不论数据量 1K 或 11K，不论 Top-K 5 或 100，都是 ~274ms
4. **AOSS 服务端极快** — 实际搜索 20-30ms，大部分延迟在网络+SigV4 签名
5. **Top-K 大小对两者影响都很小** — Top-K 从 5 到 100，延迟几乎不变

### 写入性能对比

| 规模 | S3 Vectors | AOSS |
|------|-----------|------|
| 1K 向量 | 1.1s | 20.9s |
| 10K 向量 | 11.2s | 68.1s |

S3 Vectors 写入吞吐约为 AOSS 的 **6 倍**（支持每次 500 向量批量 vs AOSS 受 payload 限制需控制 batch size）。

### 部署复杂度对比

| 步骤 | S3 Vectors | AOSS |
|------|-----------|------|
| 创建存储 | 1 条命令 | 4 条命令（3 策略 + collection） |
| 到可写入 | **< 5 秒** | **~5 分钟** |
| IAM 配置 | 标准 IAM | IAM + Data Access Policy |
| 首次上手时间 | 10 分钟 | 30-60 分钟 |

### AOSS 独有功能展示

**全文搜索（BM25）** — S3 Vectors 不支持：
```python
result = client.search(index="benchmark-hybrid", body={
    "size": 5,
    "query": {"match": {"title": "machine learning deep learning"}}
})
# 返回文本相关度排序的结果
```

**混合搜索（BM25 + kNN）** — 结合语义相似度和关键词匹配：
```python
result = client.search(index="benchmark-hybrid", body={
    "size": 5,
    "query": {"bool": {
        "must": [{"match": {"title": "machine learning"}}],
        "should": [{"knn": {"embedding": {"vector": query_vec, "k": 5}}}]
    }}
})
```

**聚合分析** — 数据洞察：
```python
result = client.search(index="benchmark-vectors", body={
    "size": 0,
    "aggs": {"categories": {"terms": {"field": "category"}}}
})
# 返回各 category 的文档计数
```

## 踩坑记录

!!! warning "AOSS Data Access Policy 易踩坑"
    必须同时授权 `index` 和 `collection` 两个 ResourceType 的权限，缺一个就会返回 403 Forbidden。**已查文档确认**：这是 AOSS 的权限模型设计。

!!! warning "AOSS Vector Collection 不支持自定义文档 ID"
    Bulk API 中不能指定 `_id` 字段（vector search 和 time series collection 的限制）。**已查文档确认**。

!!! warning "AOSS 最低 OCU 费用"
    即使 Collection 完全空闲，仍然按最低 2 OCU 计费（$0.48/hr）。这是与 S3 Vectors 最大的成本差异。**已查文档确认**。

!!! warning "S3 Vectors 热查询延迟"
    官方声明频繁查询可低至 ~100ms，我们实测 ~274ms（5000-11000 向量规模）。可能需要更高频率的持续查询才能触发进一步优化。**实测发现，与官方声明有差距**。

## 费用明细

### 实验成本

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 Vectors 存储+查询 | 按用量 | 11K 向量 | < $0.10 |
| AOSS Collection (2 OCU) | $0.48/hr | ~1.5 hr | ~$0.72 |
| **合计** | | | **~$0.82** |

### 生产成本估算（10 万向量，1024 维）

| 成本项 | S3 Vectors | AOSS |
|--------|-----------|------|
| 最低月费 | **~$2-6** | **~$350** |
| 存储 | ~$0.05 | ~$0.01 (S3) |
| 查询 (1000次/天) | ~$2-5 | 含在 OCU 中 |
| **总月费** | **~$2-6** | **~$350+** |

!!! danger "成本差异巨大"
    同等数据量下，S3 Vectors 月费约为 AOSS 的 **1/60 到 1/175**。如果你只需要 kNN 检索，S3 Vectors 的成本优势是压倒性的。

## 清理资源

```bash
# 1. 清理 S3 Vectors
aws s3vectors delete-index \
  --vector-bucket-name s3v-benchmark \
  --index-name idx-benchmark \
  --region us-east-1

aws s3vectors delete-vector-bucket \
  --vector-bucket-name s3v-benchmark \
  --region us-east-1

# 2. 清理 AOSS
aws opensearchserverless delete-collection \
  --id YOUR_COLLECTION_ID \
  --region us-east-1

aws opensearchserverless delete-access-policy \
  --name benchmark-access --type data \
  --region us-east-1

aws opensearchserverless delete-security-policy \
  --name benchmark-net --type network \
  --region us-east-1

aws opensearchserverless delete-security-policy \
  --name benchmark-enc --type encryption \
  --region us-east-1
```

!!! danger "务必清理"
    **AOSS 按 OCU 时间计费，不清理 Collection 会持续产生费用！** 最低 $0.48/hr = $11.52/天。

## 结论与选型建议

### 选 S3 Vectors 的场景 ✅

- **纯 RAG 向量存储** — 存 embedding、查最近邻、返回原文
- **Bedrock Knowledge Bases 集成** — 原生支持，零额外配置
- **预算敏感** — 月费 $2-6 vs $350+
- **简单运维** — 零配置、零管理，S3 级别的持久性
- **高写入吞吐** — 支持每次 500 向量批量，写入速度快 6 倍

### 选 OpenSearch Serverless 的场景 ✅

- **需要全文搜索** — BM25 关键词匹配
- **需要混合搜索** — 语义 + 关键词联合排序
- **需要聚合分析** — 统计、分组、指标计算
- **复杂过滤** — 地理位置、日期范围、嵌套查询
- **已有 OpenSearch 生态** — 现有 Dashboards、告警、数据管道

### 架构建议

```
简单 RAG → S3 Vectors
全功能搜索平台 → AOSS
混合方案 → S3 Vectors (冷/大量存储) + AOSS (热/查询层)
```

AWS 官方也支持 S3 Vectors 与 OpenSearch 的**分层存储模式**：热数据在 AOSS 提供低延迟搜索，冷数据在 S3 Vectors 降低存储成本。

## 参考链接

- [Amazon S3 Vectors 文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [Amazon S3 Vectors 限制](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html)
- [OpenSearch Serverless Vector Search](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html)
- [OpenSearch Serverless Scaling](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scaling.html)
- [S3 Vectors GA What's New](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-vectors-generally-available/)
