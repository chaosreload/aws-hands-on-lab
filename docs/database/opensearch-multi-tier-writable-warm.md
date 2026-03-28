# Amazon OpenSearch Service 多层存储实战：可写 Warm 层取代 UltraWarm 的全新架构

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟（含 domain 创建等待 ~20 分钟）
    - **预估费用**: $15-25（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

2025 年 12 月，Amazon OpenSearch Service 发布了基于 OpenSearch Optimized Instances (OI2) 的多层存储（Multi-Tier Storage）架构。这一更新的核心突破在于：**warm 层现在支持写操作**。

此前，OpenSearch Service 通过 UltraWarm 提供 warm 存储，但 UltraWarm 的一个关键限制是——迁移到 warm 层的索引变为**只读**。如果需要写入，必须先手动迁回 hot 层，这给日志分析、可观测性等场景带来不便。

新的多层存储架构彻底改变了这一限制：数据从 hot 迁移到 warm 后，仍然可以继续写入。这意味着你可以用更低成本的 warm 存储承载不太频繁访问的数据，同时保留完整的读写能力。

## 前置条件

- AWS 账号（需要 OpenSearch Service 相关 IAM 权限）
- AWS CLI v2 已配置
- 终端工具（用于 curl 命令）

## 核心概念

### 新旧架构对比

| 特性 | UltraWarm（旧） | OI2 Multi-Tier Warm（新） |
|------|-----------------|--------------------------|
| **写操作** | ❌ 只读，需迁回 hot 才能写 | ✅ 直接写入 warm 层 |
| **存储介质** | 数据主要在 S3，按需加载到本地 | 本地 NVMe 缓存 + S3 同步 |
| **数据恢复** | 依赖索引快照 | 自动从 S3 恢复 |
| **实例类型** | ultrawarm1.medium / large | oi2.large ~ oi2.8xlarge |
| **版本要求** | OpenSearch/ES 6.8+ | OpenSearch 3.3+ |
| **最大可寻址存储** | 按实例固定（最大 20 TiB） | 本地缓存的 5 倍 |

### OI2 实例关键特性

- **本地 NVMe 存储**：不需要 EBS 配置，使用实例自带 NVMe 磁盘
- **双角色支持**：oi2.large ~ oi2.8xlarge 可同时作为 hot 和 warm 节点
- **同步复制到 S3**：数据写入后同步复制到 S3，提供高持久性
- **自动数据恢复**：节点故障时自动从 S3 恢复，无需手动干预

### 限制条件

- 必须启用 **encryption at rest**
- 专用主节点（Master Node）必须使用 **Graviton 实例**
- **刷新间隔**最低 10 秒（默认 10 秒）
- Indexing 仅在 primary shard 上执行，replica 从 S3 同步

## 动手实践

### Step 1: 创建多层存储 Domain

准备 domain 配置文件：

```json
// /tmp/opensearch-domain-config.json
{
  "DomainName": "multi-tier-test",
  "EngineVersion": "OpenSearch_3.5",
  "ClusterConfig": {
    "InstanceType": "oi2.large.search",
    "InstanceCount": 3,
    "DedicatedMasterEnabled": true,
    "DedicatedMasterType": "r6g.large.search",
    "DedicatedMasterCount": 3,
    "ZoneAwarenessEnabled": true,
    "ZoneAwarenessConfig": {
      "AvailabilityZoneCount": 3
    },
    "WarmEnabled": true,
    "WarmCount": 2,
    "WarmType": "oi2.large.search"
  },
  "EncryptionAtRestOptions": {
    "Enabled": true
  },
  "NodeToNodeEncryptionOptions": {
    "Enabled": true
  },
  "DomainEndpointOptions": {
    "EnforceHTTPS": true
  },
  "AdvancedSecurityOptions": {
    "Enabled": true,
    "InternalUserDatabaseEnabled": true,
    "MasterUserOptions": {
      "MasterUserName": "admin",
      "MasterUserPassword": "YourStrongP@ss1"
    }
  },
  "AccessPolicies": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"*\"},\"Action\":\"es:*\",\"Resource\":\"arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/multi-tier-test/*\"}]}",
  "IPAddressType": "ipv4"
}
```

关键配置说明：

