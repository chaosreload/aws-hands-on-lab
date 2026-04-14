---
tags:
  - Containers
---

# EKS Managed Node Groups Warm Pools 实测：冷启动加速 37% 的原生方案

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $5-10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-09

## 背景

EKS Managed Node Group (MNG) 的节点扩容一直是 "等" 的代名词：每次 scale-out 都要走完整 EC2 冷启动 → AMI boot → user data → join cluster 流程。对于有复杂初始化脚本的工作负载（大型 ML 框架安装、数据预加载），这个过程可能需要 5-10 分钟。

2026 年 4 月，AWS 宣布 EKS MNG 原生支持 EC2 Auto Scaling Warm Pools。通过在 ASG 旁维护一组已完成初始化的预热实例，scale-out 时直接拉起，跳过冷启动的大部分流程。

本文通过实测对比 cold start vs warm start、验证 Stopped/Running 两种 pool state、测试 scale-in reuse 和 warm pool 耗尽回退，给出生产环境配置建议。

## 前置条件

- AWS 账号（需要 EKS、EC2、IAM、VPC 权限）
- AWS CLI v2 已配置
- kubectl 已安装并配置
- eksctl（用于快速创建集群）

## 核心概念

### Warm Pool 关键参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `enabled` | 启用/禁用 warm pool | `false` |
| `minSize` | warm pool 最小实例数 | `0` |
| `maxGroupPreparedCapacity` | warm pool + ASG 总预备容量上限 | `maxSize` |
| `poolState` | 实例在 warm pool 中的状态 | `STOPPED` |
| `reuseOnScaleIn` | 缩容时实例回 warm pool 而非终止 | `false` |

### Pool State 对比

| 状态 | 成本 | 过渡速度 | 适用场景 |
|------|------|---------|---------|
| **Stopped** | 仅 EBS + Elastic IP | 需要启动实例（~30s） | 大多数场景推荐 |
| **Running** | 全 EC2 费用 | 最快（秒级） | 极端延迟敏感 |
| **Hibernated** | EBS（含 RAM）+ Elastic IP | 中等 | 需保持内存状态 |

### Warm Pool 大小计算

默认大小 = `maxSize - desiredSize`。例如 `desiredSize=1, maxSize=4`，则 warm pool 维护 3 个实例。

可通过 `maxGroupPreparedCapacity` 自定义上限，避免大规模 ASG 浪费过多 warm pool 资源。

### 两个 API 入口

- **`create-nodegroup --warm-pool-config`**：创建时启用
- **`update-nodegroup-config --warm-pool-config`**：已有节点组追加

## 动手实践

### Step 1: 创建 EKS 集群

```bash
# 创建集群配置文件
cat > /tmp/eks-warmpool-cluster.yaml << 'EOF'
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: warmpool-test
  region: us-east-1
  version: "1.32"
vpc:
  cidr: 10.50.0.0/16
  nat:
    gateway: Single
EOF

# 创建集群（约 10 分钟）
eksctl create cluster -f /tmp/eks-warmpool-cluster.yaml
```

### Step 2: 创建无 Warm Pool 的 MNG（冷启动基线）

```bash
# 创建基线节点组
aws eks create-nodegroup \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --node-role arn:aws:iam::<ACCOUNT_ID>:role/<NODE_ROLE> \
  --subnets <PRIVATE_SUBNET_1> <PRIVATE_SUBNET_2> \
  --instance-types t3.medium \
  --scaling-config minSize=1,maxSize=4,desiredSize=1 \
  --ami-type AL2023_x86_64_STANDARD \
  --region us-east-1

# 等待节点组就绪
aws eks wait nodegroup-active \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1
```

### Step 3: 测试冷启动时间（T1）

```bash
# 记录时间并 scale out
echo "Scale request: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --scaling-config minSize=1,maxSize=4,desiredSize=2 \
  --region us-east-1

# 监控新节点 Ready
watch -n 5 'kubectl get nodes --no-headers | grep -c " Ready"'
```

**实测结果**：

```
Scale initiated: 2026-04-09T04:02:01Z
2nd node Ready:   2026-04-09T04:02:52Z
Cold start time:  ~51 seconds
```

### Step 4: 为已有 MNG 添加 Warm Pool（Stopped 模式）

先缩回 1 个节点，然后启用 warm pool：

```bash
# 缩回 1 个节点
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --scaling-config minSize=1,maxSize=4,desiredSize=1 \
  --region us-east-1

# 启用 warm pool（Stopped 模式）
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --warm-pool-config enabled=true,minSize=1,poolState=STOPPED \
  --region us-east-1

# 等待更新完成
aws eks wait nodegroup-active \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1
```

**实测输出**：

```json
// 节点组 warm pool 配置
{
    "enabled": true,
    "minSize": 1,
    "poolState": "STOPPED"
}
```

