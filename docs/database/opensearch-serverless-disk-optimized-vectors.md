# OpenSearch Serverless Disk-Optimized Vectors 实测：用磁盘向量换取 97% 内存节省

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: ~$2-5（AOSS 2 OCU × 数小时）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

向量搜索是 RAG、语义搜索、推荐系统的核心能力，但大规模向量索引的**内存成本**一直是痛点。以 100M 条 768 维向量为例，全精度存储需要约 286 GB RAM。

2025 年 9 月，AWS 宣布 OpenSearch Serverless 支持 **disk-optimized vectors**（`mode: "on_disk"`）。通过**二进制量化**（Binary Quantization）将向量压缩至原始大小的 1/32，配合两阶段搜索（压缩索引快速筛选 + 全精度 rescore），在几乎不损失 recall 的前提下，将内存需求降低高达 97%。

本文通过实测对比 memory-optimized 与 disk-optimized 向量索引的延迟、recall、以及关键参数调优效果，帮你评估这项能力是否适合你的场景。

## 前置条件

- AWS 账号（需要 OpenSearch Serverless 权限：`aoss:*`）
- AWS CLI v2 已配置
- `curl` + `awscurl`（用于签名请求访问 AOSS endpoint）

## 核心概念

### Memory-Optimized vs Disk-Optimized

| 特性 | in_memory（默认） | on_disk（新） |
|------|-------------------|--------------|
| 引擎 | faiss / nmslib | faiss（32x/16x/8x）、lucene（4x） |
| 压缩 | 无（4 bytes/dim） | 默认 32x 二进制量化（~0.125 bytes/dim） |
| 搜索方式 | 单阶段 HNSW | 两阶段：压缩索引搜索 + 全精度 rescore |
| 延迟 | 最低 | 略高（rescore 开销，大规模时体现） |
| Recall | 基线 | 接近基线（rescoring 保证） |
| 数据类型 | float / byte / binary | **仅 float** |
| Radial search | 支持 | ❌ 不支持 |
| 适用场景 | 低延迟优先 | 成本优先、大规模向量 |

### 两阶段搜索原理

1. **Phase 1 — 压缩索引搜索**：在二进制量化后的 HNSW 图上执行 ANN 搜索，快速获取 `k × oversample_factor` 个候选
2. **Phase 2 — 全精度 Rescore**：从磁盘加载候选向量的全精度版本，重新计算距离，输出最终 top-k

`oversample_factor` 默认 3.0，即检索 `k × 3` 个候选再精选。后文会实测不同 oversample_factor 对 recall 的影响。

### Compression Level 选项

| Level | 引擎 | 内存节省 | 适用场景 |
|-------|------|---------|---------|
| **32x**（默认） | faiss | ~97% | 最大成本节省，大规模 RAG |
| 16x | faiss | ~94% | 平衡成本与 recall |
| 8x | faiss | ~87% | 更高 recall 要求 |
| 4x | lucene | ~75% | 最高 recall，使用 lucene 引擎 |

## 动手实践

### Step 1: 创建 Serverless Collection

OpenSearch Serverless 需要先创建加密策略、网络策略、数据访问策略，然后才能创建 collection。

```bash
# 创建加密策略
aws opensearchserverless create-security-policy \
  --name disk-vector-test-enc \
  --type encryption \
  --policy '{"Rules":[{"ResourceType":"collection","Resource":["collection/disk-vector-test"]}],"AWSOwnedKey":true}' \
  --region us-east-1

# 创建网络策略（公开访问，仅用于测试）
aws opensearchserverless create-security-policy \
  --name disk-vector-test-net \
  --type network \
  --policy '[{"Rules":[{"ResourceType":"collection","Resource":["collection/disk-vector-test"]},{"ResourceType":"dashboard","Resource":["collection/disk-vector-test"]}],"AllowFromPublic":true}]' \
  --region us-east-1

# 创建 collection（关闭冗余以降低成本）
aws opensearchserverless create-collection \
  --name disk-vector-test \
  --type VECTORSEARCH \
  --standby-replicas DISABLED \
  --region us-east-1
```

