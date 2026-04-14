---
tags:
  - Database
---

# Amazon DocumentDB Serverless 实战：自动扩缩的文档数据库 + 向量搜索

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: ap-southeast-1（可替换为任何支持的 Region）
    - **最后验证**: 2026-03-28

## 背景

2025 年 7 月，AWS 宣布 Amazon DocumentDB Serverless 正式 GA。这是 Amazon DocumentDB（兼容 MongoDB）的按需自动扩缩配置——你不再需要预估实例大小，数据库会根据实际负载自动调整计算和内存容量。

**为什么值得关注？**

- **成本优化**：相比为峰值预置容量，最高可节省 90% 成本
- **运维简化**：无需手动调整实例类型，容量以 0.5 DCU 粒度自动扩缩
- **GenAI 场景**：原生向量搜索 + Serverless 弹性，天然适配 Agentic AI 工作流
- **零迁移成本**：现有 provisioned 集群可直接切换为 Serverless，无需数据迁移

## 前置条件

- AWS 账号（需要 DocumentDB、EC2、VPC 相关权限）
- AWS CLI v2 已配置
- 一台位于同 VPC 的 EC2 实例（用于 mongosh 连接，DocumentDB 仅支持 VPC 内访问）
- mongosh 客户端

## 核心概念

### DCU：Serverless 的计量单位

DocumentDB Serverless 引入了 **DCU（DocumentDB Capacity Unit）** 作为计量单位：

| 项目 | 说明 |
|------|------|
| 1 DCU | ≈ 2 GiB 内存 + 对应 CPU + 网络 |
| 扩缩粒度 | 0.5 DCU |
| 容量范围 | 0.5 – 256 DCU |
| 计费方式 | 按秒计费（10 分钟最低） |
| 引擎版本 | DocumentDB 5.0.0+ |

### Provisioned vs Serverless

| 对比项 | Provisioned | Serverless |
|--------|-------------|------------|
| 容量管理 | 手动选择实例类型 | 自动扩缩（MinCapacity ~ MaxCapacity） |
| 扩缩速度 | 需修改实例类型（有短暂中断） | 秒级无中断扩缩 |
| 适用场景 | 稳定工作负载 | 可变/突发/多租户工作负载 |
| 混合部署 | - | 可在同一集群混合 Provisioned + Serverless 实例 |
| 全球集群 | ✅ 支持 | ❌ 不支持 |

### MinCapacity 对连接限制的影响

一个重要但容易忽略的细节：**MinCapacity ≤ 1 DCU 时，连接数上限会被额外限制**。

| MinCapacity | 16 DCU 时 Active 连接 | 16 DCU 时 Cursor 限制 |
|-------------|----------------------|---------------------|
| ≤ 1 DCU | 1,550 | 132 |
| > 1 DCU | 2,709 | 192 |

如果你的应用需要大量并发连接，建议将 MinCapacity 设为 1.5 DCU 以上。

## 动手实践

### Step 1: 准备网络环境

创建 Security Group（**注意：仅允许 VPC 内访问，禁止 0.0.0.0/0 入站**）：

```bash
# 设置变量
REGION="ap-southeast-1"
VPC_ID="你的VPC-ID"  # 替换为你的 VPC ID

# 创建 Security Group
SG_ID=$(aws ec2 create-security-group \
  --group-name docdb-serverless-sg \
  --description "DocumentDB Serverless - VPC only access" \
  --vpc-id $VPC_ID \
  --region $REGION \
  --query 'GroupId' --output text)

echo "Security Group: $SG_ID"

# 添加入站规则 - 仅 VPC CIDR（请替换为你的 VPC CIDR）
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 27017 \
  --cidr 172.31.0.0/16 \
  --region $REGION
```

创建 Subnet Group（需要至少 2 个 AZ 的子网）：

```bash
# 获取子网 ID（使用默认 VPC 的子网）
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[*].SubnetId' --output text \
  --region $REGION)

aws docdb create-db-subnet-group \
  --db-subnet-group-name docdb-serverless-subnet-grp \
  --db-subnet-group-description "Subnet group for DocumentDB Serverless" \
  --subnet-ids $SUBNET_IDS \
  --region $REGION
```

### Step 2: 创建 Serverless 集群

```bash
# 创建集群（关键参数：--serverless-v2-scaling-configuration）
aws docdb create-db-cluster \
  --db-cluster-identifier docdb-serverless-test \
  --engine docdb \
  --engine-version 5.0.0 \
  --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=16 \
  --master-username docdbadmin \
  --master-user-password 'YourSecurePassword123!' \
  --vpc-security-group-ids $SG_ID \
  --db-subnet-group-name docdb-serverless-subnet-grp \
  --storage-type standard \
  --region $REGION
```

添加 Serverless Writer 实例：

```bash
# 关键：--db-instance-class db.serverless
aws docdb create-db-instance \
  --db-cluster-identifier docdb-serverless-test \
  --db-instance-identifier docdb-serverless-writer \
  --db-instance-class db.serverless \
  --engine docdb \
  --region $REGION
```

