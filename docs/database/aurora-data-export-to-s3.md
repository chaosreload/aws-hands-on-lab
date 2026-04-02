# Aurora MySQL 数据导出到 S3 实测对比：Export Cluster Data vs Zero-ETL

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $2-5（含清理）
    - **Region**: ap-southeast-1（可替换）
    - **最后验证**: 2026-04-02

## 背景

客户经常问：**"Aurora MySQL 的数据怎么导出到 S3 做分析？"** AWS 提供了两种截然不同的方案：

1. **Export Cluster Data to S3** — 一次性快照导出，生成 Parquet 文件，通过 Athena 查询
2. **Zero-ETL Integration → Redshift** — 近实时 CDC 同步，数据持续流入 Redshift（也可通过 SageMaker Lakehouse 存为 Iceberg）

两种方案的配置复杂度、数据实时性、成本模型完全不同。本文通过同一个 Aurora MySQL Serverless v2 集群，**实测对比两种方案的全量导出、选择性导出、查询体验和 CDC 延迟**，帮你做出正确选型。

## 前置条件

- AWS 账号（需要 RDS、S3、KMS、IAM、Athena、Redshift Serverless 权限）
- AWS CLI v2 已配置
- 一个可以连接 Aurora 的环境（EC2 跳板机 / Cloud9 / SSM）

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "rds:*", "s3:*", "kms:*", "iam:*",
        "athena:*", "glue:*", "redshift-serverless:*",
        "redshift:*", "redshift-data:*", "lakeformation:*",
        "ssm:*", "ec2:*", "secretsmanager:*"
      ],
      "Resource": "*"
    }
  ]
}
```

生产环境请收窄权限范围。

</details>

## 核心概念

### 方案对比一览

| 维度 | Export Cluster Data | Zero-ETL → Redshift |
|------|-------------------|---------------------|
| **机制** | Clone DB → 提取 Parquet → 写入 S3 | 基于 Enhanced Binlog 的 CDC 管道 |
| **数据格式** | Apache Parquet（gz 压缩） | Redshift 原生表（可选 Iceberg via Lakehouse） |
| **实时性** | 一次性快照（point-in-time） | 近实时（秒级 CDC） |
| **查询引擎** | Athena / Redshift Spectrum | Redshift SQL |
| **配置项** | 3 个（S3 + IAM Role + KMS） | 6+（Parameter Group + Reboot + Redshift + Resource Policy + CREATE DATABASE） |
| **对源影响** | 零（Clone 后导出） | 需要 Enhanced Binlog（额外写放大） |
| **选择性导出** | `--export-only database.table` | Data Filter `include:/exclude:` |
| **定价** | 按导出数据量（¢/GB） | CDC 变更处理量 + Redshift 计算 |
| **适用场景** | 定期归档、一次性迁移 | 实时报表、持续分析 |

### Export Cluster Data 工作流

```
Aurora Cluster → [Clone] → [Extract to Parquet] → S3 Bucket → Athena Query
                  ~10min        ~3min               Parquet files
```

### Zero-ETL 工作流

```
Aurora Cluster → [Enhanced Binlog] → [CDC Pipeline] → Redshift Serverless → SQL Query
                   always-on           near-realtime     auto-replicated
```

## 动手实践

### Step 1: 创建共享 Aurora MySQL 集群

我们创建一个 Aurora MySQL Serverless v2 集群，**同时配置 Zero-ETL 所需的 Enhanced Binlog 参数**（Export Cluster Data 不需要这些参数，但提前配置不影响它的使用）。

```bash
# 变量定义
export REGION="ap-southeast-1"
export PROFILE="your-profile"  # 替换为你的 AWS Profile
export ACCOUNT_ID="123456789012"  # 替换为你的账号 ID

# 1. 创建 DB 子网组（使用默认 VPC 的子网）
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters "Name=default-for-az,Values=true" \
  --query 'Subnets[].SubnetId' --output text \
  --region $REGION --profile $PROFILE)

aws rds create-db-subnet-group \
  --db-subnet-group-name aurora-export-test-subnet \
  --db-subnet-group-description "Aurora export test" \
  --subnet-ids $SUBNET_IDS \
  --region $REGION --profile $PROFILE

# 2. 创建安全组（仅允许 VPC 内访问，绝不开放 0.0.0.0/0）
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' --output text \
  --region $REGION --profile $PROFILE)
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids $VPC_ID \
  --query 'Vpcs[0].CidrBlock' --output text \
  --region $REGION --profile $PROFILE)