- **Hot 节点**：3x `oi2.large.search` — 处理活跃数据的读写
- **Warm 节点**：2x `oi2.large.search` — 存储不太频繁访问的数据，**支持写入**
- **Master 节点**：3x `r6g.large.search` — 必须使用 Graviton 实例
- **Multi-AZ**：3 个可用区，提高可用性

!!! note "OI2 不需要 EBS"
    与 OR1/OR2/OM2 不同，OI2 使用本地 NVMe 存储，因此配置中不需要 `EBSOptions`。

创建 domain：

```bash
aws opensearch create-domain \
  --cli-input-json file:///tmp/opensearch-domain-config.json \
  --region us-east-1
```

等待 domain 变为 Active 状态（约 15-20 分钟）：

```bash
# 轮询检查状态
aws opensearch describe-domain \
  --domain-name multi-tier-test \
  --region us-east-1 \
  --query "DomainStatus.{Processing:Processing,Endpoint:Endpoint}"
```

### Step 2: 验证集群状态

Domain 就绪后，获取 endpoint 并检查集群健康状况：

```bash
ENDPOINT="https://$(aws opensearch describe-domain \
  --domain-name multi-tier-test \
  --region us-east-1 \
  --query 'DomainStatus.Endpoint' --output text)"

curl -s -u admin:'YourStrongP@ss1' "${ENDPOINT}/_cluster/health?pretty"
```

检查节点角色分布：

```bash
curl -s -u admin:'YourStrongP@ss1' \
  "${ENDPOINT}/_cat/nodes?v&h=name,node.role,heap.percent,ram.percent,cpu"
```

你会看到三种角色的节点：

- `dir` — Data + Ingest + Remote（Hot 节点）
- `irw` — Ingest + Remote + Warm（Warm 节点）
- `mr` — Master + Remote（Master 节点）

### Step 3: 向 Hot 层写入数据

创建测试索引并写入数据：

```bash
# 创建索引
curl -s -u admin:'YourStrongP@ss1' -X PUT \
  "${ENDPOINT}/log-data" \
  -H 'Content-Type: application/json' \
  -d '{
    "settings": {
      "index": {
        "number_of_shards": 1,
        "number_of_replicas": 1
      }
    },
    "mappings": {
      "properties": {
        "timestamp": {"type": "date"},
        "message": {"type": "text"},
        "level": {"type": "keyword"},
        "value": {"type": "float"}
      }
    }
  }'

# 批量写入数据
curl -s -u admin:'YourStrongP@ss1' -X POST \
  "${ENDPOINT}/log-data/_bulk" \
  -H 'Content-Type: application/json' \
  -d '
{"index":{}}
{"timestamp":"2026-03-28T08:00:00Z","message":"Application started","level":"INFO","value":42.5}
{"index":{}}
{"timestamp":"2026-03-28T08:01:00Z","message":"Memory usage high","level":"WARN","value":88.3}
{"index":{}}
{"timestamp":"2026-03-28T08:02:00Z","message":"Connection timeout","level":"ERROR","value":99.9}
'
```

!!! warning "OI2 的刷新间隔"
    OI2 实例的默认 refresh interval 为 **10 秒**（非标准的 1 秒），这是 OpenSearch Optimized 实例的设计特性。如果需要立即查看写入数据，使用 `_refresh` API：

    ```bash
    curl -s -u admin:'YourStrongP@ss1' -X POST "${ENDPOINT}/log-data/_refresh"
    ```

### Step 4: 配置 ISM 自动迁移到 Warm 层

创建 ISM（Index State Management）策略，将索引从 hot 自动迁移到 warm：

```bash
curl -s -u admin:'YourStrongP@ss1' -X PUT \
  "${ENDPOINT}/_plugins/_ism/policies/hot-to-warm" \
  -H 'Content-Type: application/json' \
  -d '{
    "policy": {
      "description": "Migrate indexes from hot to warm after 30 days",
      "default_state": "hot",
      "states": [
        {
          "name": "hot",
          "actions": [],
          "transitions": [
            {
              "state_name": "warm",
              "conditions": {
                "min_index_age": "30d"
              }
            }
          ]
        },
        {
          "name": "warm",
          "actions": [
            {
              "warm_migration": {},
              "retry": {
                "count": 5,
                "delay": "1h"
              }
            }
          ],
          "transitions": []
        }
      ]
    }
  }'
```

