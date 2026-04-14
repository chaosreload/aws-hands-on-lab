---
tags:
  - Analytics
---

# Amazon OpenSearch Service Derived Source 实测：存储优化 51%，你需要知道的取舍

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟（含域创建等待）
    - **预估费用**: < $2.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

OpenSearch 默认将每个文档的完整 JSON 存储在 `_source` 字段中。这让搜索结果返回、`update`、`reindex` 等操作变得简单直接——但代价是存储空间。对于日志、指标等大规模数据场景，`_source` 可能占据总存储的相当比例。

之前你有两个选择：

1. **保留 `_source`**（默认）：功能完整，但存储大
2. **禁用 `_source`**：省空间，但丢失 `update`、`reindex` 能力

OpenSearch 3.1 引入了第三个选项：**Derived Source**。它不存储 `_source` 字段，而是在需要时从 `doc_values` 和 `stored_fields` 动态重建。你可以同时获得存储优化和完整的 API 功能——但有性能代价。

本文通过对比实验，量化 Derived Source 在存储、查询性能、功能兼容性上的实际表现。

## 前置条件

- AWS 账号（需要 OpenSearch Service 创建/管理权限）
- AWS CLI v2 已配置
- `curl` 可用（用于直接调用 OpenSearch API）

## 核心概念

### 三种 `_source` 模式对比

| 特性 | 默认（_source 开启） | Derived Source | _source 禁用 |
|------|---------------------|----------------|-------------|
| 存储方式 | 存储完整 JSON | 不存储，从 doc_values 重建 | 不存储，无法重建 |
| 搜索返回 _source | ✅ 直接读取 | ✅ 动态重建（较慢） | ❌ 无法返回 |
| update / update_by_query | ✅ | ✅ | ❌ |
| reindex | ✅ | ✅ | ❌ |
| 适用场景 | 通用 | 存储敏感 + 需完整功能 | 纯搜索/聚合 |

### 启用方式

Derived Source 是**索引级别**的设置，必须在**创建索引时**指定：

```json
PUT my-index
{
  "settings": {
    "index": {
      "derived_source": {
        "enabled": true
      }
    }
  }
}
```

### 支持的字段类型

boolean、所有数值类型（byte/short/integer/long/float/double/half_float/scaled_float/unsigned_long）、date、date-nanos、geo_point、ip、keyword、text、wildcard。

### 限制

- **不支持 nested 字段**
- **不支持 keyword/wildcard 带 `ignore_above` 或 `normalizer` 参数**
- **不支持含 `copy_to` 的字段**
- text 字段启用 derived source 后自动存为 stored_field
- wildcard 字段需要设置 `doc_values: true`

## 动手实践

### Step 1: 创建 OpenSearch 域

```bash
aws opensearch create-domain \
  --domain-name derived-source-test \
  --engine-version OpenSearch_3.1 \
  --cluster-config InstanceType=t3.small.search,InstanceCount=1,DedicatedMasterEnabled=false,ZoneAwarenessEnabled=false \
  --ebs-options EBSEnabled=true,VolumeType=gp3,VolumeSize=10 \
  --node-to-node-encryption-options Enabled=true \
  --encryption-at-rest-options Enabled=true \
  --domain-endpoint-options EnforceHTTPS=true,TLSSecurityPolicy=Policy-Min-TLS-1-2-2019-07 \
  --advanced-security-options 'Enabled=true,InternalUserDatabaseEnabled=true,MasterUserOptions={MasterUserName=admin,MasterUserPassword=YourPassword123!}' \
  --access-policies '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":"es:*","Resource":"arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/derived-source-test/*"}]}' \
  --region us-east-1
```

等待约 15-20 分钟域创建完成，然后获取 Endpoint：

```bash
aws opensearch describe-domain \
  --domain-name derived-source-test \
  --region us-east-1 \
  --query "DomainStatus.Endpoint" \
  --output text
```

设置环境变量：

