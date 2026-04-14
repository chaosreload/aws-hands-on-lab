---
tags:
  - Database
---

# Amazon ElastiCache 向量搜索实战：Valkey 8.2 向量索引、混合搜索与性能调优

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0.30（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

向量搜索是 GenAI 应用的核心基础设施。语义缓存（Semantic Caching）可以缓存相似 prompt 的 LLM 响应降低成本；RAG 需要快速检索语义相关的文档片段；会话记忆（Conversational Memory）需要按语义匹配历史交互。

Amazon ElastiCache for Valkey 8.2 现在原生支持向量搜索，无需部署独立的向量数据库。本文通过 7 项实测验证其核心能力：HNSW vs FLAT 算法对比、混合搜索（向量 + 标签 + 数值过滤）、EF_RUNTIME 参数调优、JSON vs HASH 数据类型性能差异、边界条件处理和实时索引更新。

## 前置条件

- AWS 账号
- AWS CLI v2 已配置
- 与 ElastiCache 同 VPC 的 EC2 实例（用于连接测试）
- Python 3 + `redis` 和 `numpy` 库

## 核心概念

### 架构定位

ElastiCache 向量搜索不是要取代专用向量数据库（如 OpenSearch、pgvector），而是为**已经使用 ElastiCache 做缓存**的 GenAI 应用提供一站式方案。一个 ElastiCache 集群同时处理：会话缓存 + 语义缓存 + 向量检索 + 特征存储。

### 关键特性

| 特性 | 说明 |
|------|------|
| 算法 | HNSW（近似，快）+ FLAT（精确，慢） |
| 距离度量 | L2（欧几里得）、COSINE、IP（内积） |
| 数据类型 | FLOAT32 |
| Key 类型 | HASH、JSON |
| 过滤 | TAG（标签）+ NUMERIC（数值范围） |
| 索引更新 | 实时（修改 key 自动更新索引） |
| 可用性 | Valkey 8.2，node-based 集群，所有 AWS Region |
| 额外费用 | **无**（包含在现有 ElastiCache 定价中） |

### 两种搜索算法

- **FLAT**：暴力扫描全部向量，返回精确结果。时间复杂度 O(n)，适合小数据集或需要精确结果的场景。
- **HNSW**：基于分层图的近似最近邻搜索，牺牲少量精度换取显著性能提升。通过 `M`、`EF_CONSTRUCTION`、`EF_RUNTIME` 三个参数控制精度-性能权衡。

### 命令集

| 命令 | 功能 |
|------|------|
| `FT.CREATE` | 创建索引（定义 schema） |
| `FT.SEARCH` | KNN 搜索 + 过滤 |
| `FT.DROPINDEX` | 删除索引 |
| `FT.INFO` | 查看索引信息 |
| `FT._LIST` | 列出所有索引 |

## 动手实践

### Step 1: 创建 ElastiCache Valkey 8.2 集群

```bash
aws elasticache create-replication-group \
  --replication-group-id vs-test-vector \
  --replication-group-description 'ElastiCache Valkey 8.2 vector search test' \
  --engine valkey \
  --engine-version 8.2 \
  --cache-node-type cache.r7g.large \
  --num-cache-clusters 1 \
  --transit-encryption-enabled \
  --region us-east-1
```

!!! note "TLS 必需"
    Valkey 8.2 要求指定 `--transit-encryption-enabled`，否则创建会报错。

等待集群状态变为 `available`（约 5-8 分钟）：

```bash
aws elasticache describe-replication-groups \
  --replication-group-id vs-test-vector \
  --query 'ReplicationGroups[0].[Status,NodeGroups[0].PrimaryEndpoint]' \
  --region us-east-1
```

### Step 2: 准备测试环境

在与 ElastiCache 同 VPC 的 EC2 实例上安装依赖：

```bash
# Amazon Linux 2023
sudo yum install -y redis6 python3-pip
pip3 install redis numpy
```

