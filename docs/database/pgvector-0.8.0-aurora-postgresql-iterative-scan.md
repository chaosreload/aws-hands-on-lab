# Aurora PostgreSQL pgvector 0.8.0 实测：迭代索引扫描如何解决向量搜索 Overfiltering 问题

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $1-2（Aurora Serverless v2）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

在 RAG（检索增强生成）应用中，向量搜索通常需要配合业务条件过滤——比如"在某个产品类别中找最相似的文档"。但使用 HNSW 或 IVFFlat 近似索引时，过滤条件是在索引扫描**之后**应用的。如果匹配过滤条件的数据占比很低，初始索引扫描的候选结果可能全部被过滤掉，导致返回 **0 条结果**。这就是 **Overfiltering 问题**。

pgvector 0.8.0 引入了**迭代索引扫描（Iterative Index Scans）**，当初始扫描结果不够时，自动扩大扫描范围，直到找到足够的结果或达到配置的上限。

2025 年 4 月，Aurora PostgreSQL 正式支持 pgvector 0.8.0（PG 16.8, 15.12, 14.17, 13.20+）。本文通过实测验证这一特性在 Aurora 上的实际表现。

## 前置条件

- AWS 账号（需要 RDS、EC2 权限）
- AWS CLI v2 已配置
- psql 客户端（`apt install postgresql-client` 或等效方式）

## 核心概念

### pgvector 0.8.0 关键变更

| 特性 | 说明 |
|------|------|
| 迭代索引扫描 | 过滤后结果不足时自动扩大扫描范围 |
| strict_order 模式 | 结果严格按距离排序 |
| relaxed_order 模式 | 结果排序可能略有偏差，但 recall 更好 |
| max_scan_tuples | 控制迭代扫描的最大元组数（默认 20,000） |
| scan_mem_multiplier | 内存限制，基于 work_mem 的倍数（默认 1） |
| 改进的 cost estimation | 有 WHERE 过滤时更好地选择索引 |

### Overfiltering 问题图解

```
传统行为（iterative_scan = off）:
  HNSW Index Scan → 取 40 个最近邻（ef_search 默认值）
                   → 应用 WHERE category_id = 999
                   → 全部不匹配 → 返回 0 行 ❌

迭代扫描（iterative_scan = strict_order）:
  HNSW Index Scan → 取 40 个 → 过滤 → 不够
                   → 继续扫描更多 → 过滤 → 不够
                   → 继续... → 找到匹配行 → 返回结果 ✅
```

## 动手实践

### Step 1: 创建 Aurora PostgreSQL 集群

```bash
# 创建 DB 子网组（使用默认 VPC 的子网）
aws rds create-db-subnet-group \
  --db-subnet-group-name pgvector-test-subnet-group \
  --db-subnet-group-description "pgvector 0.8.0 testing" \
  --subnet-ids subnet-xxx subnet-yyy subnet-zzz \
  --region us-east-1

# 创建安全组（⚠️ 不要使用 0.0.0.0/0）
aws ec2 create-security-group \
  --group-name pgvector-test-sg \
  --description "pgvector Aurora test - restricted access" \
  --vpc-id vpc-xxx \
  --region us-east-1

# 只允许你的 IP 访问 5432
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxx \
  --protocol tcp --port 5432 \
  --cidr YOUR_IP/32 \
  --region us-east-1

# 创建 Aurora Serverless v2 集群
aws rds create-db-cluster \
  --db-cluster-identifier pgvector-test-cluster \
  --engine aurora-postgresql \
  --engine-version 16.8 \
  --master-username postgres \
  --master-user-password 'YourSecurePassword' \
  --db-subnet-group-name pgvector-test-subnet-group \
  --vpc-security-group-ids sg-xxx \
  --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=4 \
  --region us-east-1

# 创建实例
aws rds create-db-instance \
  --db-instance-identifier pgvector-test-instance \
  --db-instance-class db.serverless \
  --engine aurora-postgresql \
  --db-cluster-identifier pgvector-test-cluster \
  --publicly-accessible \
  --region us-east-1

# 等待实例就绪（约 10 分钟）
aws rds wait db-instance-available \
  --db-instance-identifier pgvector-test-instance \
  --region us-east-1
```

### Step 2: 安装 pgvector 并准备测试数据

```bash
# 连接数据库
PGPASSWORD='YourSecurePassword' psql -h YOUR_CLUSTER_ENDPOINT -U postgres -d postgres
```

