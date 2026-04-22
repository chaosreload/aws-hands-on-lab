# Lambda S3 Files 实测：原生挂载 + 多函数共享 workspace 实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45-60 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-22

## 背景

AWS Lambda 长期以来在处理 S3 对象时有两种主流做法：要么用 `boto3.download_file` 把对象拉到 `/tmp`（受 512 MB~10 GB ephemeral storage 限制），要么直接流式读取（复杂度高，且不支持多函数间共享状态）。现在 Lambda 原生支持挂载 [Amazon S3 Files](https://aws.amazon.com/s3/features/files/)——S3 桶以文件系统形式直接出现在 `/mnt/shared`，多个 Lambda 函数甚至可以同时读写同一个文件系统，无需自建同步逻辑。

这个能力特别适合 **AI/ML stateful 工作流**：orchestrator 函数克隆代码库到共享 workspace，多个 agent 函数并行分析代码，durable functions SDK 负责 checkpoint 和自动恢复，S3 Files 则提供跨步骤的数据共享。本文通过真实 AWS 账号跑完完整 hands-on，给出冷启动/执行时间/内存/代码复杂度四维对比，并验证"Capacity Provider 不兼容"这一边界条件。

## 前置条件

- AWS 账号（需要 Lambda、S3、S3 Files、EC2 VPC、IAM 权限）
- AWS CLI v2 已配置（`aws --version` 需要 ≥ 2.15）
- 目标 Region 同时支持 Lambda 和 S3 Files（[查询可用 Region](https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/)）

<details>
<summary>最小 IAM Policy（点击展开）</summary>

Lambda 执行角色（挂载 + 读写 + 直读 S3 + 日志 + VPC）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"S3Files","Effect":"Allow","Action":["s3files:ClientMount","s3files:ClientWrite"],"Resource":"*"},
    {"Sid":"S3Direct","Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion"],"Resource":"arn:aws:s3:::BUCKET/*"},
    {"Sid":"VPC","Effect":"Allow","Action":["ec2:CreateNetworkInterface","ec2:DescribeNetworkInterfaces","ec2:DeleteNetworkInterface"],"Resource":"*"},
    {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}
  ]
}
```

直读 S3 要求 Lambda 内存 ≥ 512 MB；托管策略 `AmazonS3FilesClientReadWriteAccess` 已包含 S3 Files 客户端权限。

</details>

## 核心概念

### Lambda S3 Files 集成关键参数

| 维度 | 说明 | 来源 |
|------|------|------|
| 底层 | 基于 Amazon EFS 构建 | 官方 What's New |
| VPC 要求 | **必须** Lambda 与 mount target 同 VPC、同 AZ | Lambda 开发者指南 |
| 协议 | NFS v4.1/4.2，端口 **TCP 2049** | S3 Files 先决条件 |
| 挂载路径 | 必须以 `/mnt/` 开头 | Lambda 控制台强制 |
| Access Point | 未选则 Lambda 自动创建（UID/GID=1000，根目录 `/lambda`，权限 755） | Lambda 文档 |
| 直读 S3 优化 | 仅在 Lambda 内存 **≥ 512 MB** 时启用 | Lambda 文档 |
| 并发挂载 | 多 Lambda 可同时挂载同一文件系统 | What's New |
| 费用 | **仅** 标准 Lambda + S3 + S3 Files 费用，无额外挂载费 | What's New |
| **不兼容** | ❌ 配置了 Capacity Provider（Lambda Managed Instances）的函数 | What's New + 本文实测 |

### 与传统 S3 API 模式的差异

| 方面 | 传统 `boto3.download_file` | S3 Files 挂载 |
|------|--------------------------|---------------|
| 代码 | 上传 → 下载 → open → read | `open("/mnt/shared/...")` |
| /tmp 限制 | 受 512 MB~10 GB 影响 | 不受限 |
| 函数间共享 | 需 S3 polling + 版本号自建同步 | 内核级共享，立即可见 |
| 内存占用 | 对象整份读入进程内存 | 按页懒加载 |
| 冷启动 | 不需 VPC，较快 | 需 VPC，但实测并不更慢（见下文） |
| 依赖 | boto3 / aws sdk | 无（纯 POSIX） |

## 动手实践

### Step 1: 搭建 VPC + S3 Gateway Endpoint

S3 Files 要求 Lambda 必须在 VPC 内。为了不走 NAT，我们加一个 S3 Gateway Endpoint，让直读 S3 走私网。

```bash
export AWS_REGION=us-east-1
export AWS_PROFILE=your-profile

