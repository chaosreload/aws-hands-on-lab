# AWS DMS Serverless 自动存储扩展实测：134GB Full Load 验证

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 3 小时（数据准备 ~90min + DMS provisioning ~15min + Full Load ~51min + 清理 ~30min）
    - **预估费用**: ~$5-8（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-24

## 背景

AWS DMS Serverless 是 AWS 数据库迁移服务的无服务器版本，用户无需管理复制实例，只需指定最小/最大 DCU（DMS Capacity Units），服务会自动扩缩容。

**痛点**：此前 DMS Serverless 复制有 **100GB 默认存储容量限制**。在处理大事务量或启用详细日志时，存储可能不足导致复制失败。

**新功能**（2025-04-23）：DMS Serverless 现在支持**自动存储扩展**，当存储达到限制时自动增加，无需手动干预，没有固定上限。

本文将通过 MySQL → MySQL 的 134GB 全量加载 + CDC 持续复制场景，实测验证这一功能。

## 前置条件

- AWS 账号（需要 DMS、RDS、EC2、CloudWatch 权限）
- AWS CLI v2 已配置
- 默认 VPC 可用

## 核心概念

| 项目 | 之前 | 现在 |
|------|------|------|
| 存储容量 | 100GB 固定上限 | 自动扩展，无上限 |
| 存储管理 | 无法手动调整 | 完全自动，无需干预 |
| 适用场景 | 小型迁移 | 大事务量、详细日志、LOB 数据 |
| 额外费用 | N/A | 包含在 DCU 计费中 |

**关键概念**：

- **DCU（DMS Capacity Unit）**：1 DCU = 2GB RAM，计费单位为 DCU-hour
- **计算扩展**：通过 MinCapacityUnits / MaxCapacityUnits 控制
- **存储扩展**：新功能，独立于计算扩展，完全自动

## 动手实践

### 架构图

```
[MySQL Source RDS] ──> [DMS Serverless Replication] ──> [MySQL Target RDS]
   db.r6g.large           1-32 DCU (自动)            db.r6g.large
   300GB gp3             存储自动扩展                 400GB gp3
```

### Step 1: 创建安全组

!!! warning "安全最佳实践"
    入站规则建议限制为您的 IP 或 VPC CIDR，遵循最小权限原则。避免使用过于宽泛的 CIDR 范围。

```bash
# 获取默认 VPC ID
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text \
  --region us-east-1)

# 创建安全组
SG_ID=$(aws ec2 create-security-group \
  --group-name dms-test-sg \
  --description "DMS test - VPC internal only" \
  --vpc-id $VPC_ID \
  --region us-east-1 \
  --query 'GroupId' --output text)

# 添加入站规则（仅 VPC CIDR）
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 3306 \
  --cidr 172.31.0.0/16 \
  --region us-east-1
```

### Step 2: 创建 RDS MySQL 实例

```bash
# 创建参数组（启用 binlog 用于 CDC）
aws rds create-db-parameter-group \
  --db-parameter-group-name dms-mysql80-params \
  --db-parameter-group-family mysql8.0 \
  --description "MySQL 8.0 params for DMS CDC" \
  --region us-east-1

# 设置 CDC 必需参数
aws rds modify-db-parameter-group \
  --db-parameter-group-name dms-mysql80-params \
  --parameters \
    "ParameterName=binlog_format,ParameterValue=ROW,ApplyMethod=immediate" \
    "ParameterName=binlog_row_image,ParameterValue=full,ApplyMethod=immediate" \
    "ParameterName=binlog_checksum,ParameterValue=NONE,ApplyMethod=immediate" \
  --region us-east-1

# 创建源 RDS（300GB gp3）
aws rds create-db-instance \
  --db-instance-identifier dms-source-mysql \
  --db-instance-class db.r6g.large \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password 'YourPassword123!' \
  --allocated-storage 300 --storage-type gp3 \
  --db-parameter-group-name dms-mysql80-params \
  --vpc-security-group-ids $SG_ID \
  --no-publicly-accessible \
  --region us-east-1

# 创建目标 RDS（400GB gp3，预留存储余量）
aws rds create-db-instance \
  --db-instance-identifier dms-target-mysql \
  --db-instance-class db.r6g.large \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password 'YourPassword123!' \
  --allocated-storage 400 --storage-type gp3 \
  --vpc-security-group-ids $SG_ID \
  --no-publicly-accessible \
  --region us-east-1

# 等待实例就绪
aws rds wait db-instance-available \
  --db-instance-identifier dms-source-mysql --region us-east-1
aws rds wait db-instance-available \
  --db-instance-identifier dms-target-mysql --region us-east-1
```

