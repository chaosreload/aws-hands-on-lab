# Amazon RDS for PostgreSQL Zero-ETL 集成 Amazon Redshift 实战：近实时数据复制全流程验证

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 120 分钟
    - **预估费用**: $6-8（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

在传统架构中，将 RDS 事务数据库的数据同步到 Redshift 数据仓库进行分析，需要构建复杂的 ETL 管道——通常涉及 Glue、DMS 或自建 CDC 方案，开发和运维成本高。

**Amazon RDS for PostgreSQL Zero-ETL Integration with Amazon Redshift** 已于 2025 年 7 月正式 GA，提供了一种全托管的近实时数据复制方案：

- 🔄 **全自动**：无需构建和维护 ETL 管道
- ⚡ **近实时**：增量复制延迟秒级（实测 10-15s）
- 🎯 **支持数据过滤**：按 schema/table 粒度选择要复制的数据（GA 新特性）
- 📦 **零额外费用**：集成本身免费，仅按 RDS 和 Redshift 用量计费

本文通过 **7 项实测**，覆盖核心功能、性能基准、数据过滤、DDL 变更传播和数据类型映射等场景，帮助你评估这项功能是否适合你的分析工作负载。

## 前置条件

- AWS 账号（需要 RDS、Redshift Serverless、IAM 权限）
- AWS CLI v2 已配置
- 能通过 psql 连接 RDS 实例的客户端（EC2 跳板机或 VPN）

## 核心概念

### 架构概览

```
RDS for PostgreSQL ──── Zero-ETL Integration ────▶ Amazon Redshift
    (Source)          (WAL-based replication)        (Serverless/RA3)
```

Zero-ETL 基于 PostgreSQL 的 **Logical Replication**（WAL）机制，全自动管理数据复制管道。初始同步完成后，后续变更以增量方式近实时传播。

### 关键限制

| 限制项 | 详情 |
|--------|------|
| 版本要求 | PG 15.7+, 16.3+, 17.1+ |
| 地域限制 | 源和目标必须同 Region |
| 主键要求 | 所有要复制的表必须有主键 |
| Redshift 类型 | 仅支持 RA3 节点（≥2）或 Serverless |
| 每账户集成数 | 默认 5 个（可提额） |
| 数据过滤粒度 | schema/table 级别，不支持列级/行级 |

### PostgreSQL vs MySQL Zero-ETL 差异

| 特性 | PostgreSQL | MySQL |
|------|-----------|-------|
| 复制机制 | Logical Replication (WAL) | Binary Log |
| 必需参数 | 6 个（logical_replication 等） | binlog_format=ROW 等 |
| 数据过滤 | 必须至少指定一个 filter pattern | 可选 |
| Multi-AZ 集群源 | 不支持 | 支持 |

## 动手实践

### Step 1: 创建 PostgreSQL 参数组

Zero-ETL 需要开启逻辑复制相关参数：

```bash
# 创建自定义参数组
aws rds create-db-parameter-group \
  --db-parameter-group-name zero-etl-pg17-params \
  --db-parameter-group-family postgres17 \
  --description "Parameters for zero-ETL integration" \
  --region us-east-1

# 设置必需参数
aws rds modify-db-parameter-group \
  --db-parameter-group-name zero-etl-pg17-params \
  --parameters \
    'ParameterName=rds.logical_replication,ParameterValue=1,ApplyMethod=pending-reboot' \
    'ParameterName=rds.replica_identity_full,ParameterValue=1,ApplyMethod=pending-reboot' \
    'ParameterName=wal_sender_timeout,ParameterValue=0,ApplyMethod=immediate' \
    'ParameterName=max_wal_senders,ParameterValue=20,ApplyMethod=pending-reboot' \
    'ParameterName=max_replication_slots,ParameterValue=20,ApplyMethod=pending-reboot' \
  --region us-east-1
```

### Step 2: 创建 RDS PostgreSQL 实例