等待实例就绪（约 5-10 分钟）：

```bash
aws docdb wait db-instance-available \
  --db-instance-identifier docdb-serverless-writer \
  --region $REGION

# 获取集群端点
ENDPOINT=$(aws docdb describe-db-clusters \
  --db-cluster-identifier docdb-serverless-test \
  --query 'DBClusters[0].Endpoint' --output text \
  --region $REGION)

echo "Cluster endpoint: $ENDPOINT"
```

### Step 3: 连接并测试 CRUD

下载 TLS 证书并连接：

```bash
# 在 EC2 实例上执行
wget https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem

mongosh "mongodb://docdbadmin:YourSecurePassword123!@${ENDPOINT}:27017/\
?tls=true&tlsCAFile=global-bundle.pem\
&replicaSet=rs0&readPreference=secondaryPreferred&retryWrites=false"
```

在 mongosh 中执行 CRUD 操作：

```javascript
// 切换到测试数据库
db = db.getSiblingDB("testdb");

// 插入文档
db.products.insertMany([
  {name: "Widget A", price: 9.99, category: "widgets", stock: 100},
  {name: "Widget B", price: 19.99, category: "widgets", stock: 50},
  {name: "Gadget X", price: 49.99, category: "gadgets", stock: 25}
]);

// 查询
db.products.find({category: "widgets"});

// 更新
db.products.updateOne(
  {name: "Widget A"},
  {$set: {price: 12.99, stock: 95}}
);

// 聚合
db.products.aggregate([
  {$group: {_id: "$category", avgPrice: {$avg: "$price"}}}
]);
```

### Step 4: 测试向量搜索

DocumentDB Serverless 支持原生向量搜索，无需额外配置：

```javascript
// 创建 HNSW 向量索引
db.runCommand({
  createIndexes: "vectors",
  indexes: [{
    key: {"embedding": "vector"},
    name: "vector_idx",
    vectorOptions: {
      type: "hnsw",
      dimensions: 3,
      similarity: "cosine"
    }
  }]
});

// 插入向量文档（模拟 embedding）
db.vectors.insertMany([
  {title: "AI 基础", embedding: [1.0, 0.0, 0.0]},
  {title: "云架构", embedding: [0.0, 1.0, 0.0]},
  {title: "数据库", embedding: [0.0, 0.0, 1.0]},
  {title: "AI 架构", embedding: [0.707, 0.707, 0.0]},
  {title: "全栈 AI", embedding: [0.577, 0.577, 0.577]}
]);

// 向量搜索 - 查找与 [1, 0, 0]（"AI"方向）最相似的 3 个文档
db.vectors.aggregate([
  {$search: {
    vectorSearch: {
      vector: [1.0, 0.0, 0.0],
      path: "embedding",
      similarity: "cosine",
      k: 3
    }
  }},
  {$project: {title: 1, _id: 0}}
]);
// 结果：AI 基础 → AI 架构 → 全栈 AI（按 cosine 相似度排序）
```

### Step 5: 观察自动扩缩

插入大量数据触发扩容：

```javascript
// 批量插入 50,000 文档
for (var batch = 0; batch < 50; batch++) {
  var docs = [];
  for (var i = 0; i < 1000; i++) {
    docs.push({
      batchId: batch, docId: i,
      timestamp: new Date(),
      data: "x".repeat(500),
      value: Math.random() * 1000
    });
  }
  db.loadtest.insertMany(docs);
}

db.loadtest.countDocuments(); // 50,000
```

通过 CloudWatch 观察 DCU 变化：

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DocDB \
  --metric-name ServerlessDatabaseCapacity \
  --dimensions Name=DBInstanceIdentifier,Value=docdb-serverless-writer \
  --start-time "$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --period 60 \
  --statistics Average Minimum Maximum \
  --region $REGION
