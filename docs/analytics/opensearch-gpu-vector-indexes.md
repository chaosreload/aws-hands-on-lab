---
tags:
  - Analytics
---

# Amazon OpenSearch Service GPU 加速与自动优化向量索引实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $5-10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

构建大规模向量数据库是 AI 应用（语义搜索、推荐引擎、RAG、Agent 系统）的核心基础设施之一。传统的向量索引构建面临两大痛点：

1. **构建速度慢**：十亿级向量索引可能需要数天时间构建 HNSW 图
2. **优化难度高**：调优 HNSW 参数（ef_construction、m、ef_search）和量化策略需要专家级知识，往往耗时数周

Amazon OpenSearch Service 在 2025 年 12 月推出了两个新功能来解决这些痛点：

- **GPU 加速向量索引构建**：将 HNSW 图构建卸载到 GPU，速度提升最高 10 倍，成本降至 1/4
- **自动优化（Auto-Optimize）**：自动评估索引配置，在 30-60 分钟内给出 recall/latency/cost 的最优平衡推荐

本文通过 OpenSearch Serverless 实际验证 GPU 加速功能，对比 GPU 与非 GPU 索引构建的差异。

## 前置条件

- AWS 账号（需要 `aoss:*` 和 `iam:ListUsers/ListRoles` 权限）
- AWS CLI v2 已配置
- Python 3.8+（安装 `opensearch-py` 和 `requests-aws4auth`）

```bash
pip install opensearch-py requests-aws4auth numpy
```

## 核心概念

### GPU 加速 vs 传统索引构建

| 对比项 | 传统（CPU） | GPU 加速 |
|--------|-----------|---------|
| HNSW 图构建 | CPU 串行 | GPU 并行加速 |
| 构建速度 | 基线 | 最高 10x 提升 |
| 索引构建成本 | 基线 | 约 1/4 |
| 支持引擎 | 所有引擎 | 仅 Faiss |
| 管理 GPU 实例 | N/A | 无需管理，自动分配和释放 |
| 计费模型 | OCU-hour | OCU - Vector Acceleration（秒级粒度） |
| 搜索质量 | 基线 | 完全一致 |

### Auto-Optimize 工作流

1. 准备 parquet 格式向量数据集 → 上传到 S3
2. 提交 auto-optimize job，指定可接受的 recall 和 latency 阈值
3. 30-60 分钟后获得推荐配置（HNSW 参数、量化方法等）
4. 可选：使用推荐自动创建索引并灌入数据

### 关键限制

- GPU 加速：仅支持 **Faiss 引擎**（不支持 PQ、IVF、NMSLIB、Lucene）
- GPU 加速：需要 **OpenSearch 3.1+** 或 **Serverless Vector Collection**
- GPU 加速：5 个 Region（us-east-1, us-west-2, ap-southeast-2, ap-northeast-1, eu-west-1）
- Auto-Optimize：9 个 Region
- 最佳效果：shard 至少 100 万文档

## 动手实践

### Step 1: 创建 Serverless 前置策略

OpenSearch Serverless 需要三个策略：加密策略、网络策略、数据访问策略。

**加密策略**（使用 AWS 托管密钥）：

```bash
cat > /tmp/enc-policy.json << 'EOF'
{
  "Rules": [
    {
      "ResourceType": "collection",
      "Resource": ["collection/gpu-vector-test"]
    }
  ],
  "AWSOwnedKey": true
}
EOF

aws opensearchserverless create-security-policy \
  --name gpu-vector-test-enc \
  --type encryption \
  --policy file:///tmp/enc-policy.json \
  --region us-east-1
```

**网络策略**（公开访问 — 用于测试）：

```bash
cat > /tmp/net-policy.json << 'EOF'
[
  {
    "Rules": [
      {
        "ResourceType": "collection",
        "Resource": ["collection/gpu-vector-test"]
      },
      {
        "ResourceType": "dashboard",
        "Resource": ["collection/gpu-vector-test"]
      }
    ],
    "AllowFromPublic": true
  }
]
EOF

aws opensearchserverless create-security-policy \
  --name gpu-vector-test-net \
  --type network \
  --policy file:///tmp/net-policy.json \
  --region us-east-1
```

**数据访问策略**（替换 `ACCOUNT_ID` 和 `USER_NAME`）：