!!! tip "为什么 Target 用 400GB？"
    Target 存储建议大于 Source 实际数据量。134GB 数据写入 Target 后加上索引和 InnoDB 开销，300GB 虽然够用，但留出余量更安全。

!!! tip "重要：设置 Binlog 保留"
    RDS MySQL 默认不保留 binlog，这会导致 CDC 阶段报 Error 1236。必须设置 binlog retention hours：
    ```sql
    CALL mysql.rds_set_configuration('binlog retention hours', 24);
    ```

### Step 3: 准备测试数据（~134GB）

为了验证超越旧版 100GB 限制的存储扩展，我们需要生成约 134GB 的 LOB 数据：6 个表，每表 210,000 行，每行包含约 100KB 的 LONGTEXT 字段。

```bash
# 获取源 RDS 端点
SOURCE_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier dms-source-mysql \
  --query 'DBInstances[0].Endpoint.Address' --output text \
  --region us-east-1)

# 创建数据库、表结构和数据生成存储过程
mysql -h $SOURCE_ENDPOINT -u admin -p'YourPassword123!' <<'SQL'
CREATE DATABASE IF NOT EXISTS dms_test;
USE dms_test;

-- 创建 6 个结构相同的 LOB 表
DELIMITER //
CREATE PROCEDURE create_lob_tables()
BEGIN
  DECLARE i INT DEFAULT 1;
  WHILE i <= 6 DO
    SET @sql = CONCAT(
      'CREATE TABLE IF NOT EXISTS lob_', i, ' (',
      '  id INT AUTO_INCREMENT PRIMARY KEY,',
      '  name VARCHAR(255),',
      '  big_blob LONGTEXT,',
      '  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
      ') ENGINE=InnoDB'
    );
    PREPARE stmt FROM @sql;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
    SET i = i + 1;
  END WHILE;
END //
DELIMITER ;
CALL create_lob_tables();

-- 批量插入存储过程（每次插入 1000 行，循环 210 次 = 210,000 行/表）
DELIMITER //
CREATE PROCEDURE populate_lob_table(IN table_num INT)
BEGIN
  DECLARE batch INT DEFAULT 0;
  DECLARE batch_size INT DEFAULT 1000;
  DECLARE total_batches INT DEFAULT 210;

  WHILE batch < total_batches DO
    SET @sql = CONCAT(
      'INSERT INTO lob_', table_num, ' (name, big_blob) ',
      'SELECT CONCAT(''row-'', seq), REPEAT(''X'', 100000) ',
      'FROM (SELECT @rownum := @rownum + 1 AS seq ',
      'FROM information_schema.columns a, information_schema.columns b, ',
      '(SELECT @rownum := ', batch * batch_size, ') r LIMIT ', batch_size, ') t'
    );
    PREPARE stmt FROM @sql;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
    SET batch = batch + 1;
  END WHILE;
END //
DELIMITER ;

-- 设置 binlog retention（CDC 必需）
CALL mysql.rds_set_configuration('binlog retention hours', 24);
SQL
```

```bash
# 依次填充 6 个表（每个表约 22GB，总计约 134GB）
# 每个表约需 15 分钟，总计约 90 分钟
for i in $(seq 1 6); do
  echo "$(date): Populating lob_$i (210,000 rows × 100KB)..."
  mysql -h $SOURCE_ENDPOINT -u admin -p'YourPassword123!' dms_test \
    -e "CALL populate_lob_table($i);"
  echo "$(date): lob_$i complete"
done

# 验证数据量
mysql -h $SOURCE_ENDPOINT -u admin -p'YourPassword123!' dms_test -e "
  SELECT table_name, table_rows,
    ROUND(data_length/1024/1024/1024, 1) AS data_gb
  FROM information_schema.tables
  WHERE table_schema='dms_test' ORDER BY table_name;
"
```

