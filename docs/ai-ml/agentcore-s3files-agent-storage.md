---
tags:
  - AgentCore
  - Storage
  - S3
  - What's New
---

# AgentCore Runtime + S3 Files 实测：AI Agent 文件存储无限扩展的三种方案对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐⭐ 高级
    - **预估时间**: 90 分钟
    - **预估费用**: $20-50（含 NAT Gateway、AgentCore Runtime session、S3 Files）
    - **Region**: us-west-2
    - **最后验证**: 2026-04-09

## 背景

AI Agent 需要文件存储——写代码、存中间结果、缓存模型输出。Amazon Bedrock AgentCore Runtime 提供了 **session storage**，每个 session 最多 **1GB**。对于简单场景够用，但当 Agent 需要处理大文件（数据分析、代码仓库）、跨 session 共享文件、或存储超过 1GB 数据时，session storage 就不够了。

本文实测三种 Agent 文件存储方案并做对比：

1. **Session Storage**（内置）— 极低延迟，1GB 上限
2. **S3 API 直接访问** — 无限容量，延迟较高
3. **S3 Files NFS 挂载** — 文件系统语义 + 无限容量（理论最优）

结论先说：**三种方案在 AgentCore 中都可行**。方案 3（S3 Files NFS）需要自定义容器镜像预装 `amazon-efs-utils`，但成功后可获得 POSIX 文件系统语义 + 无限容量。推荐 **三层混合架构**：session storage（热）+ S3 Files NFS（温）+ S3 API（冷）。

## 前置条件

- AWS 账号（需要 Bedrock AgentCore、S3、ECR、EC2/VPC 权限）
- AWS CLI v2 已配置（支持 `bedrock-agentcore-control`、`bedrock-agentcore`、`s3files` 命令）
- Docker（支持 arm64 交叉编译，`docker buildx`）

<details>
<summary>AgentCore Execution Role 最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::YOUR-BUCKET", "arn:aws:s3:::YOUR-BUCKET/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

### 三种方案一览

| 维度 | Session Storage | S3 API | S3 Files NFS |
|------|----------------|--------|-------------|
| 容量 | 1GB（不可调） | 无限 | 无限 |
| 延迟（1KB 写） | **0.89ms** | 63ms | 12.84ms |
| 延迟（1KB 读） | **0.27ms** | 33ms | 2.88ms |
| 文件系统语义 | ✅ 完整 POSIX | ❌ 对象 API | ✅ NFS 4.2 |
| 跨 session 共享 | ❌ 隔离 | ✅ | ✅ |
| Stop/Resume 持久 | ✅ | ✅ | ✅ |
| AgentCore 可用性 | ✅ 内置 | ✅ boto3 | ✅ 需自定义镜像 |
| 成本 | 免费（含在 Runtime 价格中） | S3 标准价格 | $0.30/GB/月 + 访问费 |

### AgentCore Session Storage 限制

| 限制 | 值 | 可调 |
|------|-----|------|
| 最大存储大小 | 1 GB | ❌ |
| 最大文件数 | ~100,000-200,000 | ❌ |
| 最大目录深度 | 200 层 | ❌ |
| 不活跃清理 | 14 天 | ❌ |
| 版本更新后 | 重置（清空） | ❌ |
| 跨 session | 不支持 | ❌ |

### AgentCore MicroVM 环境

| 属性 | 值 |
|------|-----|
| vCPU / Memory | 2 vCPU / 8 GB |
| 内核 | Amazon Linux 2023 (aarch64) |
| 运行用户 | root (uid=0) |
| CAP_SYS_ADMIN | ✅ 有 |
| Session 最大生命周期 | 8 小时 |
| Idle 超时 | 15 分钟 |
| Container 架构 | **arm64 only** |

## 动手实践

### Step 1: 创建 Agent 测试代码

创建一个包含存储测试功能的 Agent 应用。核心代码结构：

```python
import os, time, boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
WORKSPACE = "/mnt/workspace"
S3_BUCKET = os.environ["S3_BUCKET"]
s3 = boto3.client("s3", region_name="us-west-2")

@app.entrypoint
def handle_request(payload):
    prompt = payload.get("prompt", "")
    if prompt == "perf_suite":
        return run_performance_tests()
    elif prompt == "capacity_test":
        return test_capacity_limit()
    # ... more tests
```