```

## 测试结果

### CRUD 性能

| 操作 | 数据量 | 耗时 | 吞吐量 |
|------|--------|------|--------|
| 批量插入 | 50,000 文档 | 2.1 秒 | ~23,800 docs/sec |
| 聚合查询 | 50,000 文档 × 10 次 | 0.9 秒 | ~11 queries/sec |

### DCU 自动扩缩行为

| 时间段 | DCU 范围 | 事件 |
|--------|---------|------|
| 初始化 | 5.0 → 16.0 | 实例创建后 warmup，瞬间升至 MaxCapacity |
| 稳定期 | 6.0 | 无负载时稳定在 6 DCU |
| 负载测试 | 6.0 → 11.0 | 50K 文档插入时升至 11 DCU |
| 缩容期 | 6.0 → 1.5 | 负载结束后 ~2 分钟缩至 1.5 DCU |

**关键发现**：

- **扩容速度快**：负载到来时 DCU 在秒级响应
- **缩容也很快**：负载结束后 ~2 分钟从 6 DCU 降至 1.5 DCU
- **不会缩至最低**：有数据在 buffer pool 中时，稳定在 1.5 DCU 而非 0.5 DCU
- **DCU 利用率峰值**：初始化时达到 100%，正常负载时约 37-41%

### 向量搜索验证

| 查询向量 | Top-3 结果 | 正确性 |
|----------|-----------|--------|
| [1, 0, 0] | AI 基础 → AI 架构 → 全栈 AI | ✅ cosine 排序正确 |
| [0, 1, 0] | 云架构 → AI 架构 → 全栈 AI | ✅ cosine 排序正确 |

## 踩坑记录

!!! warning "踩坑 1：Vector Search 不支持 $meta: searchScore"
    在 MongoDB Atlas 中可以用 `{score: {$meta: "searchScore"}}` 获取相似度分数，但在 DocumentDB 中会报错 `query requires text score metadata, but it is not available`。搜索结果已按相似度排序，但无法直接获取分数值。
    **状态**：⚠️ 实测发现，官方未记录

!!! warning "踩坑 2：必须在同一 VPC 内连接"
    DocumentDB 没有公网端点，必须从同一 VPC 内的资源（EC2、Lambda、Cloud9 等）连接。跨 VPC 需要 VPC Peering 或 Transit Gateway。

!!! warning "踩坑 3：MinCapacity 影响连接限制上限"
    MinCapacity ≤ 1 DCU 时，即使实例扩容到高 DCU，active connections 等限制也会被 cap 在较低水平（如 1,550）。生产环境建议 MinCapacity ≥ 1.5 DCU。
    **状态**：已查文档确认

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| DocumentDB Serverless (DCU-hours) | ~$0.12/DCU-hr | ~6 DCU × 0.3 hr | ~$0.22 |
| DocumentDB Storage | $0.10/GB-month | < 0.1 GB | < $0.01 |
| EC2 t3.micro (bastion) | $0.0104/hr | ~0.5 hr | < $0.01 |
| **合计** | | | **< $0.25** |

## 清理资源

```bash
REGION="ap-southeast-1"

# 1. 删除 DocumentDB 实例
aws docdb delete-db-instance \
  --db-instance-identifier docdb-serverless-writer \
  --region $REGION

# 等待实例删除完成
aws docdb wait db-instance-deleted \
  --db-instance-identifier docdb-serverless-writer \
  --region $REGION

# 2. 删除集群（跳过最终快照）
aws docdb delete-db-cluster \
  --db-cluster-identifier docdb-serverless-test \
  --skip-final-snapshot \
  --region $REGION

# 3. 删除 Subnet Group
aws docdb delete-db-subnet-group \
  --db-subnet-group-name docdb-serverless-subnet-grp \
  --region $REGION

# 4. 删除 Security Group
aws ec2 delete-security-group --group-id $SG_ID --region $REGION

# 5. 如果创建了 bastion EC2，也要清理
aws ec2 terminate-instances --instance-ids <INSTANCE_ID> --region $REGION
```

!!! danger "务必清理"
    DocumentDB Serverless 按秒计费，但 idle 状态仍会消耗最少 0.5 DCU 的费用。Lab 完成后请立即执行清理步骤。

## 结论与建议

### 适用场景

| 场景 | 推荐度 | 理由 |
|------|--------|------|
| 变量/突发工作负载 | ⭐⭐⭐ | Serverless 的核心价值 |
| 多租户 SaaS | ⭐⭐⭐ | 每租户独立集群，自动管理容量 |
| Agentic AI (RAG/向量搜索) | ⭐⭐⭐ | 原生向量搜索 + 弹性扩缩 |
| 开发测试环境 | ⭐⭐⭐ | 空闲时最低 0.5 DCU，极低成本 |
| 稳定高负载生产 | ⭐ | 用 Provisioned 更划算 |
| 全球多 Region 部署 | ❌ | 不支持 Global Clusters |

### 生产环境建议

1. **MinCapacity 设置**：生产环境建议 ≥ 1.5 DCU，避免连接限制被 cap
2. **监控指标**：重点关注 `DCUUtilization` 和 `ServerlessDatabaseCapacity`
3. **混合部署**：可以在同一集群混合 Provisioned Writer + Serverless Reader
4. **迁移路径**：现有集群可直接添加 Serverless 实例作为 Reader 测试，确认效果后再通过 failover 切换

## 参考链接

- [Amazon DocumentDB Serverless 概览](https://aws.amazon.com/documentdb/serverless)
- [官方文档 - Using DocumentDB Serverless](https://docs.aws.amazon.com/documentdb/latest/developerguide/docdb-serverless.html)
- [How Serverless Works](https://docs.aws.amazon.com/documentdb/latest/developerguide/docdb-serverless-how-it-works.html)
- [Serverless 限制](https://docs.aws.amazon.com/documentdb/latest/developerguide/docdb-serverless-limitations.html)
- [实例限制表](https://docs.aws.amazon.com/documentdb/latest/developerguide/docdb-serverless-instance-limits.html)
- [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-documentdb-serverless/)
- [AWS Blog](https://aws.amazon.com/blogs/aws/amazon-documentdb-serverless-is-now-available/)
- [DocumentDB 定价](https://aws.amazon.com/documentdb/pricing/)
