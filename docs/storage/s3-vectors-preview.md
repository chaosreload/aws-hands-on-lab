---
tags:
  - Storage
---

# Amazon S3 Vectors (Preview) 实战：云原生向量存储初体验

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

2025 年 7 月，AWS 发布了 Amazon S3 Vectors（Preview）—— 首个云原生对象存储中内置向量存储与查询能力的服务。它引入了全新的 **Vector Bucket** 类型，专为 AI Agent、RAG（检索增强生成）和语义搜索场景设计，号称可将向量数据的上传、存储和查询成本降低最高 90%。

**为什么值得关注？**

- 传统方案（OpenSearch、Pinecone 等向量数据库）需要单独部署和管理，成本高
- S3 Vectors 无需预置基础设施，按用量付费，继承 S3 的弹性和持久性
- 原生集成 Bedrock Knowledge Bases，一站式构建 RAG 应用

本文将从零开始，通过 AWS CLI 和 Python SDK 完成 S3 Vectors 的完整操作流程，并实测查询性能、元数据过滤、距离度量对比等关键特性。

## 前置条件

- AWS 账号（Preview 期间需在支持 Region 中使用）
- AWS CLI v2（需包含 s3vectors 子命令）
- Python 3 + Boto3（用于 SDK 操作）
- IAM 权限：`s3vectors:*`（或按需最小化授权）

## 核心概念

S3 Vectors 引入了三层架构：

| 层级 | 说明 | 类比 |
|------|------|------|
| **Vector Bucket** | 新的 S3 bucket 类型，专为向量优化 | 数据库实例 |
| **Vector Index** | bucket 内的向量索引，指定维度和距离度量 | 数据表 |
| **Vector** | 索引内的单条数据：key + 向量 + 元数据 | 数据行 |

**与传统 S3 的关键区别**：

| 特性 | 普通 S3 Bucket | Vector Bucket |
|------|---------------|---------------|
| 命名空间 | `s3` | `s3vectors` |
| 存储内容 | 对象（文件） | 向量（embedding + metadata） |
| 查询方式 | Key 精确查找 | 向量相似度搜索 |
| Block Public Access | 可配置 | **强制开启，不可关闭** |
| 创建后可改配置 | 部分可改 | name/加密/维度/距离度量均不可改 |

### 关键限制

| 限制项 | 值 |
|--------|-----|
| 每 Region 每账号 Vector Bucket 数 | 10,000 |
| 每 Bucket Vector Index 数 | 10,000 |
| 每 Index 最大向量数 | 20 亿 |
| 向量维度范围 | 1 - 4,096 |
| 每向量总 metadata | ≤ 40 KB |
| 每向量可过滤 metadata | ≤ 2 KB |
| PutVectors 每次最多 | 500 条 |
| QueryVectors Top-K 最大 | 100 |
| 写入 + 删除 RPS per index | ≤ 1,000 |

## 动手实践

### Step 1: 创建 Vector Bucket

```bash
# 创建向量专用 bucket
aws s3vectors create-vector-bucket \
  --vector-bucket-name my-vectors-demo \
  --region us-east-1
```

验证创建成功：

```bash
aws s3vectors list-vector-buckets --region us-east-1
```

### Step 2: 创建 Vector Index

创建使用 Cosine 距离度量的 1024 维向量索引：

```bash
aws s3vectors create-index \
  --vector-bucket-name my-vectors-demo \
  --index-name movies-cosine \
  --data-type float32 \
  --dimension 1024 \
  --distance-metric cosine \
  --region us-east-1
```

!!! warning "不可更改配置"
    Vector Index 创建后，以下配置**不可修改**：index name、dimension、distance metric、non-filterable metadata keys。请根据你的 embedding 模型仔细选择。

### Step 3: 写入向量数据（Python SDK）

```python
import boto3
import random
import math

s3v = boto3.client("s3vectors", region_name="us-east-1")

# 生成归一化的随机向量（实际场景中应使用 embedding 模型）
def gen_vector(dim, seed):
    random.seed(seed)
    v = [random.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x*x for x in v))
    return [x/norm for x in v]

# 批量写入向量，附带可过滤元数据
vectors = [
    {
        "key": "tech-ai",
        "data": {"float32": gen_vector(1024, seed=100)},
        "metadata": {"category": "technology", "topic": "AI", "year": 2025}
    },
    {
        "key": "tech-cloud",
        "data": {"float32": gen_vector(1024, seed=102)},
        "metadata": {"category": "technology", "topic": "cloud", "year": 2024}
    },
    {
        "key": "nature-ocean",
        "data": {"float32": gen_vector(1024, seed=201)},
        "metadata": {"category": "nature", "topic": "ocean", "year": 2023}
    },
]

resp = s3v.put_vectors(
    vectorBucketName="my-vectors-demo",
    indexName="movies-cosine",
    vectors=vectors
)
print(f"写入状态: {resp['ResponseMetadata']['HTTPStatusCode']}")
```

