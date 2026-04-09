# S3 Files vs S3 File Gateway vs Mountpoint CSI 实测：三种 S3 挂载方案全面对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $3-5（S3 Files 测试 < $1 + File Gateway m5.xlarge ~$0.19/h × 1h + 150GB gp3）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-08

## 背景

将 S3 桶挂载为文件系统一直是许多应用的刚需——传统应用无法直接调用对象存储 API，但又想利用 S3 的无限扩展性和低成本。

AWS 目前提供了三种主要的 S3 挂载方案：

1. **S3 Files**（2026 年 4 月 GA）——S3 原生文件系统能力，基于 EFS 构建，NFS 协议，零额外基础设施
2. **S3 File Gateway**（2018 年）——Storage Gateway 系列，需要独立 EC2 网关设备，NFS/SMB 协议
3. **Mountpoint for Amazon S3 CSI Driver**——基于 FUSE 的用户态文件系统，主要面向 EKS/Kubernetes 场景

三种方案的架构理念完全不同，适用场景也各有侧重。本文通过实测数据和文档分析，给出完整的三方对比和选型建议。

## 前置条件

- AWS 账号（需要 S3、EC2、Storage Gateway、IAM 权限）
- AWS CLI v2.34.26+（S3 Files 需要 `aws s3files` 子命令）
- 同一 VPC、同一 AZ 的测试环境（消除网络变量）

## 核心概念

### 三方架构对比

| 维度 | S3 Files | S3 File Gateway | Mountpoint CSI |
|------|----------|----------------|----------------|
| **底层技术** | Amazon EFS（内核级 NFS） | Storage Gateway AMI（虚拟设备） | FUSE（用户态文件系统） |
| **架构** | S3 原生 → Mount Target → NFS | EC2 Gateway → NFS/SMB → S3 | Pod → FUSE mount → S3 API |
| **缓存层** | 高性能存储层（托管） | EC2 本地 EBS（自管理） | 无持久缓存 |
| **协议** | NFS v4.1/4.2 | NFS v3/v4.1 + SMB v2/v3 | FUSE（非标准网络协议） |
| **写入模型** | 完整读写，~60s 批量写回 S3 | 完整读写，~1s 写回 S3 | 仅新建文件顺序写（不可修改已有文件） |
| **删除支持** | ✅ 原生支持 | ✅ 原生支持 | 默认禁止，需 `--allow-delete` 启动参数 |
| **重命名** | ✅ 支持 | ✅ 支持 | ❌ 通用桶不支持（仅 Express One Zone） |
| **同步方向** | 双向自动 | 写→S3 自动，S3→NFS 需手动 | 无同步概念（直接 S3 API） |
| **额外基础设施** | 无 | m5.xlarge EC2 + 150GB+ EBS | CSI Driver DaemonSet |
| **部署步骤** | 3 步 | 13 步 | `kubectl apply`（EKS Add-on） |
| **POSIX 权限** | ✅ 完整（chmod/chown → S3 元数据） | ✅ 完整（chmod/chown → S3 元数据） | ❌ 不模拟 |
| **文件锁** | ✅ NFS 锁 | ✅ NFS 锁 | ❌ 不支持 |
| **多客户端共享写** | ✅ 支持 | ✅ 支持 | ❌ 不支持 |
| **适用计算服务** | EC2, Lambda, EKS, ECS | EC2（仅限） | EKS, 自管理 K8s |
| **发布时间** | 2026 年 4 月（GA） | 2018 年（成熟服务） | 2023 年（GA） |

### 读路由差异

**S3 Files** 有智能读路由：

- 小文件（< 128KB）从高性能存储层读
- 大文件（≥ 1MB）直接从 S3 流式读
- 刚修改未同步的数据从文件系统读

**File Gateway** 的读路由更简单：

- 所有读操作优先走本地 EBS 缓存
- 缓存未命中时从 S3 拉取到缓存再返回
- 缓存空间有限（150GB-64TiB），满了会淘汰

**Mountpoint CSI** 直接走 S3 API：

- 每次读都是 S3 GET 请求（无本地缓存层）
- 顺序读时自动多并发预取，优化大文件吞吐
- 随机读也支持，但每次都是网络往返

### Mountpoint CSI 写入限制详解

Mountpoint CSI 的写入模型与传统文件系统差异很大，理解这些限制对选型至关重要：

