# S3 Files vs S3 File Gateway 实测：原生文件系统访问与经典网关方案的全面对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $3-5（S3 Files 测试 < $1 + File Gateway m5.xlarge ~$0.19/h × 1h + 150GB gp3）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-08

## 背景

将 S3 桶挂载为文件系统一直是许多应用的刚需——传统应用无法直接调用对象存储 API，但又想利用 S3 的无限扩展性和低成本。

在 2026 年 4 月 S3 Files 发布之前，**S3 File Gateway** 是唯一的 AWS 官方方案：部署一台 Storage Gateway EC2 作为 NFS/SMB 中间层，客户端挂载 Gateway 的文件共享来读写 S3。

S3 Files 的出现改变了这个局面——它是 S3 的原生能力，无需额外基础设施，3 条命令即可挂载。

但 weichao 提了一个好问题：**两种方案在实际性能上差多少？什么场景该选哪个？**

本文通过相同的测试矩阵，对两种方案做完全一致的实测对比。

## 前置条件

- AWS 账号（需要 S3、EC2、Storage Gateway、IAM 权限）
- AWS CLI v2.34.26+（S3 Files 需要 `aws s3files` 子命令）
- 同一 VPC、同一 AZ 的测试环境（消除网络变量）

## 核心概念

### 架构对比

| 维度 | S3 Files | S3 File Gateway |
|------|----------|----------------|
| **架构** | S3 原生服务 → Mount Target → NFS | EC2 Gateway 虚拟设备 → NFS/SMB → S3 |
| **缓存层** | S3 高性能存储层（托管） | Gateway EC2 本地 EBS（自管理） |
| **协议** | NFS v4.1/4.2 | NFS v3/v4.1 + SMB v2/v3 |
| **同步方向** | 双向自动 | 写→S3 自动，S3→NFS 需手动 RefreshCache |
| **额外基础设施** | 无 | m5.xlarge EC2 + 150GB+ EBS |
| **部署步骤** | 3 步 | 13 步 |
| **挂载命令** | `mount -t s3files fs-xxx:/ /mnt` | `mount -t nfs gw-ip:/bucket /mnt` |
| **发布时间** | 2026 年 4 月（GA） | 2018 年（成熟服务） |

### 读路由差异

**S3 Files** 有智能读路由：

- 小文件（< 128KB）从高性能存储层读
- 大文件（≥ 1MB）直接从 S3 流式读
- 刚修改未同步的数据从文件系统读

**File Gateway** 的读路由更简单：

- 所有读操作优先走本地 EBS 缓存
- 缓存未命中时从 S3 拉取到缓存再返回
- 缓存空间有限（150GB-64TiB），满了会淘汰

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

**结论：S3 Files 反向同步自动且快速（数秒），File Gateway 需要手动 RefreshCache 且等待时间更长。**

## 测试结果

### 核心对比表

| # | 测试维度 | S3 Files | File Gateway | 优势方 |
|---|---------|----------|-------------|-------|
| 1 | **部署步骤** | 3 步 | 13 步 | ✅ S3 Files |
| 2 | **部署时间** | ~3-4 分钟 | ~7-8 分钟 | ✅ S3 Files |
| 3 | **小文件读延迟 (1KB)** | 8-11ms | 6ms 首次 / 2ms 缓存 | ✅ File Gateway |
| 4 | **大文件读延迟 (10MB)** | 319ms 首次 / 6ms 缓存 | 58ms 首次 / 4-5ms 缓存 | ✅ File Gateway |
| 5 | **写回 S3 延迟** | ~62 秒 | ~1 秒 | ✅ File Gateway |
| 6 | **反向同步 (S3→NFS)** | 数秒（自动） | 需 RefreshCache + ~90s | ✅ S3 Files |
| 7 | **缓存命中读取** | ~6ms | ~2-5ms | ✅ File Gateway |
| 8 | **POSIX 权限** | ✅ 存为 S3 元数据 | ✅ 存为 S3 元数据 | 平手 |
| 9 | **额外基础设施成本** | $0 | ~$0.19/h (EC2) + ~$12/mo (EBS) | ✅ S3 Files |
| 10 | **协议支持** | NFS v4.1/4.2 | NFS v3/v4.1 + SMB v2/v3 | ✅ File Gateway |
| 11 | **挂载容量显示** | 8.0 EB | 8.0 EB | 平手 |
| 12 | **NFS 实际版本** | v4.2 | v4.2 | 平手 |