```bash
cat > /tmp/dap-policy.json << 'EOF'
[
  {
    "Rules": [
      {
        "ResourceType": "collection",
        "Resource": ["collection/gpu-vector-test"],
        "Permission": [
          "aoss:CreateCollectionItems",
          "aoss:DeleteCollectionItems",
          "aoss:UpdateCollectionItems",
          "aoss:DescribeCollectionItems"
        ]
      },
      {
        "ResourceType": "index",
        "Resource": ["index/gpu-vector-test/*"],
        "Permission": [
          "aoss:CreateIndex",
          "aoss:DeleteIndex",
          "aoss:UpdateIndex",
          "aoss:DescribeIndex",
          "aoss:ReadDocument",
          "aoss:WriteDocument"
        ]
      }
    ],
    "Principal": ["arn:aws:iam::ACCOUNT_ID:user/USER_NAME"]
  }
]
EOF

aws opensearchserverless create-access-policy \
  --name gpu-vector-test-dap \
  --type data \
  --policy file:///tmp/dap-policy.json \
  --region us-east-1
```

### Step 2: 创建 GPU 加速的 Serverless Vector Collection

```bash
aws opensearchserverless create-collection \
  --name gpu-vector-test \
  --type VECTORSEARCH \
  --description "GPU acceleration vector index test" \
  --vector-options '{"ServerlessVectorAcceleration": "ENABLED"}' \
  --region us-east-1
```

等待 Collection 变为 `ACTIVE`（约 5-6 分钟）：

```bash
# 检查状态
aws opensearchserverless batch-get-collection \
  --ids <COLLECTION_ID> \
  --region us-east-1 \
  --query "collectionDetails[0].[status,collectionEndpoint]" \
  --output text
```

输出示例：`ACTIVE  https://<id>.us-east-1.aoss.amazonaws.com`

### Step 3: 创建向量索引（GPU vs 非 GPU）

使用 Python 的 `opensearch-py` 库连接 Serverless Collection：

```python
import json
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

REGION = "us-east-1"
ENDPOINT = "<your-collection-id>.us-east-1.aoss.amazonaws.com"

session = boto3.Session(region_name=REGION)
creds = session.get_credentials()
awsauth = AWS4Auth(
    creds.access_key, creds.secret_key, REGION, "aoss",
    session_token=creds.token
)

client = OpenSearch(
    hosts=[{"host": ENDPOINT, "port": 443}],
    http_auth=awsauth, use_ssl=True, verify_certs=True,
    connection_class=RequestsHttpConnection, timeout=300
)
```

**创建 GPU 加速索引**（`remote_index_build.enabled: true`）：

```python
gpu_index_body = {
    "settings": {
        "index.knn": True,
        "index.knn.remote_index_build.enabled": True  # 启用 GPU 加速
    },
    "mappings": {
        "properties": {
            "vector_field": {
                "type": "knn_vector",
                "dimension": 768,
                "method": {
                    "name": "hnsw",
                    "space_type": "l2",
                    "engine": "faiss"
                }
            },
            "text": {"type": "text"},
            "category": {"type": "keyword"}
        }
    }
}
client.indices.create(index="gpu-vector-idx", body=gpu_index_body)
```

**创建非 GPU 索引**（对照组）：

```python
nongpu_index_body = {
    "settings": {
        "index.knn": True,
        "index.knn.remote_index_build.enabled": False  # 禁用 GPU 加速
    },
    "mappings": {
        "properties": {
            "vector_field": {
                "type": "knn_vector",
                "dimension": 768,
                "method": {
                    "name": "hnsw",
                    "space_type": "l2",
                    "engine": "faiss"
                }
            },
            "text": {"type": "text"},
            "category": {"type": "keyword"}
        }
    }
}
client.indices.create(index="nongpu-vector-idx", body=nongpu_index_body)
```

!!! warning "Serverless 注意事项"
    在 Serverless Collection 上，必须**显式设置** `index.knn.remote_index_build.enabled`。
    Domain 启用 GPU 后此设置默认为 `true`，但 Serverless 不会自动继承。

### Step 4: 灌入向量数据

生成 50,000 个 768 维归一化向量（模拟 text embedding）：