# VPC + 两个子网（跨 AZ）
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.210.0.0/16 --region $AWS_REGION \
  --query "Vpc.VpcId" --output text)
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames --region $AWS_REGION
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support --region $AWS_REGION

SUBNET_A=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.210.1.0/24 \
  --availability-zone ${AWS_REGION}a --region $AWS_REGION \
  --query "Subnet.SubnetId" --output text)
SUBNET_B=$(aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 10.210.2.0/24 \
  --availability-zone ${AWS_REGION}b --region $AWS_REGION \
  --query "Subnet.SubnetId" --output text)

RT_ID=$(aws ec2 create-route-table --vpc-id $VPC_ID --region $AWS_REGION \
  --query "RouteTable.RouteTableId" --output text)
aws ec2 associate-route-table --route-table-id $RT_ID --subnet-id $SUBNET_A --region $AWS_REGION
aws ec2 associate-route-table --route-table-id $RT_ID --subnet-id $SUBNET_B --region $AWS_REGION

# S3 Gateway Endpoint（私网直读 S3，零费用）
aws ec2 create-vpc-endpoint --vpc-id $VPC_ID \
  --service-name com.amazonaws.${AWS_REGION}.s3 \
  --route-table-ids $RT_ID --region $AWS_REGION

# 两个安全组：Mount Target SG 只接受来自 Lambda SG 的 TCP 2049
SG_MT=$(aws ec2 create-security-group --group-name s3fs-mt-sg --description "MT NFS" \
  --vpc-id $VPC_ID --region $AWS_REGION --query "GroupId" --output text)
SG_LAMBDA=$(aws ec2 create-security-group --group-name s3fs-lambda-sg --description "Lambda" \
  --vpc-id $VPC_ID --region $AWS_REGION --query "GroupId" --output text)
aws ec2 authorize-security-group-ingress --group-id $SG_MT \
  --protocol tcp --port 2049 --source-group $SG_LAMBDA --region $AWS_REGION
```

!!! danger "安全红线"
    Mount Target SG 的 ingress **只允许** Lambda SG 作为源（使用 `--source-group`，不要用 `--cidr 0.0.0.0/0`）。NFS 流量在私网内即可。

### Step 2: 创建 S3 Bucket 和 S3 Files 文件系统

```bash
# S3 bucket（versioning + SSE-S3 是 S3 Files 的硬性要求）
BUCKET=lambda-s3-files-$(aws sts get-caller-identity --query Account --output text)-$(date +%s)
aws s3api create-bucket --bucket $BUCKET --region $AWS_REGION
aws s3api put-bucket-versioning --bucket $BUCKET --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket $BUCKET \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# S3 Files FS role（让 S3 Files 服务代表我们访问 bucket 并管 EventBridge 同步规则）
# ...（完整 role trust + inline policy 见资源清理章节末尾的参考脚本）

# 创建文件系统 + 两个 Mount Target（每 AZ 一个）
FS_ID=$(aws s3files create-file-system --bucket arn:aws:s3:::$BUCKET \
  --role-arn $ROLE_ARN --region $AWS_REGION --query "fileSystemId" --output text)

# 等文件系统 available 后再创建 mount target
MT_A=$(aws s3files create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET_A \
  --security-groups $SG_MT --region $AWS_REGION --query "mountTargetId" --output text)
MT_B=$(aws s3files create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET_B \
  --security-groups $SG_MT --region $AWS_REGION --query "mountTargetId" --output text)

# Lambda 专用 Access Point
AP_ID=$(aws s3files create-access-point --file-system-id $FS_ID \
  --posix-user uid=1000,gid=1000 \
  --root-directory "path=/lambda,creationPermissions={ownerUid=1000,ownerGid=1000,permissions=755}" \
  --region $AWS_REGION --query "accessPointId" --output text)
