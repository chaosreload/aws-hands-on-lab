---
tags:
  - Storage
---

# Amazon S3 Vectors GA 实测：40 倍 Scale 提升 + Per-Index 加密 + 性能基准

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

!!! tip "前置阅读"
    本文是 [S3 Vectors Preview 实战](../s3-vectors-preview/) 的 **GA 升级篇**。如果你还不了解 S3 Vectors 的基础概念（Vector Bucket / Index / Vector 三层架构、元数据过滤语法等），建议先阅读 Preview 篇。本文聚焦 **Preview → GA 的变化**，不重复基础内容。

## 背景

2025 年 7 月，AWS 发布 Amazon S3 Vectors Preview，我们第一时间做了[动手测试](../s3-vectors-preview/)。Preview 期间用户创建了超过 25 万个索引，写入超过 400 亿向量，执行超过 10 亿次查询。

2025 年 12 月，S3 Vectors 正式 GA，带来了一系列重大升级。最核心的变化是 **单索引向量容量从 5000 万提升到 20 亿（40 倍）**，这意味着大多数场景不再需要分片索引。同时新增了 Per-Index KMS 加密、资源标签、CloudFormation 支持等生产级功能。

本文通过实测验证 GA 版本的新能力，重点回答三个问题：

1. **Scale 变化实际感受如何？** Top-K 从 30 提升到 100，实测延迟变化？
2. **Per-Index KMS 加密怎么配？** 多租户隔离的新姿势
3. **GA 延迟 vs Preview 延迟？** 官方声称频繁查询可达 ~100ms，实测如何？

## Preview → GA 关键变化一览

| 指标 | Preview | GA | 变化 |
|------|---------|-----|------|
| 每索引最大向量数 | 5000 万 | **20 亿** | 40x ↑ |
| Top-K 结果数 | 30 | **100** | 3.3x ↑ |
| Region 数量 | 5 | **14** | +9 Regions |
| 写入吞吐 | 未公开 | **1,000 PUT TPS** | 新指标 |
| 频繁查询延迟 | sub-second | **~100ms** | 优化 |
| 每向量 metadata keys | 未明确 | **50** | 新限制 |
| Per-Index KMS 加密 | ❌ | ✅ | **新功能** |
| 资源标签（Tagging） | ❌ | ✅ | **新功能** |
| CloudFormation | ❌ | ✅ | **新功能** |
| AWS PrivateLink | ❌ | ✅ | **新功能** |
| Bedrock KB 集成 | Preview | ✅ GA | 正式发布 |
| OpenSearch 集成 | Preview | ✅ GA | 正式发布 |

## 前置条件

- AWS 账号
- AWS CLI v2（含 `s3vectors` 子命令）
- Python 3 + Boto3
- IAM 权限：`s3vectors:*`、`kms:CreateKey`、`kms:PutKeyPolicy`（测试 KMS 加密需要）

## 动手实践

### Step 1: 创建 Vector Bucket 和 Index

```bash
# 创建向量 bucket
aws s3vectors create-vector-bucket \
  --vector-bucket-name s3v-ga-demo \
  --region us-east-1
```

```bash
# 创建基础索引（SSE-S3 默认加密）
aws s3vectors create-index \
  --vector-bucket-name s3v-ga-demo \
  --index-name idx-basic \
  --data-type float32 \
  --dimension 1024 \
  --distance-metric cosine \
  --region us-east-1
```

### Step 2: 配置 Per-Index KMS 加密（GA 新功能）

这是 GA 版本最重要的企业级新功能之一。Preview 期间加密只能在 bucket 级别设置，GA 允许每个 index 使用独立的 KMS key，支持多租户隔离。

**首先创建 KMS key：**

```bash
KEY_ID=$(aws kms create-key \
  --description 's3v-ga-test-key' \
  --region us-east-1 \
  --query 'KeyMetadata.KeyId' \
  --output text)
echo "Key ID: $KEY_ID"
```

**配置 KMS key policy（关键步骤）：**

!!! warning "踩坑：必须授权 S3 Vectors 服务主体"
    创建 per-index KMS 加密的索引时，KMS key 必须有 resource policy 授权给 `indexing.s3vectors.amazonaws.com` 服务主体。否则会报 `AccessDeniedException: Insufficient access to perform asynchronous indexing`。（已查文档确认）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Enable IAM User Permissions",
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::<ACCOUNT_ID>:root"},
      "Action": "kms:*",
      "Resource": "*"
    },
    {
      "Sid": "Allow S3 Vectors indexing service",
      "Effect": "Allow",
      "Principal": {
        "Service": "indexing.s3vectors.amazonaws.com"
      },
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "<ACCOUNT_ID>"
        }
      }
    }
  ]
}
```

```bash
# 保存 policy 到文件后应用
aws kms put-key-policy \
  --key-id $KEY_ID \
  --policy-name default \
  --policy file://kms-policy.json \
  --region us-east-1