SG_ID=$(aws ec2 create-security-group \
  --group-name aurora-export-test-sg \
  --description "Aurora export test - VPC internal only" \
  --vpc-id $VPC_ID \
  --region $REGION --profile $PROFILE \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID --protocol tcp --port 3306 \
  --cidr $VPC_CIDR \
  --region $REGION --profile $PROFILE

# 3. 创建参数组（含 Zero-ETL 必需的 Enhanced Binlog）
aws rds create-db-cluster-parameter-group \
  --db-cluster-parameter-group-name aurora-export-test-params \
  --db-parameter-group-family aurora-mysql8.0 \
  --description "Zero-ETL enabled params" \
  --region $REGION --profile $PROFILE

aws rds modify-db-cluster-parameter-group \
  --db-cluster-parameter-group-name aurora-export-test-params \
  --parameters \
    "ParameterName=aurora_enhanced_binlog,ParameterValue=1,ApplyMethod=pending-reboot" \
    "ParameterName=binlog_backup,ParameterValue=0,ApplyMethod=pending-reboot" \
    "ParameterName=binlog_format,ParameterValue=ROW,ApplyMethod=pending-reboot" \
    "ParameterName=binlog_replication_globaldb,ParameterValue=0,ApplyMethod=pending-reboot" \
    "ParameterName=binlog_row_image,ParameterValue=full,ApplyMethod=pending-reboot" \
    "ParameterName=binlog_row_metadata,ParameterValue=full,ApplyMethod=pending-reboot" \
  --region $REGION --profile $PROFILE

# 4. 创建 Aurora MySQL Serverless v2 集群
aws rds create-db-cluster \
  --db-cluster-identifier aurora-export-test \
  --engine aurora-mysql \
  --engine-version 8.0.mysql_aurora.3.08.0 \
  --master-username admin \
  --master-user-password 'YourStrongPassword!' \
  --db-subnet-group-name aurora-export-test-subnet \
  --vpc-security-group-ids $SG_ID \
  --db-cluster-parameter-group-name aurora-export-test-params \
  --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=2 \
  --storage-encrypted \
  --region $REGION --profile $PROFILE

# 5. 创建 Serverless v2 实例
aws rds create-db-instance \
  --db-instance-identifier aurora-export-test-instance-1 \
  --db-cluster-identifier aurora-export-test \
  --db-instance-class db.serverless \
  --engine aurora-mysql \
  --region $REGION --profile $PROFILE

# 等待可用
aws rds wait db-instance-available \
  --db-instance-identifier aurora-export-test-instance-1 \
  --region $REGION --profile $PROFILE

# 6. Reboot 使 binlog 参数生效
aws rds reboot-db-instance \
  --db-instance-identifier aurora-export-test-instance-1 \
  --region $REGION --profile $PROFILE

aws rds wait db-instance-available \
  --db-instance-identifier aurora-export-test-instance-1 \
  --region $REGION --profile $PROFILE
```

### Step 2: 灌入测试数据

连接 Aurora 集群（通过 VPC 内的 EC2 或 SSM），创建 3 个数据库共 7 张表：

```sql
-- Database 1: ecommerce_db（电商场景）
CREATE DATABASE ecommerce_db;
USE ecommerce_db;

CREATE TABLE customers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100), email VARCHAR(100), city VARCHAR(50),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT, order_date DATETIME,
  total_amount DECIMAL(10,2), status VARCHAR(20), notes TEXT
);
CREATE TABLE products (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(100), category VARCHAR(50),
  price DECIMAL(10,2), stock INT
);

-- Database 2: analytics_db（分析场景）
CREATE DATABASE analytics_db;
USE analytics_db;

CREATE TABLE page_views (
  id INT AUTO_INCREMENT PRIMARY KEY,
  page_url VARCHAR(200), visitor_id VARCHAR(36),
  view_time DATETIME, duration_seconds INT, referrer VARCHAR(200)
);
CREATE TABLE sessions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(36), user_agent VARCHAR(200),
  ip_address VARCHAR(45), start_time DATETIME, end_time DATETIME
);

-- Database 3: hr_db（HR 场景）
CREATE DATABASE hr_db;
USE hr_db;