**强一致性**：写入后立即可查询，无需等待索引构建。

### Step 4: 语义相似度查询

```python
# 用 tech-ai 的向量作为查询，查找最相似的向量
query_vec = gen_vector(1024, seed=100)

resp = s3v.query_vectors(
    vectorBucketName="my-vectors-demo",
    indexName="movies-cosine",
    queryVector={"float32": query_vec},
    topK=5,
    returnMetadata=True,
    returnDistance=True
)

for v in resp["vectors"]:
    print(f"key={v['key']}, distance={v['distance']:.6f}, metadata={v['metadata']}")
```

实测输出示例：

```
key=tech-ai,      distance=0.000371  # 几乎完全匹配（自身）
key=nature-ocean,  distance=0.965107  # 不同主题，距离约 1.0
key=tech-cloud,    distance=0.965503  # 不同主题，距离约 1.0
```

Cosine distance 范围 [0, 2]：0 = 方向完全一致，1 = 正交，2 = 完全相反。

### Step 5: 元数据过滤查询

S3 Vectors 使用 MongoDB 风格的过滤操作符：

```python
# 只搜索 category=technology 的向量
resp = s3v.query_vectors(
    vectorBucketName="my-vectors-demo",
    indexName="movies-cosine",
    queryVector={"float32": query_vec},
    topK=5,
    returnMetadata=True,
    returnDistance=True,
    filter={"category": {"$eq": "technology"}}
)
# 仅返回 tech-ai 和 tech-cloud

# 复合过滤：类别为 nature 且 year >= 2024
resp = s3v.query_vectors(
    vectorBucketName="my-vectors-demo",
    indexName="movies-cosine",
    queryVector={"float32": query_vec},
    topK=5,
    returnMetadata=True,
    returnDistance=True,
    filter={"$and": [{"category": {"$eq": "nature"}}, {"year": {"$gte": 2024}}]}
)
```

支持的过滤操作符：

| 操作符 | 说明 | 示例 |
|--------|------|------|
| `$eq` | 精确匹配 | `{"genre": {"$eq": "drama"}}` |
| `$ne` | 不等于 | `{"genre": {"$ne": "comedy"}}` |
| `$gt` / `$gte` | 大于 / 大于等于 | `{"year": {"$gte": 2020}}` |
| `$lt` / `$lte` | 小于 / 小于等于 | `{"price": {"$lt": 100}}` |
| `$in` / `$nin` | 在/不在数组中 | `{"genre": {"$in": ["a", "b"]}}` |
| `$and` / `$or` | 逻辑组合 | `{"$and": [{...}, {...}]}` |
| `$exists` | 字段是否存在 | `{"genre": {"$exists": true}}` |

!!! tip "过滤机制"
    S3 Vectors 在向量搜索过程中**同时**评估过滤条件（非先搜索再过滤），这意味着更可能找到匹配结果。但当匹配向量很少时，可能返回少于 Top-K 的结果。

## 测试结果

### Cosine vs Euclidean 距离对比

使用相同数据集，分别在 Cosine 和 Euclidean 索引上查询：

| Key | Cosine Distance | Euclidean Distance | 排名 |
|-----|-----------------|-------------------|------|
| tech-ai | 0.000371 | 0.000741 | #1 |
| nature-ocean | 0.965107 | 1.926492 | #2 |
| tech-cloud | 0.965503 | 1.931744 | #3 |
| nature-forest | 1.010939 | 2.019973 | #4 |
| tech-ml | 1.011161 | 2.021582 | #5 |
| nature-mountain | 1.014651 | 2.027812 | #6 |

**发现**：

- 对于归一化向量，两种度量的排序完全一致
- 数学关系：Euclidean distance ≈ 2 × Cosine distance（对 unit vectors 成立：`euclidean² = 2 × cosine`）
- **选择建议**：使用 embedding 模型推荐的度量即可（Titan Text v2 推荐 Cosine）

### 查询延迟实测

10 次连续查询（Cosine, 1024 维, 6 条数据）：