```

**创建 KMS 加密的索引：**

```bash
aws s3vectors create-index \
  --vector-bucket-name s3v-ga-demo \
  --index-name idx-kms \
  --data-type float32 \
  --dimension 1024 \
  --distance-metric cosine \
  --encryption-configuration "{
    \"sseType\": \"aws:kms\",
    \"kmsKeyArn\": \"arn:aws:kms:us-east-1:<ACCOUNT_ID>:key/$KEY_ID\"
  }" \
  --region us-east-1
```

!!! note "KMS key 必须使用完整 ARN"
    S3 Vectors 不支持 Key ID 或 Key Alias，必须使用完整的 KMS Key ARN。（已查文档确认）

**验证两个索引的加密配置差异：**

```bash
# 默认加密（SSE-S3）
aws s3vectors get-index \
  --vector-bucket-name s3v-ga-demo \
  --index-name idx-basic \
  --region us-east-1 \
  --query 'index.encryptionConfiguration'
# 输出: {"sseType": "AES256"}

# Per-index KMS 加密
aws s3vectors get-index \
  --vector-bucket-name s3v-ga-demo \
  --index-name idx-kms \
  --region us-east-1 \
  --query 'index.encryptionConfiguration'
# 输出: {"sseType": "aws:kms", "kmsKeyArn": "arn:aws:kms:..."}
```

### Step 3: 资源标签（GA 新功能）

GA 版本支持为 vector bucket 和 index 添加标签，用于成本归因和基于属性的访问控制（ABAC）。

```bash
# 给 bucket 打标签
aws s3vectors tag-resource \
  --resource-arn arn:aws:s3vectors:us-east-1:<ACCOUNT_ID>:bucket/s3v-ga-demo \
  --tags '{"project": "s3v-ga-lab", "environment": "test"}' \
  --region us-east-1

# 给 index 打标签
aws s3vectors tag-resource \
  --resource-arn arn:aws:s3vectors:us-east-1:<ACCOUNT_ID>:bucket/s3v-ga-demo/index/idx-basic \
  --tags '{"project": "s3v-ga-lab", "index-type": "basic"}' \
  --region us-east-1

# 验证标签
aws s3vectors list-tags-for-resource \
  --resource-arn arn:aws:s3vectors:us-east-1:<ACCOUNT_ID>:bucket/s3v-ga-demo \
  --region us-east-1
# 输出: {"tags": {"environment": "test", "project": "s3v-ga-lab"}}
```

### Step 4: 批量写入 5000 向量

为了测试 GA 版本的 Top-K 100 和查询性能，我们先批量写入 5000 个 1024 维向量：

```python
import boto3, random, math, time

s3v = boto3.client("s3vectors", region_name="us-east-1")

def gen_vector(dim, seed):
    """生成归一化的随机向量"""
    random.seed(seed)
    v = [random.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x*x for x in v))
    return [round(x/norm, 8) for x in v]

# 10 批 × 500 向量 = 5000 向量
for batch in range(10):
    vectors = []
    for i in range(500):
        idx = batch * 500 + i
        vectors.append({
            "key": f"vec-{idx:05d}",
            "data": {"float32": gen_vector(1024, seed=idx)},
            "metadata": {
                "batch": batch,
                "idx": idx,
                "category": f"cat-{idx % 10}"
            }
        })
    t0 = time.time()
    s3v.put_vectors(
        vectorBucketName="s3v-ga-demo",
        indexName="idx-basic",
        vectors=vectors
    )
    print(f"Batch {batch}: {(time.time()-t0)*1000:.0f}ms")
```

实测写入结果：

| 批次 | 向量数 | 耗时 |
|------|--------|------|
| Batch 0（冷启动） | 500 | 3335ms |
| Batch 1-9（稳定态） | 500 | 1483-1744ms |
| **总计** | **5000** | **20.83s** |
| **有效吞吐** | | **~240 vectors/s** |

!!! info "关于 1,000 TPS 的理解"
    官方的 "1,000 vectors per second" 指的是 **streaming single-vector updates**（每次 PUT 1 个向量，1000 次/秒）。批量写入 500 向量/请求时，受限于每次请求的网络和处理时间，实际吞吐约 240 vectors/s。两种模式面向不同场景：实时流式更新 vs 批量导入。（已查文档确认："Combined PutVectors and DeleteVectors requests per second per vector index: Up to 1,000"）

### Step 5: Top-K 100 查询测试（GA 新能力）

Preview 版本 Top-K 最大为 30，GA 提升到 100。我们测试不同 Top-K 值对延迟的影响：

```python
query_vec = gen_vector(1024, seed=9999)