```sql
-- 安装 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 确认版本
SELECT extversion FROM pg_extension WHERE extname='vector';
-- 预期输出: 0.8.0

-- 创建测试表：128 维向量 + 类别字段
CREATE TABLE items (
    id bigserial PRIMARY KEY,
    embedding vector(128),
    category_id int,
    title text
);

-- 插入 100,000 条随机向量数据（分批插入）
-- 100 个常见类别，每类约 500-1000 条
INSERT INTO items (embedding, category_id, title)
SELECT
    ('[' || string_agg(round(random()::numeric, 4)::text, ',') || ']')::vector(128),
    (random() * 99 + 1)::int,
    'item_' || gs.id
FROM generate_series(1, 100000) AS gs(id),
     LATERAL generate_series(1, 128) AS dim(d)
GROUP BY gs.id;

-- 插入稀有类别（仅 5 条，模拟极端过滤场景）
INSERT INTO items (embedding, category_id, title)
SELECT
    ('[' || string_agg(round(random()::numeric, 4)::text, ',') || ']')::vector(128),
    999,
    'rare_item_' || gs.id
FROM generate_series(1, 5) AS gs(id),
     LATERAL generate_series(1, 128) AS dim(d)
GROUP BY gs.id;

-- 创建 HNSW 索引
CREATE INDEX items_hnsw_cosine_idx ON items
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- 创建 IVFFlat 索引（用于对比）
CREATE INDEX items_ivfflat_cosine_idx ON items
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- 更新统计信息
ANALYZE items;

-- 确认数据
SELECT count(*) AS total, count(DISTINCT category_id) AS categories FROM items;
-- 预期: 100005 行, 101 个类别
```

### Step 3: 验证 Overfiltering 问题

```sql
-- 强制使用索引
SET enable_seqscan = off;

-- ===== 场景 A: iterative_scan = OFF（传统行为） =====
SET hnsw.iterative_scan = off;

EXPLAIN (ANALYZE, BUFFERS)
SELECT id, title, embedding <=> (SELECT embedding FROM items LIMIT 1) AS distance
FROM items
WHERE category_id = 999
ORDER BY embedding <=> (SELECT embedding FROM items LIMIT 1)
LIMIT 10;

-- 结果: 0 行！Index Scan 只扫描 40 个候选，全部被过滤掉
```

```sql
-- ===== 场景 B: iterative_scan = strict_order =====
SET hnsw.iterative_scan = strict_order;

EXPLAIN (ANALYZE, BUFFERS)
SELECT id, title, embedding <=> (SELECT embedding FROM items LIMIT 1) AS distance
FROM items
WHERE category_id = 999
ORDER BY embedding <=> (SELECT embedding FROM items LIMIT 1)
LIMIT 10;

-- 结果: 找到 2 行（扫描 ~12,000 个元组后找到）
-- 距离严格递增排序
```

```sql
-- ===== 场景 C: iterative_scan = relaxed_order =====
SET hnsw.iterative_scan = relaxed_order;

SELECT id, title, embedding <=> (SELECT embedding FROM items LIMIT 1) AS distance
FROM items
WHERE category_id = 999
ORDER BY embedding <=> (SELECT embedding FROM items LIMIT 1)
LIMIT 10;

-- 结果: 同样 2 行，但耗时略少
```

### Step 4: 测试边界条件

```sql
-- ===== max_scan_tuples 极小值 =====
SET hnsw.iterative_scan = strict_order;
SET hnsw.max_scan_tuples = 10;

SELECT id, title FROM items
WHERE category_id = 999
ORDER BY embedding <=> (SELECT embedding FROM items LIMIT 1)
LIMIT 10;

-- 结果: 0 行（迭代扫描提前终止）

-- ===== IVFFlat 对比 =====
SET ivfflat.iterative_scan = relaxed_order;
SET ivfflat.max_probes = 100;

SELECT id, title, embedding <=> (SELECT embedding FROM items LIMIT 1) AS distance
FROM items
WHERE category_id = 999
ORDER BY embedding <=> (SELECT embedding FROM items LIMIT 1)
LIMIT 10;

-- 结果: 5 行（全部找到！IVFFlat 在极端过滤下 recall 更好）
```

## 测试结果

### Overfiltering 对比（稀有类别: 5/100,005 = 0.005%）

| 配置 | 返回行数 | 扫描元组数 | Shared Buffers Hit | 耗时 |
|------|---------|-----------|-------------------|------|
| **OFF**（传统） | **0** ❌ | 40 | 2,028 | 1.6ms |
| **strict_order** | **2** ✅ | ~12,058 | 24,967 | 23.1ms |
| **relaxed_order** | **2** ✅ | ~12,233 | 25,142 | 21.7ms |
| max_scan_tuples=10 | **0** ❌ | ~1,841 | 3,820 | 3.4ms |
| 精确搜索 (seq scan) | **5** ✅ | 100,005 | 718 | 20.7ms |
| IVFFlat + relaxed | **5** ✅ | 100,005 | — | 77.3ms |

### 常见类别对比（category_id=1, ~524 条, 0.5%）

