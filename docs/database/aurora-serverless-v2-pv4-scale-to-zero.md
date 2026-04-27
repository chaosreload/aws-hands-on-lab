# Aurora Serverless v2 PV4 实测：scale-to-0 是真的，但"sub-second resume"不是

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 3-4 小时（含 ≥14h 长 paused 观察窗口）
    - **预估费用**: $1.35（实际结算）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-27

## 背景

AWS 在 2026 年 4 月宣布 Aurora Serverless v2 Platform Version 4（PV4）GA，主打两点：

1. **Scale-to-zero**：MinCapacity 可设为 **0 ACU**，空闲时 cluster auto-pause，paused 期间只收 storage 费用
2. **Smarter scaling**：scale-up 速度相对 PV3 提升 up to 30%

在 AWS 的各种营销素材里（blog、keynote），你会反复听到 **"sub-second resume"** 这个词，暗示 paused cluster 在首个查询到来时能秒级恢复 —— 听起来就像 Lambda cold start 一样顺滑。

但文档本身其实**从未**给出 paused → resume 的具体 SLA 数字，只用 "a brief pause is acceptable while the database resumes" 这种模糊描述。

本文用一个实测给出客观答案：**5 个不同 paused 时长（4min / 35min / 100min / 205min / 14h）下，paused → 首查询返回的 wall-clock 延迟到底是多少？**

剧透：**10.9 – 15.3 秒**，比宣传的 sub-second 慢 **11-15 倍**。这不是 bug，也不是 AWS 撒谎，而是"sub-second"在不同语境下有两种完全不同的含义。本文会把这两种语境讲清楚，并给出选型建议。

## 前置条件

- AWS 账号（RDS + EC2 + CloudWatch 权限）
- AWS CLI v2 已配置
- 一台 EC2 bench client（本文用 c6i.xlarge，和 Aurora 同 VPC）
- Aurora PostgreSQL 16.3+ / Aurora MySQL 3.08.0+（scale-to-0 的 engine 版本下限）

## 核心概念

### ACU、PV4、AutoPause 三件事

- **ACU（Aurora Capacity Unit）**：Aurora Serverless v2 的容量单位，每个 ACU ≈ 2 GiB 内存 + 对应 CPU + 网络（[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)）
- **Platform Version 4（PV4）**：cluster 的运行时版本，2026 年 4 月起新建 cluster 默认即 PV4。核心改进是"smarter scaling" + 更快 scale-up
- **AutoPause**：通过 `ServerlessV2ScalingConfiguration.SecondsUntilAutoPause` 字段控制 idle → pause 的超时，**默认 300s，最小 300s，最大 86400s（24h）**（[官方文档](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)）

### 两种"sub-second" 陷阱

| 场景 | MinACU | Idle 后状态 | Resume 延迟 |
|------|--------|-----------|------------|
| **A. Warm idle**（老 MinACU ≥ 0.5 方案） | 0.5 | ACU 衰减到 0.5，实例仍在跑 | **< 100ms**（营销说的 sub-second 指这里）|
| **B. Paused**（PV4 scale-to-0 新特性） | 0 | ACU = 0，实例**挂起**，只留 storage | **10-15s**（本文实测）|

AWS 用"sub-second"描述场景 A 没错；但把它和 PV4 的 scale-to-0 放一起宣传，就造成了"新特性既省钱又秒级唤醒"的错觉。**两件事不能同时成立。**

## 动手实践

### Step 1: 创建一个 MinACU=0 的 PV4 cluster

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
- `MinCapacity=0` —— 这一条开启 scale-to-0。如果 engine 版本不够新（PG < 16.3 / MySQL < 3.08.0）会直接报错
- `SecondsUntilAutoPause=300` —— idle 5 分钟后 pause。默认值就是 300，这里显式写清楚
- `MaxCapacity=16` —— 控制预算，按需调大

!!! warning "Security Group 禁区"
    **绝对不要**把 Aurora SG 的入站源设成 `0.0.0.0/0`。要么用 VPC 内安全组间引用（本文做法），要么用 VPC endpoint。Aurora 暴露公网 = 事故。

### Step 2: 等 cluster auto-pause