Agent 测试函数包括：
- `test_session_storage_write/read` — 不同大小文件的读写延迟
- `test_session_storage_capacity` — 1GB 上限验证
- `test_s3_write/read` — S3 API 读写延迟
- `test_nfs_mount` — S3 Files NFS 挂载尝试
- `test_hybrid_storage` — 混合方案验证

### Step 2: 构建 arm64 Docker 镜像并部署

```bash
# 构建 arm64 镜像（AgentCore 要求 arm64）
docker buildx build --platform linux/arm64 \
  -t 595842667825.dkr.ecr.us-west-2.amazonaws.com/agentcore-storage-test:latest \
  . --push

# 创建 AgentCore Runtime（带 session storage）
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name "storageTestAgent" \
  --role-arn "arn:aws:iam::595842667825:role/agentcore-storage-test-role" \
  --agent-runtime-artifact '{
    "containerConfiguration": {
      "containerUri": "595842667825.dkr.ecr.us-west-2.amazonaws.com/agentcore-storage-test:latest"
    }
  }' \
  --network-configuration '{"networkMode": "PUBLIC"}' \
  --filesystem-configurations '[{"sessionStorage": {"mountPath": "/mnt/workspace"}}]' \
  --environment-variables '{"S3_BUCKET": "my-bucket", "AWS_REGION": "us-west-2"}' \
  --region us-west-2
```

!!! warning "Container 必须是 arm64"
    AgentCore Runtime 运行在 Graviton（arm64）上。即使使用 container deployment，也必须提供 arm64 镜像。尝试推送 amd64 镜像会得到 `Architecture incompatible` 错误。

### Step 3: Session Storage 性能基线 (T1)

调用 Agent 运行性能测试：

```bash
PAYLOAD=$(echo -n '{"prompt": "perf_suite"}' | base64 -w0)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:595842667825:runtime/storageTestAgent-xxx" \
  --runtime-session-id "test-session-perf-suite-20260409a" \
  --payload "$PAYLOAD" \
  --region us-west-2 \
  /tmp/result.json && cat /tmp/result.json
```

**实测结果**：

| 文件大小 | Session Storage 写 (ms) | Session Storage 读 (ms) | S3 API 写 (ms) | S3 API 读 (ms) |
|----------|----------------------|----------------------|---------------|---------------|
| 1 KB     | 0.89                 | 0.27                 | 63.52         | 32.55         |
| 10 KB    | 0.37                 | 0.19                 | 28.81         | 25.68         |
| 100 KB   | 0.64                 | 0.24                 | 34.67         | 25.68         |
| 1 MB     | 2.89                 | 0.35                 | 106.12        | 33.20         |
| 10 MB    | 16.87                | 2.76                 | 133.75        | 120.49        |

**关键发现**：Session storage 读写延迟在亚毫秒级别，比 S3 API 快 8-135 倍。这是因为 session storage 内部实现是本地 NFS4 挂载在 `127.0.0.1:/export`，数据不出 microVM。

### Step 4: Session Storage 容量上限验证 (T2)

```bash
PAYLOAD=$(echo -n '{"prompt": "capacity_test"}' | base64 -w0)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "..." \
  --runtime-session-id "test-session-capacity-20260409abc" \
  --payload "$PAYLOAD" \
  --region us-west-2 /tmp/result.json
```

**实测结果**：

- 连续写入 20 × 50MB = 1000MB → ✅ 成功
- 第 21 个 50MB 块 → ❌ `[Errno 28] No space left on device`
- 50MB 单块写入延迟：52-69ms（稳定）

!!! danger "1GB 是硬限制"
    Session storage 的 1GB 上限不可通过 Service Quotas 调整。`df -h` 确认 `/mnt/workspace` 只有 1.0G。超出后直接 `ENOSPC`，没有预警。Agent 代码必须主动管理存储使用量。

### Step 5: S3 Files NFS 挂载尝试 (T5)

这是最关键的测试——能否在 AgentCore microVM 中 NFS 挂载 S3 Files？

**环境准备**：

1. 创建 VPC 模式的 AgentCore Runtime（指定 private subnet + security group）
2. 在同 VPC 创建 S3 Files 文件系统 + mount target
3. Security group 允许 agent → mount target 的 NFS 2049 端口

```bash
# VPC 模式创建
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name "storageTestVPC" \
  --network-configuration '{
    "networkMode": "VPC",
    "networkModeConfig": {
      "subnets": ["subnet-033a4f6f071999e17"],
      "securityGroups": ["sg-0d83b2659ae8bddae"]
    }
  }' \
  # ... 其他参数同上
```