| 操作 | S3 Files / File Gateway | Mountpoint CSI |
|------|------------------------|----------------|
| 创建新文件 | ✅ | ✅ |
| 修改已有文件 | ✅ | ❌（需 `O_TRUNC` 重写整个文件） |
| 追加写入 | ✅ | ❌（仅 Express One Zone 支持 `--incremental-upload`） |
| 删除文件 | ✅ | 需 `--allow-delete` 启动参数 |
| 重命名文件 | ✅ | ❌（仅 Express One Zone） |
| `fsync` 后继续写 | ✅ | ❌（`fsync` 后文件关闭写入） |

!!! warning "Mountpoint CSI 的核心定位"
    Mountpoint 官方明确声明：**不追求完整 POSIX 语义**。它的设计哲学是"无法在 S3 API 上高效实现的文件操作，一律不支持"。如果你的应用需要随机写入、文件锁、或完整 POSIX 权限，Mountpoint 不是正确的选择。

## 动手实践

### Step 1: 准备 File Gateway 环境

S3 Files 的部署已在 [S3 Files 实测文章](../storage/s3-files.md) 中详细记录，这里聚焦 File Gateway 的部署过程。

**创建 S3 桶**（两个方案各用独立桶，消除干扰）：

```bash
# File Gateway 测试桶
aws s3api create-bucket \
  --bucket fgw-test-20260408-36429 \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket fgw-test-20260408-36429 \
  --versioning-configuration Status=Enabled \
  --region us-east-1
```

**创建 File Gateway IAM 角色**：

```bash
# Trust policy: storagegateway.amazonaws.com
aws iam create-role \
  --role-name FGWFileShareRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "storagegateway.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
```

!!! warning "踩坑：版本化桶需要额外 IAM 权限"
    如果 S3 桶启用了版本控制（File Gateway 推荐），IAM 角色**必须**包含 `s3:GetObjectVersion`、`s3:DeleteObjectVersion`、`s3:ListBucketVersions` 权限。否则通过 S3 API 上传的文件在 NFS 端读取会返回 `Input/output error`。

    ```json
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject", "s3:GetObjectVersion",
        "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
        "s3:ListBucket", "s3:ListBucketVersions",
        "s3:GetBucketLocation", "s3:GetBucketVersioning",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::YOUR-BUCKET", "arn:aws:s3:::YOUR-BUCKET/*"]
    }
    ```

### Step 2: 部署 File Gateway EC2

```bash
# 查找最新 File Gateway AMI
aws ec2 describe-images --owners amazon \
  --filters "Name=name,Values=*-FILE_S3*" \
  --query "Images | sort_by(@, &CreationDate) | [-1].{Name:Name,Id:ImageId}" \
  --region us-east-1
# 结果: ami-0e65bd9d85c75e631 (aws-storage-gateway-FILE_S3-2.1.4)

# 启动 File Gateway EC2 (m5.xlarge + 150GB 缓存 EBS)
aws ec2 run-instances \
  --image-id ami-0e65bd9d85c75e631 \
  --instance-type m5.xlarge \
  --subnet-id subnet-0ff81ae4d8f7aa00b \
  --security-group-ids sg-07a78a8029e6fdb59 \
  --block-device-mappings '[{
    "DeviceName": "/dev/xvdf",
    "Ebs": {"VolumeSize": 150, "VolumeType": "gp3", "DeleteOnTermination": true}
  }]' \
  --region us-east-1
```

!!! tip "安全组配置"
    File Gateway EC2 安全组需要：

    - 入站 TCP 80（HTTP）← 客户端 SG（仅用于激活，激活后可删除）
    - 入站 TCP 2049（NFS）← 客户端 SG
    - **绝不开放 0.0.0.0/0 入站**

### Step 3: 激活 Gateway 并创建 NFS 共享

