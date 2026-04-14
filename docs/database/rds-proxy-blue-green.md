---
tags:
  - Database
---

# RDS Blue/Green Deployments + RDS Proxy 实测：Switchover 恢复时间从 13 秒降到 500 毫秒

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $3-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-10

## 背景

RDS Blue/Green Deployments 让你在不影响生产的情况下准备一个同步的 staging 环境，做完变更后一键 switchover。问题在于：switchover 完成后应用端通过 DNS 发现新实例需要时间——DNS TTL 缓存导致连接恢复可能需要 10 秒以上。

2026 年 4 月 9 日，AWS 发布了 RDS Proxy 对 Blue/Green Deployments 的支持。RDS Proxy 在 switchover 期间主动监测实例状态变化，不等 DNS 传播就直接把连接路由到新环境。

本文实测对比了有 Proxy 和无 Proxy 两种场景下的 switchover 恢复时间，用数据说话。

## 前置条件

- AWS 账号（需要 RDS、EC2、Secrets Manager、IAM 权限）
- AWS CLI v2 已配置
- 一个 VPC，至少 2 个可用区的子网

## 核心概念

### RDS Proxy + Blue/Green Switchover 机制

| 阶段 | 无 Proxy | 有 Proxy |
|------|---------|---------|
| Blue 进入 read-only | 写操作报错 1290 | 写操作报错 1290 |
| 实例重命名 | Green → 原名，Blue → -old1 | 同左 |
| 连接恢复 | 等 DNS 传播（TTL 缓存） | **Proxy 主动检测并路由** |
| 应用改动 | 无需 | 无需 |

### 关键限制

| 限制 | 说明 |
|------|------|
| Proxy 注册时机 | **必须在创建 B/G 部署之前**将 Blue 实例注册为 Proxy target |
| 已有 B/G 部署 | 无法将已有 B/G 的实例注册到 Proxy |
| Aurora Global DB | 不支持 Proxy + B/G 组合 |
| Secrets Manager 密码 | B/G 不支持 SM 管理的 master password |
| 仅单 Region | Proxy 仅在单 Region 配置下检测 switchover |
| 支持引擎 | RDS for MySQL、PostgreSQL、MariaDB |

## 动手实践

### Step 1: 创建基础设施

创建 Security Group（**仅允许 VPC CIDR 访问，不用 0.0.0.0/0**）：

```bash
# Security Group
aws ec2 create-security-group \
  --group-name bg-test-sg \
  --description 'RDS Blue/Green test - MySQL 3306' \
  --vpc-id vpc-026ac6d47a16a6d2d \
  --region us-east-1

aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxxxxx \
  --protocol tcp --port 3306 \
  --cidr 172.31.0.0/16 \
  --region us-east-1
```

创建 Secrets Manager 密钥（RDS Proxy 依赖）：

```bash
aws secretsmanager create-secret \
  --name bg-test-db-secret \
  --secret-string '{"username":"admin","password":"YourSecurePassword"}' \
  --region us-east-1
```

创建 IAM Role（让 Proxy 访问 Secrets Manager）：

```bash
# Trust policy: rds.amazonaws.com
aws iam create-role --role-name bg-test-proxy-role \
  --assume-role-policy-document file://proxy-trust.json

# Inline policy: secretsmanager:GetSecretValue
aws iam put-role-policy --role-name bg-test-proxy-role \
  --policy-name SecretsManagerAccess \
  --policy-document file://proxy-sm-policy.json
```

### Step 2: 创建两个 RDS MySQL 实例

```bash
# 场景 A: 无 Proxy（基线对比）
aws rds create-db-instance \
  --db-instance-identifier bg-test-noproxy \
  --db-instance-class db.t3.medium \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password 'YourSecurePassword' \
  --allocated-storage 20 \
  --vpc-security-group-ids sg-xxxxxxxx \
  --db-subnet-group-name bg-test-subnet-group \
  --backup-retention-period 1 \
  --no-publicly-accessible --no-multi-az \
  --region us-east-1

# 场景 B: 有 Proxy
aws rds create-db-instance \
  --db-instance-identifier bg-test-proxy \
  --db-instance-class db.t3.medium \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password 'YourSecurePassword' \
  --allocated-storage 20 \
  --vpc-security-group-ids sg-xxxxxxxx \
  --db-subnet-group-name bg-test-subnet-group \
  --backup-retention-period 1 \
  --no-publicly-accessible --no-multi-az \
  --region us-east-1
```

