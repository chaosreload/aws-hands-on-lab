# Amazon ElastiCache Memcached 水平自动扩缩实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

ElastiCache for Memcached 一直以来只支持手动增减节点来调整集群容量。2025 年 4 月，AWS 正式发布了 **Memcached 水平自动扩缩**功能，通过集成 AWS Application Auto Scaling 服务，让 Memcached 集群能够根据 CloudWatch 指标或预设时间表自动增减节点数量。

这对生产环境意味着：

- **弹性应对流量波动**：不再需要人工值守或预估峰值容量
- **成本优化**：低谷期自动缩容，避免资源浪费
- **运维减负**：告别手动扩缩的操作风险

## 前置条件

- AWS 账号（需要 ElastiCache、Application Auto Scaling、CloudWatch 权限）
- AWS CLI v2 已配置
- **注意**：burstable 实例类型（如 t3、t4g）不支持 autoscaling，必须使用 m、r 或 c 系列的 large 及以上规格

## 核心概念

### 扩缩维度对比

Memcached 与 Redis/Valkey 的 autoscaling 架构有本质差异：

| 维度 | Memcached | Redis/Valkey |
|------|-----------|--------------|
| 扩缩对象 | 节点数（Nodes） | 分片数（Shards）+ 副本数（Replicas） |
| Scalable Dimension | `elasticache:cache-cluster:Nodes` | `elasticache:replication-group:NodeGroups` |
| Resource ID 格式 | `cache-cluster/{name}` | `replication-group/{name}` |
| 预定义指标 | `ElastiCacheEngineCPUUtilization` | `ElastiCachePrimaryEngineCPUUtilization` 等 |

### 两种扩缩策略

1. **Target Tracking**：设定目标指标值（如 CPU 50%），系统自动维持
2. **Scheduled Scaling**：按时间表调整 min/max 容量边界

**关键行为**：Scheduled Scaling 只调整 min/max 边界，实际的节点增减由 Target Tracking 策略驱动。两者配合使用效果最佳。

## 动手实践

### Step 1: 创建 Memcached 集群

```bash
aws elasticache create-cache-cluster \
  --cache-cluster-id mem-autoscale-test \
  --engine memcached \
  --cache-node-type cache.m7g.large \
  --num-cache-nodes 2 \
  --region us-east-1
```

等待集群状态变为 `available`：

```bash
aws elasticache wait cache-cluster-available \
  --cache-cluster-id mem-autoscale-test \
  --region us-east-1
```

验证集群信息：

```bash
aws elasticache describe-cache-clusters \
  --cache-cluster-id mem-autoscale-test \
  --show-cache-node-info \
  --region us-east-1 \
  --query 'CacheClusters[0].{Status:CacheClusterStatus,Nodes:NumCacheNodes,ConfigEndpoint:ConfigurationEndpoint,NodeType:CacheNodeType}'
```

### Step 2: 注册 Scalable Target

将集群注册为 Application Auto Scaling 的可扩缩目标，设置最小 1 节点、最大 5 节点：

```bash
aws application-autoscaling register-scalable-target \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --min-capacity 1 \
  --max-capacity 5 \
  --region us-east-1
```

验证注册：

```bash
aws application-autoscaling describe-scalable-targets \
  --service-namespace elasticache \
  --resource-id cache-cluster/mem-autoscale-test \
  --region us-east-1
```

### Step 3: 配置 Target Tracking 策略

创建策略配置文件：

```bash
cat > /tmp/target-tracking-config.json << 'EOF'
{
  "TargetValue": 50,
  "PredefinedMetricSpecification": {
    "PredefinedMetricType": "ElastiCacheEngineCPUUtilization"
  },
  "ScaleOutCooldown": 300,
  "ScaleInCooldown": 300
}
EOF
```

应用策略：

```bash
aws application-autoscaling put-scaling-policy \
  --policy-name mem-cpu-target-tracking \
  --policy-type TargetTrackingScaling \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --target-tracking-scaling-policy-configuration file:///tmp/target-tracking-config.json \
  --region us-east-1
```

系统自动创建两个 CloudWatch 告警：

```bash
aws cloudwatch describe-alarms \
  --alarm-name-prefix 'TargetTracking-cache-cluster/mem-autoscale-test' \
  --region us-east-1 \
  --query 'MetricAlarms[*].{Name:AlarmName,State:StateValue,Threshold:Threshold}'
```

你会看到：

- **AlarmHigh**：CPUUtilization > 50%（触发扩容）
- **AlarmLow**：CPUUtilization < 37.5%（触发缩容，= 目标值 × 70%）

### Step 4: 配置 Scheduled Scaling

设置一个定时扩容（例如每天业务高峰前扩到 4 节点）：

```bash
aws application-autoscaling put-scheduled-action \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --scheduled-action-name mem-peak-hours \
  --schedule "at(2026-03-28T10:00:00)" \
  --scalable-target-action MinCapacity=4,MaxCapacity=5 \
  --region us-east-1
```

### Step 5: 监控扩缩活动

查看所有扩缩事件：

```bash
aws application-autoscaling describe-scaling-activities \
  --service-namespace elasticache \
  --resource-id cache-cluster/mem-autoscale-test \
  --region us-east-1
```

## 测试结果

### 扩缩时间实测