**测试 1：PUBLIC 模式（无 VPC）**

```
mount.nfs4: Failed to resolve server fs-xxx.s3files.us-west-2.amazonaws.com: Name or service not known
```

预期失败——PUBLIC 模式下 Agent 无法访问 VPC 内的 S3 Files mount target DNS。

**测试 2：VPC 模式（IP 直连）**

```
mount.nfs4: access denied by server while mounting 172.31.46.231:/
```

进展！网络连通了（VPC 模式有效），但 S3 Files 拒绝了请求。

**原因分析**：

查阅 S3 Files 文档确认：

> "S3 Files always mounts a file system using TLS encryption and IAM authentication and these cannot be disabled."

S3 Files 要求通过 `amazon-efs-utils` mount helper 进行 IAM 认证挂载（`-t s3files` 而非 `-t nfs4`）。mount helper 需要：

1. `amazon-efs-utils` 包（>= v3.0.0）
2. EC2 instance profile 的 IAM 凭证
3. `efs-proxy` 进程建立 TLS 连接
4. `amazon-efs-mount-watchdog` 监控进程

AgentCore microVM 虽然有 `CAP_SYS_ADMIN`（可以执行 mount），但缺少完整的 efs-utils 生态系统。且 S3 Files 官方支持的计算环境只有 EC2、Lambda、EKS、ECS——不包含 AgentCore。

!!! success "S3 Files NFS 挂载在 AgentCore 中可行！"
    需要自定义容器镜像预装 `amazon-efs-utils` + `stunnel`，使用 `mount -t s3files`（不是 `mount -t efs`）。efs-utils 会自动通过 stunnel 建立 TLS 隧道，使用 IAM execution role 认证。

**更新（2026-04-09）：容器预装 efs-utils 方案验证成功！**

上述裸 `mount.nfs4` 直连 IP 的方式确实不可行（S3 Files 强制 TLS+IAM），但通过 **自定义容器镜像预装 `amazon-efs-utils`**，可以成功挂载：

```dockerfile
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# 预装 S3 Files mount 所需依赖
RUN yum install -y amazon-efs-utils stunnel nfs-utils python3-pip && \
    yum clean all
RUN pip3 install botocore boto3
# ... 你的 agent 代码
```

Agent 启动后执行：

```python
import subprocess, os

S3FILES_FS_ID = os.environ.get("S3FILES_FS_ID")
MOUNT_POINT = "/mnt/s3data"
os.makedirs(MOUNT_POINT, exist_ok=True)

# 关键：必须用 mount -t s3files（不是 mount -t efs）
result = subprocess.run(
    ["mount", "-t", "s3files", f"{S3FILES_FS_ID}:/", MOUNT_POINT],
    capture_output=True, text=True, timeout=60
)
if result.returncode == 0:
    print(f"S3 Files mounted at {MOUNT_POINT}")
    # df -h 显示 8.0 Exabytes 可用
```

**三种 mount 方式对比**：

| 方式 | 结果 | 原因 |
|------|------|------|
| `mount -t nfs4 <IP>:/` | ❌ access denied | 缺少 TLS/IAM |
| `mount -t efs -o tls,iam <FS_ID>:/` | ❌ DNS 解析失败 | efs-utils 用 EFS API 查 FS，S3 Files FS 不在 EFS 中 |
| `mount -t s3files <FS_ID>:/` | ✅ 成功 | S3 Files 专用 mount helper，正确处理 TLS+IAM |

**S3 Files 挂载性能实测**（3 次运行平均）：

| 文件大小 | S3 Files 写 (ms) | S3 Files 读 (ms) | vs Session Storage | vs S3 API |
|----------|-----------------|-----------------|-------------------|----------|
| 1 KB     | 12.84           | 2.88            | 14x / 11x 慢      | **5x 快** / 11x 快 |
| 100 KB   | 23.80           | 3.35            | 37x / 14x 慢      | **1.5x 快** / 8x 快 |
| 1 MB     | 41.85           | 7.35            | 14x / 21x 慢      | **2.5x 快** / 5x 快 |
| 10 MB    | 111.61          | 35.88           | 7x / 13x 慢       | **1.2x 快** / 3x 快 |

**挂载内部实现**：`127.0.0.1:/ on /mnt/s3data type nfs4 (vers=4.2)` — efs-utils 通过 stunnel 在本地端口建立 TLS 隧道到 S3 Files mount target，NFS 流量通过加密隧道传输。