```bash
# 从客户端 EC2 获取激活密钥（需等 Gateway 启动 2-3 分钟）
curl "http://172.31.13.45/?gatewayType=FILE_S3&activationRegion=us-east-1&no_redirect"
# 返回: GGR7O-OJOHO-REP4R-9UIF9-5T6DR

# 激活 Gateway
aws storagegateway activate-gateway \
  --activation-key "GGR7O-OJOHO-REP4R-9UIF9-5T6DR" \
  --gateway-name "fgw-gateway" \
  --gateway-timezone "GMT" \
  --gateway-region us-east-1 \
  --gateway-type "FILE_S3" \
  --region us-east-1
# 返回: gateway/sgw-EA485B83

# 等待 ~60s 后添加缓存磁盘
aws storagegateway list-local-disks \
  --gateway-arn arn:aws:storagegateway:us-east-1:595842667825:gateway/sgw-EA485B83 \
  --region us-east-1

aws storagegateway add-cache \
  --gateway-arn arn:aws:storagegateway:us-east-1:595842667825:gateway/sgw-EA485B83 \
  --disk-ids "/dev/nvme0n1" \
  --region us-east-1

# 创建 NFS 文件共享
aws storagegateway create-nfs-file-share \
  --client-token "fgw-share" \
  --gateway-arn arn:aws:storagegateway:us-east-1:595842667825:gateway/sgw-EA485B83 \
  --location-arn arn:aws:s3:::fgw-test-20260408-36429 \
  --role arn:aws:iam::595842667825:role/FGWFileShareRole-36429 \
  --default-storage-class S3_STANDARD \
  --client-list '["172.31.0.0/16"]' \
  --squash RootSquash \
  --region us-east-1
```

!!! warning "踩坑：Gateway 激活后需等待连接"
    `activate-gateway` 成功后，Gateway 需要约 60 秒才能真正连接到 AWS 服务端。在此期间调用 `list-local-disks` 会返回 `GatewayNotConnected` 错误。

### Step 4: 挂载并执行对比测试

```bash
# File Gateway 挂载
sudo mount -t nfs -o nolock,hard 172.31.13.45:/fgw-test-20260408-36429 /mnt/fgw

# S3 Files 挂载（在同一台 EC2 上对比）
sudo mount -t s3files fs-0d3cc3a5aa52b21fd:/ /mnt/s3files
```

**写延迟测试**：

```bash
# File Gateway: 写 1KB 文件
sudo dd if=/dev/urandom of=/mnt/fgw/small-1kb.bin bs=1024 count=1

# S3 Files: 写 1KB 文件
sudo dd if=/dev/urandom of=/mnt/s3files/small-1kb.bin bs=1024 count=1
```

**读延迟测试**：

```bash
# 清除 OS 页缓存
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null

# File Gateway 小文件读
time cat /mnt/fgw/small-1kb.bin > /dev/null   # 6ms (首次) / 2ms (缓存)

# S3 Files 小文件读
time cat /mnt/s3files/small-1kb.bin > /dev/null  # 8-11ms (首次)
```

### Step 5: 写回同步延迟对比

这是两个方案最显著的差异之一。

**File Gateway**：

```bash
# NFS 写文件
echo 'sync-test' | sudo tee /mnt/fgw/sync-test.txt
# 写入时间: 04:47:30

# 检查 S3
aws s3api head-object --bucket fgw-test-20260408-36429 --key sync-test.txt
# S3 出现时间: 04:47:31 → 延迟 ~1 秒
```

**S3 Files**：

```bash
# NFS 写文件
echo 'sync-test' | sudo tee /mnt/s3files/sync-test.txt
# 写入时间: 02:08:17

# 检查 S3
aws s3api head-object --bucket s3files-test-20260408-16287 --key sync-test.txt
# S3 出现时间: 02:09:19 → 延迟 62 秒
```

**结论：File Gateway 写回 S3 延迟仅 ~1 秒，比 S3 Files 的 ~60 秒快了 60 倍。**

### Step 6: 反向同步对比

从 S3 API 上传文件，检查 NFS 端何时可见。

**S3 Files**：

```bash
# S3 API 上传
echo 'reverse' | aws s3 cp - s3://s3files-test-20260408-16287/reverse.txt
# NFS 端 ls → 数秒内自动可见（EventBridge 触发）
```

**File Gateway**：

```bash
# S3 API 上传
echo 'reverse' | aws s3 cp - s3://fgw-test-20260408-36429/reverse.txt
# NFS 端 ls → 不可见！

# 必须手动调用 RefreshCache
aws storagegateway refresh-cache \
  --file-share-arn arn:aws:storagegateway:...:share/share-300D6F5B
# 等待 ~60-90 秒后 NFS 端才可见
```

**Mountpoint CSI**：