验证连接：

```bash
redis6-cli -h <your-endpoint> -p 6379 --tls PING
# 预期输出：PONG
```

### Step 3: 创建 HNSW 向量索引

```python
import redis
import numpy as np
import struct

HOST = "<your-endpoint>"
r = redis.Redis(host=HOST, port=6379, ssl=True, decode_responses=False)

def float_vec_to_bytes(vec):
    """将 numpy 向量转为 FLOAT32 字节序列"""
    return struct.pack(f'{len(vec)}f', *vec)

# 创建 HNSW 索引：128 维向量 + TAG + NUMERIC 字段
r.execute_command(
    "FT.CREATE", "idx_products",
    "ON", "HASH",
    "PREFIX", "1", "product:",
    "SCHEMA",
    "embedding", "VECTOR", "HNSW", "6",
    "DIM", "128", "TYPE", "FLOAT32", "DISTANCE_METRIC", "COSINE",
    "category", "TAG",
    "price", "NUMERIC"
)
```

### Step 4: 插入向量数据

```python
categories = ["electronics", "books", "clothing", "food", "sports"]
np.random.seed(42)

for i in range(1000):
    vec = np.random.randn(128).astype(np.float32)
    vec = vec / np.linalg.norm(vec)  # L2 归一化
    r.hset(f"product:{i}", mapping={
        "embedding": float_vec_to_bytes(vec),
        "category": categories[i % 5],
        "price": str(float(i % 100 + 1))
    })
```

### Step 5: KNN 向量搜索

```python
# 生成查询向量
query_vec = np.random.randn(128).astype(np.float32)
query_vec = query_vec / np.linalg.norm(query_vec)

# 搜索最相似的 5 个产品
result = r.execute_command(
    "FT.SEARCH", "idx_products",
    "*=>[KNN 5 @embedding $query_vec]",
    "PARAMS", "2", "query_vec", float_vec_to_bytes(query_vec),
    "DIALECT", "2"
)
# result[0] = 匹配数量, result[1] = key, result[2] = fields, ...
```

### Step 6: 混合搜索（向量 + 过滤器）

```python
# TAG 过滤：只搜索 electronics 分类
result = r.execute_command(
    "FT.SEARCH", "idx_products",
    "@category:{electronics}=>[KNN 5 @embedding $query_vec]",
    "PARAMS", "2", "query_vec", float_vec_to_bytes(query_vec),
    "DIALECT", "2"
)

# NUMERIC 范围过滤：价格 10-50 的 books 分类
result = r.execute_command(
    "FT.SEARCH", "idx_products",
    "@category:{books} @price:[10 50]=>[KNN 5 @embedding $query_vec]",
    "PARAMS", "2", "query_vec", float_vec_to_bytes(query_vec),
    "DIALECT", "2"
)
```

### Step 7: 调整 EF_RUNTIME 参数

```python
# 提高 EF_RUNTIME 以获得更高召回率（牺牲延迟）
result = r.execute_command(
    "FT.SEARCH", "idx_products",
    "*=>[KNN 5 @embedding $query_vec EF_RUNTIME 200]",
    "PARAMS", "2", "query_vec", float_vec_to_bytes(query_vec),
    "DIALECT", "2"
)
```

### Step 8: 使用 JSON 数据类型

```python
import json

# 创建 JSON 索引（注意 JSON path 需要 AS 别名）
r.execute_command(
    "FT.CREATE", "idx_json_products",
    "ON", "JSON",
    "PREFIX", "1", "jproduct:",
    "SCHEMA",
    "$.embedding", "AS", "embedding", "VECTOR", "HNSW", "6",
    "DIM", "128", "TYPE", "FLOAT32", "DISTANCE_METRIC", "COSINE",
    "$.category", "AS", "category", "TAG",
    "$.price", "AS", "price", "NUMERIC"
)

# 插入 JSON 文档（向量用数组格式）
vec = np.random.randn(128).astype(np.float32)
vec = vec / np.linalg.norm(vec)
doc = {
    "embedding": vec.tolist(),
    "category": "electronics",
    "price": 29.99
}
r.execute_command("JSON.SET", "jproduct:0", "$", json.dumps(doc))
```

