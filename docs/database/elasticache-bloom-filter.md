---
tags:
  - Database
---

# Amazon ElastiCache Bloom Filter 实测：比 Set 省 98% 内存的概率型数据结构

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $3（Serverless 按用量计费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

在缓存场景中，"这个元素是否存在？" 是最常见的查询之一。传统做法是用 Set 数据类型存储元素集合，但当集合规模达到百万级时，内存消耗会成为瓶颈。

2025 年 7 月，Amazon ElastiCache 在 Valkey 8.1 版本中引入了 **Bloom Filter** 数据类型。Bloom Filter 是一种概率型数据结构，用极少的内存实现 "可能存在" 或 "一定不存在" 的快速判断，内存效率比 Set 高 **98% 以上**。

**典型使用场景**：

- 广告/推荐去重：用户是否已看过某个广告？
- 欺诈检测：信用卡是否在被盗名单中？
- 垃圾内容过滤：URL 是否在恶意列表中？
- 用户名查重：该用户名是否已注册？

## 前置条件

- AWS 账号
- AWS CLI v2 已配置
- ElastiCache Serverless 或 Valkey 8.1+ 集群
- 能够通过 `redis-cli` / `valkey-cli`（支持 TLS）连接到集群

## 核心概念

### Bloom Filter vs Set

| 特性 | Bloom Filter | Set |
|------|-------------|-----|
| 内存效率 | 极高（~120KB / 10万元素） | 低（~6-8MB / 10万元素） |
| 查询结果 | "可能存在" 或 "一定不存在" | 精确 |
| False Positive | 可能（概率可配置） | 不会 |
| False Negative | 不会 | 不会 |
| 删除元素 | ❌ 不支持 | ✅ 支持 |
| 适用场景 | 大规模存在性检查，容忍少量误判 | 精确集合操作 |

### Scaling vs Non-scaling

- **Scaling（默认）**：达到容量后自动扩展，新建 sub-filter，容量 = 上一个 × expansion rate
- **Non-scaling**：固定容量，满后拒绝添加（返回错误）

### 支持的命令

| 命令 | 功能 |
|------|------|
| `BF.RESERVE` | 创建 Bloom Filter（指定 fp rate、capacity） |
| `BF.ADD` / `BF.MADD` | 添加单个 / 批量元素 |
| `BF.EXISTS` / `BF.MEXISTS` | 检查单个 / 批量元素 |
| `BF.INSERT` | 创建 + 添加（组合命令） |
| `BF.CARD` | 返回已添加元素数量 |
| `BF.INFO` | 返回详细信息（容量、大小、filter 数等） |

!!! note "限制"
    - 单个 Bloom Filter 对象内存上限 **128 MB**
    - `BF.LOAD` 不支持（ElastiCache 不使用 AOF）
    - Bloom 数据类型的 RDB 不兼容非 Valkey 的 Bloom 实现

## 动手实践

### Step 1: 创建 ElastiCache Serverless 集群

```bash
aws elasticache create-serverless-cache \
  --serverless-cache-name bloom-filter-test \
  --engine valkey \
  --major-engine-version 8 \
  --region us-east-1
```

等待集群创建完成（通常 1-2 分钟）：

```bash
aws elasticache describe-serverless-caches \
  --serverless-cache-name bloom-filter-test \
  --region us-east-1 \
  --query 'ServerlessCaches[0].{Status:Status,Endpoint:Endpoint.Address,Version:FullEngineVersion}'
```

输出示例：

```json
{
    "Status": "available",
    "Endpoint": "bloom-filter-test-xxxxx.serverless.use1.cache.amazonaws.com",
    "Version": "8.1"
}
```

!!! tip "连接准备"
    Serverless 集群需要 TLS 连接。确保你的测试实例与 ElastiCache 在同一 VPC 中，且 Security Group 允许 6379 端口入站。

### Step 2: 基础操作 — 创建与查询

连接到集群：

```bash
redis-cli -h <your-endpoint> -p 6379 --tls
```

