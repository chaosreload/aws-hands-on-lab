---
description: "Test S3 Tables with Glue Data Catalog IAM-only authorization — simplified Iceberg table permissions without Lake Formation."
---
# S3 Tables + Glue Data Catalog：IAM-only 权限模式实测

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 20 分钟
    - **预估费用**: < $0.50
    - **Region**: us-east-1
    - **最后验证**: 2026-03-18

## 背景

S3 Tables 是 AWS 在 S3 上原生支持的 Apache Iceberg 表格式存储。之前要让 Athena、Redshift 等分析引擎访问 S3 Tables，必须配置 AWS Lake Formation 的细粒度权限 — 对简单场景来说太重了。

2026 年 3 月 17 日起，AWS Glue Data Catalog 支持 **IAM-based authorization** for S3 Tables：一个 IAM policy 搞定存储 + 目录 + 查询引擎的所有权限，不用碰 Lake Formation。需要细粒度控制时，可以随时 opt-in Lake Formation。

**一句话**：简单场景用 IAM，复杂场景再上 Lake Formation。

## 前置条件

- AWS 账号（需要 `s3tables:*`、`glue:CreateCatalog`、`glue:PassConnection`、`athena:*` 权限）
- AWS CLI v2（最新版本）

## 核心概念

### S3 Tables 到 Data Catalog 的映射

```
S3 Tables                       Glue Data Catalog
─────────                       ──────────────────
Table Bucket      ───映射为───>  Child Catalog
  └── Namespace   ───映射为───>  Database
       └── Table  ───映射为───>  Table
```

集成后，在 Athena 中的四级路径：`s3tablescatalog / bucket-name / namespace / table`

### IAM-only vs Lake Formation

| 维度 | IAM-only（新默认） | Lake Formation |
|------|-------------------|----------------|
| 配置复杂度 | 低（一个 IAM policy） | 高（LF admin + grants） |
| 权限粒度 | 资源级（bucket / namespace / table） | 列级、行级 |
| Credential vending | ❌ | ✅（第三方引擎也支持） |
| 适用场景 | 单团队、快速验证、简单分析 | 多团队、数据湖治理 |
| 迁移 | 可随时 opt-in LF | 可回退到 IAM-only |

## 动手实践

### Step 1: 创建 S3 Table Bucket

```bash
aws s3tables create-table-bucket \
  --name my-analytics-tables \
  --region us-east-1
```

输出：

```json
{
    "arn": "arn:aws:s3tables:us-east-1:ACCOUNT:bucket/my-analytics-tables"
}
```

### Step 2: 创建 Glue Data Catalog 集成（IAM-only 模式）

创建 `catalog.json`：

```json
{
  "Name": "s3tablescatalog",
  "CatalogInput": {
    "FederatedCatalog": {
      "Identifier": "arn:aws:s3tables:us-east-1:ACCOUNT:bucket/*",
      "ConnectionName": "aws:s3tables"
    },
    "CreateDatabaseDefaultPermissions": [
      {
        "Principal": {
          "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
        },
        "Permissions": ["ALL"]
      }
    ],
    "CreateTableDefaultPermissions": [
      {
        "Principal": {
          "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"
        },
        "Permissions": ["ALL"]
      }
    ],
    "AllowFullTableExternalDataAccess": "True"
  }
}
```

```bash
aws glue create-catalog --region us-east-1 --cli-input-json file://catalog.json
```

!!! tip "关键参数"
    `IAM_ALLOWED_PRINCIPALS` + `AllowFullTableExternalDataAccess: True` 是 IAM-only 模式的核心配置。这告诉 Data Catalog：不走 Lake Formation，用 IAM 控制访问。

验证：

```bash
aws glue get-catalog --catalog-id s3tablescatalog --region us-east-1
```

### Step 3: 创建 Namespace 和 Table

```bash
# 创建 namespace
aws s3tables create-namespace \
  --table-bucket-arn "arn:aws:s3tables:us-east-1:ACCOUNT:bucket/my-analytics-tables" \
  --namespace mydata
```

创建 `table-definition.json`：

```json
{
    "tableBucketARN": "arn:aws:s3tables:us-east-1:ACCOUNT:bucket/my-analytics-tables",
    "namespace": "mydata",
    "name": "orders",
    "format": "ICEBERG",
    "metadata": {
        "iceberg": {
            "schema": {
                "fields": [
                    {"name": "order_id", "type": "int", "required": true},
                    {"name": "order_date", "type": "date"},
                    {"name": "amount", "type": "double"},
                    {"name": "customer", "type": "string"}
                ]
            }
        }
    }
}
```

```bash
aws s3tables create-table --cli-input-json file://table-definition.json --region us-east-1
```

!!! warning "必须包含 metadata.iceberg.schema"
    如果省略 `metadata` 参数，表会缺少 Iceberg `metadata_location` 属性，INSERT 时会报错：
    ```
    ICEBERG_INVALID_METADATA: Table is missing [metadata_location] property
    ```

??? note "也可以用 Athena DDL 创建"
    ```bash
    aws athena start-query-execution \
      --query-string 'CREATE TABLE mydata.orders (
        order_id INT,
        order_date DATE,
        amount DOUBLE,
        customer STRING
      )' \
      --query-execution-context '{"Catalog": "s3tablescatalog/my-analytics-tables", "Database": "mydata"}' \
      --work-group primary
    ```
    Athena DDL 会自动设置 Iceberg metadata，无需手动指定 schema JSON。但注意：通过 Athena 创建的表会继承 Table Bucket 的默认加密设置，无法自定义。

### Step 4: 写入和查询数据