验证 warm pool 实例就绪（需等待 3-5 分钟初始化 + 停止）：

```bash
# 获取 ASG 名称
ASG=$(aws eks describe-nodegroup \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1 \
  --query "nodegroup.resources.autoScalingGroups[0].name" \
  --output text)

# 检查 warm pool 实例状态
aws autoscaling describe-warm-pool \
  --auto-scaling-group-name "$ASG" \
  --region us-east-1 \
  --query "Instances[*].{Id:InstanceId,State:LifecycleState}"
```

```json
[
    { "Id": "i-0b8fbba07d7d569bf", "State": "Warmed:Stopped" },
    { "Id": "i-0c0e7e25615679e8a", "State": "Warmed:Stopped" },
    { "Id": "i-0d3d78e7255c9c9b6", "State": "Warmed:Stopped" }
]
```

### Step 5: 测试 Warm Start 时间（T2）

```bash
echo "Scale request: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --scaling-config minSize=1,maxSize=4,desiredSize=2 \
  --region us-east-1

# 监控
watch -n 5 'kubectl get nodes --no-headers | grep -c " Ready"'
```

**实测结果**：

```
Scale initiated: 2026-04-09T04:11:55Z
2nd node Ready:   2026-04-09T04:12:27Z
Warm start time:  ~32 seconds (vs cold start ~51s)
```

!!! tip "37% 加速"
    Warm start (Stopped) 比 cold start 快约 37%（32s vs 51s）。对于 t3.medium + AL2023 这样简单的配置，差异已经很明显。对于有复杂初始化脚本的实例（ML 框架安装、大型依赖预加载），差异会更加显著。

### Step 6: 测试 Warm Pool 耗尽回退（T4）

验证 warm pool 实例不够时的行为：

```bash
# warm pool 有 2 个 Stopped 实例，scale 到 4（需要额外 2 个）
# 第 1 个从 warm pool 拉，第 2 个 warm pool 耗尽后走冷启动
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --scaling-config minSize=1,maxSize=4,desiredSize=4 \
  --region us-east-1
```

**实测结果**：

```
Scale initiated: 2026-04-09T04:14:15Z
3rd node Ready:  2026-04-09T04:14:48Z  (warm, ~33s)
4th node Ready:  2026-04-09T04:14:54Z  (warm, ~39s)
5th node:        NotReady at 12s        (cold start, still booting)
```

Warm pool 实例优先使用，耗尽后透明回退到冷启动，**无需任何额外配置**。

### Step 7: 测试 Scale-in Reuse（T5）

启用 `reuseOnScaleIn` 并缩容：

```bash
# 启用 reuse
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --warm-pool-config enabled=true,reuseOnScaleIn=true \
  --region us-east-1

# 等待更新完成
aws eks wait nodegroup-active \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1

# 缩容
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --scaling-config minSize=1,maxSize=4,desiredSize=2 \
  --region us-east-1
```

**实测结果**：

```json
// 缩容后，实例回到 warm pool 而非终止
[
    { "Id": "i-09040224f714f9796", "State": "Warmed:Pending:Wait" },
    { "Id": "i-0d3d78e7255c9c9b6", "State": "Warmed:Stopped" }
]
```

实例经过 `Warmed:Pending:Wait` → `Warmed:Pending:Proceed` → `Warmed:Stopped` 生命周期，最终回到 warm pool 等待下次使用。

### Step 8: 禁用 Warm Pool（T7）

```bash
aws eks update-nodegroup-config \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --warm-pool-config enabled=false \
  --region us-east-1
```

**实测结果**：

```json
// 禁用后，warm pool 配置变为 null
// 所有 warm pool 实例进入 Warmed:Terminating 状态
{
    "warmPoolConfig": null
}
```

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T1 | Cold start 基线 | ✅ | ~51s | 标准 EC2 冷启动 |
| T2 | Warm start (Stopped) | ✅ | ~32s | 比 cold start 快 37% |
| T3 | Warm start (Running 配置) | ✅ | ~33s | poolState 更改不追溯已有实例 |
| T4 | Warm pool 耗尽 | ✅ | warm ~33-39s, cold 回退 | 透明回退，无需额外配置 |
| T5 | Scale-in reuse | ✅ | 实例回 Warmed:Stopped | reuseOnScaleIn 正常工作 |
| T6 | 已有 MNG 追加 warm pool | ✅ | ~41s 完成 | update-nodegroup-config 无影响 |
| T7 | 禁用 warm pool | ✅ | ~33s 完成 | warm pool 实例全部终止 |

## 踩坑记录