| 指标 | 值 |
|------|-----|
| 平均延迟 | 305ms |
| 最小延迟 | 267ms |
| 最大延迟 | 391ms |

- 前 3 次查询偏慢（371-391ms），之后稳定在 267-295ms
- **冷启动效应明显**：不频繁访问的 index 首次查询较慢
- 符合官方声明："sub-second for infrequent queries, as low as 100ms for frequent queries"

### 维度边界测试

| 测试 | 结果 |
|------|------|
| dim=1 创建 + 写入 + 查询 | ✅ 正常工作 |
| dim=4096 创建 + 写入 + 查询 | ✅ 正常，查询延迟 391ms |

dim=1 查询结果验证了 Cosine distance 计算的正确性：

- [0.8] vs [1.0]: distance=0（同方向）
- [0.8] vs [0.5]: distance=0（同方向）
- [0.8] vs [-1.0]: distance=2（完全反方向）

## 踩坑记录

!!! warning "注意"
    1. **Filter 语法是 MongoDB 风格**：使用 `$eq`, `$and`, `$gte` 等操作符，**不是** AWS SDK 风格的 `andAll`/`eq`。这一点在当前 Boto3 文档中不够显眼，建议直接参考 S3 用户指南的 [Metadata filtering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html) 页面。（已查文档确认）

    2. **GetVectors 默认不返回向量数据和 metadata**：需要显式传 `--return-data` 和 `--return-metadata` 参数。（已查文档确认）

    3. **QueryVectors 默认不返回 distance**：需要显式传 `returnDistance=True`（SDK）或 `--return-distance`（CLI）。（已查文档确认）

    4. **创建后锁死的配置很多**：encryption type、dimension、distance metric、non-filterable metadata keys 创建后均不可更改，必须提前规划。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 Vectors 存储（Preview） | 按量计费 | 数条测试数据 | < $0.01 |
| S3 Vectors 查询（Preview） | 按量计费 | ~30 次查询 | < $0.01 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 删除所有 vector index（需逐个删除）
for idx in movies-cosine; do
  aws s3vectors delete-index \
    --vector-bucket-name my-vectors-demo \
    --index-name $idx \
    --region us-east-1
done

# 2. 删除 vector bucket（需先删除所有 index）
aws s3vectors delete-vector-bucket \
  --vector-bucket-name my-vectors-demo \
  --region us-east-1

# 3. 验证清理完成
aws s3vectors list-vector-buckets --region us-east-1
```

!!! danger "务必清理"
    虽然 Preview 期间费用极低，但 Lab 完成后请执行清理步骤，养成良好习惯。

## 结论与建议

**S3 Vectors 适合什么场景？**

- ✅ **大规模低频查询**：百万级向量、每天查询千次级别 —— 成本远低于 OpenSearch
- ✅ **RAG 知识库**：配合 Bedrock Knowledge Bases，一站式搭建
- ✅ **冷热分层**：高频查询用 OpenSearch，低频长尾用 S3 Vectors
- ⚠️ **不适合**：毫秒级低延迟（高频场景），复杂的混合搜索（聚合、facet）

**与现有方案对比**：

| 特性 | S3 Vectors | OpenSearch Serverless | Pinecone |
|------|-----------|----------------------|----------|
| 管理开销 | 零（全托管） | 低 | 低 |
| 查询延迟 | 267ms-1s | 10-100ms | 10-100ms |
| 成本（大规模存储） | **极低** | 高 | 高 |
| 混合搜索 | ❌ 仅向量 | ✅ | 部分 |
| 元数据过滤 | ✅ MongoDB 风格 | ✅ | ✅ |
| AWS 原生集成 | ✅ Bedrock/OpenSearch | ✅ | ❌ |

**生产环境建议**：

1. Preview 阶段不建议用于生产负载
2. 提前规划好 dimension 和 distance metric（创建后不可改）
3. 元数据设计要区分 filterable 和 non-filterable
4. 搭配 Bedrock Knowledge Bases 使用效果最佳

## 参考链接

- [S3 Vectors 官方文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [S3 Vectors 限制与约束](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html)
- [Metadata 过滤语法](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-s3-vectors-preview-native-support-storing-querying-vectors/)
- [AWS News Blog](https://aws.amazon.com/blogs/aws/introducing-amazon-s3-vectors-first-cloud-storage-with-native-vector-support-at-scale/)
- [S3 定价页面](https://aws.amazon.com/s3/pricing/)
