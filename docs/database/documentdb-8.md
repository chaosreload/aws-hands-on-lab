# Amazon DocumentDB 8.0 全面实测：压缩、查询优化与新特性动手验证

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $1-3（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

Amazon DocumentDB 8.0 是一次重大版本升级，带来了 MongoDB 8.0 API 兼容、全新的 Query Planner V3、Zstandard 字典压缩、Views、Collation 等大量新功能。官方公告称查询延迟最高提升 7x、压缩率最高提升 5x。

这些数字在实际场景下表现如何？本文通过 9 项测试逐一验证 DocumentDB 8.0 的核心新功能，用数据说话。

## 前置条件

- AWS 账号（需要 DocumentDB、EC2、VPC 权限）
- AWS CLI v2 已配置
- mongosh（MongoDB Shell）
- 一台可连接 DocumentDB 的 EC2 实例（DocumentDB 仅支持 VPC 内访问）

## 核心概念

### DocumentDB 8.0 vs 5.0 关键变化

| 特性 | DocumentDB 5.0 | DocumentDB 8.0 |
|------|----------------|----------------|
| MongoDB 兼容性 | 5.0 API | 6.0 / 7.0 / 8.0 API |
| Query Planner | V1 / V2 | **V3（默认）** |
| 压缩算法 | LZ4 | **Zstd 字典压缩（默认）** |
| Views | ❌ | ✅ 只读视图 |
| Collation | ❌ | ✅ 语言敏感排序 |
| 聚合阶段 | 15 个 | **21 个**（+6 新增） |
| 新操作符 | - | $pow, $rand, $dateTrunc |

### Planner V3 核心优化

- **$match 前置推送**：自动将 $match 移到管道前端，减少后续处理数据量
- **$lookup + $unwind 合并**：自动合并连续的 lookup/unwind 操作
- **Distinct Scan**：对低基数索引使用高效的 DISTINCT_SCAN 策略

!!! warning "Planner V3 限制"
    - 不支持 Elastic Clusters（回退到 V1）
    - 不支持 planHint（依赖内部优化器选择）

## 动手实践

### Step 1: 创建 DocumentDB 8.0 集群

```bash
# 创建子网组
aws docdb create-db-subnet-group \
  --db-subnet-group-name docdb8-lab-subnet \
  --db-subnet-group-description 'DocumentDB 8.0 lab' \
  --subnet-ids subnet-xxx subnet-yyy subnet-zzz \
  --region us-east-1

# 创建安全组（仅允许 VPC 内部访问，切勿开放 0.0.0.0/0）
aws ec2 create-security-group \
  --group-name docdb8-lab-sg \
  --description 'DocumentDB 8.0 lab - VPC internal only' \
  --vpc-id vpc-xxx \
  --region us-east-1

aws ec2 authorize-security-group-ingress \
  --group-id sg-xxx \
  --protocol tcp --port 27017 \
  --cidr 172.31.0.0/16 \
  --region us-east-1

# 创建 DocumentDB 8.0 集群
aws docdb create-db-cluster \
  --db-cluster-identifier docdb8-lab \
  --engine docdb \
  --engine-version 8.0.0 \
  --master-username admin \
  --master-user-password 'YourStrongPassword!' \
  --db-subnet-group-name docdb8-lab-subnet \
  --vpc-security-group-ids sg-xxx \
  --no-deletion-protection \
  --region us-east-1

# 添加实例
aws docdb create-db-instance \
  --db-instance-identifier docdb8-lab-inst \
  --db-instance-class db.t3.medium \
  --engine docdb \
  --db-cluster-identifier docdb8-lab \
  --region us-east-1
```

等待实例可用（约 10-15 分钟）：

```bash
aws docdb wait db-instance-available \
  --db-instance-identifier docdb8-lab-inst \
  --region us-east-1
```

### Step 2: 连接集群

```bash
# 下载 CA 证书
wget -qO /tmp/global-bundle.pem \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

# 连接（替换 endpoint）
mongosh "mongodb://admin:YourStrongPassword!@<cluster-endpoint>:27017/\
?tls=true&tlsCAFile=/tmp/global-bundle.pem&retryWrites=false&directConnection=true"
```

### Step 3: 准备测试数据

```javascript
use testdb

// 插入 1000 条产品文档
const docs = [];
const categories = ["electronics", "clothing", "food", "books", "toys"];
const statuses = ["active", "inactive", "pending", "archived"];
for (let i = 0; i < 1000; i++) {
  docs.push({
    name: "Product Item Number " + i,
    description: "This is a detailed description for product item " + i +
      ". It contains information about features, specifications, and usage.",
    category: categories[i % 5],
    status: statuses[i % 4],
    price: Math.random() * 1000,
    quantity: Math.floor(Math.random() * 500),
    tags: ["tag" + (i % 10), "tag" + (i % 20), "common-tag"],
    metadata: { createdAt: new Date(), updatedBy: "system", version: 1 }
  });
}
db.products.insertMany(docs);
db.products.createIndex({category: 1});
db.products.createIndex({price: 1});
```