| 操作 | 触发方式 | 节点变化 | 耗时 |
|------|----------|----------|------|
| 扩容 | Scheduled（min 提升到 4） | 2 → 4 | ~4.5 分钟 |
| 缩容 | Target Tracking（CPU < 37.5%） | 4 → 3 | ~4.4 分钟 |

### 关键行为观察

1. **扩容触发机制**：当 Scheduled Action 将 min 提升到高于当前节点数时，Application Auto Scaling 立即触发扩容
2. **缩容是渐进式的**：Target Tracking 每次只减少 1 个节点，完成后等待 cooldown 期（本例 300 秒）再评估是否继续缩容
3. **CloudWatch 告警自动管理**：创建 Target Tracking 策略时自动创建，删除策略时自动删除
4. **Service-Linked Role**：首次注册时自动创建 `AWSServiceRoleForApplicationAutoScaling_ElastiCacheRG`

## 踩坑记录

!!! warning "踩坑 1：Burstable 实例不支持 Autoscaling"
    使用 `cache.t3.micro` 创建集群后，注册 scalable target 报错：
    ```
    ValidationException: The following instance: cache.t3.micro is not supported for AutoScaling
    ```
    **必须使用 m、r 或 c 系列的 large 及以上实例**。官方文档未明确列出 Memcached 支持的实例类型，但 Redis/Valkey 文档提到支持 Large、XLarge、2XLarge 规格的 R7g、R6g、M7g、M6g、M5、C7gn 等系列。实测 Memcached 与此一致。（实测发现，官方未明确记录）

!!! warning "踩坑 2：预定义指标名称与文档不符"
    Memcached 文档中写的 `ElastiCacheCPUUtilization` 是**无效的** PredefinedMetricType。API 报 ValidationException 要求使用枚举中的有效值。正确的指标名是 `ElastiCacheEngineCPUUtilization`。（实测发现，疑似文档 bug）

!!! warning "踩坑 3：Max Capacity 上限 90"
    尝试设置 `--max-capacity 100` 时报错：
    ```
    ValidationException: Maximum capacity cannot be greater than 90
    ```
    即使 Memcached 集群的 Service Quota 是每集群 60 节点，Application Auto Scaling API 有自己的硬限制 90。（实测发现，官方未记录）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| cache.m7g.large × 2-4 节点 | ~$0.158/hr | ~0.5 hr | ~$0.32 |
| CloudWatch 告警 | 免费层 | 2 个 | $0.00 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
# 1. 删除 scaling policy
aws application-autoscaling delete-scaling-policy \
  --policy-name mem-cpu-target-tracking \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --region us-east-1

# 2. 删除 scheduled actions
aws application-autoscaling delete-scheduled-action \
  --scheduled-action-name mem-peak-hours \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --region us-east-1

# 3. 取消注册 scalable target
aws application-autoscaling deregister-scalable-target \
  --service-namespace elasticache \
  --scalable-dimension elasticache:cache-cluster:Nodes \
  --resource-id cache-cluster/mem-autoscale-test \
  --region us-east-1

# 4. 删除 ElastiCache 集群
aws elasticache delete-cache-cluster \
  --cache-cluster-id mem-autoscale-test \
  --region us-east-1
```

!!! danger "务必清理"
    cache.m7g.large 按小时计费（约 $0.158/hr per node）。Lab 完成后请执行清理步骤，避免产生意外费用。删除顺序很重要：先清理 autoscaling 配置，再删除集群。

## 结论与建议

### 适用场景

- **流量有明显波峰波谷的缓存集群**：配合 Scheduled + Target Tracking 双策略
- **需要弹性应对突发流量**：单独使用 Target Tracking，自动响应负载变化
- **希望从 Serverless 迁移到 Node-based 但保留弹性**：Autoscaling 提供类似的容量自适应能力

### 生产环境建议

1. **实例选型**：推荐 m7g.large 或 r7g.large（性价比最优且支持 autoscaling）
2. **双策略配合**：Scheduled 设定基线容量 + Target Tracking 应对突发
3. **Cooldown 调优**：生产环境建议 ScaleOutCooldown=300、ScaleInCooldown=600（缩容更保守）
4. **注意缩容数据丢失**：Memcached 无持久化，被移除节点的缓存数据直接丢失，确保应用能承受 cache miss
5. **监控 Scaling Activities**：定期检查扩缩日志，确认策略按预期工作

### vs Serverless Memcached

| 维度 | Node-based + Autoscaling | Serverless |
|------|--------------------------|------------|
| 容量控制 | 自定义 min/max + 策略 | 全自动 |
| 成本模型 | 按节点小时计费 | 按用量计费 |
| 实例选择 | 灵活选择实例类型 | 无需选择 |
| 适用场景 | 需要精细控制的大规模集群 | 快速启动、中小规模 |

## 参考链接

- [AWS What's New: Horizontal autoscaling for ElastiCache Memcached](https://aws.amazon.com/about-aws/whats-new/2025/04/horizontal-autoscaling-amazon-elasticache-memcached/)
- [ElastiCache 文档: On-demand scaling for Memcached clusters](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Scaling-self-designed.mem-heading.html)
- [Application Auto Scaling: ElastiCache integration](https://docs.aws.amazon.com/autoscaling/application/userguide/services-that-can-integrate-elasticache.html)
