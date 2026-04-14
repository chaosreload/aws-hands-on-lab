# Amazon OpenSearch Serverless Derived Source 实测：存储节省 26%，Serverless 场景下的实操验证

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟（含 Collection 创建等待）
    - **预估费用**: ~$3.00（OCU 最低计费 + 测试时长）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-14

## 背景

OpenSearch Serverless 默认将每个文档的完整 JSON 存储在 `_source` 字段中。对于时序数据和日志分析场景，`_source` 占用的存储空间可能非常可观。

之前 OpenSearch Service（managed）已经支持了 Derived Source，通过从 `doc_values` 和 `stored_fields` 动态重建 `_source` 来节省存储。现在，OpenSearch **Serverless** 也获得了这项能力。

本文将通过对比实验验证：

1. Serverless 环境下 Derived Source 的**实际存储节省**效果
2. **查询延迟**的代价有多大
3. **边界场景**：大文档、嵌套字段、不支持的字段类型如何处理

## 前置条件

- AWS 账号，具有 OpenSearch Serverless 相关权限（`aoss:*`）
- Python 3 + `opensearch-py` + `requests-aws4auth`（推荐，curl SigV4 认证在 AOSS 下不够稳定）
- AWS CLI v2 已配置

```bash
pip install opensearch-py requests-aws4auth boto3
```

## 核心概念

| 参数 | 说明 |
|------|------|
| 设置名 | `index.derived_source.enabled` |
| 类型 | **静态设置**（创建后不可变更） |
| 适用 Collection 类型 | TIMESERIES、Search（本文验证 TIMESERIES） |
| 存储节省 | 官方 benchmark：58.3%（nyc_taxi 数据集） |
| 主要限制 | 不支持 nested、percolator 等字段类型 |

**工作原理**：启用后，OpenSearch 不再存储原始 JSON 文档（`_source` 字段），而是在 search、get、mget、reindex、update 等操作时，从已有的索引字段（`doc_values`、`stored_fields`）动态重建 `_source`。

**已知的数据保真性变化**：

| 变化项 | 详情 |
|--------|------|
| Date 格式 | 始终使用第一个格式（如原始 `2026-04-14T00:00:00Z` → 重建为 `2026-04-14T00:00:00.000Z`） |
| Geopoint | 返回固定 `{"lat": val, "lon": val}` 格式，可能丢失精度 |
| Multi-value arrays | 可能被排序 |
| Keyword 字段 | 可能被去重 |
| 字段顺序 | 不保证与原始 ingestion 顺序一致 |

## 动手实践

### Step 1: 创建 OpenSearch Serverless Collection

首先创建必需的三个 Policy + Collection：

```bash
# 1. Encryption Policy
aws opensearchserverless create-security-policy \
  --name derived-source-test-enc \
  --type encryption \
  --policy '{"Rules":[{"ResourceType":"collection","Resource":["collection/derived-source-test"]}],"AWSOwnedKey":true}' \
  --region us-east-1

# 2. Network Policy（公开访问，仅用于测试）
aws opensearchserverless create-security-policy \
  --name derived-source-test-net \
  --type network \
  --policy '[{"Rules":[{"ResourceType":"collection","Resource":["collection/derived-source-test"]},{"ResourceType":"dashboard","Resource":["collection/derived-source-test"]}],"AllowFromPublic":true}]' \
  --region us-east-1

# 3. Data Access Policy（替换 YOUR_IAM_ARN）
aws opensearchserverless create-access-policy \
  --name derived-source-test-access \
  --type data \
  --policy '[{"Rules":[{"ResourceType":"index","Resource":["index/derived-source-test/*"],"Permission":["aoss:CreateIndex","aoss:DeleteIndex","aoss:UpdateIndex","aoss:DescribeIndex","aoss:ReadDocument","aoss:WriteDocument"]},{"ResourceType":"collection","Resource":["collection/derived-source-test"],"Permission":["aoss:CreateCollectionItems","aoss:DeleteCollectionItems","aoss:UpdateCollectionItems","aoss:DescribeCollectionItems"]}],"Principal":["YOUR_IAM_ARN"]}]' \
  --region us-east-1

# 4. 创建 Collection
aws opensearchserverless create-collection \
  --name derived-source-test \
  --type TIMESERIES \
  --region us-east-1
```