```

**实测耗时**：
- 文件系统 `creating → available`：约 15-30 秒
- Mount Target `creating → available`：约 **3-4 分钟**（每个）
- Access Point：约 15 秒

### Step 3: 部署对照组 Lambda（传统 S3 download/upload）

```python
# traditional_s3/lambda_function.py
import os, time, boto3, uuid
s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET"]
TMP = "/tmp"

def handler(event, context):
    size_bytes = int(event.get("size_bytes", 1024 * 1024))
    fname = f"traditional-{uuid.uuid4().hex}.bin"
    local_in  = f"{TMP}/in-{fname}"
    local_out = f"{TMP}/out-{fname}"

    t0 = time.time()
    with open(local_in, "wb") as f:
        f.write(os.urandom(size_bytes))            # 1) 本地生成
    t_gen = time.time()
    s3.upload_file(local_in, BUCKET, f"traditional/{fname}")  # 2) 上传
    t_up = time.time()
    s3.download_file(BUCKET, f"traditional/{fname}", local_out)  # 3) 下载
    t_down = time.time()
    with open(local_out, "rb") as f: data = f.read()             # 4) 处理
    t_proc = time.time()

    os.remove(local_in); os.remove(local_out)
    return {"mode": "traditional", "size_bytes": size_bytes,
            "timings_ms": {
                "generate": round((t_gen  - t0)*1000, 2),
                "upload":   round((t_up   - t_gen)*1000, 2),
                "download": round((t_down - t_up)*1000, 2),
                "process":  round((t_proc - t_down)*1000, 2),
                "total":    round((t_proc - t0)*1000, 2)}}
```

```bash
aws lambda create-function --function-name lambda-s3-trad \
  --runtime python3.12 --role $EXEC_ROLE --handler lambda_function.handler \
  --zip-file fileb://traditional_s3.zip \
  --timeout 60 --memory-size 1024 \
  --environment Variables={BUCKET=$BUCKET} --region $AWS_REGION
```

### Step 4: 部署 S3 Files 挂载 Lambda

```python
# s3files_mount/lambda_function.py
import os, time, uuid
MOUNT = os.environ.get("MOUNT_PATH", "/mnt/shared")

def handler(event, context):
    size_bytes = int(event.get("size_bytes", 1024 * 1024))
    fname = f"s3files-{uuid.uuid4().hex}.bin"
    path = f"{MOUNT}/{fname}"

    t0 = time.time()
    with open(path, "wb") as f:
        f.write(os.urandom(size_bytes))
        f.flush(); os.fsync(f.fileno())
    t_write = time.time()
    with open(path, "rb") as f: data = f.read()
    t_read = time.time()

    os.remove(path)
    return {"mode": "s3files_mount", "size_bytes": size_bytes,
            "timings_ms": {
                "write": round((t_write - t0)*1000, 2),
                "read":  round((t_read - t_write)*1000, 2),
                "total": round((t_read - t0)*1000, 2)}}
```

```bash
AP_ARN=$(aws s3files list-access-points --file-system-id $FS_ID \
  --query "accessPoints[0].accessPointArn" --output text --region $AWS_REGION)

aws lambda create-function --function-name lambda-s3-fs \
  --runtime python3.12 --role $EXEC_ROLE --handler lambda_function.handler \
  --zip-file fileb://s3files_mount.zip \
  --timeout 60 --memory-size 1024 \
  --vpc-config "SubnetIds=$SUBNET_A,$SUBNET_B,SecurityGroupIds=$SG_LAMBDA" \
  --file-system-configs "Arn=$AP_ARN,LocalMountPath=/mnt/shared" \
  --environment Variables={MOUNT_PATH=/mnt/shared} --region $AWS_REGION
```

关键点：`--file-system-configs` 的 `Arn` 是 **Access Point ARN**，不是文件系统 ARN。

!!! tip "Lambda 激活时间"
    带 VPC + S3 Files 的函数从 `Pending` 到 `Active` 实测约 **2-3 分钟**（需要 ENI 挂 ENA、挂载验证）。无 VPC 的函数通常 < 10 秒。

### Step 5: 对比实验（1 MB + 50 MB × cold/warm）

```bash
# 1 MB
aws lambda invoke --function-name lambda-s3-trad \
  --payload "$(echo '{"size_bytes": 1048576}' | base64 -w0)" out.json >/dev/null