!!! note "数据生成耗时"
    6 个表 × 210K 行 × 100KB ≈ 134GB。在 db.r6g.large 上，整个数据准备过程约需 90 分钟。请耐心等待。

### Step 4: 配置 DMS Serverless

```bash
# 创建 DMS 子网组（使用默认 VPC 子网，至少 2 个 AZ）
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[0:2].SubnetId' --output text \
  --region us-east-1)

aws dms create-replication-subnet-group \
  --replication-subnet-group-identifier dms-test-subnet-group \
  --replication-subnet-group-description "DMS test subnets" \
  --subnet-ids $SUBNET_IDS \
  --region us-east-1

# 获取目标 RDS 端点
TARGET_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier dms-target-mysql \
  --query 'DBInstances[0].Endpoint.Address' --output text \
  --region us-east-1)

# 创建源端点
SOURCE_ARN=$(aws dms create-endpoint \
  --endpoint-identifier dms-source-ep \
  --endpoint-type source --engine-name mysql \
  --server-name $SOURCE_ENDPOINT --port 3306 \
  --username admin --password 'YourPassword123!' \
  --database-name dms_test \
  --region us-east-1 \
  --query 'Endpoint.EndpointArn' --output text)

# 创建目标端点
TARGET_ARN=$(aws dms create-endpoint \
  --endpoint-identifier dms-target-ep \
  --endpoint-type target --engine-name mysql \
  --server-name $TARGET_ENDPOINT --port 3306 \
  --username admin --password 'YourPassword123!' \
  --database-name dms_test \
  --region us-east-1 \
  --query 'Endpoint.EndpointArn' --output text)
```

### Step 5: 创建并启动 Serverless 复制

```bash
# 将配置写入文件（1-32 DCU）
cat > /tmp/compute-config.json <<'JSON'
{
  "MinCapacityUnits": 1,
  "MaxCapacityUnits": 32,
  "ReplicationSubnetGroupId": "dms-test-subnet-group",
  "VpcSecurityGroupIds": ["YOUR_SG_ID"]
}
JSON
# 替换 SG ID
sed -i "s/YOUR_SG_ID/$SG_ID/" /tmp/compute-config.json

cat > /tmp/table-mappings.json <<'JSON'
{
  "rules": [{
    "rule-type": "selection",
    "rule-id": "1",
    "rule-name": "select-all",
    "object-locator": {"schema-name": "dms_test", "table-name": "%"},
    "rule-action": "include"
  }]
}
JSON

# 创建复制配置（1-32 DCU 自动扩展）
aws dms create-replication-config \
  --replication-config-identifier dms-storage-test \
  --replication-type full-load-and-cdc \
  --source-endpoint-arn $SOURCE_ARN \
  --target-endpoint-arn $TARGET_ARN \
  --compute-config file:///tmp/compute-config.json \
  --table-mappings file:///tmp/table-mappings.json \
  --region us-east-1

# 获取复制 ARN
REPLICATION_ARN=$(aws dms describe-replication-configs \
  --filters Name=replication-config-id,Values=dms-storage-test \
  --query 'ReplicationConfigs[0].ReplicationConfigArn' --output text \
  --region us-east-1)

# 启动复制
aws dms start-replication \
  --replication-config-arn $REPLICATION_ARN \
  --start-replication-type start-replication \
  --region us-east-1
```

### Step 6: 监控复制状态

```bash
# 查看复制状态和进度
aws dms describe-replications \
  --filters Name=replication-config-arn,Values=$REPLICATION_ARN \
  --query 'Replications[0].{
    Status:Status,
    Progress:ReplicationStats.FullLoadProgressPercent,
    TablesLoaded:ReplicationStats.TablesLoaded,
    ProvisionState:ProvisionData.ProvisionState,
    DCU:ProvisionData.ProvisionedCapacityUnits
  }' --region us-east-1
```