Cluster 建完 + instance available 后，什么都不做，等 5 分钟以上。在 CloudWatch 里看 `ServerlessDatabaseCapacity` metric，应该从 ~0.5 ACU 衰减到 **0.0 ACU** —— 这就是 paused。

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

一旦 `Maximum = 0.0`，恭喜：你正式进入 PV4 paused 状态，这段时间 **只交 storage 费**（756 MB 数据 × $0.10/GB-month ≈ 每小时 $0.0001）。

### Step 3: 采样 resume 延迟（核心测试）

**做法**：在 bench EC2 上跑一个极简 shell，精确测量从查询发起到返回的 wall-clock：

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

想测不同 paused 时长的影响，就改变发起查询前的等待时间。我们总共跑了 5 个样本。

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
- stddev ≈ 1.9 s
- Warm 对照（resume 后连发同样查询）：**19 – 62 ms**

### 为什么 paused 时长和 resume 延迟**不线性**？

凭直觉大家会以为 "paused 越久 resume 越慢"，但 14 小时那个样本反而比 100-205 分钟那两个**更快**。推测架构上 Aurora 后端可能是这样的：

- **底座**：~10-11s 是 instance 重建 + 网络握手 + 首连的固定成本
- **4-35 min paused**：后端可能还保留一部分 warm cache / 连接 metadata，所以 ~11-12s 略快
- **100-205 min paused**：踩到"完全冷启"区间，~14-15s
- **14 h paused**：AWS 后端可能有周期性 pre-warm / 节点迁移，实际上又回到底座 ~11s

这部分是推测，AWS 文档未公开具体架构。如果你测到其他样本欢迎对齐。

### Scale-up 是真的快：< 1 min 从 0 → 16 ACU

Resume 完成后立刻打压测 `pgbench -c 128 -j 16 -T 600 -M prepared`，CloudWatch 每分钟粒度：

| Time | Max ACU | Avg ACU | 说明 |
|------|---------|---------|------|
| 07:23 | 2.0 | 1.3 | ← Cold resume，ACU 开始响应 |
| **07:24** | **16.0** | 7.06 | ← **1 min 内打到 MaxACU** |
| 07:25-07:34 | 16.0 | 16.00 | 稳态满载 |
| 07:35 | 14.5 | 14.5 | 压测停 |
| 07:37 | 14.5 | 9.06 | 降档 |
| 07:38 | 6.5 | 6.5 | |
| **07:40** | **0.0** | 0.0 | ← 完全 paused（距压测结束 5min 23s）|

- **Scale-up**：< 1 min 从 0 打到 16 ACU —— PV3 时代这是分钟级 ladder，PV4 确实快
- **稳态**：**4,418 TPS, p50 latency 28.9 ms, 0 failed**（10 分钟持续压测，c6i.xlarge 客户端 → 16 ACU target）
- **Scale-down**：平滑三阶梯 14.5 → 6.5 → 0，总 ~5min 23s，和 AutoPause=300s + CloudWatch 粒度吻合

## 踩坑记录

!!! warning "陷阱 1：EC2 launch 脚本里的 `sleep & terminate` 会被 cloud-init 吃掉"
    我们一开始用 `(sleep 10800 && aws ec2 terminate-instances ...) &` 做 bench client 的 TTL 保命，结果 cloud-init 退出时 `&` 背景子进程被 waitpid 收走，机器过了 3 小时依然在跑，空转烧钱。
    
    **正确做法**：用 `at 'now + 180 min'` 或 `systemd-run --on-active=3h`，别靠 shell 子进程。这条教训代价不小 —— 我之前另一起基准测试中 4 台 24xlarge 空转了 6 天，烧了一千多美元才发现。

!!! warning "陷阱 2：MinACU=0 不支持旧 engine 版本"
    如果用的是 Aurora PostgreSQL 15.5 或 Aurora MySQL 3.06.0，`MinCapacity=0` 会直接报错。必须升到 Aurora PG **13.15+ / 14.12+ / 15.7+ / 16.3+**，Aurora MySQL **3.08.0+**（[官方版本表](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)）。