!!! note "mount-watchdog 警告可忽略"
    挂载时会看到 `Could not start amazon-efs-mount-watchdog, unrecognized init system "python3"` 警告。不影响挂载功能。watchdog 负责监控 stunnel 进程健康，在 microVM 环境中不是必需的。

### Step 6: 混合方案验证 (T6)

既然 S3 Files NFS 不可行，最佳方案是 **session storage（热）+ S3 API（冷）** 的分层架构。

```python
# 混合存储策略示例
def store_file(filename, data, hot=True):
    if hot and len(data) < 100 * 1024 * 1024:  # <100MB 且标记为热
        # 写入 session storage（0.3-17ms）
        with open(f"/mnt/workspace/{filename}", "wb") as f:
            f.write(data)
    else:
        # 写入 S3（29-134ms，无容量限制）
        s3.put_object(Bucket=BUCKET, Key=f"agent-data/{filename}", Body=data)

def read_file(filename):
    local_path = f"/mnt/workspace/{filename}"
    if os.path.exists(local_path):
        # 热命中：0.2-2.8ms
        with open(local_path, "rb") as f:
            return f.read()
    else:
        # 冷获取：26-120ms
        resp = s3.get_object(Bucket=BUCKET, Key=f"agent-data/{filename}")
        return resp["Body"].read()
```

**实测结果**：

| 操作 | 延迟 (ms) |
|------|----------|
| 热数据写（session storage, 100KB） | 1.26 |
| 热数据读（session storage, 100KB） | 0.31 |
| 冷数据写（S3 API, 10MB） | 133.36 |
| 冷数据读（S3 API, 10MB） | 112.91 |
| **分层热命中** | **0.49** |
| **分层冷获取** | **112.92** |

热命中比冷获取快 **230 倍**。混合方案的价值明确。

### Step 7: 跨 Session 共享验证 (T7)

```python
# Session A: 写入 S3
s3.put_object(Bucket=BUCKET, Key="shared/report.json", Body=report_data)

# Session B: 读取同一文件
resp = s3.get_object(Bucket=BUCKET, Key="shared/report.json")
data = resp["Body"].read()  # ✅ 成功
```

**结果**：S3 API 跨 session 读写完美工作。Session storage 则完全隔离——session A 写的文件，session B 看不到。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T1 | Session Storage 读写性能 | ✅ | 写 0.37-16.87ms, 读 0.19-2.76ms | 内部 NFS4 on localhost |
| T2 | Session Storage 1GB 上限 | ✅ | 1000MB 成功, 1050MB 失败 | `ENOSPC` 硬限制 |
| T3 | S3 API 写性能 | ✅ | 29-134ms | 容量无限 |
| T4 | S3 API 读性能 | ✅ | 26-120ms | 随文件大小增长 |
| T5 | S3 Files NFS 挂载 | ✅ 成功 | 写 12-112ms, 读 3-36ms | 需容器预装 efs-utils + `mount -t s3files` |
| T6 | 混合方案 | ✅ | 热 0.49ms vs 冷 113ms | 230x 差异 |
| T7 | 跨 session 共享 | ✅ (S3) | S3 可共享 | Session storage 隔离 |
| T8 | Stop/Resume 持久性 | ✅ | 两者都持久 | 文档确认 |
| T9 | 大文件（10MB） | ✅ | Session: 17ms, S3: 134ms | Session storage 容量受限 |

## 踩坑记录

!!! warning "踩坑 1: Container 部署强制 arm64"
    AgentCore Runtime 运行在 Graviton 实例上。官方文档提到 "direct code deployment 仅 arm64"，但实测发现 **container deployment 也只接受 arm64 镜像**。尝试推送 amd64 镜像会得到 `Architecture incompatible for uri` 错误。

    ```
    An error occurred (ValidationException): Architecture incompatible for uri 
    '...'. Supported architectures: [arm64]
    ```

    使用 `docker buildx build --platform linux/arm64` 解决。

!!! warning "踩坑 2: Session Storage 1GB 到达时无预警"
    没有接近容量上限的警告或事件。Agent 写到 1000MB 一切正常，第 1001MB 直接 `[Errno 28] No space left on device`。建议在 Agent 代码中主动跟踪 `/mnt/workspace` 使用量。

!!! warning "踩坑 3: Runtime 版本更新会清空 Session Storage"
    更新 AgentCore Runtime（如推新镜像版本）会创建新版本，新 session 的 filesystem 会被重置为空。这意味着 **重要数据不能只存在 session storage 中**。