for k in [5, 10, 30, 50, 100]:
    latencies = []
    for _ in range(10):
        t0 = time.time()
        resp = s3v.query_vectors(
            vectorBucketName="s3v-ga-demo",
            indexName="idx-basic",
            queryVector={"float32": query_vec},
            topK=k,
            returnMetadata=True,
            returnDistance=True
        )
        latencies.append((time.time() - t0) * 1000)
    p50 = sorted(latencies)[4]
    print(f"Top-K={k:3d}: p50={p50:.0f}ms, returned={len(resp['vectors'])}")
```

## 测试结果

### Top-K 延迟对比（10 次采样, 5000 向量, 1024 维, cosine）

| Top-K | 平均延迟 | P50 | P90 | 最小 | 最大 | 返回数 |
|-------|---------|-----|-----|------|------|--------|
| 5 | 394ms | 344ms | 517ms | 271ms | 567ms | 5 |
| 10 | 290ms | 275ms | 320ms | 270ms | 323ms | 10 |
| 30 | 323ms | 278ms | 450ms | 269ms | 553ms | 30 |
| 50 | 279ms | 274ms | 281ms | 270ms | 322ms | 50 |
| **100** | **277ms** | **277ms** | **281ms** | **271ms** | **291ms** | **100** |

**关键发现：Top-K 大小对查询延迟几乎没有影响。** 从 Top-K=5 到 Top-K=100，P50 延迟稳定在 275ms 左右。这意味着你可以放心使用 Top-K=100 获取更多上下文，而不用担心性能惩罚。

### 冷查询 vs 热查询延迟

| 状态 | 查询延迟 | 说明 |
|------|---------|------|
| 冷查询（90s 空闲后） | **1044ms** | 仍在 sub-second 范围（勉强） |
| 热查询（连续查询） | **274ms** (avg) | 非常稳定，标准差 < 3ms |
| 冷热差距 | **~4x** | 冷启动惩罚明显 |

### GA vs Preview 延迟对比

| 指标 | Preview 实测 | GA 实测 | 变化 |
|------|-------------|---------|------|
| 热查询平均 | 305ms | 274ms | **-10%** ↓ |
| 热查询最小 | 267ms | 269ms | 基本持平 |
| 冷启动首查 | 371-391ms* | 1044ms | 不同条件 |
| Top-K 上限 | 30 | **100** | 3.3x ↑ |

*Preview 的"冷启动"是新建索引后立即查询，GA 的冷启动是 90s 空闲后，条件不完全一致。

!!! note "关于 ~100ms 延迟"
    官方声称频繁查询可达 ~100ms。我们实测热查询稳定在 ~274ms，未能复现 100ms。这可能与查询频率有关 —— 我们的测试是连续 10 次查询后即停止，而 "频繁查询" 可能指持续高频（如每秒数次）的场景。对于偶发查询场景，274ms 的热查询延迟已经非常实用。

### 元数据边界测试

| 测试 | 结果 | 错误信息 |
|------|------|---------|
| 50 个 metadata keys | ✅ 成功 | — |
| 51 个 metadata keys | ❌ 拒绝 | "Metadata object must have at most 50 keys" |
| 10 个 non-filterable keys | ✅ 成功 | — |
| 11 个 non-filterable keys | ❌ 拒绝 | "must have length between 1 and 10, inclusive" |

### Per-Index KMS 加密验证

| 索引 | 加密类型 | KMS Key | 写入 | 查询 |
|------|---------|---------|------|------|
| idx-basic | SSE-S3 (AES256) | — | ✅ | ✅ |
| idx-kms | SSE-KMS | 自定义 CMK | ✅ | ✅ 394ms |

两个索引的读写行为完全一致，KMS 加密对使用体验透明。

## 踩坑记录

!!! warning "注意"

    1. **KMS key policy 必须授权 S3 Vectors 服务主体**：创建 per-index KMS 加密的索引前，必须先更新 KMS key policy，授权 `indexing.s3vectors.amazonaws.com` 的 `kms:Decrypt` 权限。否则创建索引会直接报 `AccessDeniedException`。（已查文档确认）

    2. **KMS key 只能用 ARN，不支持 Key ID 或 Alias**：`--encryption-configuration` 中的 `kmsKeyArn` 必须是完整的 Key ARN 格式。（已查文档确认）

    3. **"1,000 TPS" 是请求级别，不是向量级别**：官方限制 "Combined PutVectors and DeleteVectors requests per second per vector index: Up to 1,000" 指的是 API 请求数，每个请求最多包含 500 个向量。批量写入时的实际向量吞吐取决于 batch size 和网络延迟。（已查文档确认）

    4. **加密和维度仍然创建后不可改**：与 Preview 一致，encryption type、dimension、distance metric、non-filterable metadata keys 创建后均不可修改。GA 没有改变这一限制。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 Vectors 存储 | 按量计费 | 5000 向量 × 1024 维 | < $0.01 |
| S3 Vectors 查询 | 按量计费 | ~100 次查询 | < $0.01 |
| KMS Key | $1/月 | 按比例（测试后立即删除） | < $0.05 |
| **合计** | | | **< $0.10** |

!!! tip "定价亮点"
    GA 版本的定价有阶梯优惠：索引超过 10 万向量后，每 TB 查询费用更低。对于大规模向量存储场景，成本优势更明显。

## 清理资源

```bash
# 1. 删除所有 vector index
for idx in idx-basic idx-kms idx-nfm10; do
  aws s3vectors delete-index \
    --vector-bucket-name s3v-ga-demo \
    --index-name $idx \
    --region us-east-1