CREATE TABLE departments (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50), budget DECIMAL(12,2));
CREATE TABLE employees (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), department_id INT, salary DECIMAL(10,2), hire_date DATE, is_active TINYINT(1));

-- 灌入测试数据（省略详细 INSERT 语句，完整脚本见 GitHub）
-- customers: 500 rows, orders: 1000 rows, products: 200 rows
-- page_views: 5000 rows, sessions: 2000 rows
-- departments: 10 rows, employees: 100 rows
```

**实测数据量**：

| 数据库.表 | 行数 |
|-----------|------|
| ecommerce_db.customers | 500 |
| ecommerce_db.orders | 1,000 |
| ecommerce_db.products | 200 |
| analytics_db.page_views | 5,000 |
| analytics_db.sessions | 2,000 |
| hr_db.departments | 10 |
| hr_db.employees | 100 |
| **总计** | **8,810** |

### Step 3: 方案 A — Export Cluster Data to S3

#### 3.1 准备 IAM Role 和 KMS Key

```bash
# 创建 KMS Key
KMS_KEY=$(aws kms create-key \
  --description "Aurora export encryption" \
  --region $REGION --profile $PROFILE \
  --query 'KeyMetadata.KeyId' --output text)

# 更新 KMS Key Policy（允许 export 服务使用）
# 见上方"前置条件"中的完整 Policy

# 创建 IAM Role
cat > /tmp/export-trust.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "export.rds.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name aurora-export-to-s3-role \
  --assume-role-policy-document file:///tmp/export-trust.json \
  --profile $PROFILE

# 创建 S3 Bucket
aws s3 mb s3://aurora-export-test-${ACCOUNT_ID} --region $REGION --profile $PROFILE

# 授权 Role 访问 S3
aws iam put-role-policy --role-name aurora-export-to-s3-role \
  --policy-name S3Access --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"s3:PutObject*\",\"s3:ListBucket\",\"s3:GetObject*\",\"s3:DeleteObject*\",\"s3:GetBucketLocation\"],
      \"Resource\": [\"arn:aws:s3:::aurora-export-test-${ACCOUNT_ID}\",\"arn:aws:s3:::aurora-export-test-${ACCOUNT_ID}/*\"]
    }]
  }" --profile $PROFILE
```

#### 3.2 全量导出

```bash
aws rds start-export-task \
  --export-task-identifier aurora-export-full-001 \
  --source-arn "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:aurora-export-test" \
  --s3-bucket-name "aurora-export-test-${ACCOUNT_ID}" \
  --s3-prefix "export-full" \
  --iam-role-arn "arn:aws:iam::${ACCOUNT_ID}:role/aurora-export-to-s3-role" \
  --kms-key-id "$KMS_KEY" \
  --region $REGION --profile $PROFILE
```

**实测输出**：
```json
{
    "Status": "STARTING",
    "PercentProgress": 0,
    "SourceType": "CLUSTER"
}
```

**耗时实测**：

| 阶段 | 时间 | 备注 |
|------|------|------|
| 请求提交 | 05:10 UTC | Status: STARTING |
| Clone 完成 + 提取开始 | 05:22 UTC | Status: IN_PROGRESS |
| 导出完成 | 05:25 UTC | Status: COMPLETE |
| **端到端** | **~15 分钟** | 含 Clone 阶段 |

**S3 输出结构**：
```
export-full/aurora-export-full-001/
├── analytics_db/
│   ├── analytics_db.page_views/1/part-00000-xxx.gz.parquet  (69 KB)
│   └── analytics_db.sessions/1/part-00000-xxx.gz.parquet    (49 KB)
├── ecommerce_db/
│   ├── ecommerce_db.customers/1/part-00000-xxx.gz.parquet   (5 KB)
│   ├── ecommerce_db.orders/1/part-00000-xxx.gz.parquet      (14 KB)
│   └── ecommerce_db.products/1/part-00000-xxx.gz.parquet    (4 KB)
├── hr_db/
│   ├── hr_db.departments/1/part-00000-xxx.gz.parquet        (1 KB)
│   └── hr_db.employees/1/part-00000-xxx.gz.parquet          (3 KB)
├── export_info_xxx.json
└── export_tables_info_xxx.json
```

#### 3.3 选择性导出

```bash
aws rds start-export-task \
  --export-task-identifier aurora-export-partial-001 \
  --source-arn "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:aurora-export-test" \
  --s3-bucket-name "aurora-export-test-${ACCOUNT_ID}" \
  --s3-prefix "export-partial" \
  --iam-role-arn "arn:aws:iam::${ACCOUNT_ID}:role/aurora-export-to-s3-role" \
  --kms-key-id "$KMS_KEY" \
  --export-only "ecommerce_db" \
  --region $REGION --profile $PROFILE