```bash
# Mountpoint 直接访问 S3，S3 API 上传后立即可见（无缓存层）
# 但注意：如果 Mountpoint 已经缓存了目录列表，可能需要短暂等待
```

**结论：S3 Files 反向同步自动且快速（数秒），File Gateway 需要手动 RefreshCache 且等待时间更长。Mountpoint 因无缓存层理论上最快，但受限于 S3 最终一致性。**

## 测试结果

### 核心三方对比表

| # | 测试维度 | S3 Files | File Gateway | Mountpoint CSI |
|---|---------|----------|-------------|----------------|
| 1 | **部署复杂度** | 3 步 | 13 步 | `kubectl apply`（EKS Add-on） |
| 2 | **额外基础设施** | 无 | m5.xlarge + 150GB EBS | DaemonSet Pod |
| 3 | **小文件读延迟 (1KB)** | 8-11ms | 6ms / 2ms 缓存 | ~10-20ms（每次 S3 GET）* |
| 4 | **大文件读延迟 (10MB)** | 319ms 首次 / 6ms 缓存 | 58ms / 4-5ms 缓存 | 取决于 S3 吞吐* |
| 5 | **大文件顺序读吞吐** | 高（S3 直读） | 高（EBS 缓存 + S3） | **极高**（多并发 S3 GET）* |
| 6 | **写回 S3 延迟** | ~62 秒 | ~1 秒 | 即时（close 后）* |
| 7 | **写入能力** | 完整读写 | 完整读写 | 仅新建文件顺序写 |
| 8 | **反向同步** | 数秒（自动） | 需 RefreshCache + ~90s | 即时（直接 S3 API） |
| 9 | **POSIX 权限** | ✅ 完整 | ✅ 完整 | ❌ 不支持 |
| 10 | **文件锁** | ✅ | ✅ | ❌ |
| 11 | **文件重命名** | ✅ | ✅ | ❌（通用桶） |
| 12 | **协议** | NFS v4.1/4.2 | NFS + SMB | FUSE |
| 13 | **额外月成本** | $0 | ~$162（EC2+EBS） | ~$0（仅 DaemonSet 资源） |
| 14 | **适用计算服务** | EC2/Lambda/EKS/ECS | EC2 only | EKS / 自管理 K8s |
| 15 | **Fargate 支持** | 需确认 | ❌ | ❌ |

*\* Mountpoint CSI 数据基于文档分析和架构推断（FUSE + 直接 S3 API），非同环境实测。S3 Files 和 File Gateway 数据为同一 VPC 实测。*

### S3 Files vs File Gateway 延迟实测对比

| 操作 | S3 Files | File Gateway | 倍数差异 |
|------|----------|-------------|---------|
| 小文件首次读 (1KB) | 8ms | 6ms | FGW 快 1.3x |
| 小文件缓存读 (1KB) | N/A | 2ms | — |
| 大文件首次读 (10MB) | 319ms | 58ms | FGW 快 5.5x |
| 大文件缓存读 (10MB) | 6ms | 4ms | FGW 快 1.5x |
| 写回 S3 同步 | 62,000ms | ~1,000ms | FGW 快 62x |
| S3→NFS 反向同步 | ~3,000ms | ~90,000ms* | S3F 快 30x |

*\* File Gateway 需手动调用 RefreshCache API*

## 踩坑记录

!!! warning "踩坑 1: File Gateway 版本化桶需要额外 IAM 权限"
    当 S3 桶启用版本控制时，File Gateway 的 IAM 角色除了基本的 `s3:GetObject` 等权限外，还**必须**包含 `s3:GetObjectVersion`、`s3:ListBucketVersions` 等版本相关权限。否则通过 S3 API 直接上传到桶的文件，在 NFS 端 `cat` 会返回 `Input/output error`，但 `ls` 能看到文件元数据。

    这个错误信息没有任何提示是权限问题，排查时容易误判为网络或 Gateway 故障。

!!! warning "踩坑 2: File Gateway 反向同步不是自动的"
    与 S3 Files 不同，File Gateway **不会自动感知** S3 API 直接写入的变更。如果有外部程序直接向 S3 桶写入文件，NFS 客户端看不到这些文件，除非：
    
    1. 调用 `RefreshCache` API
    2. 或配置 S3 Event Notification + Lambda 自动触发 RefreshCache

    这对于需要双向数据流的场景是一个重大限制。