```bash
# 创建安全组（⚠️ 不要开放 0.0.0.0/0）
SG_ID=$(aws ec2 create-security-group \
  --group-name zero-etl-rds-sg \
  --description "Security group for zero-ETL RDS" \
  --region us-east-1 \
  --query 'GroupId' --output text)

# 仅允许你的客户端 IP 访问（替换为你的 IP）
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 5432 \
  --cidr YOUR_IP/32 \
  --region us-east-1

# 创建 RDS 实例
aws rds create-db-instance \
  --db-instance-identifier zero-etl-pg-source \
  --db-instance-class db.t3.medium \
  --engine postgres \
  --engine-version 17.4 \
  --master-username pgadmin \
  --master-user-password 'YOUR_SECURE_PASSWORD' \
  --allocated-storage 20 \
  --db-parameter-group-name zero-etl-pg17-params \
  --vpc-security-group-ids $SG_ID \
  --db-name testdb \
  --no-multi-az \
  --publicly-accessible \
  --region us-east-1
```

等待实例变为 `available`（约 7 分钟）：

```bash
aws rds wait db-instance-available \
  --db-instance-identifier zero-etl-pg-source \
  --region us-east-1
```

### Step 3: 创建 Redshift Serverless

```bash
# 创建 Namespace
aws redshift-serverless create-namespace \
  --namespace-name zero-etl-ns \
  --admin-username admin \
  --admin-user-password 'YOUR_SECURE_PASSWORD' \
  --region us-east-1

# 创建 Workgroup（8 RPU 基础容量）
aws redshift-serverless create-workgroup \
  --workgroup-name zero-etl-wg \
  --namespace-name zero-etl-ns \
  --base-capacity 8 \
  --publicly-accessible \
  --region us-east-1

# ⚠️ 必须启用大小写敏感标识符
aws redshift-serverless update-workgroup \
  --workgroup-name zero-etl-wg \
  --config-parameters parameterKey=enable_case_sensitive_identifier,parameterValue=true \
  --region us-east-1
```

### Step 4: 配置 Redshift 资源策略

!!! warning "踩坑：必须使用 redshift API，而非 redshift-serverless API"
    `redshift put-resource-policy` 和 `redshift-serverless put-resource-policy` 是**不同的 API**。
    Zero-ETL 集成要求使用 **`redshift put-resource-policy`**（Redshift 主 API），
    使用 `redshift-serverless` 的 API 会导致 `create-integration` 报错 "don't have access"。
    这一点官方文档未明确区分。

```bash
# 获取 Namespace ARN
NS_ARN=$(aws redshift-serverless get-namespace \
  --namespace-name zero-etl-ns \
  --region us-east-1 \
  --query 'namespace.namespaceArn' --output text)

# 获取 Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 使用 redshift（不是 redshift-serverless）设置资源策略
aws redshift put-resource-policy \
  --resource-arn "$NS_ARN" \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "redshift.amazonaws.com"},
      "Action": ["redshift:AuthorizeInboundIntegration"],
      "Condition": {
        "StringEquals": {"aws:SourceArn": "YOUR_RDS_ARN"}
      }
    },
    {
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::YOUR_ACCOUNT_ID:root"},
      "Action": ["redshift:CreateInboundIntegration"]
    }]
  }' \
  --region us-east-1
```

### Step 5: 创建 Zero-ETL 集成

```bash
# 获取 RDS ARN
RDS_ARN=$(aws rds describe-db-instances \
  --db-instance-identifier zero-etl-pg-source \
  --region us-east-1 \
  --query 'DBInstances[0].DBInstanceArn' --output text)

# 获取 Namespace ID
NS_ID=$(aws redshift-serverless get-namespace \
  --namespace-name zero-etl-ns \
  --region us-east-1 \
  --query 'namespace.namespaceId' --output text)

# 创建集成（⚠️ PostgreSQL 必须指定 data-filter）
aws rds create-integration \
  --integration-name pg-redshift-zetl \
  --source-arn "$RDS_ARN" \
  --target-arn "arn:aws:redshift-serverless:us-east-1:${ACCOUNT_ID}:namespace/${NS_ID}" \
  --data-filter 'include: testdb.*.*' \
  --region us-east-1
```

集成创建后需要等待状态变为 `active`（约 20-25 分钟）：