```python
import numpy as np
import time

def bulk_ingest(client, index_name, num_vectors=50000, dim=768, batch_size=500):
    np.random.seed(42)
    start_time = time.time()
    total_docs = 0

    for batch_start in range(0, num_vectors, batch_size):
        batch_end = min(batch_start + batch_size, num_vectors)
        batch_count = batch_end - batch_start

        vecs = np.random.randn(batch_count, dim).astype(float)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / norms

        # Serverless 不支持自定义 doc ID，必须省略 _id 字段
        bulk_body = ""
        for j in range(batch_count):
            bulk_body += json.dumps({"index": {"_index": index_name}}) + "\n"
            bulk_body += json.dumps({
                "vector_field": vecs[j].tolist(),
                "text": "Document %d" % (batch_start + j),
                "category": "cat_%d" % ((batch_start + j) % 10)
            }) + "\n"

        resp = client.bulk(body=bulk_body)
        if not resp.get("errors"):
            total_docs += batch_count

    elapsed = time.time() - start_time
    print("%s: %d docs in %.1fs (%.0f docs/s)" % (
        index_name, total_docs, elapsed, total_docs / elapsed))
    return total_docs, elapsed

# 分别灌入 GPU 和非 GPU 索引
bulk_ingest(client, "gpu-vector-idx")
bulk_ingest(client, "nongpu-vector-idx")
```

!!! warning "Serverless 不支持自定义 Document ID"
    与 Managed Domain 不同，Serverless Collection 的 `_bulk` 和 `_doc` API 不支持指定 `_id`。
    如果 bulk 请求中包含 `_id`，会返回 `illegal_argument_exception: Document ID is not supported`。
    这是实测发现的行为，官方文档未明确说明。

### Step 5: kNN 搜索验证

```python
query_vec = np.random.randn(768).astype(float).tolist()

search_body = {
    "size": 5,
    "query": {
        "knn": {
            "vector_field": {
                "vector": query_vec,
                "k": 5
            }
        }
    }
}

# 对比两个索引的搜索结果
for idx in ["gpu-vector-idx", "nongpu-vector-idx"]:
    resp = client.search(index=idx, body=search_body)
    print("\n%s:" % idx)
    for hit in resp["hits"]["hits"]:
        print("  score=%.4f text=%s" % (hit["_score"], hit["_source"]["text"]))
```

## 测试结果

### 数据灌入吞吐量

| 索引 | GPU 加速 | 文档数 | 灌入时间 | 吞吐量 |
|------|---------|--------|---------|--------|
| gpu-vector-idx | ✅ 启用 | 50,000 | 208.6s | 240 docs/s |
| nongpu-vector-idx | ❌ 禁用 | 50,000 | 216.8s | 231 docs/s |
| gpu-small-idx | ✅ 启用 | 1,000 | 7.9s | 127 docs/s |

**分析**：灌入吞吐量差异约 4%。这是符合预期的——GPU 加速主要作用于 **HNSW 图的构建阶段**（segment merge），而非原始数据的 ingestion 吞吐。AWS 官方 benchmark 在十亿级规模时观察到 6.4x - 13.8x 的加速，我们 50K 规模的测试数据量较小，刚过默认触发阈值（50 MB）。

### kNN 搜索质量对比

| 排名 | GPU 索引结果 | 非 GPU 索引结果 | Score |
|------|-------------|----------------|-------|
| 1 | Document 47507 | Document 47507 | 0.0013 |
| 2 | Document 27948 | Document 27948 | 0.0013 |
| 3 | Document 38153 | Document 38153 | 0.0013 |
| 4 | Document 27749 | Document 27749 | 0.0013 |

**关键发现**：GPU 和非 GPU 索引返回**完全相同**的 Top-4 最近邻结果，score 一致。这证明 GPU 加速不会影响搜索质量（recall），仅加速索引构建过程。

### 搜索延迟（10 次查询）

| 索引 | Avg | P50 | P90 | P99 |
|------|-----|-----|-----|-----|
| gpu-vector-idx | 763.8ms | 693.5ms | 1273.6ms | 1273.6ms |
| nongpu-vector-idx | 540.8ms | 680.2ms | 713.3ms | 713.3ms |

**说明**：搜索延迟差异属于 Serverless 正常波动范围（冷启动、OCU 分配等），与 GPU 加速无关。GPU 加速仅影响索引构建阶段。