将策略附加到索引：

```bash
curl -s -u admin:'YourStrongP@ss1' -X POST \
  "${ENDPOINT}/_plugins/_ism/add/log-data" \
  -H 'Content-Type: application/json' \
  -d '{"policy_id": "hot-to-warm"}'
```

!!! tip "测试时使用短时间"
    测试时可以将 `min_index_age` 设为 `1m`（1 分钟）加速迁移。ISM 每 5-8 分钟运行一次检查，因此实际迁移可能需要 10-15 分钟。

查看迁移进度：

```bash
curl -s -u admin:'YourStrongP@ss1' -X GET \
  "${ENDPOINT}/_plugins/_ism/explain/log-data?pretty"
```

迁移完成后，验证索引已在 warm 层：

```bash
curl -s -u admin:'YourStrongP@ss1' \
  "${ENDPOINT}/log-data/_settings?flat_settings=true&pretty" | \
  grep -E "tiering.state|composite_store"
```

预期输出：

```
"index.composite_store.type" : "tiered-storage",
"index.tiering.state" : "WARM",
```

### Step 5: 向 Warm 层写入数据（核心新功能）

**这是与 UltraWarm 的关键区别**。在旧的 UltraWarm 中，以下写入操作会被拒绝。而在新的 OI2 multi-tier 架构中：

```bash
# 单文档写入 warm 层索引
curl -s -u admin:'YourStrongP@ss1' -X POST \
  "${ENDPOINT}/log-data/_doc" \
  -H 'Content-Type: application/json' \
  -d '{
    "timestamp": "2026-03-28T09:00:00Z",
    "message": "Written directly to warm tier!",
    "level": "INFO",
    "value": 99.99
  }'

# 批量写入 warm 层索引
curl -s -u admin:'YourStrongP@ss1' -X POST \
  "${ENDPOINT}/log-data/_bulk" \
  -H 'Content-Type: application/json' \
  -d '
{"index":{}}
{"timestamp":"2026-03-28T09:01:00Z","message":"Warm tier bulk write 1","level":"INFO","value":111.1}
{"index":{}}
{"timestamp":"2026-03-28T09:02:00Z","message":"Warm tier bulk write 2","level":"WARN","value":222.2}
{"index":{}}
{"timestamp":"2026-03-28T09:03:00Z","message":"Warm tier bulk write 3","level":"DEBUG","value":333.3}
'
```

✅ **写入成功！** 索引在 warm 层的状态不会改变（`tiering.state` 保持 `WARM`），数据正常写入并可查询。

## 测试结果

### 写入性能对比：Hot 层 vs Warm 层

| 场景 | Hot 层 | Warm 层 | 差异 |
|------|--------|---------|------|
| 5 docs bulk 写入 | 162ms | 91ms | - |
| 100 docs bulk 写入 | 131ms | 378ms | Warm 慢约 2.9x |
| 500 docs bulk 写入（avg 3次） | 158ms | 203ms | Warm 慢约 28% |
| 1000 docs bulk 写入 | - | 340ms | 无报错 |

**分析**：Warm 层写入在大批量场景下比 Hot 层慢约 20-50%。这是预期行为——warm 层数据需要额外的 S3 同步。随着 NVMe 缓存预热，差距会缩小。

### 查询性能对比：Hot 层 vs Warm 层

| 查询类型 | Hot 层（avg 3次） | Warm 层（avg 3次） | 差异 |
|---------|------------------|-------------------|------|
| match_all | 10.7ms | 14ms | 基本持平 |
| range query | 13.3ms | 8.7ms | Warm 更快（缓存命中） |
| aggregation | 20.7ms | 19ms | 基本持平 |

**关键发现**：**查询性能几乎无差异**。缓存预热后，hot 和 warm 层查询延迟均为个位数毫秒。这得益于 OI2 的本地 NVMe 缓存 + S3 同步架构。

### 核心功能验证

| 测试项 | 结果 | 说明 |
|--------|------|------|
| 创建 multi-tier domain | ✅ | 3 hot + 2 warm + 3 master |
| Hot 层写入查询 | ✅ | 正常 |
| ISM hot→warm 迁移 | ✅ | ~13 分钟完成 |
| **Warm 层写入** | ✅ | 单文档 + bulk 均成功 |
| 大量写入后状态保持 | ✅ | 1000+ 文档写入后仍为 WARM |
| 查询性能对比 | ✅ | 基本持平 |