等待两个实例 available（约 10 分钟）。

### Step 3: 创建 RDS Proxy 并注册目标

!!! warning "顺序很重要"
    **必须先将实例注册为 Proxy target，再创建 Blue/Green 部署。** 顺序反了会被 API 拒绝。

```bash
# 创建 Proxy
aws rds create-db-proxy \
  --db-proxy-name bg-test-rds-proxy \
  --engine-family MYSQL \
  --auth '{"AuthScheme":"SECRETS","SecretArn":"arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:bg-test-db-secret-xxxxx","IAMAuth":"DISABLED"}' \
  --role-arn arn:aws:iam::ACCOUNT:role/bg-test-proxy-role \
  --vpc-subnet-ids subnet-aaa subnet-bbb subnet-ccc \
  --vpc-security-group-ids sg-xxxxxxxx \
  --region us-east-1
```

等待 Proxy available（约 3-5 分钟），然后注册目标：

```bash
aws rds register-db-proxy-targets \
  --db-proxy-name bg-test-rds-proxy \
  --db-instance-identifiers bg-test-proxy \
  --region us-east-1
```

**实测输出**：Target 状态初始为 `REGISTERING`，经过 `UNAVAILABLE`（等待 Proxy 扩容，约 7 分钟），最终变为 `AVAILABLE`。

### Step 4: 创建 Blue/Green 部署

```bash
# 场景 B（有 Proxy）
aws rds create-blue-green-deployment \
  --blue-green-deployment-name bg-deploy-proxy \
  --source arn:aws:rds:us-east-1:ACCOUNT:db:bg-test-proxy \
  --region us-east-1

# 场景 A（无 Proxy）
aws rds create-blue-green-deployment \
  --blue-green-deployment-name bg-deploy-noproxy \
  --source arn:aws:rds:us-east-1:ACCOUNT:db:bg-test-noproxy \
  --region us-east-1
```

等待两个 B/G 部署变为 `AVAILABLE`（约 12 分钟）。此时 Green 实例已创建并同步。

### Step 5: Switchover 对比测试（核心实验）

在 VPC 内的 EC2 上运行持续写入脚本（每 0.5 秒一次 INSERT），然后触发 switchover：

```bash
# 持续写入脚本（后台运行）
nohup /tmp/measure.sh <endpoint> <label> 180 &

# 触发 switchover
aws rds switchover-blue-green-deployment \
  --blue-green-deployment-identifier bgd-xxxxx \
  --switchover-timeout 300 \
  --region us-east-1
```

#### 场景 B: 有 RDS Proxy — 通过 Proxy Endpoint 连接

**实测时间线**：

```
05:51:04 UTC  开始持续写入（通过 Proxy endpoint）
05:51:30 UTC  触发 switchover
05:51:39.065  ❌ 首次失败: ERROR 1290 (HY000) - server running with --read-only
05:51:39.586  ✅ 恢复写入
```

**结果: 中断 ~521ms，仅 1 次写入失败**

#### 场景 A: 无 Proxy — 通过直连 Endpoint 连接

**实测时间线**：

```
05:54:38 UTC  开始持续写入（通过直连 endpoint）
05:54:56 UTC  触发 switchover
05:55:05.039  ❌ 首次失败: ERROR 1290 (HY000) - server running with --read-only
05:55:05-18   持续失败（DNS 传播中）
05:55:18.647  ✅ 恢复写入
```

**结果: 中断 ~13.6s，7 次写入失败**

### Step 6: CloudWatch 日志分析

查看 Proxy 的 CloudWatch 日志（`/aws/rds/proxy/bg-test-rds-proxy`）：