## 测试结果

### HNSW vs FLAT 性能对比（1000 向量，128 维，100 次查询）

| 算法 | p50 | p90 | p99 | avg |
|------|-----|-----|-----|-----|
| HNSW | 1.00ms | 1.07ms | 1.21ms | 1.01ms |
| FLAT | 1.10ms | 1.18ms | 1.24ms | 1.11ms |

> 在 1000 条向量规模下 HNSW 比 FLAT 快约 10%。随着数据量增大，差距会显著扩大——FLAT 是 O(n) 线性扫描，而 HNSW 是 O(log n)。

### EF_RUNTIME 参数调优

| EF_RUNTIME | p50 | p90 | p99 | avg | 相对延迟 |
|------------|-----|-----|-----|-----|---------|
| 10（默认） | 0.98ms | 1.04ms | 1.19ms | 0.99ms | 基准 |
| 50 | 1.06ms | 1.14ms | 1.25ms | 1.08ms | +9% |
| 200 | 1.17ms | 1.27ms | 1.34ms | 1.19ms | +20% |
| 500 | 1.29ms | 1.37ms | 1.61ms | 1.31ms | +32% |

> **调优建议**：小数据集（< 10K 向量）用默认值 10 即可。大数据集且召回率要求高时，建议从 50 开始调整，在延迟和召回率之间找到平衡点。

### 混合搜索延迟影响

| 搜索模式 | p50 | p90 |
|----------|-----|-----|
| 纯向量搜索 | 0.99ms | 1.05ms |
| 向量 + TAG + NUMERIC 过滤 | 1.13ms | 1.20ms |

> 混合搜索增加约 14% 延迟，但能显著缩小搜索范围，实际应用中提升结果相关性。

### JSON vs HASH 数据类型

| 数据类型 | p50 | p90 | p99 |
|----------|-----|-----|-----|
| HASH | 1.00ms | 1.07ms | 1.21ms |
| JSON | 1.11ms | 1.24ms | 1.40ms |

> HASH 比 JSON 快约 11%。如果只存储向量 + 简单元数据，优先用 HASH。JSON 适合需要嵌套结构的复杂文档场景。

### 距离度量对比

| 距离度量 | p50 | p90 | avg |
|----------|-----|-----|-----|
| L2 | 0.93ms | 1.02ms | 0.96ms |
| COSINE | 0.95ms | 1.02ms | 0.96ms |
| IP | 0.96ms | 1.03ms | 0.97ms |

> 三种距离度量性能几乎一致。选择标准：COSINE 适合归一化向量（最常用），L2 适合未归一化向量，IP 适合对向量长度敏感的场景。

### 实时索引更新验证

- 插入新向量后**立即**执行搜索，新向量**即刻可被检索** ✅
- 无需手动重建索引，修改 HASH/JSON key 会自动更新关联索引

### 边界条件测试

| 场景 | 行为 |
|------|------|
| 搜索空索引 | 返回 0 结果（不报错）|
| 无效距离度量（如 MANHATTAN） | 明确报错："Unknown argument MANHATTAN" |
| 错误维度向量查询 | 报错："query vector blob size (256) does not match index's expected size (512)" |

## 踩坑记录

!!! warning "INFO 版本号混淆"
    Valkey 8.2 的 `INFO server` 返回 `redis_version: 7.2.4`，这是 Redis 兼容版本号，不是实际引擎版本。实际版本通过 `aws elasticache describe-cache-clusters` 的 `EngineVersion` 字段确认。（已查文档确认）

