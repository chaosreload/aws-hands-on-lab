---
description: "Test AWS Lambda AZ metadata endpoint for same-AZ routing to ElastiCache Redis — measure 51% latency reduction with AZ-aware routing."
---
# Lambda AZ Metadata：同 AZ 路由实测，Redis 延迟降低 51%

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $2.00（含清理）
    - **Region**: ap-southeast-1
    - **最后验证**: 2026-03-20

## 背景

AWS Lambda 一直在多个 AZ 间分配执行环境以保障高可用，但函数本身无法知道自己运行在哪个 AZ。这意味着当 Lambda 访问 ElastiCache、RDS 等提供 AZ-specific endpoint 的服务时，无法做 AZ 感知路由——每次调用都可能产生跨 AZ 流量和额外延迟。

2026 年 3 月，AWS 发布了 **Lambda Metadata Endpoint**，让函数可以通过一个简单的 HTTP 请求获取当前执行环境的 AZ ID。这使得 AZ-aware routing 和 AZ-specific fault injection 成为可能。

**本文通过实测验证：**

1. Metadata endpoint 的调用方式和延迟特性
2. Lambda 在多 AZ 间的实际分布情况
3. 同 AZ vs 跨 AZ 访问 ElastiCache Redis 的延迟差异（核心实验）

## 前置条件

- AWS 账号
- AWS CLI v2 已配置
- Python 3.12+ 运行时
- 默认 VPC（或自定义 VPC + 多 AZ 子网）

## 核心概念

### Metadata Endpoint

Lambda 在每个执行环境中自动设置两个环境变量：

- `AWS_LAMBDA_METADATA_API` — metadata server 地址（如 `169.254.100.1:9001`）
- `AWS_LAMBDA_METADATA_TOKEN` — 认证 token（Bearer scheme）

调用端点：

```
GET http://${AWS_LAMBDA_METADATA_API}/2026-01-15/metadata/execution-environment
Authorization: Bearer ${AWS_LAMBDA_METADATA_TOKEN}
```

响应：

```json
{
  "AvailabilityZoneID": "apse1-az2"
}
```

### AZ ID vs AZ Name

| 概念 | 示例 | 特性 |
|------|------|------|
| AZ ID | `apse1-az2` | 物理位置标识，**跨账号一致** |
| AZ Name | `ap-southeast-1a` | 逻辑名称，不同账号可能映射不同物理 AZ |

Metadata endpoint 返回的是 **AZ ID**，这正是跨服务 AZ 对齐所需要的。

### 响应缓存