!!! warning "关闭冗余"
    `--standby-replicas DISABLED` 将最小 OCU 从 4 降至 2，仅建议用于开发测试。生产环境应保持默认冗余。

等待 collection 状态变为 `ACTIVE`（通常 5-8 分钟）：

```bash
aws opensearchserverless batch-get-collection \
  --names disk-vector-test \
  --region us-east-1 \
  --query 'collectionDetails[0].status'
```

创建数据访问策略（替换 `YOUR_ARN` 为你的 IAM 用户/角色 ARN）：

```bash
aws opensearchserverless create-access-policy \
  --name disk-vector-test-access \
  --type data \
  --policy '[{"Rules":[{"ResourceType":"index","Resource":["index/disk-vector-test/*"],"Permission":["aoss:*"]},{"ResourceType":"collection","Resource":["collection/disk-vector-test"],"Permission":["aoss:*"]}],"Principal":["YOUR_ARN"]}]' \
  --region us-east-1
```

### Step 2: 创建对比索引

Collection 就绪后，通过 OpenSearch API 创建三个索引进行对比。使用 `awscurl` 进行 SigV4 签名请求：

```bash
ENDPOINT="https://<collection-id>.us-east-1.aoss.amazonaws.com"

# 索引 1: Memory-Optimized（默认模式）
awscurl --service aoss --region us-east-1 \
  -X PUT "$ENDPOINT/mem-index" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {"index": {"knn": true}},
    "mappings": {
      "properties": {
        "embedding": {
          "type": "knn_vector",
          "dimension": 768,
          "space_type": "cosinesimil"
        },
        "title": {"type": "text"},
        "category": {"type": "keyword"}
      }
    }
  }'

# 索引 2: Disk-Optimized，32x 压缩（默认）
awscurl --service aoss --region us-east-1 \
  -X PUT "$ENDPOINT/disk-32x-index" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {"index": {"knn": true}},
    "mappings": {
      "properties": {
        "embedding": {
          "type": "knn_vector",
          "dimension": 768,
          "mode": "on_disk",
          "space_type": "cosinesimil"
        },
        "title": {"type": "text"},
        "category": {"type": "keyword"}
      }
    }
  }'

# 索引 3: Disk-Optimized，16x 压缩
awscurl --service aoss --region us-east-1 \
  -X PUT "$ENDPOINT/disk-16x-index" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {"index": {"knn": true}},
    "mappings": {
      "properties": {
        "embedding": {
          "type": "knn_vector",
          "dimension": 768,
          "mode": "on_disk",
          "compression_level": "16x",
          "space_type": "cosinesimil"
        },
        "title": {"type": "text"},
        "category": {"type": "keyword"}
      }
    }
  }'
```

关键参数说明：

- `mode: "on_disk"` — 启用 disk-optimized 向量存储
- `compression_level: "16x"` — 覆盖默认的 32x 压缩
- `space_type: "cosinesimil"` — 余弦相似度，RAG 场景最常用

### Step 3: 写入测试数据

向三个索引各写入 1000 条 768 维标准化随机向量：

```bash
# 生成测试数据（Python 脚本）
python3 -c "
import json, random, math
random.seed(42)
for i in range(1000):
    vec = [random.gauss(0,1) for _ in range(768)]
    norm = math.sqrt(sum(x*x for x in vec))
    vec = [x/norm for x in vec]
    cats = ['technology','science','business','health','entertainment']
    doc = {'title': f'Document {i}', 'category': cats[i%5], 'embedding': vec}
    print(json.dumps({'index': {'_index': 'INDEX_NAME', '_id': str(i)}}))
    print(json.dumps(doc))
" > bulk_data.json

# 替换 INDEX_NAME 并批量写入（对三个索引分别执行）
for idx in mem-index disk-32x-index disk-16x-index; do
  sed "s/INDEX_NAME/$idx/g" bulk_data.json | \
  awscurl --service aoss --region us-east-1 \
    -X POST "$ENDPOINT/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    --data-binary @-
done
```

### Step 4: 执行 k-NN 搜索对比