!!! info "踩坑 3: File Gateway 激活需要先等待启动"
    Storage Gateway AMI 启动后需要约 2-3 分钟才能响应 HTTP 激活请求。过早 curl 会超时。激活成功后还需等约 60 秒 Gateway 才真正连接到 AWS，否则 `list-local-disks` 等 API 会报 `GatewayNotConnected`。

!!! info "踩坑 4: POSIX 权限元数据格式不同"
    两种方案都将 POSIX 权限存为 S3 对象用户元数据，但格式不同：
    
    - S3 Files: `file-permissions: 0100755`（含文件类型前缀）
    - File Gateway: `file-permissions: 0644`（纯权限位）
    - Mountpoint CSI: 不存储 POSIX 权限
    
    如果你计划在方案间迁移，权限映射需要注意这个差异。

!!! warning "踩坑 5: Mountpoint CSI 不支持修改已有文件"
    这是 Mountpoint 最容易踩的坑。如果你的应用尝试 `open()` 一个已有文件并写入（不带 `O_TRUNC`），操作会**静默失败或报 IO error**。Mountpoint 的写入模型只支持：
    
    - 创建新文件 → 顺序写入 → `close()`
    - 覆盖已有文件 → 必须 `O_TRUNC` 重写整个文件（需 `--allow-overwrite` 启动参数）
    
    这意味着大量传统应用（日志轮转、数据库、配置更新）无法直接使用 Mountpoint。

## 费用明细

### S3 Files 计费模型详解

S3 Files 的费用由三部分组成：高性能存储费、数据访问费、底层 S3 费用。

#### 高性能存储费

| 计费项 | 价格（us-east-1） | 说明 |
|-------|-----------------|------|
| 高性能存储 | **$0.30/GB/月** | 只对文件系统上的活跃数据收费，按小时 prorated |

!!! info "存储费的关键机制"
    - **只对"热数据"收费** — 不是按 S3 桶总量，而是按高性能存储上的活跃数据量
    - **大文件不占存储** — ≥ 128KB（可配置阈值）的文件直接从 S3 流式读，不存入高性能存储
    - **自动过期** — 超过配置窗口（默认 30 天）未读的数据自动移除，过期操作不收费
    - **最小计费 6 KiB** — 每个文件/元数据最小按 6 KiB 计费
    - **$0.30/GB 不便宜** — 相比 S3 Standard $0.023/GB 贵了 13 倍，但只对小文件活跃集收取

#### 数据访问费

| 操作类型 | 价格 | 说明 |
|---------|------|------|
| 文件系统写入 | **$0.06/GB** | 写入高性能存储，最小计费 32 KB/次 |
| 文件系统读取（小文件） | **$0.03/GB** | 从高性能存储读取，最小计费 32 KB/次 |
| 同步导出（写回 S3） | **$0.03/GB** | 按 read 计费 |
| 同步导入（S3 → 文件系统） | **$0.06/GB** | 按 write 计费 |
| 大文件读取（≥ 128KB） | **$0** | 直接从 S3 流式读，只付标准 S3 GET 费 |
| 元数据操作（ls/stat/chmod） | **4 KB 按 read 计费** | 每次操作 4 KB |
| commit（fsync/close after write） | **4 KB 按 write 计费** | 每次操作 4 KB |
| 数据过期清理 | **$0** | 免费 |

!!! warning "写操作有双重计费"
    写入一个文件实际产生两笔费用：写入高性能存储 $0.06/GB + 同步回 S3 $0.03/GB = **合计 $0.09/GB**。大量小于 32KB 的小文件操作会因最小计费单位产生放大效应。

#### 底层 S3 费用（不变）

S3 Files 不替代 S3 存储费用，以下费用照常收取：

- S3 桶存储费（S3 Standard $0.023/GB/月）
- S3 Files 代你发起的 S3 PUT/GET 请求按标准费率
- EventBridge 通知费（反向同步检测变更用）

#### 定价示例（来自 AWS 官方）

场景：100GB S3 桶，文件应用读 10GB（94% 大文件 + 6% 小文件），写 1GB（0.25GB 最终同步回 S3）