### Step 4: 验证 Zstd 压缩 vs LZ4

```javascript
// 创建 LZ4 压缩的集合
db.createCollection("coll_lz4", {
  storageEngine: { documentDB: { compression: { enable: true, algorithm: "lz4" } } }
});

// 创建 Zstd 压缩的集合
db.createCollection("coll_zstd", {
  storageEngine: { documentDB: { compression: { enable: true, algorithm: "zstd" } } }
});

// 向两个集合插入相同数据
db.coll_lz4.insertMany(docs);
db.coll_zstd.insertMany(docs);

// 对比压缩效果
db.coll_lz4.stats();
db.coll_zstd.stats();
```

### Step 5: 验证 Query Planner V3

```javascript
// 观察 $match 前置优化
// 原始管道：先 $project 再 $match
db.products.explain().aggregate([
  { $project: { category: 1, price: 1, name: 1 } },
  { $match: { category: "electronics" } }
]);
// Planner V3 自动将 $match 推前，使用 IXSCAN
```

### Step 6: 创建和使用 Views

```javascript
// 创建只读视图
db.createView("electronics_view", "products", [
  { $match: { category: "electronics" } },
  { $project: { _id: 1, name: 1, price: 1, status: 1 } }
]);

// 查询视图
db.electronics_view.find().limit(3);
db.electronics_view.countDocuments();

// 验证只读限制
db.electronics_view.insertOne({name: "test"});
// → 报错：Namespace testdb.electronics_view is a view, not a collection
```

### Step 7: 测试 Collation

```javascript
// 创建带 Collation 的集合（大小写不敏感）
db.createCollection("coll_collation", {
  collation: { locale: "en", strength: 2 }
});

db.coll_collation.insertMany([
  { name: "apple" }, { name: "Banana" }, { name: "cherry" },
  { name: "Apple" }, { name: "BANANA" }, { name: "Cherry" }
]);

// 大小写不敏感排序
db.coll_collation.find({}, {_id:0, name:1}).sort({name: 1});
// → apple, Apple, Banana, BANANA, cherry, Cherry

// 大小写不敏感查询
db.coll_collation.find({name: "apple"});
// → 返回 "apple" 和 "Apple" 两条
```

### Step 8: 新聚合操作符

```javascript
// $pow — 计算幂
db.products.aggregate([
  { $limit: 3 },
  { $project: { name: 1, price: 1, priceSquared: { $pow: ["$price", 2] } } }
]);

// $rand — 生成随机数
db.products.aggregate([
  { $limit: 3 },
  { $project: { name: 1, random: { $rand: {} } } }
]);

// $dateTrunc — 日期截断
db.products.aggregate([
  { $limit: 3 },
  { $project: {
    originalDate: "$metadata.createdAt",
    truncatedToDay: { $dateTrunc: { date: "$metadata.createdAt", unit: "day" } },
    truncatedToHour: { $dateTrunc: { date: "$metadata.createdAt", unit: "hour" } }
  }}
]);

// $bucket — 分桶统计
db.products.aggregate([
  { $bucket: {
    groupBy: "$price",
    boundaries: [0, 200, 400, 600, 800, 1000],
    default: "Other",
    output: { count: { $sum: 1 }, avgPrice: { $avg: "$price" } }
  }}
]);
```

## 测试结果

### 压缩对比（1000 文档）

| 指标 | LZ4 | Zstd | 差异 |
|------|-----|------|------|
| 文档数 | 1,000 | 1,000 | - |
| 平均文档大小 | 585.89 bytes | 585.89 bytes | - |
| 逻辑大小 (size) | 585,890 bytes | 585,890 bytes | - |
| 存储大小 (storageSize) | 663,552 bytes | 516,096 bytes | **Zstd 小 22%** |
| 压缩比 | 0.88x | 1.14x | Zstd 优 |

### 压缩边界：少量文档（50 条）

| 指标 | 值 |
|------|----|
| 文档数 | 50 |
| 逻辑大小 | 7,840 bytes |
| 存储大小 | 49,152 bytes |
| 压缩比 | 0.16x |

!!! note "字典训练门槛"
    Zstd 字典压缩需要至少 100 条文档才能训练字典。少于 100 条时，存储开销可能大于数据本身。

### Planner V3 验证

| 测试场景 | Planner 行为 | 索引使用 |
|----------|-------------|----------|
| $project → $match 管道 | $match 自动推前到 $project 之前 | IXSCAN (category_1) ✅ |
| $match + $group + $sort | $match 使用索引扫描 | IXSCAN (price_1) ✅ |
| View + find with filter | 使用源集合索引 | IXSCAN (category_1) ✅ |

### Collation 排序对比

| 排序方式 | 结果 |
|----------|------|
| Binary（默认） | Apple → BANANA → Banana → Cherry → apple → cherry |
| Collation (en, strength:2) | apple → Apple → Banana → BANANA → cherry → Cherry |

### 新功能验证汇总