done

# 2. 删除 vector bucket（需先删除所有 index）
aws s3vectors delete-vector-bucket \
  --vector-bucket-name s3v-ga-demo \
  --region us-east-1

# 3. 禁用并计划删除 KMS key（最短等待 7 天）
aws kms schedule-key-deletion \
  --key-id <KEY_ID> \
  --pending-window-in-days 7 \
  --region us-east-1

# 4. 验证清理完成
aws s3vectors list-vector-buckets --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 S3 Vectors 费用极低，但 KMS key 每月 $1，不删除会持续计费。

## 结论与建议

### GA 版本升级亮点总结

| 能力 | 对生产环境的意义 |
|------|-----------------|
| 20 亿向量/索引 | 绝大多数场景无需分片，架构大幅简化 |
| Top-K 100 | RAG 场景可获取更多上下文，且无延迟惩罚 |
| Per-Index KMS 加密 | 多租户隔离的关键能力，满足合规要求 |
| 资源标签 | 成本归因 + ABAC 访问控制 |
| CloudFormation + PrivateLink | IaC 部署 + 私有网络连接，生产级就绪 |
| 14 Regions | 全球覆盖显著扩大 |

### 实测建议

1. **放心使用 Top-K=100**：实测证明 Top-K 大小对延迟几乎无影响，RAG 场景建议直接用 100 获取更丰富的上下文
2. **多租户场景用 Per-Index KMS**：每个租户一个索引 + 独立 KMS key，数据隔离和密钥管理都能满足
3. **注意冷启动延迟**：不频繁查询的索引首次查询约 1 秒，之后稳定在 ~274ms。对延迟敏感的场景可考虑定期 warmup 查询
4. **批量导入用大 batch**：写入吞吐受限于请求次数（1,000 TPS），用 500 向量/batch 比逐条写入效率高得多

### 与 Preview 文章的互补关系

| 内容 | Preview 篇 | GA 篇（本文） |
|------|-----------|--------------|
| 基础 CRUD | ✅ 详细 | 简要 |
| Cosine vs Euclidean | ✅ 对比 | — |
| 元数据过滤语法 | ✅ 详细 | — |
| 维度边界 1-4096 | ✅ 测试 | — |
| Per-Index KMS 加密 | — | ✅ 新增 |
| Top-K 100 | — | ✅ 新增 |
| 资源标签 | — | ✅ 新增 |
| 写入吞吐测试 | — | ✅ 新增 |
| 冷/热延迟对比 | 部分 | ✅ 完整 |

## 参考链接

- [S3 Vectors 官方文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [S3 Vectors 限制与约束](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html)
- [S3 Vectors 加密文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-data-encryption.html)
- [S3 定价页面](https://aws.amazon.com/s3/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-vectors-generally-available/)
- [AWS News Blog](https://aws.amazon.com/blogs/aws/amazon-s3-vectors-now-generally-available-with-increased-scale-and-performance/)
- [S3 Vectors Preview 实战（前置阅读）](../s3-vectors-preview/)