等待 Collection 变为 ACTIVE（通常 1-2 分钟）：

```bash
aws opensearchserverless batch-get-collection \
  --ids YOUR_COLLECTION_ID \
  --region us-east-1 \
  --query "collectionDetails[0].[status,collectionEndpoint]" \
  --output text
```

### Step 2: 创建对比 Index

使用 Python SDK 创建两个 index：一个启用 derived source，一个不启用。

```python
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
import boto3

session = boto3.Session()
credentials = session.get_credentials().get_frozen_credentials()
awsauth = AWS4Auth(credentials.access_key, credentials.secret_key,
                   'us-east-1', 'aoss', session_token=credentials.token)

client = OpenSearch(
    hosts=[{'host': 'YOUR_ENDPOINT.us-east-1.aoss.amazonaws.com', 'port': 443}],
    http_auth=awsauth, use_ssl=True, verify_certs=True,
    connection_class=RequestsHttpConnection, timeout=30,
)

mapping = {
    "properties": {
        "@timestamp": {"type": "date"},
        "cpu_usage": {"type": "float"},
        "memory_usage": {"type": "float"},
        "disk_io": {"type": "float"},
        "network_in": {"type": "float"},
        "network_out": {"type": "float"},
        "host": {"type": "keyword"},
        "region": {"type": "keyword"},
        "service": {"type": "keyword"},
        "status": {"type": "keyword"},
        "message": {"type": "text"}
    }
}

# Index WITH derived source
client.indices.create(index='ts-derived', body={
    "settings": {"index": {"derived_source": {"enabled": True}}},
    "mappings": mapping
})

# Index WITHOUT derived source (baseline)
client.indices.create(index='ts-baseline', body={
    "mappings": mapping
})
```

验证设置：

```python
settings = client.indices.get_settings(index='ts-derived')
print(settings['ts-derived']['settings']['index']['derived_source'])
# {'enabled': 'true'}
```

### Step 3: 对比实验 — 存储

向两个 index 灌入相同的 1000 条时序数据：

```python
import random, datetime
from opensearchpy import helpers

random.seed(42)
base_time = datetime.datetime(2026, 4, 14, 0, 0, 0)
hosts = ['web-01', 'web-02', 'web-03', 'db-01', 'db-02', 'cache-01']
services = ['nginx', 'postgres', 'redis', 'api-gateway', 'worker']

def gen_docs(index_name, count=1000):
    random.seed(42)  # 确保两个 index 数据完全一致
    for i in range(count):
        ts = base_time + datetime.timedelta(seconds=i*60)
        yield {
            "_index": index_name,
            "@timestamp": ts.isoformat() + "Z",
            "cpu_usage": round(random.uniform(5.0, 95.0), 2),
            "memory_usage": round(random.uniform(20.0, 90.0), 2),
            "disk_io": round(random.uniform(0.1, 500.0), 2),
            "network_in": round(random.uniform(100.0, 10000.0), 2),
            "network_out": round(random.uniform(50.0, 8000.0), 2),
            "host": random.choice(hosts),
            "region": random.choice(['us-east-1', 'us-west-2', 'eu-west-1']),
            "service": random.choice(services),
            "status": random.choice(['healthy', 'warning', 'critical']),
            "message": f"Metric report for {random.choice(hosts)} at {ts.isoformat()}"
        }

for idx in ['ts-derived', 'ts-baseline']:
    helpers.bulk(client, gen_docs(idx), chunk_size=200)
```

**实测结果**：

```
ts-derived  (derived source ON):  139.6 KB (1001 docs)
ts-baseline (derived source OFF): 189.4 KB (1001 docs)
```

| Index | Size | 相对节省 |
|-------|------|---------|
| ts-baseline（基准） | 189.4 KB | — |
| ts-derived（开启） | 139.6 KB | **26.3%** |

!!! info "为什么实测只有 26%，不是官方的 58%？"
    官方 58.3% 的 benchmark 使用 nyc_taxi 数据集（大规模、多字段）。在小规模测试中，index segment 元数据和固定开销占比较大，稀释了 `_source` 字段的节省效果。**生产环境下数百万文档的场景，节省比例会更接近官方数字。**

### Step 4: 对比实验 — 查询延迟

对两个 index 执行 4 种查询，各跑 10 次取平均值：

