# EC2 Capacity Reservation Topology API：启动实例前掌握 GPU 容量的网络拓扑

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 20 分钟
    - **预估费用**: < $0.15（Capacity Reservation 按分钟计费，测试后立即取消）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

大规模 AI 训练和 HPC 工作负载中，成百上千的 GPU/Trainium 实例需要高效通信。这些实例在数据中心网络中的**物理位置**直接影响训练吞吐量——同一网络交换机下的实例通信延迟最低。

之前，AWS 提供了 `DescribeInstanceTopology` API，让你了解**已运行实例**的网络拓扑。但问题是：你必须先启动实例才能看到拓扑，这在容量规划阶段为时已晚。

**新功能** `DescribeCapacityReservationTopology` API 解决了这个"先有鸡还是先有蛋"的问题——创建 Capacity Reservation 后、启动实例前，就能看到预留容量在网络层级中的相对位置。

## 前置条件

- AWS 账号，IAM 用户/角色需要 `ec2:DescribeCapacityReservationTopology` 和 `ec2:CreateCapacityReservation` 权限
- AWS CLI v2 已配置
- 账号有支持实例类型（g6e/p4d/trn1 等）的 On-Demand 配额

## 核心概念

### AWS 网络拓扑模型

AWS 数据中心网络分为 3-4 层（Layer），每个实例或 Capacity Reservation 映射到一组 **NetworkNodes**：

```
Layer i   (顶层)    NN-1 ─────────────┐
                     │                 │
Layer ii  (中层)    NN-2              NN-3
                   ╱    ╲              │
Layer iii (底层) NN-4   NN-5         NN-6
                 │  │     │            │
               实例1 实例2  实例3      实例4
```

**规则**：共享的底层 NetworkNode 越多 → 物理距离越近 → 通信延迟越低。

### 两个 API 的关系

| 维度 | DescribeInstanceTopology | DescribeCapacityReservationTopology |
|------|-------------------------|-------------------------------------|
| 查询对象 | 运行中的实例 | pending/active 的 Capacity Reservation |
| 使用时机 | 实例启动后，做作业调度 | 实例启动前，做容量规划 |
| NetworkNodes 数量 | 3-4 个 | 1-3 个（取决于 CR 类型） |
| 定价 | 免费 | 免费 |

**典型工作流**：

1. 创建多个 Capacity Reservation → 调用 **CR Topology API** 了解预留容量的位置关系
2. 根据拓扑决定哪些 CR 的容量适合运行紧耦合的训练任务
3. 启动实例 → 调用 **Instance Topology API** 获取更细粒度的拓扑信息
4. 基于实例拓扑做精确的作业调度和 rank 分配

### 支持的实例类型

API 仅支持 AI/ML/HPC 实例类型：

- **GPU 实例**: g6e.\*, g7e.\*, p3dn.24xlarge, p4d.24xlarge, p4de.24xlarge, p5.48xlarge, p5e.48xlarge, p5en.48xlarge, p6e-gb200.36xlarge, p6-b200.48xlarge, p6-b300.48xlarge
- **Trainium 实例**: trn1.2xlarge, trn1.32xlarge, trn1n.32xlarge, trn2.48xlarge, trn2u.48xlarge
- **HPC 实例**: hpc6a.48xlarge, hpc6id.32xlarge, hpc7g.\*, hpc7a.\*, hpc8a.96xlarge

## 动手实践

### Step 1: 验证权限（DryRun）

```bash
aws ec2 describe-capacity-reservation-topology \
  --dry-run \
  --region us-east-1
```

预期输出：`DryRunOperation` 错误表示权限正常。

### Step 2: 创建 Capacity Reservation

创建 3 个 CR 用于对比拓扑——2 个在同一 AZ，1 个在不同 AZ：

```bash
# CR-1: us-east-1a
aws ec2 create-capacity-reservation \
  --instance-type g6e.xlarge \
  --instance-platform Linux/UNIX \
  --availability-zone us-east-1a \
  --instance-count 1 \
  --end-date-type unlimited \
  --instance-match-criteria open \
  --region us-east-1
# 记录输出中的 CapacityReservationId，例如 cr-aaa

# CR-2: us-east-1a（同 AZ）
aws ec2 create-capacity-reservation \
  --instance-type g6e.xlarge \
  --instance-platform Linux/UNIX \
  --availability-zone us-east-1a \
  --instance-count 1 \
  --end-date-type unlimited \
  --instance-match-criteria open \
  --region us-east-1
# 记录 cr-bbb

# CR-3: us-east-1b（不同 AZ）
aws ec2 create-capacity-reservation \
  --instance-type g6e.xlarge \
  --instance-platform Linux/UNIX \
  --availability-zone us-east-1b \
  --instance-count 1 \
  --end-date-type unlimited \
  --instance-match-criteria open \
  --region us-east-1
# 记录 cr-ccc
```