状态流转（实测）：
```
initializing → preparing_metadata_resources → fetching_metadata
→ calculating_capacity → provisioning_capacity (~12分钟)
→ replication_starting → running
```

### Step 7: CDC 验证

```bash
# Full Load 完成后（约 51 分钟），在源端插入测试数据验证 CDC
mysql -h $SOURCE_ENDPOINT -u admin -p'YourPassword123!' dms_test -e "
  INSERT INTO lob_1 (name, big_blob)
  VALUES ('cdc-test-row', REPEAT('Z', 100000));
"

# 等待约 15 秒后检查目标端
mysql -h $TARGET_ENDPOINT -u admin -p'YourPassword123!' dms_test -e "
  SELECT id, name, LENGTH(big_blob) AS blob_size, created_at
  FROM lob_1 WHERE name = 'cdc-test-row';
"
```

## 测试结果

### Full Load 性能（134GB）

| 指标 | 值 |
|------|-----|
| 数据总量 | 134GB（6 表 × 210K 行 × 100KB LONGTEXT） |
| 表数量 | 6 |
| 总行数 | 1,260,000 |
| Full Load 耗时 | 51 分钟 |
| 平均吞吐量 | 32MB/s（峰值 64MB/s） |
| DCU | 自动扩展至 32（Max） |
| 存储报错 | **无** ✅ |

### CDC 复制验证

Full Load 完成后，在 Source 插入测试行（100KB LONGTEXT），**15 秒内同步到 Target**，确认 CDC 正常工作。

!!! success "关键结论"
    134GB 数据成功完成 Full Load + CDC，远超旧版 100GB 存储限制。整个过程中无存储相关报错，DMS Serverless 的存储自动扩展完全透明运作。

### CloudWatch 指标

| 指标 | 值 |
|------|-----|
| CapacityUtilization | ~4% |
| CPUUtilization | ~7% |
| CDCLatencyTarget | < 15s |
| FullLoadThroughputBandwidthTarget | 32MB/s avg（峰值 64MB/s） |

### 存储扩展行为

**关键发现：存储扩展完全透明**

- `describe-replications` 的 `ProvisionData` 中**无存储字段**
- CloudWatch 中**无显式存储指标**（如 FreeableStorage、StorageUsed）
- 存储扩展在后台自动进行，用户不可见
- 134GB 数据 Full Load **无任何存储相关报错**，远超旧版 100GB 限制
- 这是一个"你不需要关心它"的功能——正是它设计的初衷

## 踩坑记录

!!! warning "踩坑 1：binlog retention 未设置导致 CDC 失败"
    **现象**：Full Load 成功后，CDC 阶段报错 `Error 1236 reading binary log`。
    
    **原因**：RDS MySQL 默认 `binlog retention hours` 为 NULL（不保留 binlog）。当 DMS 尝试读取 binlog 进行 CDC 时，binlog 已被清理。
    
    **解决**：`CALL mysql.rds_set_configuration('binlog retention hours', 24);`
    
    **已查文档确认**：这是 RDS MySQL + DMS CDC 的标准要求。

!!! warning "踩坑 2：DMS Serverless Replication Settings 修改受限"
    **现象**：尝试通过 `modify-replication-config --replication-settings` 修改日志级别，返回 "Invalid task settings json"。
    
    **实测发现，官方未记录**：DMS Serverless 的 replication settings 修改可能比经典 DMS 更严格。尝试传入完整或部分 settings JSON 均报错。创建时需要一次性指定好所有设置。

!!! warning "踩坑 3：计算资源直接扩展到 Max"
    **现象**：设置 MinCapacityUnits=1, MaxCapacityUnits=32，但启动后直接 provision 了 32 DCU。
    
    **已查文档确认**：DMS Serverless 根据 metadata 分析预测需要的容量，可能直接分配 Max 值。实际 CapacityUtilization 仅 ~4%，但计费按 provisioned DCU 算。这意味着 MaxCapacityUnits 设置需要谨慎——设多大就可能按多大计费。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| RDS db.r6g.large × 2 | ~$0.26/hr × 2 | ~3 hr | ~$1.56 |