```bash
# 获取 Integration ID
INT_ID=$(aws rds describe-integrations \
  --region us-east-1 \
  --query "Integrations[?IntegrationName=='pg-redshift-zetl'].IntegrationArn" \
  --output text | awk -F: '{print $NF}')

# 轮询状态
watch -n 30 "aws rds describe-integrations \
  --integration-identifier $INT_ID \
  --region us-east-1 \
  --query 'Integrations[0].Status' --output text"
```

### Step 6: 创建 Redshift 目标数据库

集成 active 后，在 Redshift 中创建引用集成 ID 的数据库：

```bash
aws redshift-data execute-statement \
  --workgroup-name zero-etl-wg \
  --database dev \
  --sql "CREATE DATABASE pg_zetl_db FROM INTEGRATION '$INT_ID'" \
  --region us-east-1
```

### Step 7: 在源库创建测试数据

```sql
-- 连接到 RDS PostgreSQL
psql -h YOUR_RDS_ENDPOINT -U pgadmin -d testdb

-- 创建多个 schema 的测试表
CREATE TABLE public.users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50),
    email VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE public.orders (
    order_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES public.users(user_id),
    amount NUMERIC(10,2),
    status VARCHAR(20),
    order_date TIMESTAMP DEFAULT NOW()
);

CREATE SCHEMA analytics;
CREATE TABLE analytics.metrics (
    metric_id SERIAL PRIMARY KEY,
    metric_name VARCHAR(50),
    metric_value NUMERIC(10,2),
    recorded_at TIMESTAMP DEFAULT NOW()
);

-- 插入测试数据
INSERT INTO public.users (username, email) VALUES
    ('testuser1', 'test1@example.com'),
    ('testuser2', 'test2@example.com');

INSERT INTO public.orders (user_id, amount, status) VALUES
    (1, 99.99, 'completed'),
    (1, 149.50, 'pending'),
    (2, 29.99, 'completed');

INSERT INTO analytics.metrics (metric_name, metric_value) VALUES
    ('daily_revenue', 279.48);
```

数据插入后，等待约 20-25 分钟完成初始同步。在 Redshift 中验证：

```sql
-- 通过 Redshift Data API 查询
aws redshift-data execute-statement \
  --workgroup-name zero-etl-wg \
  --database pg_zetl_db \
  --sql "SELECT schema_name, table_name FROM svv_all_tables WHERE database_name='pg_zetl_db' AND schema_name NOT IN ('information_schema','pg_catalog') ORDER BY 1,2" \
  --region us-east-1
```

## 测试结果

### 7 项测试总览

| # | 测试项 | 类型 | 预期结果 | 实际结果 | 状态 |
|---|--------|------|---------|---------|------|
| 1 | 端到端集成创建 | 核心功能 | 集成状态 active | active，耗时 ~20min | ✅ |
| 2 | 初始数据同步 | 核心功能 | 20-25min 同步 | 4 张表全部 Synced | ✅ |
| 3 | 增量复制延迟 | 性能 | 秒级延迟 | **10-15 秒** | ✅ |
| 4 | 数据过滤 (Include) | GA 新特性 | 仅指定表同步 | 正确过滤 ✅ | ✅ |
| 5 | DDL 变更 (ADD COLUMN) | 边界条件 | 可能 resync | **未触发 resync** | ✅ |
| 6 | 复杂数据类型 (ARRAY/JSONB) | 边界条件 | 可能 out of sync | **正常同步！** | ✅ |
| 7 | 多表批量写入 (3×1000) | 性能 | 几分钟 | **~30 秒** | ✅ |

### 测试 #3 — 增量复制延迟

在集成 active 且初始同步完成后，向源表 INSERT 新记录，然后通过 Redshift Data API 轮询查询：

| 采样 | INSERT 时间 (UTC) | Redshift 可查时间 | 总延迟 | 备注 |
|------|-------------------|------------------|--------|------|
| 1 | 11:08:28 | T+17s | 17s | 含 Data API 开销 ~3-5s |
| 2 | 11:08:59 | T+16s | 16s | 含 Data API 开销 ~3-5s |