```

**结果**：只导出了 `ecommerce_db` 的 3 张表，`analytics_db` 和 `hr_db` 完全不出现在 S3 中。

!!! warning "选择性导出仍按整个集群计费"
    官方文档明确说明：*"You're charged for exporting the entire DB cluster, whether you export all or partial data."* 即使只导出一张表，费用与全量导出相同。

#### 3.4 用 Athena 查询 Parquet

```bash
# 创建 Glue Database
aws glue create-database \
  --database-input '{"Name": "aurora_export_parquet"}' \
  --region $REGION --profile $PROFILE

# 创建外部表（示例：customers）
aws athena start-query-execution \
  --query-string "CREATE EXTERNAL TABLE aurora_export_parquet.ecommerce_customers (
    id int, name string, email string, city string, created_at timestamp
  ) STORED AS PARQUET
  LOCATION 's3://aurora-export-test-${ACCOUNT_ID}/export-full/aurora-export-full-001/ecommerce_db/ecommerce_db.customers/'" \
  --work-group primary \
  --region $REGION --profile $PROFILE

# 查询验证
aws athena start-query-execution \
  --query-string "SELECT city, COUNT(*) as cnt FROM aurora_export_parquet.ecommerce_customers GROUP BY city ORDER BY cnt DESC" \
  --work-group primary \
  --region $REGION --profile $PROFILE
```

**Athena 查询结果**：
```
city       | cnt
-----------+-----
Mumbai     | 116
Seoul      | 105
Sydney     | 100
Singapore  |  94
Tokyo      |  85
```

所有 7 张表的行数与 Aurora 源完全一致 ✅。

### Step 4: 方案 B — Zero-ETL → Redshift Serverless

#### 4.1 创建 Redshift Serverless

```bash
# 创建 Namespace
aws redshift-serverless create-namespace \
  --namespace-name aurora-zero-etl-ns \
  --admin-username admin \
  --admin-user-password 'YourStrongPassword!' \
  --region $REGION --profile $PROFILE

# 创建 Workgroup（必须开启 case sensitivity）
aws redshift-serverless create-workgroup \
  --workgroup-name aurora-zero-etl-wg \
  --namespace-name aurora-zero-etl-ns \
  --base-capacity 8 \
  --config-parameters parameterKey=enable_case_sensitive_identifier,parameterValue=true \
  --subnet-ids $SUBNET_IDS \
  --security-group-ids $SG_ID \
  --region $REGION --profile $PROFILE

# 等待 Workgroup AVAILABLE（约 1-2 分钟）
```

#### 4.2 配置 Resource Policy（最容易踩坑的一步）

```bash
# 获取 Namespace ARN
NS_ARN=$(aws redshift-serverless get-namespace \
  --namespace-name aurora-zero-etl-ns \
  --region $REGION --profile $PROFILE \
  --query 'namespace.namespaceArn' --output text)

CLUSTER_ARN="arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:aurora-export-test"

# ⚠️ 必须用 `aws redshift put-resource-policy`，不要用 `aws redshift-serverless put-resource-policy`
cat > /tmp/redshift-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "redshift.amazonaws.com"},
      "Action": "redshift:AuthorizeInboundIntegration",
      "Condition": {
        "StringEquals": {"aws:SourceArn": "${CLUSTER_ARN}"}
      }
    },
    {
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::${ACCOUNT_ID}:root"},
      "Action": "redshift:CreateInboundIntegration"
    }
  ]
}
EOF

aws redshift put-resource-policy \
  --resource-arn "$NS_ARN" \
  --policy file:///tmp/redshift-policy.json \
  --region $REGION --profile $PROFILE