## 踩坑记录

!!! warning "OI2 默认 refresh interval 为 10 秒"
    写入数据后不会立即可搜索，需等待 10 秒或手动调用 `_refresh`。这不是 bug，是 OpenSearch Optimized 实例的设计——更长的 refresh 间隔换来更高的写入吞吐。**已查文档确认。**

!!! warning "Writable Warm 功能标记为 experimental"
    集群设置中显示 `opensearch.experimental.feature.writable_warm_index.enabled = true`。虽然 AWS 已在 What's New 中正式发布此功能，但内部 feature flag 仍标记为 experimental。**实测发现，官方文档未明确记录此 flag。**

!!! warning "ISM 迁移时间不精确"
    ISM 策略的检查间隔为 5-8 分钟（含随机抖动），加上迁移本身的处理时间，从配置策略到迁移完成可能需要 10-15 分钟。生产环境中请合理规划时间窗口。**已查文档确认。**

## 费用明细

| 资源 | 单价（us-east-1） | 用量 | 费用 |
|------|-------------------|------|------|
| 3x oi2.large.search (hot) | ~$0.50/hr × 3 | ~5 hr | ~$7.50 |
| 2x oi2.large.search (warm) | ~$0.50/hr × 2 | ~5 hr | ~$5.00 |
| 3x r6g.large.search (master) | ~$0.17/hr × 3 | ~5 hr | ~$2.55 |
| Managed Storage (S3) | < $0.01 | < 10 GB | < $0.25 |
| **合计** | | | **~$15.30** |

## 清理资源

```bash
# 删除 OpenSearch domain
aws opensearch delete-domain \
  --domain-name multi-tier-test \
  --region us-east-1

# 确认删除完成（约 5-10 分钟）
aws opensearch describe-domain \
  --domain-name multi-tier-test \
  --region us-east-1 2>&1 | grep -q "ResourceNotFoundException" && \
  echo "Domain deleted successfully"
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。OpenSearch domain 按小时计费，忘记清理将持续产生费用。以本 Lab 配置为例，每天费用约 $72。

## 结论与建议

### 适用场景

1. **日志分析 / 可观测性**：日志数据按时间降温到 warm，但偶尔需要补写历史数据（如延迟到达的日志）
2. **安全分析**：安全事件数据迁移到 warm 后仍需更新威胁标签
3. **时序数据**：IoT / 指标数据的冷热分层，warm 层保持可写便于数据修正

### 对比 UltraWarm 的选择建议

| 场景 | 推荐方案 |
|------|---------|
| 数据完全只读，追求最低存储成本 | UltraWarm（更低实例成本） |
| 数据偶尔需要写入 / 更新 | **OI2 Multi-Tier Warm** |
| 需要自动故障恢复 | **OI2 Multi-Tier Warm** |
| 已有 OpenSearch 3.3+ 版本 | **OI2 Multi-Tier Warm** |

### 生产环境建议

1. **版本选择**：使用 OpenSearch 3.5（最新），获得最佳兼容性
2. **容量规划**：Warm 可寻址存储 = 本地 NVMe 缓存的 5 倍，按此比例规划
3. **ISM 策略**：根据业务数据访问模式设置合理的迁移阈值（如 30 天 / 90 天）
4. **监控指标**：关注 `WarmCPUUtilization`、`WarmJVMMemoryPressure`、`ReplicationLagMaxTime`
5. **写入优化**：warm 层推荐使用大 bulk size（10 MB），利用多客户端并行写入

## 参考链接

- [What's New: Writable Warm Tier on OpenSearch Optimized Instances](https://aws.amazon.com/about-aws/whats-new/2025/12/writeable-warm-tier-opensearch-optimized-instances/)
- [OpenSearch Optimized Instances 官方文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/or1.html)
- [UltraWarm Storage 官方文档](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/ultrawarm.html)
- [Index State Management (ISM)](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/ism.html)
- [OpenSearch Service 定价](https://aws.amazon.com/opensearch-service/pricing/)