| 配置 | 返回行数 | 扫描元组数 | 耗时 |
|------|---------|-----------|------|
| OFF | **0** ❌ | 40 | 1.5ms |
| strict_order | **10** ✅ | ~1,821 | 12.2ms |

### 索引构建耗时（100K × 128维）

| 索引类型 | 参数 | 构建时间 |
|---------|------|---------|
| HNSW | m=16, ef_construction=128 | **63.1 秒** |
| IVFFlat | lists=100 | **1.4 秒** |

## 踩坑记录

!!! warning "HNSW 迭代扫描不保证找到所有匹配行"
    在极端稀有过滤场景（5/100K = 0.005%），HNSW iterative_scan 只找到 2/5 行（40% recall），即使把 max_scan_tuples 增大到 200,000 也没有改善。这是 HNSW **图结构的固有限制**——不是所有节点在图遍历中都可达。

    对于极低选择性过滤，建议使用 IVFFlat + `ivfflat.iterative_scan = relaxed_order` 或精确搜索。

    **状态**: 实测发现，官方文档未明确说明此行为。

!!! warning "ef_search 默认值决定初始扫描量"
    `hnsw.ef_search` 默认为 40，这意味着初始 HNSW 扫描只返回约 40 个最近邻候选。对于 **任何占比低于 ~1%** 的类别，不启用 iterative_scan 几乎必然返回 0 行。在 RAG 场景中，如果有 metadata 过滤，**强烈建议默认启用 iterative_scan**。

!!! tip "relaxed_order vs strict_order 怎么选"
    在我们的测试中，两者 recall 相同，relaxed 略快（~6%）。对于大多数应用场景，`relaxed_order` 是更好的默认选择。如果你需要严格的距离排序（如 top-K 精确排名），使用 `strict_order` 搭配 materialized CTE。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Aurora Serverless v2 | $0.12/ACU-hr | 0.5 ACU × ~2hr | ~$0.12 |
| 存储 | $0.10/GB-month | <100MB | <$0.01 |
| **合计** | | | **~$0.15** |

## 清理资源

```bash
# 1. 删除 DB 实例
aws rds delete-db-instance \
  --db-instance-identifier pgvector-test-instance \
  --skip-final-snapshot \
  --region us-east-1

# 2. 删除集群
aws rds delete-db-cluster \
  --db-cluster-identifier pgvector-test-cluster \
  --skip-final-snapshot \
  --region us-east-1

# 3. 等待集群删除完成
aws rds wait db-cluster-deleted \
  --db-cluster-identifier pgvector-test-cluster \
  --region us-east-1

# 4. 删除子网组
aws rds delete-db-subnet-group \
  --db-subnet-group-name pgvector-test-subnet-group \
  --region us-east-1

# 5. 检查安全组无残留 ENI
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=sg-xxx \
  --region us-east-1

# 6. 删除安全组
aws ec2 delete-security-group \
  --group-id sg-xxx \
  --region us-east-1
```

!!! danger "务必清理"
    Aurora Serverless v2 即使空闲也会按最低 ACU 计费。Lab 完成后请立即执行清理步骤。

## 结论与建议

### pgvector 0.8.0 迭代索引扫描实测总结

1. **Overfiltering 是真实且严重的**：不启用 iterative_scan，HNSW 索引 + WHERE 过滤在低选择性条件下直接返回 0 行
2. **iterative_scan 是 RAG 应用的必备配置**：任何使用 metadata 过滤的向量搜索都应该启用
3. **HNSW vs IVFFlat 选择取决于过滤场景**：
    - 常见过滤条件（>1% 数据匹配）→ HNSW + strict_order（快且可靠）
    - 极端稀有过滤（<0.1%）→ IVFFlat + relaxed_order（recall 更好）
    - 精确结果要求 → 精确搜索（seq scan）
4. **推荐的生产配置**：

```sql
-- 在应用连接初始化中设置
SET hnsw.iterative_scan = relaxed_order;
SET hnsw.ef_search = 100;  -- 提高初始候选数量
SET hnsw.max_scan_tuples = 50000;  -- 根据数据量调整
```

### 适用场景

- ✅ RAG 应用中的 metadata 过滤向量搜索（文档类型、日期范围、权限等）
- ✅ 多租户向量数据库（按 tenant_id 过滤）
- ✅ 电商推荐系统（按类别、价格区间过滤的相似商品搜索）

## 参考链接

- [AWS What's New: pgvector 0.8.0 on Aurora PostgreSQL](https://aws.amazon.com/about-aws/whats-new/2025/04/pgvector-0-8-0-aurora-postgresql/)
- [pgvector GitHub - Iterative Index Scans](https://github.com/pgvector/pgvector?tab=readme-ov-file#iterative-index-scans)
- [Aurora PostgreSQL 扩展版本](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html)
- [Using Aurora PostgreSQL as a Knowledge Base for Amazon Bedrock](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.VectorDB.html)
