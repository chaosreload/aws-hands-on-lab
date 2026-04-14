---
tags:
  - Database
---

# Aurora DSQL Connectors 实战：用 Python 和 Node.js 三行代码连接分布式 SQL 数据库

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $0（Free Tier 内）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

Amazon Aurora DSQL 是 AWS 在 re:Invent 2024 推出的 serverless 分布式 SQL 数据库，兼容 PostgreSQL 16。和传统数据库不同，Aurora DSQL **完全基于 IAM 认证**——没有密码，只有时效 token。

这意味着每次连接都需要：

1. 用 AWS SDK 生成 IAM token
2. 把 token 当密码传给 PostgreSQL 驱动
3. 处理 token 过期、刷新等逻辑

对于应用开发者来说，这些「胶水代码」重复且易错。**Aurora DSQL Connectors** 的出现就是为了解决这个问题——它作为现有 PostgreSQL 驱动的认证插件层，自动处理 IAM token 的生成和管理。

本文通过实际创建 DSQL 集群，分别用手动 token 和 Connector 两种方式连接，**对比代码量和开发体验**，并测试边界条件。

## 前置条件

- AWS 账号（需要 `dsql:CreateCluster`、`dsql:DbConnectAdmin` 权限）
- AWS CLI v2 已配置
- Python 3.10+（测试 Python Connector）
- Node.js 20+（测试 Node.js Connector）
- psql（PostgreSQL 客户端，可选）

## 核心概念

### Aurora DSQL 认证流程

Aurora DSQL 不使用传统密码，所有连接都通过 IAM 认证：

| 概念 | 说明 |
|------|------|
| **认证 Token** | 用 AWS SigV4 签名生成的临时凭证，默认 15 分钟过期 |
| **Token 有效期** | 最短不限（实测 1 秒也可），最长 604,800 秒（7 天） |
| **Session 有效期** | 建立连接后 session 最长 1 小时，与 token 过期时间无关 |
| **Token 生成** | 纯本地操作（签名计算），不联系 AWS 服务端验证凭证 |

### Connectors 做了什么

Connectors 不是新的数据库驱动，而是现有驱动的**认证插件层**：

```
应用代码 → Connector（自动 token 生成） → PostgreSQL 驱动 → Aurora DSQL
```

### 支持的语言和驱动