cat out.json

aws lambda invoke --function-name lambda-s3-fs \
  --payload "$(echo '{"size_bytes": 1048576}' | base64 -w0)" out.json >/dev/null
cat out.json

# 50 MB
aws lambda invoke --function-name lambda-s3-trad \
  --payload "$(echo '{"size_bytes": 52428800}' | base64 -w0)" out.json >/dev/null
cat out.json

aws lambda invoke --function-name lambda-s3-fs \
  --payload "$(echo '{"size_bytes": 52428800}' | base64 -w0)" out.json >/dev/null
cat out.json
```

**实测输出**（1 MB 冷启动）：

```json
// Traditional
{"mode":"traditional","timings_ms":{"generate":4.97,"upload":152.94,"download":69.72,"process":0.34,"total":227.97}}
// S3 Files mount
{"mode":"s3files_mount","timings_ms":{"write":55.91,"read":5.02,"total":60.93}}
```

**实测输出**（50 MB 冷启动）：

```json
// Traditional
{"mode":"traditional","timings_ms":{"generate":476.83,"upload":1496.90,"download":582.31,"process":104.54,"total":2660.59}}
// S3 Files mount
{"mode":"s3files_mount","timings_ms":{"write":781.39,"read":498.41,"total":1279.80}}
```

### Step 6: 多 Lambda 共享同一 workspace（核心价值）

部署两个独立函数：`lambda-s3-writer` 和 `lambda-s3-reader`，都挂载 `/mnt/shared`。

```bash
KEY=shared-$(date +%s).txt
PAYLOAD="msg from writer UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Writer 写入
aws lambda invoke --function-name lambda-s3-writer \
  --payload "$(echo "{\"op\":\"write\",\"key\":\"$KEY\",\"payload\":\"$PAYLOAD\"}" | base64 -w0)" \
  out.json >/dev/null; cat out.json

# Reader 立即读同一 key（**另一个 Lambda 执行环境**）
aws lambda invoke --function-name lambda-s3-reader \
  --payload "$(echo "{\"op\":\"read\",\"key\":\"$KEY\"}" | base64 -w0)" \
  out.json >/dev/null; cat out.json
```

**实测输出**：

```json
// Writer
{"op":"write","path":"/mnt/shared/shared-1776883557.txt","elapsed_ms":14.02,"bytes":40}
// Reader (独立 Lambda 函数)
{"op":"read","path":"/mnt/shared/shared-1776883557.txt","elapsed_ms":8.32,"content":"msg from writer UTC=2026-04-22T18:45:57Z"}
```

**5 个并发 agent 写入**（模拟 durable functions orchestrator → agents 场景）：

```bash
seq 1 5 | xargs -P 5 -I {} aws lambda invoke --function-name lambda-s3-writer \
  --payload "$(echo '{"op":"write","key":"agent-{}.txt","payload":"agent {}"}' | base64 -w0)" \
  /tmp/agent-{}.json >/dev/null
```

所有 5 个并行写入均 < 15 ms，reader 立即 `list` 可见：

```json
{"op":"list","files":["agent-1.txt","agent-2.txt","agent-3.txt","agent-4.txt","agent-5.txt"],"count":5}
```

### Step 7: 边界测试 — Capacity Provider 不兼容

What's New 原文明确写"Lambda functions not configured with a capacity provider"。实测三个组合确认：

```bash
# 尝试 1: 给已有 S3 Files + VPC 函数加 Capacity Provider
aws lambda update-function-configuration --function-name lambda-s3-fs \
  --capacity-provider-config "LambdaManagedInstancesCapacityProviderConfig={CapacityProviderArn=...}"
# → InvalidParameterValueException: You can't use Lambda Managed Instance functions
#   with VPC configurations.

# 尝试 2: 创建 CP + S3 Files (无 VPC)
aws lambda create-function --capacity-provider-config ... --file-system-configs ...
# → InvalidParameterValueException: Function must be configured to execute in a VPC
#   to reference access point arn:aws:s3files:...