```bash
EP="https://$(aws opensearch describe-domain --domain-name derived-source-test --region us-east-1 --query 'DomainStatus.Endpoint' --output text)"
```

### Step 2: 创建三个对照索引

我们创建三个索引，使用相同的 mapping，只是 `_source` 配置不同：

**索引 A — Derived Source 开启：**

```bash
curl -s -u admin:'YourPassword123!' -X PUT "$EP/logs-derived" \
  -H "Content-Type: application/json" \
  -d '{
  "settings": {
    "index": {
      "derived_source": { "enabled": true },
      "number_of_shards": 1,
      "number_of_replicas": 0
    }
  },
  "mappings": {
    "properties": {
      "service_name": { "type": "keyword" },
      "log_level": { "type": "keyword" },
      "request_id": { "type": "keyword" },
      "message": { "type": "text" },
      "status_code": { "type": "integer" },
      "response_time_ms": { "type": "integer" },
      "cpu_usage": { "type": "float" },
      "timestamp": { "type": "date" },
      "client_ip": { "type": "ip" },
      "is_error": { "type": "boolean" },
      "location": { "type": "geo_point" }
    }
  }
}'
```

**索引 B — 普通索引（默认 `_source`）：**

```bash
curl -s -u admin:'YourPassword123!' -X PUT "$EP/logs-normal" \
  -H "Content-Type: application/json" \
  -d '{
  "settings": {
    "index": { "number_of_shards": 1, "number_of_replicas": 0 }
  },
  "mappings": {
    "properties": {
      "service_name": { "type": "keyword" },
      "log_level": { "type": "keyword" },
      "request_id": { "type": "keyword" },
      "message": { "type": "text" },
      "status_code": { "type": "integer" },
      "response_time_ms": { "type": "integer" },
      "cpu_usage": { "type": "float" },
      "timestamp": { "type": "date" },
      "client_ip": { "type": "ip" },
      "is_error": { "type": "boolean" },
      "location": { "type": "geo_point" }
    }
  }
}'
```

**索引 C — `_source` 完全禁用：**

```bash
curl -s -u admin:'YourPassword123!' -X PUT "$EP/logs-nosource" \
  -H "Content-Type: application/json" \
  -d '{
  "settings": {
    "index": { "number_of_shards": 1, "number_of_replicas": 0 }
  },
  "mappings": {
    "_source": { "enabled": false },
    "properties": {
      "service_name": { "type": "keyword" },
      "log_level": { "type": "keyword" },
      "request_id": { "type": "keyword" },
      "message": { "type": "text" },
      "status_code": { "type": "integer" },
      "response_time_ms": { "type": "integer" },
      "cpu_usage": { "type": "float" },
      "timestamp": { "type": "date" },
      "client_ip": { "type": "ip" },
      "is_error": { "type": "boolean" },
      "location": { "type": "geo_point" }
    }
  }
}'
```

### Step 3: 批量灌入测试数据

生成 1000 条模拟日志数据并灌入三个索引：

```python
# gen_bulk_data.py
import json, random, uuid
from datetime import datetime, timedelta

services = ["api-gateway", "auth-service", "order-service",
            "payment-service", "notification-service"]
levels = ["INFO", "WARN", "ERROR", "DEBUG"]
messages = [
    "Request processed successfully in {}ms",
    "Connection timeout after {}ms, retrying",
    "User authentication completed for session {}",
    "Database query returned {} results",
    "Cache hit ratio: {}%, serving from cache",
]

base_time = datetime(2026, 3, 28, 6, 0, 0)

for idx_name in ["logs-derived", "logs-normal", "logs-nosource"]:
    with open(f"bulk_{idx_name}.ndjson", "w") as f:
        for i in range(1000):
            ts = base_time + timedelta(seconds=random.randint(0, 3600))
            level = random.choice(levels)
            is_error = level == "ERROR"
            doc = {
                "service_name": random.choice(services),
                "log_level": level,
                "request_id": str(uuid.uuid4()),
                "message": random.choice(messages).format(random.randint(1, 10000)),
                "status_code": random.choice([500, 502]) if is_error else random.choice([200, 201, 404]),
                "response_time_ms": random.randint(1, 5000),
                "cpu_usage": round(random.uniform(0.1, 99.9), 2),
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "client_ip": f"192.168.{random.randint(1,254)}.{random.randint(1,254)}",
                "is_error": is_error,
                "location": {"lat": round(random.uniform(-90, 90), 6),
                             "lon": round(random.uniform(-180, 180), 6)}
            }
            f.write(json.dumps({"index": {"_index": idx_name}}) + "\n")
            f.write(json.dumps(doc) + "\n")

print("Done: 1000 docs x 3 indexes")
```