| 计费项 | 计算 | 费用 |
|-------|------|------|
| 高性能存储 | 0.6GB 小文件 + 0.25GB 写入 = 0.85GB × $0.30 | $0.255 |
| 写入访问 | 1GB × $0.06 | $0.060 |
| 写回 S3 同步 | 0.25GB × $0.03 | $0.008 |
| 小文件读取 | 0.6GB × $0.03 | $0.018 |
| 小文件导入同步 | 0.6GB × $0.06 | $0.036 |
| 大文件直读 S3 | 9.4GB × $0 | **$0** |
| **S3 Files 月度合计** | | **$0.38** |
| + S3 桶存储 | 100GB × $0.023 | +$2.30 |

### File Gateway 方案费用

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| m5.xlarge EC2 (Gateway) | $0.192/h | 2h | $0.38 |
| t3.small EC2 (Client) | $0.0208/h | 2h | $0.04 |
| 150GB gp3 EBS | $0.08/GB/月 | 2h | $0.02 |
| S3 存储 | 标准 | < 1GB | < $0.01 |
| **合计** | | | **~$0.45** |

### Mountpoint CSI 方案费用

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EKS 集群 | $0.10/h | 已有集群则 $0 | $0 |
| S3 请求费 | 标准 S3 GET/PUT | 按量 | 标准 S3 费用 |
| **合计** | | | **仅 S3 标准费用** |

### 三方持续运行月成本对比

以「100GB S3 桶 + 10GB 活跃小文件 + 每月读 100GB + 写 10GB」为例：

| 方案 | 基础设施月成本 | 数据访问月成本 | S3 存储月成本 | 合计 |
|------|-------------|-------------|-------------|------|
| **S3 Files** | $0 | ~$3-5（高性能存储 + 访问费） | $2.30 | **~$5-7** |
| **File Gateway** | **~$162**（EC2 + EBS） | $0（缓存本地） | $2.30 | **~$164** |
| **Mountpoint CSI** | $0 | ~$0.04（S3 GET 请求） | $2.30 | **~$2.34** |

!!! tip "成本选型关键点"
    - **小数据量 + 偶尔访问**：三方差异不大，选最简单的（S3 Files 或 Mountpoint）
    - **大数据量 + 频繁小文件读写**：S3 Files 的 $0.30/GB 存储费 + $0.06/GB 写入费会累积
    - **持续运行**：File Gateway 有 ~$162/月固定成本，数据量小时最不划算
    - **纯大文件读取**：Mountpoint CSI 最便宜（仅 S3 GET 费用）

## 清理资源

### File Gateway 清理

```bash
# 1. 删除 NFS 文件共享
aws storagegateway delete-file-share \
  --file-share-arn arn:aws:storagegateway:us-east-1:595842667825:share/share-300D6F5B \
  --region us-east-1

# 2. 删除 Gateway
aws storagegateway delete-gateway \
  --gateway-arn arn:aws:storagegateway:us-east-1:595842667825:gateway/sgw-EA485B83 \
  --region us-east-1

# 3. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids i-0cfb689dec8ed42eb i-0801c968b82491e88 --region us-east-1

# 4. 等待实例终止后删除安全组
aws ec2 wait instance-terminated --instance-ids i-0cfb689dec8ed42eb i-0801c968b82491e88 --region us-east-1
aws ec2 delete-security-group --group-id sg-07a78a8029e6fdb59 --region us-east-1
aws ec2 delete-security-group --group-id sg-09546650aea2b189c --region us-east-1

# 5. 删除 IAM 角色
aws iam delete-role-policy --role-name FGWFileShareRole-36429 --policy-name S3Access
aws iam delete-role --role-name FGWFileShareRole-36429
aws iam remove-role-from-instance-profile --instance-profile-name FGWClientRole-36429-profile --role-name FGWClientRole-36429
aws iam delete-instance-profile --instance-profile-name FGWClientRole-36429-profile
aws iam detach-role-policy --role-name FGWClientRole-36429 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam detach-role-policy --role-name FGWClientRole-36429 --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam delete-role --role-name FGWClientRole-36429

# 6. 清空并删除 S3 桶
aws s3 rm s3://fgw-test-20260408-36429 --recursive --region us-east-1
aws s3api delete-bucket --bucket fgw-test-20260408-36429 --region us-east-1
```

!!! danger "务必清理"
    File Gateway 的 m5.xlarge EC2 实例每小时 $0.192，**一天不清理就是 $4.6**。Lab 完成后请立即执行清理步骤。

## 结论与建议

### 一句话总结