| DMS Serverless 32 DCU | $0.068/DCU-hr | 32 DCU × 1.5 hr | ~$3.26 |
| RDS 存储 (300GB + 400GB gp3) | $0.08/GB-mo | ~3 hr | ~$0.06 |
| 数据传输（同 VPC） | 免费 | - | $0.00 |
| **合计** | | | **~$5-8** |

!!! note "费用说明"
    实际费用取决于 Lab 执行时间。上表按数据准备 ~90min + DMS 运行 ~1.5hr + 清理 ~30min 估算。如果中途排查问题导致时间延长，费用会相应增加。

## 清理资源

```bash
# 1. 停止并删除 DMS 复制
aws dms stop-replication \
  --replication-config-arn $REPLICATION_ARN \
  --region us-east-1

# 等待 "stopped" 状态
aws dms describe-replications \
  --filters Name=replication-config-arn,Values=$REPLICATION_ARN \
  --query 'Replications[0].Status' --region us-east-1

aws dms delete-replication-config \
  --replication-config-arn $REPLICATION_ARN \
  --region us-east-1

# 2. 删除 DMS 端点
aws dms delete-endpoint --endpoint-arn $SOURCE_ARN --region us-east-1
aws dms delete-endpoint --endpoint-arn $TARGET_ARN --region us-east-1

# 3. 删除 DMS 子网组（等待复制删除完成后）
aws dms delete-replication-subnet-group \
  --replication-subnet-group-identifier dms-test-subnet-group \
  --region us-east-1

# 4. 删除 RDS 实例
aws rds delete-db-instance \
  --db-instance-identifier dms-source-mysql \
  --skip-final-snapshot --region us-east-1
aws rds delete-db-instance \
  --db-instance-identifier dms-target-mysql \
  --skip-final-snapshot --region us-east-1

# 5. 删除参数组（等待 RDS 删除完成后）
aws rds delete-db-parameter-group \
  --db-parameter-group-name dms-mysql80-params \
  --region us-east-1

# 6. 删除安全组
# 先检查 ENI 残留
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=$SG_ID \
  --query 'NetworkInterfaces[*].NetworkInterfaceId' \
  --region us-east-1
# 确认无残留 ENI 后删除
aws ec2 delete-security-group --group-id $SG_ID --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。DMS Serverless 按 DCU-hour 计费（实测直接 provision Max 值），不停止会持续产生费用。RDS db.r6g.large 也是 ~$0.26/hr。

## 结论与建议

### 存储扩展的价值

对于 DMS Serverless 用户，存储自动扩展解决了一个实际痛点：

1. **大数据量迁移**：不再需要担心 100GB 存储限制——实测 134GB Full Load 成功完成
2. **LOB 数据处理**：大 TEXT/BLOB 字段的表可以安心迁移（实测 100KB LONGTEXT × 126 万行）
3. **详细日志**：可以放心启用 DETAILED 级别日志排查问题
4. **零运维**：存储扩展完全自动，无需监控或手动干预

### 生产环境建议

1. **谨慎设置 MaxCapacityUnits**：DMS Serverless 可能直接 provision Max 值，按 provisioned DCU 计费（实测 CapacityUtilization 仅 ~4%）。建议先用小值测试，了解实际需求后再调整
2. **必须设置 binlog retention**：MySQL CDC 的前提条件，建议至少 24 小时
3. **监控 CDCLatencyTarget**：虽然存储不再是瓶颈，但大 LOB 数据的 CDC 延迟可能较高
4. **存储扩展不可观测**：目前没有 CloudWatch 指标可以监控存储使用量，只能通过复制是否正常运行来间接判断

## 参考链接

- [AWS DMS Serverless 自动存储扩展公告](https://aws.amazon.com/about-aws/whats-new/2025/04/aws-dms-serverless-automatic-storage-scaling/)
- [AWS DMS Serverless 组件文档](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Serverless.Components.html)
- [AWS DMS Serverless 限制](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Serverless.Limitations.html)
- [AWS DMS 定价](https://aws.amazon.com/dms/pricing/)