```bash
python3 gen_bulk_data.py

for idx in logs-derived logs-normal logs-nosource; do
  curl -s -u admin:'YourPassword123!' -X POST "$EP/_bulk" \
    -H "Content-Type: application/x-ndjson" \
    --data-binary @bulk_${idx}.ndjson
done
```

### Step 4: 对比存储大小

强制合并后比较：

```bash
# Force merge for accurate sizes
for idx in logs-derived logs-normal logs-nosource; do
  curl -s -u admin:'YourPassword123!' -X POST "$EP/$idx/_forcemerge?max_num_segments=1"
done

sleep 3

# Compare
curl -s -u admin:'YourPassword123!' \
  "$EP/_cat/indices/logs-*?v&h=index,docs.count,store.size,pri.store.size&s=index"
```

### Step 5: 验证功能兼容性

**Update 操作：**

```bash
# 获取一个文档 ID
DOC_ID=$(curl -s -u admin:'YourPassword123!' "$EP/logs-derived/_search?size=1" | \
  python3 -c 'import sys,json; print(json.load(sys.stdin)["hits"]["hits"][0]["_id"])')

# 更新文档
curl -s -u admin:'YourPassword123!' -X POST "$EP/logs-derived/_update/$DOC_ID" \
  -H "Content-Type: application/json" \
  -d '{"doc": {"status_code": 999, "message": "UPDATED via _update API"}}'
```

**Reindex 操作：**

```bash
curl -s -u admin:'YourPassword123!' -X POST "$EP/_reindex" \
  -H "Content-Type: application/json" \
  -d '{"source": {"index": "logs-derived"}, "dest": {"index": "logs-reindexed"}}'
```

## 测试结果

### 存储对比（3000 文档 × 11 字段类型）

| 索引 | 配置 | 存储大小 | 相对普通索引 |
|------|------|---------|------------|
| logs-normal | 默认（_source 开启） | 708.9 KB | 100% |
| logs-derived | **Derived Source** | **348.4 KB** | **49.2%** |
| logs-nosource | _source 禁用 | 709.1 KB | 100.0% |

**关键发现**：Derived Source 实现了约 **51% 的存储节省**。

一个反直觉的结果：禁用 `_source` 的索引与普通索引大小几乎相同。这是因为 `_source` 禁用只是不存储原始 JSON，但 `doc_values`、倒排索引等数据结构仍然存在。而 Derived Source 的实现对存储结构进行了更深层的优化。

### 查询性能对比（10 次采样，单位 ms）

**带 `_source` 返回：**

| 索引 | Min | Median | Max | Avg |
|------|-----|--------|-----|-----|
| logs-derived | 19 | 43 | 673 | 161.8 |
| logs-normal | 4 | 5 | 8 | 5.4 |
| logs-nosource | 4 | 5 | 7 | 5.0 |

**不请求 `_source`（`"_source": false`）：**

| 索引 | Min | Median | Max | Avg |
|------|-----|--------|-----|-----|
| logs-derived | 4 | 5 | 13 | 5.6 |
| logs-normal | 4 | 6 | 187 | 25.5 |
| logs-nosource | 4 | 8 | 50 | 17.0 |