```

!!! danger "踩坑：必须用 `aws redshift` 而非 `aws redshift-serverless` 设置 Resource Policy"
    使用 `aws redshift-serverless put-resource-policy` 会导致 Principal 被序列化为 `PrincipalGroup` 格式，RDS 的 `create-integration` 无法识别，报错：
    
    ```
    InvalidParameterValue: You don't have access to zero-ETL integrations because
    the Amazon Redshift data warehouse doesn't exist or you don't have the required permissions.
    ```
    
    **正确做法**：用 `aws redshift put-resource-policy`（注意是 `redshift`，不是 `redshift-serverless`），即使目标是 Serverless。

#### 4.3 创建 Zero-ETL Integration

```bash
aws rds create-integration \
  --integration-name aurora-zero-etl-full \
  --source-arn "$CLUSTER_ARN" \
  --target-arn "$NS_ARN" \
  --region $REGION --profile $PROFILE
```

**实测输出**：
```json
{
    "IntegrationName": "aurora-zero-etl-full",
    "Status": "creating",
    "DataFilter": "include: *.*"
}
```

Integration 从 `creating` 到 `active` 大约需要 **7 分钟**。

#### 4.4 在 Redshift 中创建数据库

!!! warning "Zero-ETL 不会自动创建 Redshift 数据库"
    Integration 变为 `active` 后，状态会停留在 `PendingDbConnectState`。你需要**手动**在 Redshift 中为每个源数据库执行 `CREATE DATABASE ... FROM INTEGRATION`。

```bash
INTEGRATION_ID="your-integration-id"  # 从 create-integration 输出获取

for DB in ecommerce_db analytics_db hr_db; do
  aws redshift-data execute-statement \
    --workgroup-name aurora-zero-etl-wg \
    --database dev \
    --sql "CREATE DATABASE ${DB} FROM INTEGRATION '${INTEGRATION_ID}';" \
    --region $REGION --profile $PROFILE
done
```

等待约 30 秒后，验证数据同步：

```bash
aws redshift-data execute-statement \
  --workgroup-name aurora-zero-etl-wg \
  --database dev \
  --sql "SELECT integration_id, state, total_tables_replicated, total_tables_failed FROM svv_integration;" \
  --region $REGION --profile $PROFILE
```

**实测输出**：
```
state: CdcRefreshState
total_tables_replicated: 7
total_tables_failed: 0
```

7 张表全部成功同步 ✅。

#### 4.5 验证增量复制（CDC）

在 Aurora 中插入 3 条新记录：

```sql
INSERT INTO ecommerce_db.customers (name, email, city) VALUES
  ('NewCustomer_1', 'new1@test.com', 'NewYork'),
  ('NewCustomer_2', 'new2@test.com', 'London'),
  ('NewCustomer_3', 'new3@test.com', 'Berlin');
```

**Aurora 确认**: 503 rows（插入时间 05:38:27 UTC）

60 秒后查询 Redshift：

```bash
aws redshift-data execute-statement \
  --workgroup-name aurora-zero-etl-wg \
  --database ecommerce_db \
  --sql "SELECT count(*) FROM ecommerce_db.customers;" \
  --region $REGION --profile $PROFILE