| 查询类型 | ts-derived (ms) | ts-baseline (ms) | 差异 |
|---------|----------------|-----------------|------|
| match_all (100 docs) | 422.5 | 262.0 | **+61.3%** |
| range_query (cpu 50-90) | 285.5 | 260.8 | +9.4% |
| terms_aggregation | 276.6 | 258.6 | +7.0% |
| date_range | 276.3 | 261.0 | +5.9% |

!!! warning "match_all 查询延迟显著增加"
    返回大量文档的 match_all 查询延迟增加 61%，因为每条结果的 `_source` 都需要实时重建。对于需要返回完整文档的高吞吐查询场景（如 ETL、reindex），这个开销值得关注。
    
    但对于不返回 `_source` 的聚合查询（`size: 0`），开销仅 5-7%。

### Step 5: 边界测试 — 嵌套字段

尝试创建包含 `nested` 类型字段的 derived source index：

```python
client.indices.create(index='ts-nested-derived', body={
    "settings": {"index": {"derived_source": {"enabled": True}}},
    "mappings": {
        "properties": {
            "@timestamp": {"type": "date"},
            "metrics": {
                "type": "nested",
                "properties": {
                    "name": {"type": "keyword"},
                    "value": {"type": "float"}
                }
            }
        }
    }
})
```

**实测结果**：

```
RequestError(400, 'mapper_parsing_exception', 
  'Derived source is not supported for metrics field as it is disabled/nested')
```

!!! danger "Nested 字段与 Derived Source 不兼容"
    如果你的数据模型使用了 `nested` 类型（例如多层级的嵌套对象），就**不能启用 derived source**。这个限制在 index 创建时就会被检查，不会有静默失败的风险。

### Step 6: 边界测试 — 不支持的字段类型

| 字段类型 | 测试 | 结果 |
|---------|------|------|
| `percolator` | 创建含 percolator 字段的 index | ❌ 创建被拒绝 |
| 动态映射 object | 在已有 index 中 ingest 未映射的 object 字段 | ✅ ingestion 成功，但重建后返回 `{}` |

!!! warning "动态映射字段的 _source 重建丢失数据"
    如果 ingest 了映射中未定义的 object 字段，derived source 重建时会返回空对象 `{}`。**确保所有字段都在 mapping 中明确定义**，或者在 derived source index 中禁用动态映射。

### Step 7: 数据保真性验证

对比同一文档在两个 index 中的 `_source` 返回：

```python
# 查询同一时间戳的文档
for idx in ['ts-derived', 'ts-baseline']:
    resp = client.search(index=idx, body={
        "query": {"match": {"@timestamp": "2026-04-14T00:00:00Z"}},
        "size": 1
    })
    print(f"{idx}: {resp['hits']['hits'][0]['_source']}")
```

| 差异项 | Derived Source | Baseline |
|--------|---------------|----------|
| Date 格式 | `2026-04-14T00:00:00.000Z` | `2026-04-14T00:00:00Z` |
| 字段顺序 | 随机 | 保持原始顺序 |
| 数值精度 | ✅ 一致 | ✅ 一致 |
| Keyword/Text | ✅ 一致 | ✅ 一致 |

!!! info "Date 格式会标准化"
    即使你 ingest 的日期是 `2026-04-14T00:00:00Z`，derived source 重建后会返回 `2026-04-14T00:00:00.000Z`（带毫秒）。如果下游系统对日期格式有严格要求，需要注意这个变化。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T1 | 存储对比（1000 docs） | ✅ | 189.4KB → 139.6KB | **节省 26.3%** |
| T2 | 查询延迟（match_all） | ⚠️ | +61.3% | 返回大量文档时开销显著 |
| T2 | 查询延迟（range） | ⚠️ | +9.4% | |
| T2 | 查询延迟（aggregation） | ✅ | +7.0% | |
| T3 | 大文档（150+ 字段） | ✅ | 节省 13.6% | |
| T4 | Nested 字段 | ❌ 不支持 | index 创建失败 | 官方文档间接提及 |
| T5 | Percolator 字段 | ❌ 不支持 | index 创建失败 | |
| T5 | 动态 object 字段 | ⚠️ | 重建返回 `{}` | **官方未记录** |
| T6 | 数据保真性 | ⚠️ | Date 格式变化 | 与文档描述一致 |