**关键发现**：请求 `_source` 时，Derived Source 查询延迟约为普通索引的 **8-9 倍**（median 43ms vs 5ms）。但不请求 `_source` 时，三者性能一致。

### 功能兼容性

| 操作 | 默认 | Derived Source | _source 禁用 |
|------|------|----------------|-------------|
| 搜索返回 _source | ✅ | ✅ | ❌ |
| _update API | ✅ | ✅ | ❌ `document_source_missing_exception` |
| _reindex | ✅ | ✅ 完整迁移 | ❌ |

### 边界条件验证

| 场景 | 结果 |
|------|------|
| 创建含 **nested** 字段的 derived source 索引 | ❌ 创建时拒绝：`Derived source is not supported for tags field as it is disabled/nested` |
| 创建含 **ignore_above** keyword 的 derived source 索引 | ❌ 创建时拒绝：`Unable to derive source for [short_field] with ignore_above and/or normalizer set` |

好消息是：不兼容的配置在**索引创建时就会失败**，不会在运行时产生隐患。

## 踩坑记录

!!! warning "`_source` 重建的数据精度差异"
    从 Derived Source 重建的 `_source` 与原始数据有细微差异（实测发现，官方文档有提及 doc_values 实现可能导致格式差异）：

    - **geo_point**：重建后精度更高，如 `20.999417966231704` vs 原始 `20.999418`
    - **date**：重建后可能添加毫秒部分，如 `2026-03-28T06:31:22.000Z` vs 原始 `2026-03-28T06:31:22Z`

    如果下游系统对数据格式有严格要求（如精确字符串匹配），需要注意这个差异。

!!! warning "禁用 `_source` 不一定省空间"
    实测发现 `_source: false` 的索引与普通索引存储大小几乎相同（709.1 KB vs 708.9 KB）。如果你的目标是省空间，Derived Source 才是正确选择。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| t3.small.search（OpenSearch 域） | $0.036/hr | ~2 hr | $0.07 |
| 10 GB gp3 EBS | $0.08/GB/月 | ~2 hr | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 删除 OpenSearch 域
aws opensearch delete-domain \
  --domain-name derived-source-test \
  --region us-east-1

# 确认删除
aws opensearch describe-domain \
  --domain-name derived-source-test \
  --region us-east-1 2>&1 || echo "Domain deleted successfully"
```

!!! danger "务必清理"
    Lab 完成后请立即删除 OpenSearch 域，避免持续产生费用。t3.small.search 实例每天约 $0.86。

## 结论与建议

### 适用场景

- ✅ **日志/指标类大规模数据**：存储节省 50%+ 效果显著，且这类场景很多查询只用聚合不需要 `_source`
- ✅ **需要保留 update/reindex 能力**：相比完全禁用 `_source`，Derived Source 不牺牲功能
- ✅ **搜索场景以聚合为主**：设置 `"_source": false` 时无性能损失

### 不适用场景

- ❌ **使用 nested 字段的索引**
- ❌ **keyword 字段大量使用 `ignore_above` 或 `normalizer`**
- ❌ **查询频繁返回 `_source` 且对延迟敏感**：重建开销约 8-9 倍

### 生产建议

1. **新索引评估**：创建索引前检查字段类型是否都在支持列表内
2. **查询优化**：使用 Derived Source 后，搜索时尽量设置 `"_source": false` 或指定需要的字段
3. **监控存储节省**：实际节省比例取决于文档大小和字段分布，建议在小规模测试后再全面推广
4. **注意格式差异**：如果下游系统依赖精确的 `_source` 格式，需提前验证

## 参考链接

- [AWS What's New: Amazon OpenSearch Service announces Derived Source](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-opensearch-derived-source/)
- [OpenSearch 文档: Source field - Derived Source](https://docs.opensearch.org/latest/mappings/metadata-fields/source/#derived-source)
- [Amazon OpenSearch Service 开发者指南](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/)