使用同一查询向量在三个索引上搜索，对比延迟和结果：

```bash
# 提取 doc 0 的向量作为查询向量
QUERY_VEC=$(python3 -c "
import json, random, math
random.seed(42)
vec = [random.gauss(0,1) for _ in range(768)]
norm = math.sqrt(sum(x*x for x in vec))
print(json.dumps([x/norm for x in vec]))
")

# 在各索引上执行 k-NN 搜索
for idx in mem-index disk-32x-index disk-16x-index; do
  echo "=== $idx ==="
  awscurl --service aoss --region us-east-1 \
    -X POST "$ENDPOINT/$idx/_search" \
    -H "Content-Type: application/json" \
    -d "{
      \"size\": 10,
      \"query\": {
        \"knn\": {
          \"embedding\": {
            \"vector\": $QUERY_VEC,
            \"k\": 10
          }
        }
      }
    }" | jq '{took, hits: .hits.total.value}'
done
```

### Step 5: oversample_factor 调优

`oversample_factor` 是 disk-optimized 向量搜索最重要的调优参数。它控制 Phase 1 检索的候选数量：

```bash
# 测试不同 oversample_factor
for osf in 1.0 3.0 10.0 20.0; do
  echo "=== oversample_factor=$osf ==="
  awscurl --service aoss --region us-east-1 \
    -X POST "$ENDPOINT/disk-32x-index/_search" \
    -H "Content-Type: application/json" \
    -d "{
      \"size\": 10,
      \"query\": {
        \"knn\": {
          \"embedding\": {
            \"vector\": $QUERY_VEC,
            \"k\": 10,
            \"rescore\": {
              \"oversample_factor\": $osf
            }
          }
        }
      }
    }" | jq '{took}'
done
```

## 测试结果

### 延迟对比（k=10，50 次采样）

| 索引 | avg | p50 | p90 | 说明 |
|------|-----|-----|-----|------|
| mem-index | 29.9ms | 25ms | 36ms | 基线（nmslib 引擎） |
| disk-32x-index | 24.9ms | 21ms | 32ms | 32x 压缩 |
| disk-16x-index | 19.9ms | 18ms | 25ms | 16x 压缩 |

!!! note "小规模数据下的反直觉结果"
    实测中 disk-optimized 索引延迟**低于** memory-optimized。这是因为 1000 条文档的压缩索引足以完全装入缓存，更小的索引 = 更快的扫描。**在百万级数据量下，disk-optimized 的延迟预期会高于 memory-optimized**，因为 rescore 阶段需要从磁盘加载全精度向量。

### Recall@10 对比（mem-index 为 ground truth）

| 索引 | Q0 | Q1 | Q2 | Q3 | Q4 | 平均 |
|------|----|----|----|----|----|----|
| disk-32x | 90% | 70% | 80% | 80% | 90% | **82%** |
| disk-16x | 40% | 100% | 90% | 90% | 100% | **84%** |

Top-5 结果高度一致，差异主要在排名靠后的结果。得分对比也非常接近：

| 索引 | top score | bottom score | avg score |
|------|-----------|-------------|-----------|
| mem-index | 1.000000 | 0.540685 | 0.590477 |
| disk-32x | 1.000000 | 0.540086 | 0.590206 |
| disk-16x | 1.000000 | 0.536385 | 0.585754 |

### 🔑 关键发现：oversample_factor 调优

| oversample_factor | avg 延迟 | Recall@10 |
|-------------------|---------|-----------|
| 1.0 | 19.0ms | 82% |
| 3.0（默认） | 17.3ms | 82% |
| 5.0 | 16.4ms | 82% |
| 10.0 | 16.8ms | 82% |
| **20.0** | **14.7ms** | **96%** |

**oversample_factor=20 将 recall 从 82% 提升到 96%**，延迟反而更低（小规模缓存效应）。在生产环境中，建议从 3.0 开始，根据 recall 需求逐步增大。

### Radial Search 边界测试

```
# disk-32x-index
"unsupported_operation_exception: Radial search is not supported 
for indices which have quantization enabled"

# mem-index (nmslib)
"Engine [NMSLIB] does not support radial search"
```