```bash
# 插入数据
aws athena start-query-execution \
  --query-string "INSERT INTO orders VALUES
    (1, DATE '2026-03-18', 99.99, 'alice'),
    (2, DATE '2026-03-17', 149.50, 'bob'),
    (3, DATE '2026-03-16', 29.99, 'charlie')" \
  --query-execution-context '{"Catalog": "s3tablescatalog/my-analytics-tables", "Database": "mydata"}' \
  --work-group primary

# 查询
aws athena start-query-execution \
  --query-string 'SELECT customer, SUM(amount) as total FROM orders GROUP BY customer ORDER BY total DESC' \
  --query-execution-context '{"Catalog": "s3tablescatalog/my-analytics-tables", "Database": "mydata"}' \
  --work-group primary
```

查询结果：

| customer | total |
|----------|-------|
| bob | 149.50 |
| alice | 99.99 |
| charlie | 29.99 |

✅ 全程只用 IAM 权限，没有配置任何 Lake Formation grants。

## 实测数据

| 操作 | 耗时 | 说明 |
|------|------|------|
| 创建 Table Bucket | < 1s | 即时 |
| 创建 s3tablescatalog | < 1s | 即时 |
| CREATE TABLE (Athena) | ~1s | Iceberg metadata 初始化 |
| INSERT 3 行 | ~2s | 写 Parquet + 更新 metadata |
| SELECT (聚合) | < 1s | 小数据量 |
| SHOW SCHEMAS | < 1s | Data Catalog 查询 |
| DESCRIBE TABLE | < 1s | 元数据查询 |

## 踩坑记录

!!! warning "S3 Tables API 建表必须带完整 schema"
    `aws s3tables create-table` 不带 `metadata.iceberg.schema` 时，表会缺少 Iceberg `metadata_location`。通过 Athena INSERT 会报错：
    ```
    ICEBERG_INVALID_METADATA: Table is missing [metadata_location] property
    ```
    **实测对比**：
    
    | 建表方式 | INSERT | SELECT |
    |---------|--------|--------|
    | `create-table` + 完整 schema | ✅ | ✅ |
    | `create-table` 不带 metadata | ❌ metadata_location 缺失 | ❌ |
    | Athena DDL `CREATE TABLE` | ✅ | ✅ |

!!! warning "大写名称自动转小写"
    文档说表名和列名必须全小写，否则查询会失败。但实测通过 Athena DDL 创建时：
    
    - `CREATE TABLE UpperCase (id INT, Name STRING)` → 自动转为 `uppercase` 表 + `name` 列
    - INSERT 和 SELECT 正常工作
    
    **但是**：直接用 S3 Tables API 创建大写名称可能会真的导致问题。建议始终使用小写。

!!! tip "Athena catalog 路径格式"
    在 `--query-execution-context` 中指定 catalog：
    ```json
    {"Catalog": "s3tablescatalog/bucket-name", "Database": "namespace"}
    ```
    用 `/` 分隔 catalog 和 bucket name，不是 `.`。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| S3 Tables 存储 | 几行测试数据 | < $0.01 |
| Athena 查询 | 几次查询 | < $0.01 |
| Glue Data Catalog | API 请求 | < $0.01 |
| S3 查询结果存储 | 临时文件 | < $0.01 |
| **合计** | | **< $0.05** |

## 清理资源

```bash
REGION=us-east-1
BUCKET_ARN="arn:aws:s3tables:us-east-1:ACCOUNT:bucket/my-analytics-tables"

# 删除表（通过 S3 Tables API）
aws s3tables delete-table --table-bucket-arn $BUCKET_ARN --namespace mydata --name orders --region $REGION

# 删除 namespace
aws s3tables delete-namespace --table-bucket-arn $BUCKET_ARN --namespace mydata --region $REGION

# 删除 table bucket
aws s3tables delete-table-bucket --table-bucket-arn $BUCKET_ARN --region $REGION

# 删除 Glue catalog
aws glue delete-catalog --catalog-id s3tablescatalog --region $REGION

# 删除 Athena workgroup（如果创建了专用的）
aws athena delete-work-group --work-group s3tables-lab --recursive-delete-option --region $REGION

# 删除 S3 结果桶
aws s3 rb s3://your-athena-results-bucket --force
```

!!! danger "务必清理"
    S3 Tables 按存储量和请求数计费。Lab 完成后删除所有资源。

## 结论与建议

### 适合场景

- **快速验证**：团队想试用 S3 Tables + Athena，不想配 Lake Formation
- **简单分析**：单团队 / 小项目，IAM role 就够用
- **渐进式架构**：先 IAM-only 跑起来，数据治理需求出来再 opt-in Lake Formation

### 不适合场景

- **多团队数据湖**：需要列级 / 行级权限 → 用 Lake Formation
- **跨账号共享**：Lake Formation 的 cross-account grants 更优雅
- **第三方引擎**：需要 credential vending → Lake Formation

### 使用建议

1. **新项目默认 IAM-only** — 简单够用，别过度设计
2. **表名列名全小写** — 虽然 Athena DDL 会自动转换，但养成好习惯
3. **建表时带完整 schema** — 无论用 Athena DDL 还是 S3 Tables API，都要确保表有完整的 Iceberg schema 定义
4. **每个 Region 只需集成一次** — 创建 `s3tablescatalog` 后，所有 table bucket 自动出现在 Data Catalog

## 参考链接

- [S3 Tables integration overview](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integration-overview.html)
- [Integrating S3 Tables with AWS analytics services](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-aws.html)
- [Glue Data Catalog federation with S3 Tables](https://docs.aws.amazon.com/glue/latest/dg/glue-federation-s3tables.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/gdc-simplified-permissions-s3tables-iceberg-views/)