# 尝试 3: VPC + CP + S3 Files 三者同时
aws lambda create-function --vpc-config ... --capacity-provider-config ... --file-system-configs ...
# → InvalidParameterValueException: You can't use Lambda Managed Instance functions
#   with VPC configurations.
```

**传递结论**：S3 Files 必须 VPC，VPC 与 LMI 互斥 ⇒ **S3 Files 与 Capacity Provider 的组合在 API 级被拒绝**。

### Step 8: 冷启动分析（CloudWatch REPORT）

```bash
for F in lambda-s3-trad lambda-s3-fs; do
  echo "--- $F ---"
  aws logs filter-log-events --log-group-name /aws/lambda/$F \
    --filter-pattern "REPORT" --limit 5 \
    --query "events[*].message" --output text --region $AWS_REGION
done
```

关键字段（实测）：

```
lambda-s3-trad:  Duration 230.53 ms  Init Duration 592.50 ms  Max Memory 99 MB (1MB) / 282 MB (50MB)
lambda-s3-fs:    Duration 68.87 ms   Init Duration 112.32 ms  Max Memory 68 MB (1MB) / 207 MB (50MB)
```

## 测试结果

| # | 测试场景 | 模式 | Duration | Init | Max Memory | 结论 |
|---|---------|------|---------|------|-----------|------|
| 1 | 1 MB 读写 | 传统 S3 API | 228 ms cold / 160 ms warm | 592 ms | 99 MB | — |
| 2 | 1 MB 读写 | S3 Files mount | **61 ms cold / 57 ms warm** | **112 ms** | 68 MB | ✅ 快 **3.7×** |
| 3 | 50 MB 读写 | 传统 S3 API | 2661 ms / 2098 ms | — | 282 MB | — |
| 4 | 50 MB 读写 | S3 Files mount | **1280 ms / 1331 ms** | — | **207 MB** | ✅ 快 **2×**，内存 **-25%** |
| 5 | Writer→Reader 跨函数共享 | S3 Files mount | Write 14 ms / Read 8 ms | — | — | ✅ 无需同步逻辑 |
| 6 | 5 agent 并行写入同一 FS | S3 Files mount | 全部 < 15 ms | — | — | ✅ 并发读写稳定 |
| 7 | CP + S3 Files 组合 | 边界 | N/A | — | — | ✅ 预期报错（API 级拒绝） |
| 8 | 代码行数对比 | — | — | — | — | ✅ S3 Files 路径短 50%，无 boto3 依赖 |

## 踩坑记录

!!! warning "踩坑 1: Mount Target 创建慢（3-4 分钟）"
    `aws s3files create-mount-target` 返回立即 `status=creating`，进入 `available` 实测要 ~3-4 分钟。CloudFormation / Terraform 默认 timeout 可能不够，建议设 10 分钟。Lambda 在 MT 未 available 时引用会报 "File system not ready"。
    
    *已查文档确认*：这个延迟与 EFS Mount Target 的创建时间一致，符合底层架构。

!!! warning "踩坑 2: Access Point ARN 而非 FS ARN"
    `--file-system-configs` 的 `Arn` 字段必须填 **Access Point ARN**（格式 `arn:aws:s3files:region:account:file-system/fs-xxx/access-point/fsap-xxx`），而不是文件系统 ARN。错填后错误信息不够明确，容易误导。
    
    *实测发现，官方 CLI 文档可以更明确*。

!!! warning "踩坑 3: S3 Gateway Endpoint 是 S3 直读优化的关键"
    Lambda 内存 ≥ 512 MB 时，S3 Files 会为 ≥ 1 MB 的读取走 **S3 直读**（带宽更大）。若 VPC 子网没出公网路由（无 NAT 也无 S3 Gateway Endpoint），直读会失败并 fallback 到 mount target。为避免性能悬崖，**强烈推荐同时创建 S3 Gateway Endpoint**（免费，比 NAT 更便宜）。
    
    *已查文档确认*：Lambda 文档提到"Direct reads from Amazon S3 are supported only for functions configured with 512 MB or more of memory"。

!!! info "反直觉发现: S3 Files 冷启动更快"
    实测 `lambda-s3-trad`（无 VPC + boto3）Init Duration **592 ms**，`lambda-s3-fs`（VPC + NFS mount）Init Duration **112 ms**。原因：boto3 的 Python import + SDK 初始化比 Lambda Hyperplane ENI + NFS handshake 更重。这颠覆了"VPC Lambda 冷启动一定慢"的旧认知。
    
    *实测发现，官方未记录这个对比*。

!!! warning "踩坑 4: Capacity Provider 不兼容的三重传递链"
    What's New 只说"not configured with a capacity provider"，实测挖出完整原因链：
    1. S3 Files 需要 access point → 必须 VPC
    2. Lambda Managed Instances（CP）不兼容 VPC 配置
    3. 因此 CP + S3 Files 在 API 层被拒绝
    
    生产规划中，如果已在用 Lambda Managed Instances（例如长尾 Rust 工作负载），需要改用传统 Lambda 运行时才能用 S3 Files。
    
    *实测确认，与 What's New 公告一致*。

## 费用明细

| 资源 | 单价 | 实测用量 | 费用 |
|------|------|---------|------|
| Lambda 调用（~50 次） | $0.20/M req + ~$0.0000166/GB-s | ~60 GB-s | < $0.01 |
| S3 Files 高性能存储 | ~$0.06/GB-月 | 几百 MB 半小时 | < $0.01 |
| S3 标准存储 | $0.023/GB-月 | 测试数据 < 200 MB | < $0.01 |
| S3 GET / PUT | $0.0004/1K | 几百次 | < $0.01 |
| S3 Gateway Endpoint | 免费 | — | $0.00 |
| VPC / 子网 / SG | 免费 | — | $0.00 |
| CloudWatch Logs（摄入） | $0.50/GB | < 1 MB | < $0.01 |
| **合计** | | | **< $0.10** |

Lambda + S3 Files 本身 **没有额外挂载费**（What's New 明确声明）。

## 清理资源

!!! danger "务必清理"
    S3 Files 文件系统的高性能存储会按 GB-小时计费；VPC 资源 ENI 残留可能阻止删除 SG，务必按顺序清理。

```bash
export AWS_REGION=us-east-1