!!! info "发现: MicroVM 内部是 NFS4 on localhost"
    Session storage 的内部实现是 `127.0.0.1:/export`（本地 NFS4 挂载），这解释了为什么延迟如此低。数据实际存在 microVM 内部的 NFS 服务器中，异步复制到持久存储。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AgentCore Runtime session | 按 CPU 时间计费 | ~2 小时活跃 | ~$5 |
| NAT Gateway | $0.045/hr | ~2 hr | $0.09 |
| NAT Gateway 数据处理 | $0.045/GB | ~0.5 GB | $0.02 |
| S3 存储 | $0.023/GB | <1 GB | $0.02 |
| S3 Files 高性能存储 | $0.30/GB/月 | <1 GB | $0.30 |
| Elastic IP | $0.005/hr (unused) | ~2 hr | $0.01 |
| **合计** | | | **~$5-10** |

## 清理资源

```bash
PROFILE="weichaol-testenv2-awswhatsnewtest"
REGION="us-west-2"

# 1. 删除 AgentCore Runtimes
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id storageTestAgent-iQR14yA2f6 \
  --region $REGION --profile $PROFILE

aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id storageTestVPC-yht9T67Ls7 \
  --region $REGION --profile $PROFILE

# 2. 删除 S3 Files 资源（先 access point → mount target → file system）
aws s3files delete-access-point \
  --access-point-id fsap-01b1d7913c133708d \
  --file-system-id fs-05d4647267237c31e \
  --region $REGION --profile $PROFILE

aws s3files delete-mount-target \
  --mount-target-id fsmt-0c65c7ae68da1fdbe \
  --region $REGION --profile $PROFILE

# 等待 mount target 删除完成
sleep 60

aws s3files delete-file-system \
  --file-system-id fs-05d4647267237c31e \
  --region $REGION --profile $PROFILE

# 3. 清空并删除 S3 桶
aws s3 rm s3://agentcore-storage-test-usw2-595842667825 --recursive \
  --region $REGION --profile $PROFILE
aws s3 rb s3://agentcore-storage-test-usw2-595842667825 \
  --region $REGION --profile $PROFILE

# 4. 删除 ECR
aws ecr delete-repository --repository-name agentcore-storage-test --force \
  --region $REGION --profile $PROFILE

# 5. 删除 NAT Gateway + EIP
aws ec2 delete-nat-gateway --nat-gateway-id nat-02359b8c09e022981 \
  --region $REGION --profile $PROFILE
sleep 60  # 等待 NAT Gateway 删除
aws ec2 release-address --allocation-id eipalloc-0c78ff3ecf1bdac5b \
  --region $REGION --profile $PROFILE

# 6. 删除路由表关联和路由表
aws ec2 disassociate-route-table --association-id rtbassoc-0f332b69aa99ae907 \
  --region $REGION --profile $PROFILE
aws ec2 delete-route-table --route-table-id rtb-08159047e350770aa \
  --region $REGION --profile $PROFILE

# 7. 删除子网
aws ec2 delete-subnet --subnet-id subnet-033a4f6f071999e17 \
  --region $REGION --profile $PROFILE

# 8. 检查 ENI 残留后删除 Security Groups
aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=sg-0d83b2659ae8bddae" \
  --region $REGION --profile $PROFILE --query "NetworkInterfaces[*].NetworkInterfaceId"

aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=sg-0477f8015e2684e1c" \
  --region $REGION --profile $PROFILE --query "NetworkInterfaces[*].NetworkInterfaceId"

aws ec2 delete-security-group --group-id sg-0d83b2659ae8bddae \
  --region $REGION --profile $PROFILE
aws ec2 delete-security-group --group-id sg-0477f8015e2684e1c \
  --region $REGION --profile $PROFILE

# 9. 删除 IAM
aws iam delete-role-policy --role-name agentcore-storage-test-role \
  --policy-name agentcore-storage-test-policy --profile $PROFILE
aws iam delete-role-policy --role-name agentcore-storage-test-role \
  --policy-name agentcore-ecr-access --profile $PROFILE
aws iam delete-role --role-name agentcore-storage-test-role --profile $PROFILE

aws iam delete-role-policy --role-name s3files-storage-test-role \
  --policy-name s3files-bucket-access --profile $PROFILE
aws iam delete-role --role-name s3files-storage-test-role --profile $PROFILE
```

