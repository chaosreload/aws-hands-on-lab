# Aurora Serverless v2 PV4 实测：scale-to-0 与 resume 延迟

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 3-4 小时（含 ≥14h 长 paused 观察窗口）
    - **预估费用**: 约 $1.35（实际结算）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-27

## 背景

AWS 在 2026 年 4 月宣布 Aurora Serverless v2 Platform Version 4（PV4）GA，主要更新两点：

1. **Scale-to-zero**：`MinCapacity` 可设为 **0 ACU**，空闲时实例进入 paused 状态，仅收取 storage 费用
2. **Smarter scaling**：相对 PV3，scale-up 速度提升 up to 30%

在公开材料中（blog、keynote 等）会出现 "sub-second resume" 的描述。官方产品文档本身未给出 paused → resume 的具体 SLA 数字，相关描述是 "a brief pause is acceptable while the database resumes"。

本文记录一组实测：**5 个不同 paused 时长（4min / 35min / 100min / 205min / 14h）下，paused → 首查询返回的 wall-clock 延迟分布**，并分析"sub-second" 在不同语境下的含义差异，最后给出选型建议。

实测结果：resume wall-clock 在 **10.9 – 15.3 秒** 区间，与 warm idle 场景下的 sub-second 响应不在同一数量级。下面逐步展开。

## 前置条件

- AWS 账号（RDS + EC2 + CloudWatch 权限）
- AWS CLI v2 已配置
- 一台 EC2 bench client（本文用 c6i.xlarge，与 Aurora 同 VPC）
- Aurora PostgreSQL 16.3+ / Aurora MySQL 3.08.0+（scale-to-0 的 engine 版本下限）

## 核心概念

### ACU、PV4、AutoPause

- **ACU（Aurora Capacity Unit）**：Aurora Serverless v2 的容量单位，每个 ACU 约等于 2 GiB 内存加上相应的 CPU 和网络（[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)）
- **Platform Version 4（PV4）**：cluster 的运行时版本。2026 年 4 月起，新建 cluster 默认即为 PV4。主要改进是 smarter scaling 与更快的 scale-up
- **AutoPause**：通过 `ServerlessV2ScalingConfiguration.SecondsUntilAutoPause` 控制 idle → pause 的超时，**默认 300s，最小 300s，最大 86400s（24h）**（[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)）

### 两种 idle 状态下的 resume 含义

Aurora Serverless v2 的"空闲 → 恢复"存在两种不同场景，其 resume 延迟数量级不同：

| 场景 | MinACU | Idle 后状态 | Resume 延迟 |
|------|--------|-----------|------------|
| **A. Warm idle**（MinACU ≥ 0.5） | 0.5 | ACU 衰减至 0.5，实例保持运行 | < 100ms |
| **B. Paused**（PV4 scale-to-0） | 0 | ACU = 0，实例挂起，仅保留 storage | 10-15s（本文实测）|

公开材料中提到的 sub-second resume 对应的是场景 A。场景 B 的 resume 涉及实例重建流程，延迟需要独立测量。

## 动手实践

### Step 1: 创建 MinACU=0 的 PV4 cluster

```bash
export PROFILE=weichaol-testenv2-awswhatsnewtest
export REGION=us-east-1
export CLUSTER=asv2-pv4-lab-v3
export DB_PW='YourStrongPassword!'

aws rds create-db-cluster \
  --db-cluster-identifier $CLUSTER \
  --engine aurora-postgresql \
  --engine-version 16.8 \
  --master-username dbadmin \
  --master-user-password "$DB_PW" \
  --vpc-security-group-ids sg-0a86fd61c097c8ef7 \
  --db-subnet-group-name default-vpc-xxx \
  --serverless-v2-scaling-configuration \
    MinCapacity=0,MaxCapacity=16,SecondsUntilAutoPause=300 \
  --profile $PROFILE --region $REGION

aws rds create-db-instance \
  --db-instance-identifier ${CLUSTER}-instance-1 \
  --db-cluster-identifier $CLUSTER \
  --engine aurora-postgresql \
  --db-instance-class db.serverless \
  --profile $PROFILE --region $REGION
```

**关键参数**：

- `MinCapacity=0`：启用 scale-to-0。当 engine 版本低于 PG 16.3 / MySQL 3.08.0 时会返回参数错误
- `SecondsUntilAutoPause=300`：idle 5 分钟后进入 paused。300 也是默认值，这里显式声明
- `MaxCapacity=16`：本实验的容量上限，按需求调整

!!! warning "Security Group 配置"
    不要将 Aurora DB 的入站源设为 `0.0.0.0/0`。推荐 VPC 内通过安全组间引用（本实验做法），或者使用 VPC endpoint。