# 1. 删除 Lambda 函数（必须先于 Access Point / Mount Target）
for f in lambda-s3-trad lambda-s3-fs lambda-s3-writer lambda-s3-reader; do
  aws lambda delete-function --function-name $f --region $AWS_REGION
done

# 2. 删除 Capacity Provider（如果创建过）
aws lambda delete-capacity-provider --capacity-provider-name lambda-s3-files-cp --region $AWS_REGION

# 3. 删除 S3 Files Access Point、Mount Targets、FS
aws s3files delete-access-point --access-point-id $AP_ID --region $AWS_REGION
aws s3files delete-mount-target --mount-target-id $MT_A --region $AWS_REGION
aws s3files delete-mount-target --mount-target-id $MT_B --region $AWS_REGION
# 等 MT 完全删除（~2 分钟）
aws s3files delete-file-system --file-system-id $FS_ID --region $AWS_REGION

# 4. 删除 S3 对象和 Bucket（版本化 bucket 需要先清所有版本）
aws s3api delete-objects --bucket $BUCKET \
  --delete "$(aws s3api list-object-versions --bucket $BUCKET \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json)"
aws s3api delete-objects --bucket $BUCKET \
  --delete "$(aws s3api list-object-versions --bucket $BUCKET \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json)"
aws s3 rb s3://$BUCKET --force

# 5. 删除 CloudWatch Log Groups
for f in lambda-s3-trad lambda-s3-fs lambda-s3-writer lambda-s3-reader; do
  aws logs delete-log-group --log-group-name /aws/lambda/$f --region $AWS_REGION 2>/dev/null || true
done