创建一个 Bloom Filter（千分之一的误判率，容量 10,000）：

```bash
BF.RESERVE usernames 0.001 10000
# OK
```

添加元素：

```bash
BF.ADD usernames alice
# (integer) 1    ← 新元素

BF.MADD usernames bob charlie dave
# 1) (integer) 1
# 2) (integer) 1
# 3) (integer) 1
```

查询元素：

```bash
BF.EXISTS usernames alice
# (integer) 1    ← 可能存在

BF.EXISTS usernames eve
# (integer) 0    ← 一定不存在

BF.MEXISTS usernames alice eve charlie
# 1) (integer) 1
# 2) (integer) 0
# 3) (integer) 1
```

查看状态：

```bash
BF.CARD usernames
# (integer) 4

BF.INFO usernames
#  1) Capacity
#  2) (integer) 10000
#  3) Size
#  4) (integer) 18236
#  5) Number of filters
#  6) (integer) 1
#  7) Number of items inserted
#  8) (integer) 4
#  9) Error rate
# 10) "0.001"
# 11) Expansion rate
# 12) (integer) 2
# 13) Tightening ratio
# 14) "0.5"
# 15) Max scaled capacity
# 16) (integer) 20470000
```

### Step 3: BF.INSERT 一步到位

`BF.INSERT` 可以在创建 Bloom Filter 的同时添加元素：

```bash
BF.INSERT products CAPACITY 5000 ERROR 0.001 ITEMS laptop phone tablet
# 1) (integer) 1
# 2) (integer) 1
# 3) (integer) 1
```

### Step 4: Non-scaling Bloom Filter 边界测试

创建一个容量仅为 10 的 Non-scaling Bloom Filter：

```bash
BF.RESERVE limited_filter 0.01 10 NONSCALING
# OK
```

添加 10 个元素后尝试添加第 11 个：

```bash
# 添加 10 个元素...
BF.ADD limited_filter item_1
# ...
BF.ADD limited_filter item_10

# 尝试第 11 个
BF.ADD limited_filter item_11
# (error) ERR non scaling filter is full
```

!!! warning "Non-scaling 溢出行为"
    Non-scaling Bloom Filter 达到容量上限后，新元素添加会返回 `ERR non scaling filter is full`。在生产环境中，如果无法准确预估元素数量，建议使用默认的 Scaling 模式。

### Step 5: Scaling 自动扩展验证

```bash
BF.RESERVE scaling_filter 0.01 100 EXPANSION 2
# OK
```

添加 150 个元素后查看信息：

```bash
BF.INFO scaling_filter
#  1) Capacity
#  2) (integer) 300       ← 原始 100 + 扩展 200
#  5) Number of filters
#  6) (integer) 2         ← 已自动创建第 2 个 sub-filter
#  7) Number of items inserted
#  8) (integer) 150
```

## 测试结果

### 内存对比：Bloom Filter vs Set（10 万元素）

| 数据结构 | 元素数量 | 实测内存 | 来源 |
|---------|---------|---------|------|
| Bloom Filter (fp_rate=0.01) | 100,000 | **~120 KB** | BF.INFO SIZE |
| Set (hashtable) | 100,000 | **~6-8 MB**（理论值） | Redis Set 编码开销 |
| **内存节省** | | **>98%** | |

!!! note "Serverless 限制"
    `MEMORY USAGE` 命令在 ElastiCache Serverless 上不可用，Set 内存为基于 Redis/Valkey hashtable 编码的理论计算值。Bloom Filter 的 120 KB 是通过 `BF.INFO SIZE` 获取的精确值。

### False Positive 率验证

| 配置 | 添加元素 | 检查不存在元素 | False Positive 数 | 实测 FP 率 |
|------|---------|--------------|-----------------|-----------|
| fp_rate=0.01 (1%) | 10,000 | 10,000 | 87 | **0.87%** |

实测 False Positive 率 0.87%，低于配置的 1% 上限 ✅

### Scaling 行为