!!! warning "费用提示"
    Capacity Reservation 创建即开始计费（按 On-Demand 费率），即使没有启动实例。g6e.xlarge 约 $0.84/小时。测试完成后请立即取消。

### Step 3: 查询 Capacity Reservation 拓扑

```bash
# 查询所有 CR 的拓扑
aws ec2 describe-capacity-reservation-topology \
  --capacity-reservation-ids cr-aaa cr-bbb cr-ccc \
  --region us-east-1
```

### Step 4: 在 Cluster Placement Group 中创建 CR（进阶）

Cluster Placement Group 能获得更精细的拓扑信息：

```bash
# 创建 Cluster Placement Group
aws ec2 create-placement-group \
  --group-name topology-test-cpg \
  --strategy cluster \
  --region us-east-1

# 在 CPG 中创建 CR
aws ec2 create-capacity-reservation \
  --instance-type g6e.xlarge \
  --instance-platform Linux/UNIX \
  --availability-zone us-east-1a \
  --instance-count 1 \
  --end-date-type unlimited \
  --instance-match-criteria open \
  --placement-group-arn arn:aws:ec2:us-east-1:<ACCOUNT_ID>:placement-group/topology-test-cpg \
  --region us-east-1
# 记录 cr-ddd

# 对比普通 CR 和 CPG CR 的拓扑
aws ec2 describe-capacity-reservation-topology \
  --capacity-reservation-ids cr-aaa cr-ddd \
  --region us-east-1
```

### Step 5: 按条件过滤查询

```bash
# 按实例类型过滤
aws ec2 describe-capacity-reservation-topology \
  --filters Name=instance-type,Values=g6e.xlarge \
  --region us-east-1

# 按 AZ 过滤
aws ec2 describe-capacity-reservation-topology \
  --filters Name=availability-zone,Values=us-east-1a \
  --region us-east-1
```

## 测试结果

### 拓扑对比数据

| CR 类型 | AZ | NetworkNodes | 节点数 | 说明 |
|---------|-----|-------------|--------|------|
| 普通 CR | us-east-1a | `nn-a42db...` | 1 | 仅上层节点 |
| 普通 CR（同 AZ） | us-east-1a | `nn-a42db...` | 1 | 共享同一上层节点 ✅ |
| 普通 CR（不同 AZ） | us-east-1b | `nn-7fa84...` | 1 | 完全不同的节点 |
| CPG 中的 CR | us-east-1a | `nn-a42db...`, `nn-77092...` | 2 | 多一层拓扑粒度 |

### 关键发现

**1. 同 AZ 内的 CR 共享上层 NetworkNode**

两个 us-east-1a 的普通 CR 返回相同的 `nn-a42db10b9625f87f8`，说明它们在网络层级中位于同一上层节点下——这意味着这两个 CR 中启动的实例将有较好的网络接近性。

**2. Cluster Placement Group 提供更精细的拓扑**

这是本次实验最重要的发现：

- **普通 CR**：返回 1 个 NetworkNode（粗粒度）
- **CPG 中的 CR**：返回 2 个 NetworkNode（更精细）

CPG CR 的拓扑中，上层节点 `nn-a42db...` 与普通 CR 一致（验证了层级关系），同时多了底层节点 `nn-77092...` 提供更精确的位置信息。

**这说明：如果你需要精确的容量拓扑信息来优化作业调度，应该在 Cluster Placement Group 中创建 Capacity Reservation。**

**3. 不支持实例类型的优雅降级**

对 m5.large CR 调用 API，返回记录但**不包含 NetworkNodes 字段**（不报错）。已查文档确认：API 仅支持特定 AI/ML/HPC 实例类型。

**4. 已取消 CR 的行为**

取消后的 CR 仍返回记录（state=cancelled），但不包含 NetworkNodes。已查文档确认：仅 pending/active 状态的 CR 提供拓扑信息。