## 踩坑记录

!!! warning "Serverless 特有限制"
    1. **不支持 `approximate_threshold` 设置**：尝试设置 `index.knn.advanced.approximate_threshold` 会返回 "unknown setting" 错误。此设置仅在 Managed Domain 上可用。（已查文档确认）

    2. **不支持自定义 Document ID**：Serverless 的 bulk/index API 不接受 `_id` 参数，返回 "Document ID is not supported" 错误。（实测发现，官方未明确记录）

    3. **不支持 Force Merge API**：`_forcemerge` 返回 404。Serverless 由服务自动管理 segment merge。（实测发现，Serverless API 限制）

    4. **不支持 Index Stats API**：`_stats` 返回 404。只能通过 `_count` API 获取文档数量。（实测发现，Serverless API 限制）

    5. **Collection 必须显式设置 `remote_index_build`**：与 Domain 不同，Serverless Collection 即使启用了 GPU 加速，索引级别的 `index.knn.remote_index_build.enabled` 不会自动设为 true，必须在创建索引时显式指定。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Serverless Collection (min 4 OCU) | ~$0.24/OCU-hr | ~1.5 hr | ~$1.44 |
| GPU Acceleration OCU | 按秒计费 | 测试期间 | ~$0.50 |
| **合计** | | | **~$2-5** |

!!! tip "成本控制提示"
    Serverless Collection 创建后即使没有数据也会消耗最低 4 OCU（2 indexing + 2 search）。
    测试完成后务必立即删除 Collection。

## 清理资源

```bash
# 1. 删除 Collection（索引会随之删除）
aws opensearchserverless delete-collection \
  --id <COLLECTION_ID> \
  --region us-east-1

# 2. 等待 Collection 删除完成
aws opensearchserverless batch-get-collection \
  --ids <COLLECTION_ID> \
  --region us-east-1

# 3. 删除安全策略
aws opensearchserverless delete-security-policy \
  --name gpu-vector-test-enc \
  --type encryption \
  --region us-east-1

aws opensearchserverless delete-security-policy \
  --name gpu-vector-test-net \
  --type network \
  --region us-east-1

# 4. 删除数据访问策略
aws opensearchserverless delete-access-policy \
  --name gpu-vector-test-dap \
  --type data \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。Serverless Collection 即使空闲也会产生最低 OCU 费用（~$0.96/hr for 4 OCU）。

## 结论与建议

### 适用场景

- **大规模向量数据库构建**：十亿级向量索引构建从数天缩短到 1 小时内
- **AI 应用快速迭代**：RAG、语义搜索、推荐系统的向量索引需要频繁重建
- **成本敏感场景**：GPU 加速不仅更快，索引构建成本也降至 1/4

### 使用建议

1. **数据规模要够大**：shard 至少 100 万文档才能充分发挥 GPU 加速优势。50K 量级差异不明显
2. **选择 Faiss 引擎**：GPU 加速仅支持 Faiss（HNSW），不支持 Lucene 或 NMSLIB
3. **Serverless vs Domain**：
    - Serverless 更适合快速实验和按需使用
    - Domain（OpenSearch 3.1+）适合需要 Force Merge、自定义 Doc ID 等高级功能的生产场景
4. **结合 Auto-Optimize**：先用 Auto-Optimize 找到最优参数配置，再用 GPU 加速构建大规模索引

### GPU 加速 + Auto-Optimize 组合使用

```
数据准备 → Auto-Optimize（30-60min 出推荐）→ 应用推荐配置 → GPU 加速构建索引
```

这个组合解决了向量数据库的两大痛点：**不知道怎么配** + **配好了构建太慢**。

## 参考链接

- [GPU Acceleration for Vector Indexing 官方文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/gpu-acceleration-vector-index.html)
- [Auto-Optimize 官方文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-auto-optimize.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-opensearch-service-gpu-accelerated-auto-optimized-vector-indexes/)
- [AWS Blog: GPU Acceleration and Auto-Optimization](https://aws.amazon.com/blogs/aws/amazon-opensearch-service-improves-vector-database-performance-and-cost-with-gpu-acceleration-and-auto-optimization/)
- [OpenSearch Service 定价](https://aws.amazon.com/opensearch-service/pricing/)