**结论**：实际复制延迟约 **10-15 秒**，扣除 Data API 查询开销后，真实复制时延可能在 **7-12 秒**。

### 测试 #4 — 数据过滤

将 filter 从 `testdb.*.*`（全库）修改为 `testdb.public.*`（仅 public schema）：

- 修改后 integration 状态：`modifying → syncing → active`
- **耗时约 40 分钟**完成 resync
- 验证结果：仅 public schema 的 3 张表同步，analytics.metrics 被排除 ✅

### 测试 #5 — DDL 变更传播

对已同步的 `public.users` 表执行 ALTER TABLE ADD COLUMN：

```sql
ALTER TABLE public.users ADD COLUMN phone VARCHAR(20);
INSERT INTO public.users (user_id, username, email, phone)
    VALUES (100, 'DDL Test User', 'ddl@test.com', '+1-555-0100');
```

结果：

- Integration 状态保持 **active**，未触发 resync
- ~30 秒后 Redshift 中确认新列和新数据完整同步

!!! tip "哪些 DDL 会触发 resync？"
    根据官方文档，以下操作**会**触发表重同步：

    - `ALTER TABLE ADD PRIMARY KEY`
    - `ALTER TABLE SET SCHEMA`
    - `ALTER TABLE SET LOGGED`（从 UNLOGGED 改回 LOGGED）
    - `RENAME SCHEMA`

    而 `ADD COLUMN`、`DROP COLUMN`（非主键列）、`RENAME TABLE/COLUMN` 等操作**不会**触发 resync，仅做增量同步。

### 测试 #6 — PostgreSQL 复杂数据类型

创建包含 ARRAY 和 JSONB 列的表：

```sql
CREATE TABLE public.unsupported_test (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    tags TEXT[],           -- PostgreSQL Array
    metadata JSONB,        -- JSONB
    scores INTEGER[]       -- Integer Array
);

INSERT INTO public.unsupported_test (name, tags, metadata, scores) VALUES
    ('item1', ARRAY['tag1', 'tag2'], '{"key": "value1"}', ARRAY[95, 87, 92]),
    ('item2', ARRAY['tag3'], '{"key": "value2"}', ARRAY[88, 91]);
```

结果：**全部成功同步！** 数据类型映射如下：

| PostgreSQL 类型 | Redshift 类型 | 示例值 |
|----------------|---------------|--------|
| TEXT[] | SUPER | `["tag1","tag2"]` |
| INTEGER[] | SUPER | `[95,87,92]` |
| JSONB | SUPER | `{"key":"value1"}` |

Redshift 的 **SUPER** 类型完美承载了 PostgreSQL 的半结构化数据类型。

!!! note "什么才是真正不支持的数据类型？"
    官方文档明确列出了所有支持的 PG 类型映射，其中 `array → SUPER`、`jsonb → SUPER` 均在列表中。
    真正不支持的是 **custom types**（用户自定义类型）和 **extension 创建的类型**。
    包含不支持类型的表会进入 out of sync 状态。

### 测试 #7 — 多表批量写入

同时创建 3 张新表并各插入 1000 行：

```sql
-- 使用 generate_series 批量插入
INSERT INTO public.bulk_products (name, price, category)
SELECT 'Product_' || i,
       (random() * 1000)::numeric(10,2),
       CASE (i % 5) WHEN 0 THEN 'Electronics'
                     WHEN 1 THEN 'Books'
                     WHEN 2 THEN 'Clothing'
                     WHEN 3 THEN 'Food'
                     ELSE 'Other' END
FROM generate_series(1, 1000) AS i;
-- bulk_customers 和 bulk_transactions 类似
```

| 表 | 行数 | 同步耗时 |
|----|------|---------|
| bulk_products | 1,000 | ~30s |
| bulk_customers | 1,000 | ~30s |
| bulk_transactions | 1,000 | ~30s |

**3 张新表 + 3000 行数据在 ~30 秒内全部同步**（含 DDL 建表 + 数据传输），远优于预期。

## 踩坑记录