Connectors 现已整合到统一 [monorepo](https://github.com/awslabs/aurora-dsql-connectors)，支持 **7 种语言**：

| 语言 | 包名 | 底层驱动 |
|------|------|---------|
| Python | `aurora-dsql-python-connector` | psycopg / psycopg2 / asyncpg |
| Node.js | `@aws/aurora-dsql-node-postgres-connector` | node-postgres (pg) |
| Node.js | `@aws/aurora-dsql-postgresjs-connector` | Postgres.js |
| Java | `aurora-dsql-jdbc-connector` | PostgreSQL JDBC |
| Go | `aurora-dsql-pgx-connector` | pgx |
| .NET | `Amazon.AuroraDsql.Npgsql` | Npgsql |
| Ruby | `aurora-dsql-ruby-pg` | pg |
| Rust | `aurora-dsql-sqlx-connector` | SQLx |

## 动手实践

### Step 1: 创建 DSQL 集群

```bash
# 创建单 Region 集群（禁用删除保护，方便测试后清理）
aws dsql create-cluster \
  --region us-east-1 \
  --no-deletion-protection \
  --tags '{"Project": "hands-on-lab", "Name": "dsql-connector-test"}'
```

输出示例：

```json
{
    "identifier": "75tuz24qa7x4gcr43ptp3ev4hu",
    "arn": "arn:aws:dsql:us-east-1:123456789012:cluster/75tuz24qa7x4gcr43ptp3ev4hu",
    "status": "CREATING",
    "endpoint": "75tuz24qa7x4gcr43ptp3ev4hu.dsql.us-east-1.on.aws"
}
```

集群创建几乎是瞬时的（秒级），等状态变为 `ACTIVE` 即可：

```bash
aws dsql get-cluster \
  --identifier <你的 cluster-id> \
  --region us-east-1
```

!!! tip "Endpoint 格式"
    Aurora DSQL 的 endpoint 格式为 `{cluster-id}.dsql.{region}.on.aws`，端口固定 `5432`，数据库名固定 `postgres`。

### Step 2: 基准测试——手动 Token + psql

先验证集群工作正常，用传统方式连接：

```bash
# 生成 admin token
export PGPASSWORD=$(aws dsql generate-db-connect-admin-auth-token \
  --hostname <your-endpoint>.dsql.us-east-1.on.aws \
  --region us-east-1)

# 用 psql 连接
psql "host=<your-endpoint>.dsql.us-east-1.on.aws \
  port=5432 dbname=postgres user=admin sslmode=require" \
  -c "SELECT version();"
```

```
     version
-----------------
 PostgreSQL 16
(1 row)
```

准备测试数据：

```bash
# ⚠️ DSQL 不支持单事务中多条 DDL，必须逐条执行
psql ... -c "CREATE SCHEMA IF NOT EXISTS test;"
psql ... -c "CREATE TABLE IF NOT EXISTS test.connectors_lab (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);"
psql ... -c "INSERT INTO test.connectors_lab (message) VALUES ('Hello from psql!');"
```

### Step 3: Python Connector 测试

安装依赖：

```bash
pip install aurora-dsql-python-connector "psycopg[binary,pool]" psycopg2-binary
```

#### 对比：手动 Token vs Connector

=== "手动 Token（~10 行认证代码）"

    ```python
    import boto3
    import psycopg2

    # 手动创建 session、生成 token、传递给驱动
    session = boto3.Session(region_name="us-east-1")
    dsql_client = session.client("dsql", region_name="us-east-1")
    token = dsql_client.generate_db_connect_admin_auth_token(
        "<your-endpoint>.dsql.us-east-1.on.aws", "us-east-1"
    )

    conn = psycopg2.connect(
        host="<your-endpoint>.dsql.us-east-1.on.aws",
        port=5432,
        dbname="postgres",
        user="admin",
        password=token,
        sslmode="require"
    )
    ```

=== "Connector（~3 行）"

    ```python
    import aurora_dsql_psycopg2 as dsql

    conn = dsql.connect(
        host="<your-endpoint>.dsql.us-east-1.on.aws",
        region="us-east-1",
        user="admin"
    )
    ```

**Connector 的核心优势**：不需要导入 boto3、不需要创建 session、不需要手动生成 token——全部自动处理。

#### psycopg（推荐，支持异步）

```python
import aurora_dsql_psycopg as dsql

conn = dsql.connect(
    host="<your-endpoint>.dsql.us-east-1.on.aws",
    region="us-east-1",
    user="admin"
)

with conn.cursor() as cur:
    cur.execute("SELECT version()")
    print(cur.fetchone()[0])  # PostgreSQL 16

    cur.execute("INSERT INTO test.connectors_lab (message) VALUES (%s)",
                ("Hello from psycopg connector!",))

conn.commit()
conn.close()
```

#### psycopg2（经典版）

```python
import aurora_dsql_psycopg2 as dsql

conn = dsql.connect(
    host="<your-endpoint>.dsql.us-east-1.on.aws",
    region="us-east-1",
    user="admin"
)

with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM test.connectors_lab")
    print(cur.fetchone()[0])

conn.commit()
conn.close()
```

### Step 4: Node.js Connector 测试

安装依赖：

```bash
npm install @aws/aurora-dsql-node-postgres-connector \
  @aws-sdk/credential-providers @aws-sdk/dsql-signer pg
```

#### Client 连接

```javascript
import { AuroraDSQLClient } from "@aws/aurora-dsql-node-postgres-connector";

const client = new AuroraDSQLClient({
  host: "<your-endpoint>.dsql.us-east-1.on.aws",
  user: "admin",
  region: "us-east-1",
});

await client.connect();
const result = await client.query("SELECT version()");
console.log(result.rows[0].version);  // PostgreSQL 16
await client.end();
```

#### Pool 连接（推荐生产环境）

```javascript
import { AuroraDSQLPool } from "@aws/aurora-dsql-node-postgres-connector";

const pool = new AuroraDSQLPool({
  host: "<your-endpoint>.dsql.us-east-1.on.aws",
  user: "admin",
  region: "us-east-1",
  max: 3,
  idleTimeoutMillis: 30000,
});

// 并发查询自动管理连接
const results = await Promise.all([
  pool.query("SELECT 1 as num"),
  pool.query("SELECT 2 as num"),
  pool.query("SELECT 3 as num"),
]);

await pool.end();
```

#### 指定 AWS Profile

```javascript
import { fromIni } from "@aws-sdk/credential-providers";

const client = new AuroraDSQLClient({
  host: "<your-endpoint>.dsql.us-east-1.on.aws",
  user: "admin",
  customCredentialsProvider: fromIni({ profile: "your-profile" }),
});
```

## 测试结果

### 连接时间对比

| 方式 | 连接时间 | 认证代码行数 | 额外依赖 |
|------|---------|------------|---------|
| 手动 Token + psycopg2 | 1.517s | ~10 行 | boto3 |
| Connector (psycopg) | 1.379s | ~3 行 | 无（内置） |
| Connector (psycopg2) | 1.339s | ~3 行 | 无（内置） |
| Connector (Node.js Client) | 1.331s | ~3 行 | @aws-sdk/credential-providers |
| Connector (Node.js Pool) | 1.574s* | ~5 行 | @aws-sdk/credential-providers |

*Pool 时间包含 3 个并发查询的首次连接建立。

**关键发现**：Connector 的连接时间与手动方式基本持平，但代码量减少约 **70%**。

### 边界条件测试

| 测试场景 | 结果 | 说明 |
|---------|------|------|
| `token_duration_secs=1` | ✅ 连接成功 | Token 只在建立连接时验证，之后 session 独立有效 |
| `token_duration_secs="1"` (字符串) | ❌ 类型错误 | 必须传 int，传 string 报 `'>' not supported between instances of 'str' and 'int'` |
| 错误 Region | ❌ 连接失败 | 报错 "Network is unreachable"，能定位问题 |
| 不存在的 AWS Profile | ❌ 凭证错误 | 报错 "config profile could not be found"，信息清晰 |

## 踩坑记录

!!! warning "DSQL 不支持单事务多 DDL"
    执行多条 DDL（如 `CREATE SCHEMA` + `CREATE TABLE`）时会报错：
    ```
    ERROR: multiple ddl statements not supported in a transaction
    ```
    **解决方案**：每条 DDL 单独执行。这是 Aurora DSQL 分布式架构的限制，已查文档确认。

!!! warning "token_duration_secs 必须传整数"
    Python Connector 的 `token_duration_secs` 参数必须传 `int` 类型（如 `900`），传 `str`（如 `"900"`）会抛出类型比较错误。实测发现，官方 README 示例中部分用了字符串格式，但建议始终使用整数。

!!! warning "Token 生成是本地操作"
    `generate-db-connect-admin-auth-token` 只做本地签名计算，**不会验证凭证是否有效**。凭证过期时 token 仍能生成，但连接会失败。所以 token 生成成功≠凭证有效。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Aurora DSQL Cluster | Free Tier: 1M Read/Write RU | 测试用量 < 100 RU | $0 |
| **合计** | | | **$0** |

Aurora DSQL Free Tier 包含每月 1M 读取请求单元 + 1M 写入请求单元 + 25GB 存储，对于测试和小型应用完全够用。

## 清理资源

```bash
# 删除测试数据（可选，删集群会一起清理）
PGPASSWORD=$(aws dsql generate-db-connect-admin-auth-token \
  --hostname <your-endpoint>.dsql.us-east-1.on.aws \
  --region us-east-1) \
psql "host=<your-endpoint>.dsql.us-east-1.on.aws \
  port=5432 dbname=postgres user=admin sslmode=require" \
  -c "DROP TABLE test.connectors_lab;"

psql ... -c "DROP SCHEMA test;"

# 删除 DSQL 集群
aws dsql delete-cluster \
  --identifier <your-cluster-id> \
  --region us-east-1
```

!!! danger "务必清理"
    虽然 DSQL 有 Free Tier，但超出额度后会按使用量计费。Lab 完成后请删除测试集群。

## 结论与建议

### Connectors 的价值

1. **代码量减少 70%**：3 行代码替代 10 行认证样板代码
2. **零 Token 管理**：不再需要关心 token 生成、过期、刷新
3. **驱动透明**：使用方式与原生 psycopg/node-postgres 几乎一致
4. **连接池内置支持**：自动处理池中连接的 token 刷新

### 适用场景

- ✅ **新项目接入 DSQL**：直接用 Connector 是最简路径
- ✅ **已有 PostgreSQL 代码迁移**：改动极小（换 import + connect 参数）
- ✅ **Serverless 架构**：Lambda + DSQL + Connector 是理想组合

### 生产环境建议

- 使用 **连接池**（Python 用 psycopg pool，Node.js 用 `AuroraDSQLPool`），避免频繁创建连接
- Session 最长 1 小时自动断开，确保应用有重连逻辑
- 优先用 IAM Role 而非 Profile/AccessKey，遵循最小权限原则
- `token_duration_secs` 保持默认 900 秒即可，不需要设太长

## 参考链接

- [Aurora DSQL Connectors 文档](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/SECTION_connectors.html)
- [Aurora DSQL Connectors Monorepo (GitHub)](https://github.com/awslabs/aurora-dsql-connectors)
- [Aurora DSQL 入门指南](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/getting-started.html)
- [Aurora DSQL 认证 Token 生成](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/SECTION_authentication-token.html)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/aurora-dsql-python-node-js-jdbc-connectors-iam/)
- [Aurora DSQL 定价](https://aws.amazon.com/rds/aurora/dsql/pricing/)