```

**Redshift 结果**: **503 rows**（查询时间 05:39:51 UTC）✅

**CDC 延迟实测：< 90 秒**

## 测试结果

| # | 测试场景 | 方案 | 结果 | 关键数据 |
|---|---------|------|------|---------|
| T1 | 全量导出 | Export | ✅ | 15 min 端到端，7 表 Parquet |
| T2 | 选择性导出（DB 级） | Export | ✅ | 只导出指定 DB，其余不出现 |
| T3 | 选择性导出（Table 级） | Export | ✅ | `--export-only db.table` 语法支持 |
| T4 | Athena 查询 Parquet | Export | ✅ | 所有行数匹配，~500ms 延迟 |
| T5 | 全量同步 | Zero-ETL | ✅ | 7 min creating→active，7/7 表 |
| T6 | Data Filtering | Zero-ETL | ✅ | include/exclude Maxwell 语法 |
| T7 | Redshift SQL 查询 | Zero-ETL | ✅ | 数据完整 |
| T8 | CDC 增量验证 | Zero-ETL | ✅ | < 90 秒延迟 |

## 踩坑记录

!!! danger "踩坑 1: Redshift Resource Policy 必须用 `aws redshift` API"
    使用 `aws redshift-serverless put-resource-policy` 设置的 Policy 会被 RDS 拒绝。必须用 `aws redshift put-resource-policy`，即使目标是 Serverless Namespace。
    
    **影响**：无法创建 Zero-ETL Integration，报 "don't have access" 错误。
    
    来源：实测发现，官方文档未强调这一区别。

!!! warning "踩坑 2: Zero-ETL 需要手动 CREATE DATABASE"
    Integration 创建成功并变为 `active` 后，数据不会自动出现在 Redshift 中。需要手动为每个源数据库执行 `CREATE DATABASE ... FROM INTEGRATION`。
    
    Integration 会一直停留在 `PendingDbConnectState` 直到你创建数据库。
    
    来源：官方文档有提及，但容易被忽略。

!!! warning "踩坑 3: Lake Formation 可能阻止 Athena 查询"
    如果 Lake Formation 已注册了 S3 路径，Athena 查询会返回 `AccessDeniedException`。需要：
    
    1. 设置当前用户为 Lake Formation Admin
    2. 或 deregister S3 资源，改用 IAM-based 访问
    
    来源：实测发现，常见于账号中已有 Lake Formation 配置的场景。

!!! info "踩坑 4: Export 选择性导出仍按全量计费"
    即使 `--export-only` 只指定一张表，费用按整个 DB Cluster 数据量计算。这是 Export 任务先 Clone 整个集群再提取数据的设计决定的。
    
    来源：已查文档确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Aurora Serverless v2 | $0.12/ACU-hour | ~2 ACU × 2h | ~$0.48 |
| Redshift Serverless | $0.375/RPU-hour | 8 RPU × 1h | ~$3.00 |
| S3 存储 | $0.025/GB | < 1 MB | < $0.01 |
| KMS | $1/key/month | 1 key (pro-rated) | ~$0.03 |
| Athena 查询 | $5/TB scanned | < 1 MB | < $0.01 |
| Export 任务 | ~$0.01/GB | 1 GB × 2 次 | ~$0.02 |
| **合计** | | | **~$3.50** |

> **成本关键点**：Export Cluster Data 本身很便宜（几分钱），主要成本是 Aurora 集群本身。Zero-ETL 方案中 Redshift Serverless 的最低 8 RPU 是主要费用来源。

## 清理资源

```bash
# 1. 删除 Zero-ETL Integration
INTEGRATION_ARN=$(aws rds describe-integrations \
  --query 'Integrations[0].IntegrationArn' --output text \
  --region $REGION --profile $PROFILE)
aws rds delete-integration \
  --integration-identifier "$INTEGRATION_ARN" \
  --region $REGION --profile $PROFILE

# 2. 删除 Redshift Serverless
aws redshift-serverless delete-workgroup \
  --workgroup-name aurora-zero-etl-wg \
  --region $REGION --profile $PROFILE
# 等待 workgroup 删除完成
aws redshift-serverless delete-namespace \
  --namespace-name aurora-zero-etl-ns \
  --region $REGION --profile $PROFILE

# 3. 删除 Aurora 集群
aws rds delete-db-instance \
  --db-instance-identifier aurora-export-test-instance-1 \
  --skip-final-snapshot \
  --region $REGION --profile $PROFILE
aws rds wait db-instance-deleted \
  --db-instance-identifier aurora-export-test-instance-1 \
  --region $REGION --profile $PROFILE
aws rds delete-db-cluster \
  --db-cluster-identifier aurora-export-test \
  --skip-final-snapshot \
  --region $REGION --profile $PROFILE

# 4. 清理 S3
aws s3 rb s3://aurora-export-test-${ACCOUNT_ID} --force --region $REGION --profile $PROFILE
aws s3 rb s3://aurora-export-test-${ACCOUNT_ID}-athena --force --region $REGION --profile $PROFILE

# 5. 删除 IAM
aws iam delete-role-policy --role-name aurora-export-to-s3-role --policy-name S3Access --profile $PROFILE
aws iam delete-role-policy --role-name aurora-export-to-s3-role --policy-name GlueCrawlerPolicy --profile $PROFILE
aws iam delete-role --role-name aurora-export-to-s3-role --profile $PROFILE
aws iam delete-role-policy --role-name aurora-zero-etl-glue-transfer-role --policy-name GlueTransferPolicy --profile $PROFILE
aws iam delete-role --role-name aurora-zero-etl-glue-transfer-role --profile $PROFILE
aws iam delete-role-policy --role-name aurora-test-ec2-role --policy-name S3ReadAccess --profile $PROFILE
aws iam detach-role-policy --role-name aurora-test-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore --profile $PROFILE
aws iam remove-role-from-instance-profile --instance-profile-name aurora-test-ec2-profile --role-name aurora-test-ec2-role --profile $PROFILE
aws iam delete-instance-profile --instance-profile-name aurora-test-ec2-profile --profile $PROFILE
aws iam delete-role --role-name aurora-test-ec2-role --profile $PROFILE