### 延迟对比图（数据汇总）

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
    
    如果你计划在两种方案间迁移，权限映射需要注意这个差异。

## 费用明细

### S3 Files 方案

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| t3.small EC2 | $0.0208/h | 2h | $0.04 |
| S3 Files 高性能存储 | $0.016/GB/月 | < 1GB | < $0.02 |
| S3 Files 访问费 | $0.0025/千次 | < 100 次 | < $0.01 |
| **合计** | | | **< $0.10** |

### File Gateway 方案

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| m5.xlarge EC2 (Gateway) | $0.192/h | 2h | $0.38 |
| t3.small EC2 (Client) | $0.0208/h | 2h | $0.04 |
| 150GB gp3 EBS | $0.08/GB/月 | 2h | $0.02 |
| S3 存储 | 标准 | < 1GB | < $0.01 |
| **合计** | | | **~$0.45** |

**持续运行成本差异**：File Gateway 每月约 $150（EC2）+ $12（EBS）= **$162/月**，S3 Files 无额外基础设施成本。

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

**S3 Files 是"更简单更便宜"的选择，File Gateway 是"更快更灵活"的选择。**

### 场景化选型建议

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| **新应用上云** | ✅ S3 Files | 零额外基础设施，3 步部署，维护成本最低 |
| **需要极低写延迟** | ✅ File Gateway | 写回 S3 延迟 ~1s vs S3 Files ~60s |
| **双向数据流** | ✅ S3 Files | S3 API 写入自动同步到 NFS，无需额外配置 |
| **需要 SMB 协议** | ✅ File Gateway | S3 Files 目前仅支持 NFS |
| **Windows 客户端** | ✅ File Gateway | SMB 协议原生支持 |
| **成本敏感** | ✅ S3 Files | 无额外 EC2/EBS 成本，省 $160+/月 |
| **读延迟敏感** | ✅ File Gateway | 本地 EBS 缓存 → 2-6ms vs S3 Files 8-11ms |
| **大文件工作负载** | ✅ File Gateway | 大文件首次读 58ms vs 319ms（5.5x 差异） |
| **Lambda/EKS/ECS 集成** | ✅ S3 Files | 原生支持，File Gateway 只能 EC2 挂载 |
| **混合云（on-premises）** | ✅ File Gateway | 支持 VMware/Hyper-V/KVM/硬件设备 |

### 关键决策树

```
需要 SMB 协议？
  → 是 → File Gateway
  → 否 →
    需要混合云（on-premises 挂载）？
      → 是 → File Gateway
      → 否 →
        写回延迟 < 5s 是硬性需求？
          → 是 → File Gateway
          → 否 →
            需要 Lambda/EKS/ECS 挂载？
              → 是 → S3 Files
              → 否 → S3 Files（更简单、更便宜）
```

### 迁移建议

如果你正在使用 File Gateway 并考虑迁移到 S3 Files：

- ✅ **可以迁移**：NFS-only 工作负载、写延迟不敏感、需要降低运维复杂度
- ⚠️ **评估后迁移**：依赖 File Gateway 本地缓存加速的读密集型工作负载
- ❌ **暂不迁移**：需要 SMB 协议、需要混合云部署、写延迟 < 5s 是 SLA 要求

## 参考链接

- [Amazon S3 Files 官方文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html)
- [Amazon S3 File Gateway 官方文档](https://docs.aws.amazon.com/filegateway/latest/files3/what-is-file-s3.html)
- [Mounting Amazon S3 to EC2 using S3 File Gateway (AWS Blog)](https://aws.amazon.com/blogs/storage/mounting-amazon-s3-to-an-amazon-ec2-instance-using-a-private-connection-to-s3-file-gateway/)
- [S3 Files 实测：首个云对象存储的原生文件系统访问全面解析](../storage/s3-files.md)