- **S3 Files** — "更简单更便宜"，零额外基础设施，完整文件系统语义
- **File Gateway** — "更快更灵活"，本地缓存加速，支持 SMB + 混合云
- **Mountpoint CSI** — "最轻量最高吞吐"，但只适合只读或追加写的 K8s 工作负载

### 场景化选型建议

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| **EC2 新应用上云** | ✅ S3 Files | 零额外基础设施，3 步部署 |
| **EKS ML 训练数据读取** | ✅ Mountpoint CSI | 大文件顺序读极高吞吐，零额外成本 |
| **EKS 应用需读写共享状态** | ✅ S3 Files | 完整文件系统语义 + EKS 原生支持 |
| **需要极低写延迟** | ✅ File Gateway | 写回 S3 ~1s vs S3 Files ~60s |
| **双向数据流（S3 API + NFS）** | ✅ S3 Files | 自动双向同步，无需额外配置 |
| **需要 SMB 协议** | ✅ File Gateway | S3 Files/Mountpoint 均不支持 SMB |
| **Windows 客户端** | ✅ File Gateway | SMB 协议原生支持 |
| **Lambda 挂载 S3** | ✅ S3 Files | Lambda 原生支持，其他两个不行 |
| **成本敏感** | ✅ S3 Files / Mountpoint | 无额外 EC2/EBS 成本 |
| **读延迟极致优化** | ✅ File Gateway | 本地 EBS 缓存 2-6ms |
| **大文件批量处理（只读）** | ✅ Mountpoint CSI | 多并发 S3 GET 最高吞吐 |
| **混合云（on-premises）** | ✅ File Gateway | 支持 VMware/Hyper-V/KVM |
| **AI Agent 文件共享** | ✅ S3 Files | 多客户端读写 + 自动同步 + 低运维 |
| **数据湖 Spark/Presto 读取** | ✅ Mountpoint CSI | 只读高吞吐，原生 K8s 集成 |

### 关键决策树

```
你的工作负载在哪里运行？
├── EC2 / Lambda / ECS
│   └── 需要 SMB 协议或混合云？
│       ├── 是 → File Gateway
│       └── 否 → 写延迟 < 5s 是硬性需求？
│           ├── 是 → File Gateway
│           └── 否 → S3 Files ✅
├── EKS / Kubernetes
│   └── 需要修改已有文件 / 文件锁 / POSIX 权限？
│       ├── 是 → S3 Files（通过 NFS PV 挂载）
│       └── 否 → 主要是大文件只读/追加写？
│           ├── 是 → Mountpoint CSI ✅
│           └── 否 → S3 Files ✅
└── On-premises
    └── File Gateway ✅（唯一选择）
```

### 迁移建议

**从 File Gateway 迁移到 S3 Files**：

- ✅ **可以迁移**：NFS-only 工作负载、写延迟不敏感、需要降低运维复杂度
- ⚠️ **评估后迁移**：依赖 File Gateway 本地缓存加速的读密集型工作负载
- ❌ **暂不迁移**：需要 SMB 协议、需要混合云部署、写延迟 < 5s 是 SLA 要求

**从 Mountpoint CSI 迁移到 S3 Files**：

- ✅ **建议迁移**：需要写入已有文件、文件锁、POSIX 权限的 K8s 应用
- ❌ **不建议迁移**：只读大文件吞吐场景（Mountpoint 吞吐更高、成本更低）

## 参考链接

- [Amazon S3 Files 官方文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html)
- [Amazon S3 File Gateway 官方文档](https://docs.aws.amazon.com/filegateway/latest/files3/what-is-file-s3.html)
- [Mountpoint for Amazon S3 CSI Driver](https://docs.aws.amazon.com/eks/latest/userguide/s3-csi.html)
- [Mountpoint for Amazon S3 文件系统行为](https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md)
- [Mounting Amazon S3 to EC2 using S3 File Gateway (AWS Blog)](https://aws.amazon.com/blogs/storage/mounting-amazon-s3-to-an-amazon-ec2-instance-using-a-private-connection-to-s3-file-gateway/)
- [S3 Files 实测：首个云对象存储的原生文件系统访问全面解析](../storage/s3-files.md)
- [S3 Files vs 开源方案实测：s3fs-fuse 和 fsspec 性能对比与选型](../storage/s3-files-vs-opensource.md)
