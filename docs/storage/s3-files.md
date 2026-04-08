# Amazon S3 Files 实测：首个云对象存储的原生文件系统访问全面解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-08

## 背景

组织将数据湖和分析数据存储在 S3 中，但基于文件的工具、AI Agent 和应用程序从未能直接访问这些数据。要让文件工具访问 S3 数据，你需要管理独立的文件系统、复制数据、构建复杂的同步管道。S3 Files 消除了这些摩擦。

S3 Files 是一个共享文件系统，将任何 AWS 计算资源直接与 S3 中的数据连接。基于 Amazon EFS 构建，它通过 NFS v4.1/v4.2 协议提供完整的文件系统语义和低延迟性能，同时数据从未离开 S3。这是首个（也是目前唯一的）提供原生文件系统访问的云对象存储。

2026 年 4 月正式 GA，支持 34 个 AWS Region。本文通过 9 项实测验证其核心能力。

## 前置条件

- AWS 账号
- AWS CLI v2.34.26+（支持 `aws s3files` 子命令）
- 一个启用了版本控制的 S3 桶
- EC2 实例（Amazon Linux 2023 推荐）
- `amazon-efs-utils` v3.0.0+

<details>
<summary>最小 IAM Policy — File System 角色（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3BucketPermissions",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET"
    },
    {
      "Sid": "S3ObjectPermissions",
      "Effect": "Allow",
      "Action": ["s3:AbortMultipartUpload", "s3:DeleteObject*", "s3:GetObject*", "s3:List*", "s3:PutObject*"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/*"
    },
    {
      "Sid": "EventBridgeManage",
      "Effect": "Allow",
      "Action": ["events:DeleteRule", "events:DisableRule", "events:EnableRule", "events:PutRule", "events:PutTargets", "events:RemoveTargets"],
      "Condition": { "StringEquals": { "events:ManagedBy": "elasticfilesystem.amazonaws.com" } },
      "Resource": ["arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*"]
    }
  ]
}
```

</details>

## 核心概念

S3 Files 的架构分为三层：

```
S3 桶（数据权威来源）
   ↕ 双向自动同步
高性能存储层（活跃数据缓存，基于 EFS）
   ↕ NFS v4.1/v4.2
计算资源（EC2 / Lambda / EKS / ECS）
```

### 关键参数一览

| 参数 | 值 | 说明 |
|------|-----|------|
| 协议 | NFS v4.1 / v4.2 | 标准 Linux mount |
| 导入阈值 | 128 KB（默认） | 小于此值的文件自动缓存到高性能存储 |
| 大文件读路由 | ≥ 1 MB 从 S3 直读 | 即使缓存了也走 S3（优化吞吐） |
| 写回延迟 | ~60 秒 | 聚合批处理后一次 PUT |
| 数据过期 | 30 天（可配 1-365） | 未读数据自动从高性能存储移除 |
| 最大连接数 | 25,000 / 文件系统 | |
| 读吞吐 | 数 TB/s 聚合，3 GiB/s 单客户端 | |
| 写吞吐 | 1-5 GiB/s 聚合 | |
| 冲突策略 | **S3 桶是 source of truth** | 并发修改时 S3 版本胜出 |

### 智能读路由规则

| 场景 | 读取来源 | 费用 |
|------|----------|------|
| 小文件 < 128KB（已缓存） | 高性能存储 | FS 访问费 |
| 大文件 ≥ 1MB（已同步到 S3） | 直接从 S3 流式读 | 标准 S3 GET |
| 刚写入未同步的数据 | 高性能存储 | FS 访问费 |
| 未在高性能存储的数据 | 直接从 S3 | 标准 S3 GET |

## 动手实践

### Step 1: 创建 S3 桶并准备测试数据

```bash
# 创建桶
aws s3 mb s3://s3files-test-bucket --region us-east-1

# 启用版本控制（S3 Files 强制要求）
aws s3api put-bucket-versioning \
  --bucket s3files-test-bucket \
  --versioning-configuration Status=Enabled

# 上传测试数据 — 小文件
for i in $(seq 1 5); do
  echo "Small test file ${i}" | aws s3 cp - s3://s3files-test-bucket/small/file${i}.txt
done

# 中文件（1MB）
dd if=/dev/urandom bs=1M count=1 2>/dev/null | \
  aws s3 cp - s3://s3files-test-bucket/medium/1mb-file.bin

# 大文件（10MB）
dd if=/dev/urandom bs=1M count=10 2>/dev/null | \
  aws s3 cp - s3://s3files-test-bucket/large/10mb-file.bin
```

### Step 2: 创建 File System 和 Mount Target

```bash
# 创建 S3 File System
aws s3files create-file-system \
  --region us-east-1 \
  --bucket arn:aws:s3:::s3files-test-bucket \
  --role-arn arn:aws:iam::ACCOUNT_ID:role/S3FilesRole

# 获取文件系统 ID
FS_ID="fs-0d3cc3a5aa52b21fd"  # 从上面的输出中获取

# 创建 Mount Target
aws s3files create-mount-target \
  --region us-east-1 \
  --file-system-id $FS_ID \
  --subnet-id subnet-xxx \
  --security-groups sg-mount-target
```

!!! warning "安全组配置"
    Mount Target 安全组入站规则：TCP 2049 ← EC2 安全组。**绝不要开放 0.0.0.0/0 入站。**

File System 创建后立即变为 `available`，Mount Target 需要约 1-2 分钟。

### Step 3: 安装客户端并挂载

```bash
# 在 EC2 上安装 efs-utils（Amazon Linux 2023）
sudo yum -y install amazon-efs-utils

# 创建挂载点并挂载
sudo mkdir -p /mnt/s3files
sudo mount -t s3files fs-0d3cc3a5aa52b21fd:/ /mnt/s3files
```

**实测输出**：
```
$ df -h /mnt/s3files
Filesystem      Size  Used Avail Use% Mounted on
127.0.0.1:/     8.0E     0  8.0E   0% /mnt/s3files
```

挂载后显示 8.0 EB 虚拟容量 — 这是 NFS 协议的默认表示，不是实际限制。

### Step 4: 基础文件操作验证

```bash
$ ls -la /mnt/s3files/
drwxr-xr-x. 3 root root 10240 Apr  8 01:55 .
drwx------. 2 root root 10240 Apr  8 01:55 .s3files-lost+found-fs-xxx
drwxr-xr-x. 2 root root 10240 Apr  8 02:08 large
drwxr-xr-x. 2 root root 10240 Apr  8 02:08 medium
drwxr-xr-x. 2 root root 10240 Apr  8 02:08 small

$ cat /mnt/s3files/small/file1.txt
Small test file 1 - Wed Apr  8 01:54:17 UTC 2026
```

所有预先上传的 S3 对象（5 个小文件、1 个 1MB、1 个 10MB）通过文件系统全部可见。

### Step 5: 写回同步 — 实测 60 秒批处理窗口

```bash
# 通过 NFS 写入文件
$ echo "Hello from S3 Files" | sudo tee /mnt/s3files/test-write.txt
Write done at: 02:08:17

# 30 秒后检查 S3 — 还没有！
$ aws s3 ls s3://s3files-test-bucket/test-write.txt
(404 Not Found)

# 90 秒后检查 S3 — 出现了！
$ aws s3 ls s3://s3files-test-bucket/test-write.txt
2026-04-08 02:09:19         61 test-write.txt
```

**关键数据**：写入 02:08:17 → S3 出现 02:09:19 = **62 秒延迟**。完美匹配文档描述的 "up to 60 seconds" 批处理窗口。

### Step 6: 反向同步 — S3 API 写入秒级可见

```bash
# 通过 S3 API 直接上传
$ echo "Uploaded via S3 API" | aws s3 cp - s3://s3files-test-bucket/api-upload.txt

# NFS 端立即可见
$ cat /mnt/s3files/api-upload.txt
Uploaded via S3 API at Wed Apr  8 02:08:52 UTC 2026
```

S3 API 上传后，文件系统端在数秒内即可见。S3 Files 通过 EventBridge / S3 Event Notifications 检测桶变更。

### Step 7: 读延迟对比 — 小文件 vs 大文件

```bash
# 小文件（49 字节）— 从高性能存储读取
$ time cat /mnt/s3files/small/file1.txt > /dev/null
real    0m0.008s

$ time cat /mnt/s3files/small/file2.txt > /dev/null
real    0m0.011s

# 大文件（10MB）— 首次读取（从 S3 流式读）
$ time cat /mnt/s3files/large/10mb-file.bin > /dev/null
real    0m0.319s

# 大文件 — 再次读取（已缓存）
$ time cat /mnt/s3files/large/10mb-file.bin > /dev/null
real    0m0.006s
```

| 场景 | 延迟 | 来源 |
|------|------|------|
| 小文件（二次访问） | 8-11ms | 高性能存储 |
| 大文件（首次读取） | 319ms | S3 流式读 |
| 大文件（二次访问） | 6ms | 高性能存储缓存 |

### Step 8: POSIX 权限导出验证

```bash
# 设置权限
$ sudo chmod 755 /mnt/s3files/perm-test.txt
$ ls -la /mnt/s3files/perm-test.txt
-rwxr-xr-x. 1 root root 10 Apr  8 02:08 /mnt/s3files/perm-test.txt

# 检查 S3 对象元数据
$ aws s3api head-object --bucket s3files-test-bucket --key perm-test.txt
{
    "Metadata": {
        "fs-id": "fs-0d3cc3a5aa52b21fd:...:3",
        "user-agent": "aws-s3-files",
        "file-permissions": "0100755",
        "file-owner": "0",
        "file-group": "0",
        "file-btime": "1775614097665000000ns",
        "file-mtime": "1775614097672000000ns"
    }
}
```

POSIX 权限（UID/GID/mode）以 S3 用户定义元数据的形式持久化。`chmod`/`chown` 更改会在同步时导出到 S3。

### Step 9: 冲突处理 — S3 是 Source of Truth

```bash
# 1. 通过 NFS 写入
$ echo "NFS write at 02:10:44" | sudo tee /mnt/s3files/conflict-test.txt

# 2. 在 NFS 写入同步到 S3 之前（<60s），通过 S3 API 覆盖
$ echo "S3 API write - this should win" | aws s3 cp - s3://s3files-test-bucket/conflict-test.txt

# 3. 15 秒后从 NFS 读取
$ cat /mnt/s3files/conflict-test.txt
S3 API write at 02:11:06 - this should win
```

S3 API 写入在冲突中胜出，NFS 视图自动更新为 S3 版本。

### Step 10: Glacier 存储类 — 可见但不可读

```bash
# 复制到 Glacier 存储类
$ aws s3 cp s3://s3files-test-bucket/small/file5.txt \
    s3://s3files-test-bucket/glacier-test.txt --storage-class GLACIER

# NFS 端 — 元数据可见
$ ls -la /mnt/s3files/glacier-test.txt
-rw-r--r--. 1 root root 49 Apr  8 02:08 /mnt/s3files/glacier-test.txt

# 但无法读取内容
$ cat /mnt/s3files/glacier-test.txt
cat: /mnt/s3files/glacier-test.txt: Invalid argument
```

Glacier/Deep Archive 对象在文件系统中可见（元数据正常），但读取数据返回 `Invalid argument`。需先通过 S3 API restore 对象。

### Step 11: 目录重命名 — 文件系统即时，S3 逐对象同步

```bash
# 创建含 5 个文件的目录
$ sudo mkdir -p /mnt/s3files/rename-test/
$ for i in 1 2 3 4 5; do echo "file $i" | sudo tee /mnt/s3files/rename-test/f${i}.txt > /dev/null; done

# 重命名
$ echo "Rename start: $(date -u +%H:%M:%S)"
Rename start: 02:08:18
$ sudo mv /mnt/s3files/rename-test /mnt/s3files/renamed-dir
$ echo "Rename done: $(date -u +%H:%M:%S)"
Rename done: 02:08:18

# NFS 端即时完成（<1s）
$ ls /mnt/s3files/renamed-dir/
f1.txt  f2.txt  f3.txt  f4.txt  f5.txt

# ~10 秒后 S3 端完成逐对象同步
$ aws s3 ls s3://s3files-test-bucket/renamed-dir/
2026-04-08 02:09:29  f1.txt
2026-04-08 02:09:28  f2.txt
...
```

!!! warning "生产注意：大目录重命名代价高"
    S3 没有原生的重命名操作。S3 Files 必须对每个对象执行 PUT + DELETE。数百万文件的目录重命名可能需要数分钟在 S3 端完成，且期间 S3 桶中会同时存在新旧路径。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T1 | 基础挂载与文件操作 | ✅ 通过 | ls/cat/echo 正常 | 所有预存对象可见 |
| T2 | 写回同步验证 | ✅ 通过 | **62 秒延迟** | 匹配 ~60s 批处理窗口 |
| T3 | 反向同步 | ✅ 通过 | 数秒可见 | 通过 EventBridge 检测 |
| T4 | 小文件 vs 大文件读延迟 | ✅ 通过 | 小文件 8ms / 大文件首次 319ms | 智能读路由生效 |
| T5 | POSIX 权限导出 | ✅ 通过 | 元数据含 file-permissions | chmod/chown 同步到 S3 |
| T6 | 已有对象可见性 | ✅ 通过 | 7/7 对象全部可见 | 无需迁移 |
| T7 | 冲突处理 | ✅ 通过 | S3 版本胜出 | Source of truth 行为确认 |
| T8 | Glacier 存储类 | ✅ 通过 | 可见不可读 | 返回 Invalid argument |
| T9 | 目录重命名 | ✅ 通过 | NFS <1s / S3 ~10s | 逐对象同步 |

## 踩坑记录

!!! warning "踩坑 1: AWS CLI 版本要求"
    `aws s3files` 子命令需要 AWS CLI **v2.34.26+**。旧版本（如 2.34.14）会报 `Found invalid choice 's3files'`。
    
    ```bash
    # 检查版本
    aws --version
    # 升级
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
    unzip awscliv2.zip && sudo ./aws/install --update
    ```

!!! warning "踩坑 2: S3 Files 标签格式用小写"
    S3 Files API 的标签参数使用小写 `key`/`value`（不是 EC2 风格的 `Key`/`Value`）。
    
    ```bash
    # ❌ 错误
    --tags Key=Project,Value=test
    # ✅ 正确
    --tags key=Project,value=test
    ```

!!! info "踩坑 3: 挂载类型是 s3files 不是 efs"
    虽然基于 EFS 构建，但挂载命令使用 `mount -t s3files`，不是 `mount -t efs`。需要 `amazon-efs-utils` v3.0.0+。

!!! info "踩坑 4: AmazonElasticFileSystemUtils 托管策略不存在"
    官方文档提到 `AmazonElasticFileSystemUtils` 托管策略用于启用 CloudWatch 监控，但在 2026-04-08 实测时该策略在 IAM 中不存在。使用 `AmazonS3FilesClientFullAccess` 即可。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.small | $0.0208/hr | ~2 hr | ~$0.04 |
| S3 存储 | $0.023/GB | ~12 MB | < $0.01 |
| S3 Files 高性能存储 | 按 EFS 计费 | < 100 MB | < $0.01 |
| S3 Files 访问费 | 按读写计费 | 少量 | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 卸载文件系统
sudo umount /mnt/s3files

# 2. 删除 Mount Target
aws s3files delete-mount-target --mount-target-id fsmt-xxx --region us-east-1

# 3. 删除 File System（等 Mount Target 删除完成后）
aws s3files delete-file-system --file-system-id fs-xxx --region us-east-1

# 4. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids i-xxx

# 5. 清理 IAM
aws iam remove-role-from-instance-profile --instance-profile-name xxx --role-name xxx
aws iam delete-instance-profile --instance-profile-name xxx
aws iam detach-role-policy --role-name EC2Role --policy-arn arn:aws:iam::aws:policy/AmazonS3FilesClientFullAccess
aws iam detach-role-policy --role-name EC2Role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role-policy --role-name EC2Role --policy-name S3ReadAccess
aws iam delete-role --role-name EC2Role
aws iam delete-role-policy --role-name FSRole --policy-name S3FilesAccess
aws iam delete-role --role-name FSRole

# 6. 删除安全组
aws ec2 delete-security-group --group-id sg-ec2
aws ec2 delete-security-group --group-id sg-mt

# 7. 清空并删除 S3 桶
aws s3 rm s3://s3files-test-bucket --recursive
aws s3api delete-bucket --bucket s3files-test-bucket
```

!!! danger "务必清理"
    S3 Files 的高性能存储按 EFS 费率计费。虽然本 Lab 数据量极小，但长期不清理可能产生持续费用。

## 结论与建议

### 场景化推荐

| 场景 | 推荐使用 S3 Files | 理由 |
|------|-------------------|------|
| AI Agent 共享状态 | ✅ 强烈推荐 | 多计算实例通过 NFS 直接共享 S3 数据，无需复制 |
| ML 数据准备 | ✅ 推荐 | 文件系统工具直接处理 S3 数据湖 |
| 大文件流式处理 | ✅ 推荐 | 智能读路由自动优化，大文件直接从 S3 读取 |
| 小文件频繁随机读写 | ⚠️ 注意成本 | 高性能存储缓存有效，但需关注 FS 访问费用 |
| 大目录频繁重命名 | ❌ 不推荐 | S3 无原子重命名，逐对象同步代价高 |
| Glacier 数据直接访问 | ❌ 不支持 | 需先 restore 再访问 |

### 关键洞察

1. **60 秒写回窗口是双刃剑**：批处理降低了 S3 请求成本，但意味着 NFS 写入不会立即出现在 S3 中。对实时性要求高的场景需评估。
2. **S3 是 Source of Truth**：并发修改时 S3 API 写入会覆盖 NFS 写入。多团队混合使用需建立写入协议。
3. **读路由真的智能**：大文件自动走 S3 高吞吐通道，小文件走高性能存储低延迟通道。用户无需配置。

## 参考链接

- [Amazon S3 Files 官方文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-s3-files/)
- [S3 Files 产品页](https://aws.amazon.com/s3/features/files/)
- [S3 定价页](https://aws.amazon.com/s3/pricing/)
- [AWS News Blog](https://aws.amazon.com/blogs/aws/launching-s3-files-making-s3-buckets-accessible-as-file-systems)