| 配置 | 初始容量 | 添加元素 | 扩展后容量 | Sub-filter 数 |
|------|---------|---------|-----------|--------------|
| capacity=100, expansion=2 | 100 | 150 | 300 | 2 |

## 踩坑记录

!!! warning "ElastiCache Serverless 命令限制"
    以下命令在 ElastiCache Serverless 上**不可用**：
    
    - `MEMORY USAGE` — 返回 `ERR unknown command 'memory'`
    - `DEBUG OBJECT` — 返回 `ERR unknown command 'debug'`
    - `OBJECT ENCODING` — 返回 `ERR unknown command 'object'`
    - `INFO memory` — 无输出
    
    这是 Serverless 架构的限制，非 Bloom Filter 特有问题。如需精确内存对比，建议使用 Node-based 集群。**（实测发现，官方文档未明确列出 Serverless 限制的完整命令清单）**

!!! warning "BF.CARD 与实际添加数可能不完全一致"
    向 Bloom Filter 添加 100,000 个不同元素后，`BF.CARD` 返回 99,809 而非 100,000。这是因为 Bloom Filter 基于哈希函数，极少量不同的输入可能产生完全相同的哈希值，被 Bloom Filter 视为 "已存在" 而不增加计数。这是正常行为，不影响实际使用。**（Bloom Filter 数据结构的固有特性）**

## 费用明细

| 资源 | 计费方式 | 预估费用 |
|------|---------|---------|
| ElastiCache Serverless | ECPU + 存储 | < $1 |
| EC2 t3.micro（测试客户端） | 按小时 | < $0.50 |
| **合计** | | **< $3** |

!!! tip "Bloom Filter 无额外费用"
    Bloom Filter 作为内置数据类型，不产生额外的许可或使用费用，包含在 ElastiCache 常规计费中。

## 清理资源

```bash
# 1. 删除 ElastiCache Serverless 集群
aws elasticache delete-serverless-cache \
  --serverless-cache-name bloom-filter-test \
  --region us-east-1

# 2. 终止 EC2 测试实例
aws ec2 terminate-instances \
  --instance-ids <your-instance-id> \
  --region us-east-1

# 3. 等待实例终止后删除 Security Group
aws ec2 wait instance-terminated \
  --instance-ids <your-instance-id> \
  --region us-east-1

aws ec2 delete-security-group \
  --group-id <your-ec2-sg-id> \
  --region us-east-1
```

!!! danger "务必清理"
    ElastiCache Serverless 按使用量计费，即使空闲也会产生最低存储费用。Lab 完成后请及时删除集群。

## 结论与建议

**Bloom Filter 适合什么场景？**

✅ 大规模存在性检查（百万级 / 亿级元素）且能容忍极低误判率
✅ 对内存效率要求极高的场景（比 Set 节省 >98% 内存）
✅ 只需要 "添加 + 查询"，不需要删除元素的场景

**不适合什么场景？**

❌ 需要精确判断的场景（如金融交易去重）
❌ 需要频繁删除元素的场景
❌ 元素数量较少（<1000），内存节省不明显

**生产环境建议**：

1. **选择合适的 fp_rate**：默认即可（0.01），对精度要求高可设 0.001，但会增加内存
2. **容量规划**：如果能预估元素数量，可在 `BF.RESERVE` 时设置准确的 capacity，避免不必要的 scaling
3. **监控**：关注 `BloomFilterBasedCmds` CloudWatch 指标，追踪使用情况
4. **迁移友好**：完全兼容 valkey-bloom 模块 API，从自建 Valkey 迁移无缝衔接

## 参考链接

- [AWS What's New: Bloom filter support in Amazon ElastiCache](https://aws.amazon.com/about-aws/whats-new/2025/07/bloom-filter-amazon-elasticache/)
- [ElastiCache 官方文档: Getting started with Bloom filters](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/BloomFilters.html)
- [Valkey Bloom Filter 文档](https://valkey.io/topics/bloomfilters/)
- [Valkey Bloom Filter 命令参考](https://valkey.io/commands/#bloom)