```
05:39:35  [INFO] Green database "bg-test-proxy-green-jvecul" is successfully detected.
05:51:38  [INFO] Database "bg-test-proxy" is now available for read-only access.
05:51:39  [INFO] DB connections closed. Reason: TCP channel closed.
05:51:39  [INFO] Green database is successfully detected. (switchover 检测)
05:52:00  [INFO] TCP connection established to Green (172.31.68.60:3306).
05:52:08  [INFO] Old connections closed. Reason: target no longer associated.
05:52:08  [INFO] Database "bg-test-proxy" at Green is now available for read/write.
```

!!! tip "Proxy 提前监测 Green 环境"
    Proxy 在 B/G 部署创建完成后就开始周期性探测 Green 环境的可达性（05:39:35，远早于 switchover 05:51:30）。这让它在 switchover 发生时能秒级完成路由切换。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | 无 Proxy switchover（基线） | ✅ | 中断 13.6s, 7 次失败 | DNS 传播延迟 |
| 2 | **有 Proxy switchover** | ✅ | **中断 521ms, 1 次失败** | **26x 改善** |
| 3 | 过渡期 read-only 行为 | ✅ | MySQL 1290 错误 | 与文档一致 |
| 4 | Proxy API 状态延迟 | ✅ | API 在完成后才更新 | 流量路由更早 |
| 5 | 先建 B/G 再加 Proxy | SKIP | 文档明确限制 | API 会拒绝 |
| 6 | CloudWatch 日志 | ✅ | 完整 switchover 事件链 | 无需开 debug logging |

### 核心对比

| 指标 | 有 RDS Proxy | 无 RDS Proxy | 改善 |
|------|-------------|-------------|------|
| **应用中断时间** | **~521ms** | ~13.6s | **26x** |
| 失败写入数 | 1 | 7 | 7x |
| 恢复机制 | Proxy 主动检测 | DNS 传播 | - |
| 应用改动 | 无 | 无 | - |

## 踩坑记录

!!! warning "踩坑 1: Proxy Target 注册后需等待扩容"
    创建 Proxy 并注册 target 后，target 状态会从 `REGISTERING` → `UNAVAILABLE`（Reason: `PENDING_PROXY_CAPACITY`），需要等约 7 分钟才变为 `AVAILABLE`。
    
    这不是故障——Proxy 需要时间分配底层资源。但如果在脚本中等 target AVAILABLE 才继续，需要设置足够的超时。

!!! warning "踩坑 2: Proxy 注册必须在 B/G 创建之前"
    如果已经为一个实例创建了 B/G 部署，再尝试将它注册到 Proxy 会被 API 拒绝。正确顺序：
    
    1. 创建 RDS 实例 → 2. 创建 Proxy → 3. 注册 target → 4. 创建 B/G 部署
    
    （已查文档确认）

!!! info "发现: describe-db-proxy-targets API 延迟更新"
    在 switchover 过程中，`describe-db-proxy-targets` 始终显示 target 为 `AVAILABLE` + `READ_WRITE`，即使实际已经在切换中。API 直到 switchover 完全完成后才反映新的 target。
    
    不要依赖这个 API 来判断 switchover 进度——用 `describe-blue-green-deployments` 的状态。（实测发现，官方已记录）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| RDS db.t3.medium × 2（Blue） | $0.068/hr | ~2 hr | $0.27 |
| RDS db.t3.medium × 2（Green） | $0.068/hr | ~1 hr | $0.14 |
| RDS Proxy (2 vCPU) | $0.015/vCPU-hr | ~2 hr | $0.06 |
| EC2 t3.micro（测试客户端） | $0.0104/hr | ~1 hr | $0.01 |
| Secrets Manager | $0.40/secret/month | < 1 day | < $0.01 |
| **合计** | | | **< $1.00** |

## 清理资源