# 6. 删除 KMS（安排删除，30 天后生效）
aws kms schedule-key-deletion --key-id $KMS_KEY --pending-window-in-days 7 \
  --region $REGION --profile $PROFILE

# 7. 删除 Glue / Athena
aws glue delete-database --name aurora_export_parquet --region $REGION --profile $PROFILE
aws athena delete-work-group --work-group aurora-export-test --recursive-delete-option \
  --region $REGION --profile $PROFILE

# 8. 删除安全组和子网组
aws ec2 delete-security-group --group-id $SG_ID --region $REGION --profile $PROFILE
aws rds delete-db-subnet-group --db-subnet-group-name aurora-export-test-subnet \
  --region $REGION --profile $PROFILE

# 9. 删除 Secrets Manager
aws secretsmanager delete-secret --secret-id aurora-export-test-credentials \
  --force-delete-without-recovery --region $REGION --profile $PROFILE

# 10. 终止 EC2（如果创建了跳板机）
aws ec2 terminate-instances --instance-ids i-xxx --region $REGION --profile $PROFILE
```

!!! danger "务必清理"
    Redshift Serverless 最低 8 RPU（~$3/小时），Aurora Serverless v2 按 ACU 计费。Lab 完成后请立即清理。

## 结论与建议

### 选型决策表

| 你的场景 | 推荐方案 | 理由 |
|---------|---------|------|
| 一次性数据迁移 / 归档 | **Export Cluster Data** | 简单、便宜、无需额外基础设施 |
| 定期数据快照（日/周） | **Export Cluster Data** | 配合 Lambda/EventBridge 定时触发 |
| 近实时报表 / Dashboard | **Zero-ETL** | < 2 分钟 CDC 延迟 |
| 数据湖构建 | **两者结合** | Export 做历史全量，Zero-ETL 做增量 |
| 小团队 / 预算有限 | **Export Cluster Data** | 无需 Redshift，Athena 按量付费 |
| 已有 Redshift 环境 | **Zero-ETL** | 直接集成，无需管理 ETL 管道 |

### 配置复杂度对比

| 步骤 | Export Cluster Data | Zero-ETL |
|------|:-------------------:|:--------:|
| 创建 S3 Bucket | ✅ 需要 | ❌ 不需要 |
| 创建 IAM Role | ✅ 需要 | ❌ 不需要 |
| 创建 KMS Key | ✅ 需要 | ❌ 不需要 |
| 修改 DB 参数 + Reboot | ❌ 不需要 | ✅ 需要（Enhanced Binlog） |
| 创建 Redshift | ❌ 不需要 | ✅ 需要 |
| 配置 Resource Policy | ❌ 不需要 | ✅ 需要（且容易踩坑） |
| 手动创建目标数据库 | ❌ 不需要 | ✅ 需要（每个源 DB 都要） |
| 创建 Glue 表定义 | ✅ 需要（查 Athena 时） | ❌ 不需要 |
| **总配置步骤** | **4 步** | **6+ 步** |

### 关键提醒

1. **Export Cluster Data 对源无影响** — 它 Clone 集群后导出，不影响线上性能。
2. **Zero-ETL 的 Enhanced Binlog 有写放大** — 开启后所有写操作的 IOPS 会增加，评估成本时要考虑。
3. **Export 不是增量的** — 每次都是完整快照，不能只导出"变化的数据"。
4. **Zero-ETL 不支持 resync** — 如果 Lakehouse integration 出问题，只能删了重建。
5. **SageMaker Lakehouse 作为 Zero-ETL 目标** — 截至本文测试时，Glue Managed Catalog 无法通过 CLI 创建（仅支持控制台/SageMaker Unified Studio），CLI 自动化程度受限。

## 参考链接

- [Exporting DB cluster data to Amazon S3](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/export-cluster-data.html)
- [Aurora zero-ETL integrations](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/zero-etl.html)
- [Data filtering for Aurora zero-ETL integrations](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/zero-etl.filtering.html)
- [Configure authorization for your Amazon Redshift data warehouse](https://docs.aws.amazon.com/redshift/latest/mgmt/zero-etl-using.redshift-iam.html)
- [Amazon Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/)