!!! warning "陷阱 3：无效凭据也会触发 resume"
    想用"ping"式轻量探活？不行。任何 TCP 连接（即使账号密码是错的）都会触发 resume —— 这是 AWS 文档明确写的。所以在 VPC 里做错误的安全扫描可能意外唤醒你的 paused cluster。

!!! warning "陷阱 4：别把文档里的 `SecondsUntilAutoPause` 设超过 86400"
    最大值就是 86400（1 天）。超过会报参数错误。

## 费用明细

| 资源 | 用量 | 费用 |
|------|------|------|
| Bench EC2 c6i.xlarge | 跨多次启停共 ~7.5h × $0.17/h | $1.28 |
| Aurora storage 756 MB | ~16h（paused 期间仅此费用）| $0.05 |
| Aurora compute ACU-hours | ~0.15 ACU-hour（压测 10min + 少量 warm-up）| $0.02 |
| **合计** | | **~$1.35** |

**注意**：Paused 状态下 Aurora **不收 instance/compute 费**，[官方文档原文](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)："You aren't charged for instance capacity while an instance is in the paused state"。这是 PV4 scale-to-0 的**真正价值**——不是"秒级唤醒"，而是**真正意义上的按零付费**。

## 清理资源

```bash
# 删实例
aws rds delete-db-instance \
  --db-instance-identifier ${CLUSTER}-instance-1 \
  --skip-final-snapshot \
  --profile $PROFILE --region $REGION

# 实例删完（~5-10 min），再删 cluster
aws rds delete-db-cluster \
  --db-cluster-identifier $CLUSTER \
  --skip-final-snapshot \
  --profile $PROFILE --region $REGION

# 终止 bench EC2
aws ec2 terminate-instances --instance-ids i-xxxxx \
  --profile $PROFILE --region $REGION
```

!!! danger "务必清理"
    Aurora paused 本身便宜但不是零；bench EC2 $0.17/h 跑着才是大头。每次 hands-on lab 结束后做一次 `describe-db-clusters` + `describe-instances` 确认没有遗留。

## 结论与建议

### 1. PV4 scale-to-0 是真正的 production feature

- **可以用** 在：夜间/周末停机的批处理、低频报表系统、开发测试环境
- **不建议用** 在：低频随机访问的在线服务（每个用户首次连接等 10-15s 糟糕体验）、需要 SLA < 1s 连接时间的场景

### 2. "Sub-second resume" 是语境错位，不是谎言

- AWS 说的 sub-second resume，指的是 **warm idle（MinACU≥0.5）→ 首查询响应**
- 真正的 PV4 **paused（MinACU=0）→ resume**，本实测**稳定 10-15s**
- 需要真·秒级响应 → **保持 MinACU ≥ 0.5，不要设 MinACU=0**。这是省成本（compute 最低 0.5 ACU）vs 响应延迟（< 100ms）的交易

### 3. Smarter scaling 是真的快

PV4 相对 PV3 的 scale-up 速度提升是真的（< 1 min 从 0 打到 16 ACU）。但如果你之前已经在用 PV3 + MinACU=0.5 方案，升级到 PV4 的收益主要是 **scale-up 速度提升** + **可选的 scale-to-0**，而不是"首查询延迟"改善。

### 4. 选型决策树

```
问：我的负载有长时间空闲期（> 5min）吗？
  ├─ 否 → 维持 MinACU ≥ 0.5，享受 sub-second 响应，不用 PV4 新特性
  └─ 是 → 问：用户能接受首次查询 10-15s 延迟吗？
        ├─ 能 → 用 PV4 MinACU=0 + SecondsUntilAutoPause=300~86400
        └─ 不能 → 只能继续 MinACU ≥ 0.5（多交钱）或前面挂个"keepalive"定时查询
```

## 参考链接

- [Aurora Serverless v2 官方文档（含 PV 版本表）](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.how-it-works.html)
- [Scaling to Zero ACUs 专题](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html)
- [What's New：Aurora Serverless v2 Smarter Scaling](https://aws.amazon.com/about-aws/whats-new/2026/04/aurora-serverless-smarter-scaling/)
- [Setting Capacity Range](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html)