### Step 2: 观察 cluster auto-pause

Cluster + instance available 后，保持 idle。通过 CloudWatch 观察 `ServerlessDatabaseCapacity`，数值会从约 0.5 ACU 降至 **0.0 ACU**，即进入 paused。

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name ServerlessDatabaseCapacity \
  --dimensions Name=DBClusterIdentifier,Value=$CLUSTER \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 60 --statistics Maximum Average \
  --profile $PROFILE --region $REGION
```

当 `Maximum = 0.0` 时，cluster 已进入 paused 状态，期间仅收取 storage 费用（756 MB × $0.10/GB-month ≈ 每小时 $0.0001）。

### Step 3: 采样 resume 延迟

在 bench EC2 上以单条 SELECT 触发 resume，精确测量客户端侧 wall-clock：

```bash
export PGHOST=${CLUSTER}.cluster-xxxxxxxx.${REGION}.rds.amazonaws.com
export PGUSER=dbadmin
export PGPASSWORD="$DB_PW"
export PGDATABASE=postgres

t1=$(date +%s%N)
psql -c 'SELECT 1 AS probe, now() AS db_now;'
t2=$(date +%s%N)
echo "RESUME_WALL_MS=$(( (t2-t1)/1000000 ))"
```

通过改变发起查询前的等待时长，可以得到不同 paused 持续时间下的 resume 样本。本次实验共采集 5 个样本。

## 实测结果

### Resume wall-clock（5 样本）

| # | Paused 时长 | Resume wall-clock | 时段 (UTC) |
|---|------------|-------------------|-----------|
| 1 | ~4 min（刚 auto-pause）| **12.30 s** | 2026-04-26 11:23 |
| 2 | ~35 min | **10.90 s** | 2026-04-27 02:22 |
| 3 | ~100 min（1h40min）| **14.83 s** | 2026-04-26 07:23 |
| 4 | ~205 min（3h25min）| **15.34 s** | 2026-04-26 11:14 |
| 5 | ~14 h（840 min）| **11.43 s** | 2026-04-27 01:40 |

**统计**：

- 范围：**10.9 – 15.3 秒**
- 标准差 ≈ 1.9 s
- Warm 对照（resume 完成后立即连发同一查询）：**19 – 62 ms**

### Paused 时长与 resume 延迟的关系

一个直觉假设是"paused 越久，resume 越慢"。本组样本中，14 小时的样本（11.4s）反而比 100-205 分钟样本（14.8-15.3s）更快。一种可能的解释是：

- **底层恢复开销约 10-11s**：对应 instance 重建、网络握手、首次连接建立
- **短 paused（4-35 min）**：约 11-12s，可能有部分缓存/连接元数据尚未完全回收
- **中等 paused（100-205 min）**：约 14-15s，推测进入完全冷启路径
- **长 paused（14 h）**：约 11s，推测 AWS 后端存在周期性 pre-warm 或节点迁移机制

以上为基于实测数据的推测，AWS 未公开具体架构细节。实测样本量较小，如有不同结果欢迎校正。

### Scale-up：1 min 内从 0 → 16 ACU

Resume 完成后立即运行 `pgbench -c 128 -j 16 -T 600 -M prepared`，CloudWatch 每分钟粒度采样：

| Time | Max ACU | Avg ACU | 说明 |
|------|---------|---------|------|
| 07:23 | 2.0 | 1.3 | Cold resume，ACU 开始响应 |
| **07:24** | **16.0** | 7.06 | 1 分钟内达到 MaxACU |
| 07:25-07:34 | 16.0 | 16.00 | 稳态满载 |
| 07:35 | 14.5 | 14.5 | 压测停止 |
| 07:37 | 14.5 | 9.06 | 下降 |
| 07:38 | 6.5 | 6.5 | |
| **07:40** | **0.0** | 0.0 | 完全 paused（距压测结束 5min 23s）|

- **Scale-up**：< 1 min 从 0 达到 16 ACU。PV3 时代同样路径为分钟级阶梯扩容
- **稳态**：**4,418 TPS，p50 latency 28.9 ms，0 失败事务**（10 分钟压测，c6i.xlarge 客户端 → 16 ACU target）
- **Scale-down**：三阶梯 14.5 → 6.5 → 0，总耗时约 5 min 23s，与 AutoPause=300s + CloudWatch 1min 粒度吻合

## 踩坑记录

!!! warning "陷阱 1：EC2 launch 脚本中的 `sleep & terminate` 可能不生效"
    一种常见的 TTL 保险写法是在 user-data 中使用 `(sleep 10800 && aws ec2 terminate-instances ...) &`。但 cloud-init 退出时，该背景子进程可能被 waitpid 回收，导致预期的自动终止未触发。本次实验中对应的 bench EC2 超过 3 小时仍处于 running 状态。
    
    **推荐做法**：使用 `at 'now + 180 min'` 或 `systemd-run --on-active=3h`，避免依赖 shell 子进程。此前有另一起基准测试因缺少此保险导致 4 台 24xlarge 实例连续运行 6 天。

!!! warning "陷阱 2：MinACU=0 的 engine 版本要求"
    当 engine 版本低于 Aurora PG 13.15+ / 14.12+ / 15.7+ / 16.3+，或 Aurora MySQL 3.08.0+ 时，`MinCapacity=0` 会返回参数错误（[版本表](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)）。

!!! warning "陷阱 3：无效凭据也会触发 resume"
    根据官方文档，任何 TCP 连接尝试（包括凭据不正确的连接）都会触发 paused 实例的 resume。因此 VPC 内的非预期连接扫描可能在无感知情况下唤醒 cluster，产生 ACU 费用。

!!! warning "陷阱 4：`SecondsUntilAutoPause` 上限"
    该参数合法区间为 300-86400 秒。超出范围会返回参数错误。

## 费用明细

| 资源 | 用量 | 费用 |
|------|------|------|
| Bench EC2 c6i.xlarge | 累计 ~7.5h × $0.17/h | $1.28 |
| Aurora storage 756 MB | ~16h（paused 期间仅此项）| $0.05 |
| Aurora compute ACU-hours | ~0.15 ACU-hour（压测 10min + 启停）| $0.02 |
| **合计** | | **~$1.35** |

Paused 状态下 Aurora 不计 instance/compute 费用（[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)："You aren't charged for instance capacity while an instance is in the paused state"）。这是 scale-to-0 在成本侧的核心价值。

## 清理资源

```bash
# 先删实例
aws rds delete-db-instance \
  --db-instance-identifier ${CLUSTER}-instance-1 \
  --skip-final-snapshot \
  --profile $PROFILE --region $REGION