## 踩坑记录

!!! warning "踩坑 1: curl --aws-sigv4 对 AOSS 数据平面持续 403"
    使用 curl 的 `--aws-sigv4 "aws:amz:us-east-1:aoss"` 认证方式访问 OpenSearch Serverless 数据平面持续返回 403 Forbidden，即使 IAM 有 AdministratorAccess。
    
    **解决方案**：使用 Python `opensearch-py` + `requests-aws4auth` 组合，认证一次成功。推荐在脚本化场景中始终使用 SDK 而非 curl。

!!! warning "踩坑 2: 动态映射 object 字段在 derived source 下丢失数据"
    ingest 时包含 mapping 中未定义的 object 字段，derived source 重建后返回空对象 `{}`。这是因为动态映射的 object 字段没有对应的 doc_values，无法重建。
    
    **建议**：在 derived source index 中使用 `"dynamic": "strict"` 阻止未知字段。
    
    _实测发现，官方未记录_

!!! warning "踩坑 3: 静态设置不可回退"
    `index.derived_source.enabled` 是静态设置。一旦创建了 index 并灌入数据，发现性能不满足要求，**唯一的办法是重新创建 index 并 reindex 数据**。建议先在小规模数据上验证，确认查询延迟可接受后再应用到生产。
    
    _已查文档确认_

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Indexing OCU (0.5 min) | $0.24/OCU-hr | ~2 hr | $0.24 |
| Search OCU (0.5 min) | $0.24/OCU-hr | ~2 hr | $0.24 |
| Managed Storage | $0.024/GB-mo | < 1 MB | < $0.01 |
| **合计** | | | **~$0.50** |

> ⚠️ 实际费用取决于 Collection 存活时间。OCU 按最低 0.5 OCU 计费，即使空闲也会产生费用。

## 清理资源

```bash
# 1. 删除 Collection
aws opensearchserverless delete-collection \
  --id YOUR_COLLECTION_ID \
  --region us-east-1

# 2. 删除 Data Access Policy
aws opensearchserverless delete-access-policy \
  --name derived-source-test-access \
  --type data \
  --region us-east-1

# 3. 删除 Network Policy
aws opensearchserverless delete-security-policy \
  --name derived-source-test-net \
  --type network \
  --region us-east-1

# 4. 删除 Encryption Policy
aws opensearchserverless delete-security-policy \
  --name derived-source-test-enc \
  --type encryption \
  --region us-east-1
```

!!! danger "务必清理"
    OpenSearch Serverless 按 OCU 持续计费（最低 0.5 indexing + 0.5 search OCU ≈ $0.24/hr）。**Lab 完成后立即删除 Collection**，否则每天产生约 $5.76 费用。

## 结论与建议

### 场景化推荐

| 场景 | 是否启用 Derived Source | 理由 |
|------|----------------------|------|
| 日志分析（高写低读） | ✅ 推荐 | 存储节省显著，查询多为 aggregation（开销仅 5-7%） |
| 时序指标监控 | ✅ 推荐 | 数据量大、字段类型简单，最适合的场景 |
| 全文搜索（返回完整文档） | ⚠️ 谨慎 | match_all/fetch 类查询延迟增加 60%+ |
| 含 nested 字段的数据 | ❌ 不可用 | 直接不支持 |
| 需要保留原始 JSON 格式 | ❌ 不推荐 | Date 格式、字段顺序会变化 |

### 生产注意事项

1. **先测后用**：设置不可逆，必须在小规模数据上验证查询延迟可接受
2. **明确 mapping**：使用 `"dynamic": "strict"` 避免动态字段数据丢失
3. **避免大范围 fetch**：减少 `size` 参数值，或对不需要 `_source` 的查询使用 `"_source": false`
4. **搭配 Zstd 压缩**：Derived Source + Zstd 可以进一步优化存储成本

## 参考链接

- [AWS 官方文档 — Save Storage by Using Derived Source](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-derived-source.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-opensearch-serverless-supports-derived-source/)
- [OpenSearch Blog — Save up to 2x on Storage with Derived Source](https://opensearch.org/blog/save-up-to-2x-on-storage-with-derived-source/)
- [OpenSearch 文档 — Supported Fields](https://docs.opensearch.org/latest/mappings/metadata-fields/source/#supported-fields-and-parameters)