!!! danger "务必清理"
    NAT Gateway 持续计费 $0.045/hr（~$32/月）。S3 Files 高性能存储 $0.30/GB/月。测试完成后请立即清理。

## 结论与建议

### 方案选型指南

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 代码生成 Agent（<1GB 工作空间） | Session Storage only | 最简单，延迟最低 |
| 数据分析 Agent（>1GB 数据集） | Session Storage(热) + S3 Files(温) | 文件系统语义 + 无限容量，延迟比 S3 API 低 |
| 需要 POSIX 文件操作（grep/sed/awk） | S3 Files NFS | 完整文件系统语义，可直接用 shell 工具 |
| 多 Agent 协作（需共享文件） | S3 Files NFS 或 S3 API | S3 Files 可通过 NFS 共享挂载点 |
| 长期文件存储 | S3 API | Session storage 14 天清理 + 版本更新重置 |

### 成本对比（1 个 Agent，每月）

| 数据量 | Session Storage | S3 Standard | S3 Files |
|--------|----------------|-------------|----------|
| 100 MB | $0（含在 Runtime 价格中） | $0.002 | $0.03 + 访问费 |
| 1 GB   | $0（上限） | $0.023 | $0.30 + 访问费 |
| 10 GB  | ❌ 超限 | $0.23 | $3.00 + 访问费 |
| 100 GB | ❌ 超限 | $2.30 | $30 + 访问费 |

### 混合架构最佳实践

```
┌──────────────────────────────────────┐
│     Agent Code (microVM)              │
│                                       │
│  ┌─────────────────────────────────┐  │
│  │  /mnt/workspace (1GB)            │  │ ← 热层：当前工作文件、缓存
│  │  Session Storage                  │  │    延迟: <1ms read, <3ms write
│  └─────────────────────────────────┘  │
│            ↕ NFS 4.2 (stunnel TLS)    │
│  ┌─────────────────────────────────┐  │
│  │  /mnt/s3data (8 EB)              │  │ ← 温层：大数据集、POSIX 文件操作
│  │  S3 Files NFS (mount -t s3files)  │  │    延迟: 3-36ms read, 12-112ms write
│  └─────────────────────────────────┘  │
│            ↕ boto3                     │
│  ┌─────────────────────────────────┐  │
│  │  S3 Bucket (∞)                   │  │ ← 冷层：归档文件、跨 session 共享
│  │  s3://agent-data/                 │  │    延迟: 26-134ms
│  └─────────────────────────────────┘  │
│                                       │
└──────────────────────────────────────┘
```

**实现建议**：

1. **热门文件放 session storage** — 当前 task 的工作文件、pip/npm 缓存、临时编译产物
2. **大文件/历史文件放 S3** — 超过 100MB 的数据集、完成的产出物、需要跨 session 共享的文件
3. **主动管理容量** — 监控 `/mnt/workspace` 使用率，接近 80% 时自动归档到 S3
4. **重要数据双写** — Runtime 版本更新会清空 session storage，关键文件务必同步到 S3

### S3 Files NFS：通过自定义镜像已可用 ✅

通过容器预装 `amazon-efs-utils` + `stunnel`，S3 Files NFS 挂载已经验证可行：

- **容器镜像**：基于 Amazon Linux 2023，安装 `amazon-efs-utils stunnel nfs-utils`
- **挂载命令**：`mount -t s3files <FS_ID>:/ /mnt/s3data`（不是 `mount -t efs`）
- **性能**：写延迟 12-112ms，读延迟 3-36ms，介于 session storage 和 S3 API 之间
- **容量**：8.0 Exabytes（虚拟无限），突破 session storage 的 1GB 限制
- **语义**：完整 POSIX 文件系统（NFS 4.2），支持目录遍历、文件锁等
- **数据持久**：文件自动同步到 S3 桶，天然持久

!!! tip "三层混合架构（推荐）"
    现在三种方案都已验证可行，推荐三层混合架构：
    
    - **热层** — Session Storage（/mnt/workspace）：当前 task 工作文件，延迟 <1ms
    - **温层** — S3 Files NFS（/mnt/s3data）：需要文件系统语义的大数据集，延迟 3-36ms，容量无限
    - **冷层** — S3 API（boto3）：归档文件、跨 session 共享，延迟 26-134ms，最灵活

## 参考链接

- [AgentCore Runtime Session Storage 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html)
- [AgentCore Runtime Quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
- [S3 Files 文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html)
- [S3 Files 支持的计算环境](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-attach-compute.html)
- [AgentCore CreateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html)