# 实例删除完成后（约 5-10 min），再删 cluster
aws rds delete-db-cluster \
  --db-cluster-identifier $CLUSTER \
  --skip-final-snapshot \
  --profile $PROFILE --region $REGION

# 终止 bench EC2
aws ec2 terminate-instances --instance-ids i-xxxxx \
  --profile $PROFILE --region $REGION
```

!!! danger "务必清理"
    Aurora paused 仍计 storage 费用；bench EC2 按小时计费是本实验主要成本项。实验结束后建议执行 `describe-db-clusters` + `describe-instances` 确认无残留资源。

## 结论与建议

### 1. PV4 scale-to-0 的适用场景

- **适用**：夜间/周末停机的批处理、低频报表系统、开发测试环境
- **不适用**：低频随机访问的在线服务（首次连接延迟 10-15s）、要求连接建立 SLA < 1s 的场景

### 2. 两种"sub-second"语境

- Warm idle（MinACU ≥ 0.5）→ 首查询响应：sub-second 级
- Paused（MinACU = 0）→ resume：本实测为 10-15s 级
- 对响应延迟敏感的业务应保持 MinACU ≥ 0.5。代价是持续的 compute 最低消费

### 3. Smarter scaling 的实际效果

PV4 的 scale-up 速度提升在本实验中得到验证（< 1 min 从 0 到 16 ACU）。已在 PV3 + MinACU≥0.5 运行的工作负载，升级 PV4 的主要收益是更快的 scale-up 以及可选的 scale-to-0；首查询延迟本身不会改善。

### 4. 选型决策树

```
问：负载是否存在 > 5min 的空闲期？
  ├─ 否 → 维持 MinACU ≥ 0.5，使用 warm idle 的 sub-second 响应
  └─ 是 → 首次查询 10-15s 延迟是否可接受？
        ├─ 可接受 → PV4 MinACU=0 + SecondsUntilAutoPause=300~86400
        └─ 不可接受 → 维持 MinACU ≥ 0.5，或在前面挂一个 keepalive 定时查询
```

## 参考链接

- [Aurora Serverless v2 官方文档（含 Platform Version 表）](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)
- [Scaling to Zero ACUs with automatic pause and resume](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)
- [What's New: Aurora Serverless v2 Smarter Scaling](https://aws.amazon.com/about-aws/whats-new/2026/04/aurora-serverless-smarter-scaling/)
- [Setting Aurora Serverless v2 Capacity Range](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html)