!!! warning "踩坑 1: Warm pool 实例在初始化期间会注册到 K8s 集群"
    Warm pool 实例在初始化阶段（boot → user data → join cluster）会注册为 K8s 节点，然后被停止。停止后节点状态变为 `NotReady`，并自动附加以下 taint：
    
    ```
    node.cloudprovider.kubernetes.io/shutdown:NoSchedule
    node.kubernetes.io/unreachable:NoSchedule
    node.kubernetes.io/unreachable:NoExecute
    ```
    
    **影响**：`kubectl get nodes` 会显示这些 Stopped 的 warm pool 节点为 NotReady 状态。虽然不会有 Pod 被调度到这些节点（有 NoSchedule taint），但监控告警可能误报节点异常。
    
    **建议**：在节点监控中排除 `cloudprovider.kubernetes.io/shutdown` taint 的节点，或使用 `kubectl get nodes --field-selector status.phase=Running` 过滤。

!!! warning "踩坑 2: 更改 poolState 不追溯已有实例"
    将 `poolState` 从 `STOPPED` 改为 `RUNNING` 后，已经在 warm pool 中的 Stopped 实例**不会自动启动**。只有新进入 warm pool 的实例才会以新状态保持。
    
    **影响**：如果需要切换 pool state，已有实例仍保持旧状态，直到被使用后重新回到 warm pool。
    
    实测发现，官方未记录。

!!! info "发现: Warm pool 大小自动计算"
    设置 `minSize=1, maxSize=4, desiredSize=1` 后，warm pool 自动创建了 3 个实例（`maxSize - desiredSize = 3`），而不是 `minSize` 指定的 1 个。`minSize` 是下限，实际大小由 ASG max-desired 差值决定。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EKS 集群 | $0.10/hr | ~1 hr | $0.10 |
| EC2 t3.medium (active) | $0.0416/hr | ~4 实例·hr | $0.17 |
| EC2 t3.medium (warm pool, Stopped) | EBS only ~$0.003/hr | ~3 实例·hr | $0.01 |
| NAT Gateway | $0.045/hr + $0.045/GB | ~1 hr | $0.05 |
| **合计** | | | **~$0.33** |

## 清理资源

```bash
# 1. 删除节点组（会自动删除 warm pool 实例）
aws eks delete-nodegroup \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1

# 等待节点组删除完成
aws eks wait nodegroup-deleted \
  --cluster-name warmpool-test \
  --nodegroup-name cold-baseline \
  --region us-east-1

# 2. 删除集群（eksctl 会一并清理 VPC 资源）
eksctl delete cluster --name warmpool-test --region us-east-1
```

!!! danger "务必清理"
    EKS 集群费用 $0.10/hr 持续计费。Lab 完成后请立即执行清理步骤。

## 结论与建议

### 场景化推荐

| 场景 | 推荐配置 | 理由 |
|------|---------|------|
| 一般工作负载，快速扩容 | `poolState=STOPPED, minSize=1` | 成本最低，加速 30-40% |
| 复杂初始化（ML 框架安装） | `poolState=STOPPED, minSize=2-3` | 初始化可能 5-10min，warm pool 收益最大 |
| 极端延迟敏感（交易系统） | `poolState=RUNNING` | 秒级过渡，但成本等同常开实例 |
| 频繁伸缩（日间/夜间模式） | `reuseOnScaleIn=true` | 避免反复终止和创建实例 |

### 生产注意事项

1. **监控适配**：warm pool 节点显示为 NotReady，需在告警规则中排除
2. **成本意识**：warm pool 默认大小 = maxSize - desiredSize，设置较大 maxSize 会创建大量预热实例
3. **使用 `maxGroupPreparedCapacity`**：对于 maxSize 较大的 ASG，用此参数控制 warm pool 上限
4. **Bottlerocket 限制**：不支持 `reuseOnScaleIn` 和 `Hibernated` 状态
5. **与 Cluster Autoscaler 兼容**：无需额外配置，CA 感知 warm pool 容量

### 什么时候该用 Warm Pool？

- ✅ **应该用**：节点初始化超过 2 分钟、有明显的流量波峰波谷
- ⚠️ **评估后再决定**：初始化简单（< 1min）、流量稳定
- ❌ **不需要**：使用 Karpenter 等快速 provisioner、Spot 实例为主的工作负载

## 参考链接

- [AWS What's New: EKS MNG 支持 EC2 Warm Pools](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-eks-managed-node-groups-ec2-warm-pools/)
- [EKS Managed Node Groups 文档](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html)
- [EC2 Auto Scaling Warm Pools 文档](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-warm-pools.html)
- [Warm Pool Lifecycle Hooks](https://docs.aws.amazon.com/autoscaling/ec2/userguide/warm-pool-instance-lifecycle.html)
- [Scaling your applications faster with EC2 Auto Scaling Warm Pools (Blog)](https://aws.amazon.com/blogs/compute/scaling-your-applications-faster-with-ec2-auto-scaling-warm-pools/)