# 6. IAM（先 delete-role-policy，再 detach-role-policy，再 delete-role）
for r in lambda-s3-files-s3fs-role lambda-s3-files-lambda-exec lambda-s3-files-cp-role; do
  for p in $(aws iam list-role-policies --role-name $r --query 'PolicyNames[]' --output text); do
    aws iam delete-role-policy --role-name $r --policy-name $p
  done
  for p in $(aws iam list-attached-role-policies --role-name $r --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name $r --policy-arn $p
  done
  aws iam delete-role --role-name $r
done

# 7. 删 VPC Endpoint → SG（**先检查 ENI 残留**）→ 子网 → 路由表 → VPC
aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $S3EP --region $AWS_REGION

# ⚠️ Lambda VPC ENI 和 S3 Files Mount Target ENI 可能需要几分钟才清完
for SG in $SG_MT $SG_LAMBDA; do
  while true; do
    COUNT=$(aws ec2 describe-network-interfaces \
      --filters Name=group-id,Values=$SG --query 'length(NetworkInterfaces)' --output text --region $AWS_REGION)
    echo "SG $SG residual ENIs: $COUNT"
    [ "$COUNT" = "0" ] && break
    sleep 20
  done
  aws ec2 delete-security-group --group-id $SG --region $AWS_REGION
done

aws ec2 delete-subnet --subnet-id $SUBNET_A --region $AWS_REGION
aws ec2 delete-subnet --subnet-id $SUBNET_B --region $AWS_REGION
aws ec2 delete-route-table --route-table-id $RT_ID --region $AWS_REGION
aws ec2 delete-vpc --vpc-id $VPC_ID --region $AWS_REGION
```

## 结论与建议

### 什么场景用 S3 Files 挂载

| 场景 | 推荐模式 | 理由 |
|------|---------|------|
| 单次简单的 S3 对象读写（1 MB 以内） | 传统 boto3 | 无 VPC 配置负担，冷启动路径更简单 |
| 中大文件处理（≥ 10 MB） | **S3 Files 挂载** | 内存占用低（流式 NFS）、执行更快 |
| AI agent 并行分析共享数据 | **S3 Files 挂载** | 无需自建同步，天然跨函数可见 |
| Durable Functions + 长流程 state | **S3 Files 挂载** | 官方推荐模式，orchestrator + agents 共享 workspace |
| 已用 Capacity Provider（LMI） | **不能混用** | API 级拒绝；要么 CP，要么 S3 Files |
| 对延迟极敏感、无共享需求、< 10MB | 传统 boto3 | 冷启动路径短 |

### 迁移建议

- ✅ **新项目**：直接用 S3 Files，代码更干净、内存更省、共享免费
- ⚠️ **有 `/tmp` 依赖的存量函数**：可以把 `/tmp/xxx` 换成 `/mnt/shared/xxx`，删除 download/upload 代码
- ❌ **已用 Capacity Provider 的函数**：必须权衡 LMI 的持久化实例价值 vs S3 Files 的共享能力，当前两者不能同时

### 生产注意事项

1. **安全组只允许来源 SG，绝不用 0.0.0.0/0** — NFS 在私网内跑
2. **Mount Target 必须每 AZ 一个**，且 Lambda 子网要覆盖这些 AZ
3. **S3 Gateway Endpoint 建议常备**，让 ≥ 1 MB 的直读走私网，避免 NAT 费用 & 性能悬崖
4. **Bucket 必须 versioning + SSE-S3/KMS**，这是 S3 Files 双向同步的硬性前提
5. **冷启动预期修正**：S3 Files Lambda 冷启动实测 ~110 ms，**不一定比无 VPC 函数慢**
6. **权限清单**：`s3files:ClientMount` + `s3files:ClientWrite` + `s3:GetObject*`（内存 ≥ 512 MB 直读时）+ `AWSLambdaVPCAccessExecutionRole`

## 参考链接

- [AWS What's New — Lambda 支持 S3 Files](https://aws.amazon.com/about-aws/whats-new/2026/04/aws-lambda-amazon-s3/)
- [Lambda 开发者指南 — Configuring S3 Files access](https://docs.aws.amazon.com/lambda/latest/dg/configuration-filesystem-s3files.html)
- [Amazon S3 Files 先决条件](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-prereq-policies.html)
- [Lambda Durable Functions](https://docs.aws.amazon.com/lambda/latest/dg/durable-functions.html)
- [AmazonS3FilesClientReadWriteAccess 托管策略](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonS3FilesClientReadWriteAccess.html)