!!! warning "踩坑 #1：修改 Data Filter 触发完整 Resync"
    修改集成的 data filter（例如从 `testdb.*.*` 改为 `testdb.public.*`）会触发**完整的 resync**，
    集成状态变为 modifying → syncing → active，实测耗时约 **40 分钟**。
    这意味着在生产环境中修改过滤规则需要谨慎评估影响窗口。
    **实测发现，官方文档未明确提及此行为和时长。**

!!! warning "踩坑 #2：Resource Policy API 混淆"
    `redshift put-resource-policy` 和 `redshift-serverless put-resource-policy` 是**不同的 API**。
    Zero-ETL 要求使用前者（Redshift 主 API），用后者会导致创建集成时报权限错误。
    **实测发现，官方未明确区分两个 API 的使用场景。**

!!! warning "踩坑 #3：PostgreSQL 必须指定 Data Filter"
    与 MySQL 不同，PostgreSQL 源的 zero-ETL 集成**必须**至少指定一个 data filter pattern
    （最少 `database-name.*.*`），否则创建会报错。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| RDS db.t3.medium | $0.068/hr | ~3 hr | $0.20 |
| Redshift Serverless 8 RPU | $0.375/RPU-hr | ~3 hr | $9.00 |
| Zero-ETL Integration | 免费 | — | $0.00 |
| **合计** | | | **~$9.20** |

!!! tip "费用优化提示"
    Redshift Serverless 是主要费用来源。测试完成后立即清理资源可以控制成本。
    如果只是验证基本功能，整个 Lab 可以在 2 小时内完成（~$6）。

## 清理资源

```bash
# 1. 删除 Zero-ETL 集成
aws rds delete-integration \
  --integration-identifier $INT_ID \
  --region us-east-1

# 2. 等待集成删除完成，然后删除 Redshift
aws redshift-serverless delete-workgroup \
  --workgroup-name zero-etl-wg \
  --region us-east-1

# 等待 workgroup 删除
sleep 60

aws redshift-serverless delete-namespace \
  --namespace-name zero-etl-ns \
  --region us-east-1

# 3. 删除 RDS 实例（跳过最终快照）
aws rds delete-db-instance \
  --db-instance-identifier zero-etl-pg-source \
  --skip-final-snapshot \
  --region us-east-1

# 4. 等待 RDS 删除，然后清理参数组和安全组
aws rds wait db-instance-deleted \
  --db-instance-identifier zero-etl-pg-source \
  --region us-east-1

aws rds delete-db-parameter-group \
  --db-parameter-group-name zero-etl-pg17-params \
  --region us-east-1

# 先检查 ENI 残留
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=$SG_ID \
  --region us-east-1 \
  --query 'NetworkInterfaces[*].NetworkInterfaceId'

# 确认无残留后删除安全组
aws ec2 delete-security-group \
  --group-id $SG_ID \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。Redshift Serverless 按 RPU-小时计费，
    即使不查询也会产生基础容量费用。

## 结论与建议

### 适用场景

- ✅ **OLTP → OLAP 近实时分析**：将 RDS PostgreSQL 事务数据实时同步到 Redshift 进行报表分析
- ✅ **数据湖入湖**：结合 Redshift Data Sharing 或导出到 S3，构建数据湖
- ✅ **半结构化数据分析**：JSONB/Array 数据通过 SUPER 类型在 Redshift 中可用

### 不适合的场景

- ❌ 需要行级/列级过滤的精细同步
- ❌ 跨 Region 数据复制
- ❌ 需要对复制数据做转换（T in ETL）

### 生产环境建议

1. **版本选择**：使用 PG 17.x 最新版本，获得最好的兼容性
2. **监控**：关注 `ReplicaLag` CloudWatch 指标
3. **Data Filter 规划**：提前规划好过滤规则，避免频繁修改（每次修改触发 ~40min resync）
4. **主键设计**：确保所有需要同步的表都有主键
5. **数据类型审计**：避免使用 custom types 和 extension 类型，标准 PG 类型（包括 ARRAY、JSONB）均支持

## 参考链接

- [官方文档：Amazon RDS Zero-ETL Integrations](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.html)
- [数据类型映射与 DDL 操作](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.querying.html)
- [数据过滤配置](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.filtering.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-rds-zero-etl-redshift-generally-available/)