```bash
# 1. 删除 Blue/Green 部署（如果还存在）
aws rds delete-blue-green-deployment \
  --blue-green-deployment-identifier bgd-xxxxx \
  --delete-target \
  --region us-east-1

# 2. 取消 Proxy target 注册
aws rds deregister-db-proxy-targets \
  --db-proxy-name bg-test-rds-proxy \
  --db-instance-identifiers bg-test-proxy \
  --region us-east-1

# 3. 删除 RDS Proxy
aws rds delete-db-proxy \
  --db-proxy-name bg-test-rds-proxy \
  --region us-east-1

# 4. 删除 RDS 实例（含 old 实例）
for DB in bg-test-proxy bg-test-proxy-old1 bg-test-noproxy bg-test-noproxy-old1; do
  aws rds delete-db-instance \
    --db-instance-identifier $DB \
    --skip-final-snapshot \
    --region us-east-1
done

# 5. 终止 EC2 测试客户端
aws ec2 terminate-instances --instance-ids i-xxxxx --region us-east-1

# 6. 删除 IAM
aws iam delete-role-policy --role-name bg-test-proxy-role --policy-name SecretsManagerAccess
aws iam detach-role-policy --role-name bg-test-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam remove-role-from-instance-profile --instance-profile-name bg-test-ec2-profile --role-name bg-test-ec2-role
aws iam delete-instance-profile --instance-profile-name bg-test-ec2-profile
aws iam delete-role --role-name bg-test-proxy-role
aws iam delete-role --role-name bg-test-ec2-role

# 7. 删除 Secrets Manager
aws secretsmanager delete-secret --secret-id bg-test-db-secret --force-delete-without-recovery --region us-east-1

# 8. 检查 ENI 残留再删 Security Group
aws ec2 describe-network-interfaces --filters Name=group-id,Values=sg-xxxxxxxx --region us-east-1
# 确认无残留后：
aws ec2 delete-security-group --group-id sg-xxxxxxxx --region us-east-1
aws ec2 delete-security-group --group-id sg-yyyyyyyy --region us-east-1

# 9. 删除 DB Subnet Group
aws rds delete-db-subnet-group --db-subnet-group-name bg-test-subnet-group --region us-east-1
```

!!! danger "务必清理"
    RDS 实例按小时计费，db.t3.medium 约 $0.068/hr。Blue/Green switchover 后会留下 `-old1` 实例，别忘了清理。

## 结论与建议

### 数据说话

RDS Proxy + Blue/Green Deployments 将 switchover 期间的应用中断从 **13.6 秒降到 521 毫秒**，改善 26 倍。这个差距来自一个核心机制差异：Proxy 主动检测 Green 环境可用性并立即路由，而直连方式依赖 DNS 传播。

### 谁该用

| 场景 | 建议 |
|------|------|
| 生产环境做 MySQL/PG 版本升级 | ✅ 强烈推荐用 Proxy |
| 只做参数变更，停机可容忍 | ⚠️ 可选，Proxy 有额外成本 |
| 已经用 Proxy 做连接池 | ✅ 零额外成本，直接受益 |
| Aurora Global Database | ❌ 不支持 |
| 需要 Secrets Manager 管理 master password | ❌ B/G 不支持 |

### 生产注意事项

1. **注册顺序**：Proxy target → B/G 部署（顺序不能反）
2. **应用需支持重连**：switchover 后现有连接会被 drop，应用必须有重连逻辑
3. **写操作错误处理**：过渡期可能收到 1290 read-only 错误，应用层需要 retry
4. **不要监控 Proxy target API 判断进度**：API 延迟更新，用 `describe-blue-green-deployments` 状态
5. **Debug logging 通常不需要开**：标准 CloudWatch 日志已包含 switchover 事件

## 参考链接

- [Using RDS Proxy with Blue/Green Deployments](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-proxy-blue-green.html)
- [Switching a Blue/Green Deployment](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/blue-green-deployments-switching.html)
- [Blue/Green Deployments Overview](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/blue-green-deployments-overview.html)
- [AWS What's New: RDS Blue/Green Deployments supports RDS Proxy](https://aws.amazon.com/about-aws/whats-new/2026/04/rds-proxy-blue-green/)