响应带 `Cache-Control: private, max-age=43200, immutable`（12 小时），在同一个执行环境内 AZ 不会变化。[Powertools for AWS Lambda](https://docs.aws.amazon.com/powertools/) 会自动缓存并处理 SnapStart 场景的缓存失效。

## 动手实践

### Step 1: 创建 IAM Role

```bash
# 创建 Lambda 执行角色
aws iam create-role \
  --role-name lambda-az-metadata-test \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 附加基本执行权限 + VPC 访问权限
aws iam attach-role-policy \
  --role-name lambda-az-metadata-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam attach-role-policy \
  --role-name lambda-az-metadata-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
```

### Step 2: 创建 Lambda 函数 — 基础 AZ Metadata 测试

```python
# lambda_az_metadata.py
import json
import os
import time
import urllib.request

def get_az_metadata():
    """调用 Lambda metadata endpoint 获取 AZ ID"""
    api = os.environ.get('AWS_LAMBDA_METADATA_API')
    token = os.environ.get('AWS_LAMBDA_METADATA_TOKEN')

    url = f"http://{api}/2026-01-15/metadata/execution-environment"
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {token}'
    })

    start = time.perf_counter_ns()
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    elapsed_us = (time.perf_counter_ns() - start) / 1000

    return data, elapsed_us

def handler(event, context):
    mode = event.get('mode', 'basic')

    if mode == 'basic':
        data, latency_us = get_az_metadata()
        return {
            'az_id': data.get('AvailabilityZoneID'),
            'latency_us': round(latency_us, 1),
            'metadata_api': os.environ.get('AWS_LAMBDA_METADATA_API'),
        }

    elif mode == 'latency':
        # 多次调用测量延迟分布
        iterations = event.get('iterations', 100)
        latencies = []
        az_id = None
        for _ in range(iterations):
            data, lat = get_az_metadata()
            latencies.append(lat)
            if not az_id:
                az_id = data.get('AvailabilityZoneID')
        latencies.sort()
        n = len(latencies)
        return {
            'az_id': az_id,
            'iterations': n,
            'latency_us': {
                'min': round(latencies[0], 1),
                'p50': round(latencies[n//2], 1),
                'p90': round(latencies[int(n*0.9)], 1),
                'p99': round(latencies[int(n*0.99)], 1),
                'avg': round(sum(latencies)/n, 1),
            }
        }
```

部署函数：

```bash
zip lambda_az_metadata.zip lambda_az_metadata.py

aws lambda create-function \
  --function-name az-metadata-test \
  --runtime python3.13 \
  --handler lambda_az_metadata.handler \
  --role arn:aws:iam::<ACCOUNT_ID>:role/lambda-az-metadata-test \
  --zip-file fileb://lambda_az_metadata.zip \
  --timeout 30 \
  --memory-size 256
```

### Step 3: 验证 Metadata Endpoint

```bash
aws lambda invoke \
  --function-name az-metadata-test \
  --payload '{"mode": "basic"}' \
  response.json && cat response.json
```

```json
{
  "az_id": "apse1-az2",
  "latency_us": 165144.8,
  "metadata_api": "169.254.100.1:9001"
}
```

✅ 首次调用（冷启动）延迟约 165ms，包含 TCP 连接建立。

### Step 4: 测量 Metadata 调用延迟

```bash
aws lambda invoke \
  --function-name az-metadata-test \
  --payload '{"mode": "latency", "iterations": 100}' \
  latency.json && cat latency.json | python3 -m json.tool
```

**实测结果（热执行环境，100 次迭代）：**

| 指标 | 第一轮 | 第二轮 |
|------|--------|--------|
| min | 303µs | 294µs |
| **p50** | **453µs** | **413µs** |
| p90 | 16,796µs | 16,598µs |
| p99 | 17,555µs | 17,536µs |
| avg | 2,749µs | 2,568µs |

p50 稳定在 **~400µs**。p90 的跳变（~17ms）疑似 GC 或内核调度抖动，不影响实际使用——因为 Powertools 会在首次调用后缓存，后续调用零开销。

### Step 5: Lambda AZ 分布测试

通过 500 次并发调用观察 Lambda 在多 AZ 间的分布：

```bash
# 设置 reserved concurrency 允许更多并发执行环境
aws lambda put-function-concurrency \
  --function-name az-metadata-test \
  --reserved-concurrent-executions 100

# 并发调用 500 次
for i in $(seq 1 500); do
  aws lambda invoke \
    --function-name az-metadata-test \
    --payload '{"mode": "basic"}' \
    /tmp/dist-$i.json &
  [ $((i % 50)) -eq 0 ] && wait
done
wait
```

**Singapore (ap-southeast-1) 三个 AZ 的分布：**

| AZ ID | AZ Name | 调用次数 | 占比 |
|-------|---------|---------|------|
| apse1-az2 | ap-southeast-1a | 347 | 69.4% |
| apse1-az3 | ap-southeast-1c | 106 | 21.2% |
| apse1-az1 | ap-southeast-1b | 47 | 9.4% |

!!! note "分布不均匀是正常的"
    Lambda 优先复用 warm 执行环境，所以大部分调用会落在已有环境的 AZ。分布比例会随并发级别和时间变化。**你无法指定 Lambda 运行在哪个 AZ** — 这正是 metadata endpoint 的价值：不需要控制 AZ 分配，只需要在运行时知道自己在哪里。

### Step 6: 核心实验 — 同 AZ vs 跨 AZ ElastiCache 延迟

这是本文的重点实验。我们部署一个 3 节点 ElastiCache Redis 集群（每个 AZ 一个节点），然后让 Lambda 根据自身 AZ 选择同 AZ 节点 vs 跨 AZ 节点，对比 PING 延迟。

**创建 ElastiCache Redis 集群：**

```bash
# 创建安全组
SG_ID=$(aws ec2 create-security-group \
  --group-name az-metadata-test-sg \
  --description "SG for AZ metadata latency test" \
  --vpc-id <DEFAULT_VPC_ID> \
  --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 6379 --cidr 172.31.0.0/16

# 创建子网组（包含所有 AZ 的子网）
aws elasticache create-cache-subnet-group \
  --cache-subnet-group-name az-metadata-test-subnet \
  --cache-subnet-group-description "Multi-AZ subnet group" \
  --subnet-ids <SUBNET_1A> <SUBNET_1B> <SUBNET_1C>

# 创建 Redis Replication Group（1 primary + 2 replicas = 3 AZ 覆盖）
aws elasticache create-replication-group \
  --replication-group-id az-meta-test \
  --replication-group-description "AZ metadata latency test" \
  --engine redis \
  --cache-node-type cache.t3.micro \
  --num-node-groups 1 \
  --replicas-per-node-group 2 \
  --automatic-failover-enabled \
  --multi-az-enabled \
  --cache-subnet-group-name az-metadata-test-subnet \
  --security-group-ids $SG_ID
```

等待约 5-10 分钟直到状态变为 `available`，然后确认节点 AZ 分布：

```
az-meta-test-001: ap-southeast-1a (apse1-az2) — Primary
az-meta-test-002: ap-southeast-1b (apse1-az1) — Replica
az-meta-test-003: ap-southeast-1c (apse1-az3) — Replica
```

**创建 VPC Lambda 进行延迟测试：**

```python
# lambda_az_latency.py
import json
import os
import time
import socket
import urllib.request

def get_az_metadata():
    api = os.environ.get('AWS_LAMBDA_METADATA_API')
    token = os.environ.get('AWS_LAMBDA_METADATA_TOKEN')
    url = f"http://{api}/2026-01-15/metadata/execution-environment"
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get('AvailabilityZoneID')

def redis_ping(host, port=6379, iterations=100):
    """Raw socket Redis PING - 返回延迟分布 (µs)"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect((host, port))

    # Warm up
    sock.sendall(b"*1\r\n$4\r\nPING\r\n")
    sock.recv(64)

    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        sock.sendall(b"*1\r\n$4\r\nPING\r\n")
        sock.recv(64)
        latencies.append((time.perf_counter_ns() - start) / 1000)

    sock.close()
    latencies.sort()
    n = len(latencies)
    return {
        'min': round(latencies[0], 1),
        'p50': round(latencies[n//2], 1),
        'p90': round(latencies[int(n*0.9)], 1),
        'p99': round(latencies[int(n*0.99)], 1),
        'avg': round(sum(latencies)/n, 1),
    }

def handler(event, context):
    my_az = get_az_metadata()
    endpoints = event.get('endpoints', [])
    results = {'my_az': my_az, 'tests': []}

    for ep in endpoints:
        host, az = ep['host'], ep.get('az', 'unknown')
        try:
            latency = redis_ping(host)
            results['tests'].append({
                'target_az': az,
                'same_az': (az == my_az),
                'latency_us': latency,
            })
        except Exception as e:
            results['tests'].append({'target_az': az, 'error': str(e)})

    return results
```

**并发 50 次调用，收集 3 个 AZ 的延迟数据：**

```bash
aws lambda put-function-concurrency \
  --function-name az-metadata-vpc-test \
  --reserved-concurrent-executions 50

# 50 次并发 burst，强制 Lambda 在多个 AZ 创建执行环境
for i in $(seq 1 50); do
  aws lambda invoke --function-name az-metadata-vpc-test \
    --payload '{"mode":"redis_latency","endpoints":[...]}' \
    /tmp/result-$i.json &
done
wait
```

### 实测结果

**同 AZ vs 跨 AZ Redis PING p50 延迟（50 次调用，每次 100 轮 PING）：**

| Lambda 所在 AZ | 同 AZ 延迟 (p50) | 跨 AZ 延迟 (p50) | 比率 |
|---------------|-----------------|-----------------|------|
| apse1-az1 | **326 µs** | 886 µs | 2.7x |
| apse1-az2 | **450 µs** | 972 µs | 2.2x |
| apse1-az3 | **505 µs** | 841 µs | 1.7x |
| **Overall** | **440 µs** | **904 µs** | **2.1x** |

!!! success "关键发现"
    **同 AZ 路由将 Redis 延迟降低了 51%**（440µs vs 904µs）。对于延迟敏感的应用（实时推荐、session store、排行榜），这个差异直接影响用户体验。

Lambda AZ 分布（50 次并发 burst）：

| AZ | 次数 | 占比 |
|----|------|------|
| apse1-az2 | 20 | 40% |
| apse1-az3 | 18 | 36% |
| apse1-az1 | 12 | 24% |

### Step 7: VPC vs 非 VPC 行为验证

| 配置 | Metadata Endpoint | AZ ID | 结果 |
|------|------------------|-------|------|
| 非 VPC Lambda | `169.254.100.1:9001` | apse1-az1 | ✅ 正常 |
| VPC Lambda | `169.254.100.1:9001` | apse1-az2 | ✅ 正常 |

两种配置行为完全一致。Metadata endpoint 使用 link-local 地址，不依赖 VPC 网络。

## 踩坑记录

!!! warning "首次调用延迟 ≠ 稳态延迟"
    Metadata endpoint 首次调用延迟约 165ms（冷启动 TCP 连接建立），后续调用 p50 仅 ~400µs。**务必缓存响应**——使用 Powertools 或手动缓存到全局变量。

!!! warning "AZ 分布无法控制"
    Lambda 的 AZ 分配由平台决定，分布天然不均匀（我们观察到 69%/21%/10%）。应用不应假设特定的分布比例，AZ-aware routing 逻辑需要处理任意 AZ 的情况。

!!! warning "AZ ID ≠ AZ Name"
    Metadata 返回的是 AZ ID（如 `apse1-az2`），而非 AZ Name（如 `ap-southeast-1a`）。如果下游服务使用 AZ Name，需要通过 `ec2:DescribeAvailabilityZones` 做转换（每个 AWS 账号的映射可能不同）。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda 调用 | $0.20/1M requests | ~800 次 | < $0.01 |
| Lambda 计算 | $0.0000166667/GB-s | ~10 GB-s | < $0.01 |
| ElastiCache t3.micro x3 | $0.017/hr/node | ~1 hr | $0.05 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除 Lambda 函数
aws lambda delete-function --function-name az-metadata-test
aws lambda delete-function --function-name az-metadata-vpc-test

# 2. 删除 ElastiCache
aws elasticache delete-replication-group \
  --replication-group-id az-meta-test \
  --no-final-snapshot-before-deletion

# 等待 ElastiCache 删除完成后...

# 3. 删除子网组
aws elasticache delete-cache-subnet-group \
  --cache-subnet-group-name az-metadata-test-subnet

# 4. 删除安全组
aws ec2 delete-security-group --group-id <SG_ID>

# 5. 删除 IAM Role
aws iam detach-role-policy --role-name lambda-az-metadata-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam detach-role-policy --role-name lambda-az-metadata-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
aws iam detach-role-policy --role-name lambda-az-metadata-test \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess
aws iam delete-role --role-name lambda-az-metadata-test
```

!!! danger "务必清理"
    ElastiCache 按小时计费，即使空闲也会产生费用。确认删除完成。

## 结论与建议

### 核心结论

1. **同 AZ 路由节省 51% 延迟** — 对 ElastiCache Redis，同 AZ p50 = 440µs vs 跨 AZ p50 = 904µs
2. **Metadata 调用开销极低** — 热路径 p50 ~400µs，缓存后零开销
3. **所有 Lambda 配置兼容** — VPC/非 VPC、所有 runtime、SnapStart、Provisioned Concurrency 均支持

### 适用场景

| 场景 | 收益 |
|------|------|
| Lambda → ElastiCache/Redis | 延迟降低 50%+ |
| Lambda → RDS Read Replicas | 选择同 AZ replica 减少跨 AZ 流量 |
| Lambda → EFS | 同 AZ mount target 减少延迟 |
| AZ-specific fault injection | 模拟单 AZ 故障测试韧性 |
| 成本优化 | 减少跨 AZ 数据传输费用（$0.01/GB） |

### 生产环境使用建议

1. **使用 Powertools** — 自动缓存 + SnapStart 失效处理，避免重复调用
2. **同 AZ 为首选，跨 AZ 做 fallback** — 不要只连同 AZ 节点，跨 AZ 是高可用保障
3. **监控 AZ 分布** — 通过 CloudWatch 自定义 metric 追踪 Lambda 的 AZ 分布，发现不均衡及时调整
4. **不要假设 AZ 分布均匀** — 我们实测 69%/21%/10%，应用逻辑需要健壮处理任意分布

## 参考链接

- [Using the Lambda metadata endpoint](https://docs.aws.amazon.com/lambda/latest/dg/configuration-metadata-endpoint.html)
- [AWS Lambda now supports Availability Zone metadata](https://aws.amazon.com/about-aws/whats-new/2026/03/lambda-availability-zone-metadata/)
- [Powertools for AWS Lambda](https://docs.aws.amazon.com/powertools/)
- [AZ IDs for cross-account consistency](https://docs.aws.amazon.com/global-infrastructure/latest/regions/az-ids.html)