Disk-optimized 索引明确不支持 radial search。错误信息指向 "quantization enabled" 而非 "on_disk" — 这是因为 disk 模式本质上就是启用了二进制量化。

## 踩坑记录

!!! warning "踩坑 1: 小规模测试延迟可能误导"
    1000 条文档的测试中，disk-optimized 延迟比 memory-optimized 更低。这不代表大规模场景也如此。原因是压缩后的索引更小，完全可以缓存在内存中。**生产环境评估必须使用接近真实数据量的测试集。**（实测发现，官方未记录）

!!! warning "踩坑 2: Radial search 错误信息不直观"
    错误信息是 "Radial search is not supported for indices which have quantization enabled"，没有直接提到 "on_disk"。如果你不了解 on_disk 使用了量化，可能会困惑。（已查文档确认：on_disk 默认启用二进制量化）

!!! warning "踩坑 3: Serverless Collection 创建需要三件套"
    不像托管域只需创建 domain，Serverless 必须先创建加密策略 + 网络策略 + 数据访问策略，缺一不可。数据访问策略还必须在 collection 创建后才能生效。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AOSS OCU（Search + Indexing） | $0.24/OCU-hour | 2 OCU × ~4h | ~$1.92 |
| S3 存储 | $0.024/GB-month | < 0.1 GB | < $0.01 |
| **合计** | | | **~$2-5** |

## 清理资源

```bash
# 删除 collection（会自动删除其中的所有 index）
aws opensearchserverless delete-collection \
  --id <collection-id> \
  --region us-east-1

# 删除安全策略
aws opensearchserverless delete-security-policy \
  --name disk-vector-test-enc --type encryption --region us-east-1
aws opensearchserverless delete-security-policy \
  --name disk-vector-test-net --type network --region us-east-1

# 删除数据访问策略
aws opensearchserverless delete-access-policy \
  --name disk-vector-test-access --type data --region us-east-1
```

!!! danger "务必清理"
    AOSS collection 即使无数据也会持续产生 OCU 费用（最低 2 OCU = ~$0.48/hour = ~$11.52/天）。Lab 完成后请立即清理。

## 结论与建议

### 适用场景

- ✅ **大规模 RAG 应用**：百万级以上向量，成本敏感
- ✅ **多租户搜索**：每租户独立索引，希望控制内存成本
- ✅ **冷数据向量搜索**：延迟要求不高（>50ms 可接受）
- ❌ **实时推荐系统**：需要 sub-10ms 延迟
- ❌ **需要 radial search 的场景**

### 关键建议

1. **从 32x 开始**：默认压缩级别已经能节省 ~97% 内存，大多数 RAG 场景足够
2. **调优 oversample_factor**：默认 3.0 可能不够，建议测试 10-20 范围，实测中 20.0 将 recall 从 82% 提升到 96%
3. **用真实数据量评估延迟**：小规模测试可能给出误导性的延迟数据
4. **与 memory-optimized 对比测试**：在相同数据量下对比，确保 recall 满足业务需求
5. **注意数据类型限制**：disk-optimized 仅支持 `float`，不支持 `byte` 或 `binary` 向量

### 与其他向量存储对比

| 维度 | AOSS disk-optimized | AOSS memory-optimized | S3 Vectors |
|------|--------------------|-----------------------|------------|
| 内存效率 | ⭐⭐⭐⭐⭐ | ⭐⭐ | N/A（无索引） |
| 延迟 | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| 成本（大规模） | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ |
| 功能丰富度 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| 运维复杂度 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |

## 参考链接

- [AWS What's New: OpenSearch Serverless disk-optimized vectors](https://aws.amazon.com/about-aws/whats-new/2025/09/opensearch-serverless-disk-optimized-vectors/)
- [OpenSearch Serverless Vector Search 文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-vector-search.html)
- [OpenSearch Disk-Based Vector Search](https://opensearch.org/docs/latest/search-plugins/knn/disk-based-vector-search/)
- [OpenSearch Serverless Scaling 文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scaling.html)