| 功能 | 状态 | 备注 |
|------|------|------|
| $pow | ✅ | 正确计算幂 |
| $rand | ✅ | 返回 [0,1) 随机数 |
| $dateTrunc | ✅ | 支持 day/hour 等单位截断 |
| $bucket | ✅ | 正确按边界分桶 |
| Views | ✅ | 只读虚拟集合 |
| Collation | ✅ | 大小写不敏感排序/查询 |

## 踩坑记录

!!! warning "注意事项"

    **1. Zstd 压缩的 5x 数字需要特定条件**

    官方公告称"压缩率最高提升 5x"，但实测 1000 条文档仅达到 ~1.3x（相对 LZ4 节省 22%）。
    Zstd 字典压缩对小文档、重复字段名多的 schema 效果更好。5x 是特定场景下的峰值。
    *已查文档确认：文档中提到"especially for collections with consistent document schemas or repeated field names"。*

    **2. 少于 100 条文档时 Zstd 效果反而差**

    字典需要至少 100 条文档训练。50 条文档时，storageSize 是 logical size 的 6 倍。
    *已查文档确认："a dictionary is trained if the collection at least 100 documents"。*

    **3. Planner V3 性能提升因场景而异**

    公告称"7x 延迟改善"，文档中表述为"upto 2x overall performance improvement over Planner v2"。
    7x 是特定查询模式下的峰值（如 $match 前置 + 索引命中场景），整体改善约 2x。
    *已查文档确认。*

    **4. Collation 仅兼容 Planner V3**

    如果切换到 Planner V1/V2，Collation 可能导致"Index not found"错误。
    *已查文档确认。*

    **5. KMS 加密需要额外配置**

    创建集群时 `--storage-encrypted` 需要有效的 KMS Key 权限，默认 KMS Key 可能不可用。
    测试环境可使用 `--no-storage-encrypted`。
    *实测发现。*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| DocumentDB db.t3.medium | $0.072/hr | ~1 hr | $0.07 |
| EC2 t3.micro (跳板机) | $0.0104/hr | ~1 hr | $0.01 |
| DocumentDB I/O | $0.20/million | ~1000 | <$0.01 |
| DocumentDB Storage | $0.10/GB-month | <1 MB | <$0.01 |
| **合计** | | | **~$0.10** |

## 清理资源

```bash
# 1. 删除 DocumentDB 实例
aws docdb delete-db-instance \
  --db-instance-identifier docdb8-lab-inst \
  --region us-east-1

# 等待实例删除
aws docdb wait db-instance-deleted \
  --db-instance-identifier docdb8-lab-inst \
  --region us-east-1

# 2. 删除集群（跳过最终快照）
aws docdb delete-db-cluster \
  --db-cluster-identifier docdb8-lab \
  --skip-final-snapshot \
  --region us-east-1

# 3. 删除子网组
aws docdb delete-db-subnet-group \
  --db-subnet-group-name docdb8-lab-subnet \
  --region us-east-1

# 4. 删除跳板机
aws ec2 terminate-instances \
  --instance-ids i-xxx \
  --region us-east-1

# 5. 删除安全组（等 EC2 终止后）
aws ec2 delete-security-group --group-id sg-docdb --region us-east-1
aws ec2 delete-security-group --group-id sg-bastion --region us-east-1

# 6. 删除 Key Pair
aws ec2 delete-key-pair --key-name docdb8-lab-key --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。DocumentDB 按实例小时计费，忘记删除每月约 $52。

## 结论与建议

### 适用场景

- **Zstd 压缩**：适合文档量大（>100）、schema 一致的集合，存储节省 20%+
- **Planner V3**：对聚合管道密集型应用带来显著提升，特别是多阶段管道
- **Views**：适合需要向应用层暴露数据子集的场景，类似 SQL 视图
- **Collation**：国际化应用必备，多语言排序和大小写不敏感查询

### 生产环境建议

1. **新集群直接用 8.0** — Zstd 默认开启，Planner V3 默认启用
2. **升级现有集群** — 使用 DMS 从 5.0 迁移到 8.0，注意 Planner 行为差异
3. **Collation 使用注意** — 确保集群始终使用 Planner V3，不要回退
4. **压缩选择** — 读密集型 + 数据全部在内存：考虑 LZ4（CPU 更低）；写密集型或存储敏感：选 Zstd

## 参考链接

- [What is Amazon DocumentDB](https://docs.aws.amazon.com/documentdb/latest/developerguide/what-is.html)
- [Query Planner V3](https://docs.aws.amazon.com/documentdb/latest/developerguide/query-planner-v3.html)
- [Dictionary-based Compression](https://docs.aws.amazon.com/documentdb/latest/developerguide/dict-compression.html)
- [Views](https://docs.aws.amazon.com/documentdb/latest/developerguide/views.html)
- [Collation](https://docs.aws.amazon.com/documentdb/latest/developerguide/collation.html)
- [AWS What's New: DocumentDB 8.0](https://aws.amazon.com/about-aws/whats-new/2025/11/documentdb-8-o/)