!!! warning "TLS 强制要求"
    创建 Valkey 8.2 集群时必须显式指定 `--transit-encryption-enabled`（或 `--no-transit-encryption-enabled`），否则 API 返回 `InvalidParameterValue` 错误。（实测发现）

!!! warning "HNSW 删除/覆盖的副作用"
    官方文档明确指出：频繁删除或覆盖已索引的向量可能导致内存膨胀和召回率下降。解决方法是重建索引（`FT.DROPINDEX` + `FT.CREATE` 重新回填）。在生产环境中需要规划索引维护策略。（已查文档确认）

!!! warning "Backfill 期间不可查询"
    创建索引后会自动启动 backfill 过程扫描匹配的 key。在 backfill 完成前，查询操作会返回错误。可通过 `FT.INFO` 的 `backfill_status` 字段监控进度。大数据集创建索引时需注意这个限制。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ElastiCache cache.r7g.large | $0.252/hr | ~1 hr | $0.25 |
| EC2 t3.micro（测试客户端） | $0.0104/hr | ~0.5 hr | $0.01 |
| 向量搜索功能 | **免费** | - | $0.00 |
| **合计** | | | **~$0.26** |

## 清理资源

```bash
# 1. 删除 ElastiCache 集群
aws elasticache delete-replication-group \
  --replication-group-id vs-test-vector \
  --no-retain-primary-cluster \
  --region us-east-1

# 2. 等待集群删除完成
aws elasticache describe-replication-groups \
  --replication-group-id vs-test-vector \
  --region us-east-1
# 预期：返回 ReplicationGroupNotFoundFault 表示已删除

# 3. 终止测试 EC2 实例
aws ec2 terminate-instances \
  --instance-ids <your-instance-id> \
  --region us-east-1
```

!!! danger "务必清理"
    cache.r7g.large 每小时 $0.252，忘记清理一天就是 $6。Lab 完成后请立即执行清理步骤。

## 结论与建议

### 适用场景

- **语义缓存**：已有 ElastiCache 做缓存的 GenAI 应用，直接在同一集群上启用向量搜索，缓存相似 prompt 的 LLM 响应。官方数据显示 25% 缓存命中率即可节省 23% 成本。
- **会话记忆**：结合 LangGraph / mem0 框架，用 ElastiCache 同时存储 session state 和向量化记忆。
- **低延迟 RAG**：需要微秒级检索延迟的实时应用（语音 Agent、流式对话），ElastiCache 比磁盘存储的向量数据库快一个数量级。
- **实时推荐**：电商、内容推荐等需要实时更新向量索引的场景。

### 不太适合的场景

- **超大规模向量库**（> 1 亿级别）：内存成本高，考虑 OpenSearch 或磁盘存储方案。
- **需要复杂聚合/分析**的向量搜索：ElastiCache 的过滤能力（TAG + NUMERIC）相对简单。
- **仅需向量搜索**而不需要缓存：专用向量数据库可能更经济。

### 生产环境建议

1. **数据类型选择**：优先用 HASH（比 JSON 快 11%），除非需要嵌套文档结构。
2. **算法选择**：数据量 < 1 万用 FLAT（精确），> 1 万用 HNSW（近似但快）。
3. **EF_RUNTIME 调优**：从默认值 10 开始，根据召回率需求逐步上调，每次 2x。
4. **索引维护**：避免频繁删除/覆盖向量，定期重建索引保持最优性能。
5. **混合搜索**：利用 TAG + NUMERIC 过滤缩小搜索范围，可以在大数据集上显著减少检索延迟。

## 参考链接

- [ElastiCache 向量搜索文档](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/vector-search.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-elasticache-vector-search/)
- [官方博客：Announcing vector search for Amazon ElastiCache](https://aws.amazon.com/blogs/database/announcing-vector-search-for-amazon-elasticache/)
- [FT.CREATE 命令参考](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/vector-search-commands-ft.create.html)
- [FT.SEARCH 命令参考](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/vector-search-commands-ft.search.html)