### 边界测试汇总

| 场景 | 行为 | 备注 |
|------|------|------|
| 不支持的实例类型 (m5.large) | 返回记录，无 NetworkNodes | 实测发现，官方未明确记录 |
| 已取消的 CR | 返回记录(state=cancelled)，无 NetworkNodes | 实测发现，文档仅说"pending/active 可查" |
| DryRun 参数 | 正常工作，返回 DryRunOperation | 与文档一致 |
| 按 AZ/实例类型过滤 | 正确过滤 | 与文档一致 |

## 踩坑记录

!!! warning "注意事项"
    1. **Capacity Reservation 创建即计费** — 即使没有启动实例，CR 也按 On-Demand 费率计费。务必测试完成后立即取消。已查文档确认。
    
    2. **MaxResults 最大值仅为 10** — 与 Instance Topology API 的最大值 100 不同，CR Topology API 每页最多返回 10 条记录。管理大量 CR 时需要分页处理。已查 API Reference 确认。
    
    3. **Console 不支持查看拓扑** — 只能通过 API/CLI 查询，不提供可视化界面。已查文档确认。
    
    4. **普通 CR 的拓扑粒度较粗** — 普通 CR 可能只返回 1 个 NetworkNode。如果需要更精细的拓扑信息，应在 Cluster Placement Group 中创建 CR。实测发现，官方仅说"1-3个取决于CR类型"。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| g6e.xlarge CR × 3 | $0.84/hr | ~3 min | ~$0.13 |
| m5.large CR × 1 | $0.096/hr | ~1 min | ~$0.002 |
| Placement Group | 免费 | - | $0 |
| API 调用 | 免费 | - | $0 |
| **合计** | | | **< $0.15** |

## 清理资源

```bash
# 取消所有 Capacity Reservation
aws ec2 cancel-capacity-reservation --capacity-reservation-id cr-aaa --region us-east-1
aws ec2 cancel-capacity-reservation --capacity-reservation-id cr-bbb --region us-east-1
aws ec2 cancel-capacity-reservation --capacity-reservation-id cr-ccc --region us-east-1
aws ec2 cancel-capacity-reservation --capacity-reservation-id cr-ddd --region us-east-1

# 删除 Placement Group（必须先取消其中所有 active CR）
aws ec2 delete-placement-group --group-name topology-test-cpg --region us-east-1

# 确认无残留
aws ec2 describe-capacity-reservations \
  --filters Name=state,Values=active,pending \
  --region us-east-1
```

!!! danger "务必清理"
    Capacity Reservation 持续计费直到取消。Lab 完成后请立即执行清理步骤。

## 结论与建议

### 适用场景

- **大规模分布式训练** — 管理数百个 GPU 实例的容量规划，提前了解预留容量的网络位置
- **HPC 作业调度** — 根据拓扑信息决定哪些 CR 的实例适合运行紧耦合任务
- **容量迁移规划** — 评估不同 AZ/Region 的容量布局

### 最佳实践

1. **使用 Cluster Placement Group** — 如果需要精确拓扑，在 CPG 中创建 CR 可获得更多层级信息
2. **结合 Instance Topology API** — CR Topology 做规划，Instance Topology 做运行时调度
3. **注意分页** — MaxResults 仅 10，大量 CR 场景下需要分页遍历
4. **自动化拓扑分析** — 将 API 输出集成到作业调度器中，自动选择网络距离最近的节点

### 与 Instance Topology API 的互补关系

```
             规划阶段                      运行阶段
    ┌─────────────────────┐      ┌─────────────────────┐
    │  CR Topology API    │      │  Instance Topology   │
    │  - 粗粒度(1-3 NN)   │  →   │  - 细粒度(3-4 NN)    │
    │  - 无需启动实例      │      │  - 需要运行中实例     │
    │  - 容量规划          │      │  - 作业调度 + Rank    │
    └─────────────────────┘      └─────────────────────┘
```

## 参考链接

- [Amazon EC2 Topology 官方文档](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-topology.html)
- [DescribeCapacityReservationTopology API Reference](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeCapacityReservationTopology.html)
- [Capacity Reservations in Cluster Placement Groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/cr-cpg.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/10/capacity-reservation-topology-api-ai-ml-hpc-instance-type/)
