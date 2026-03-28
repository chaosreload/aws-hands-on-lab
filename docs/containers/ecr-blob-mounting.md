# Amazon ECR Blob Mounting 实战：跨 Repository 共享 Image Layer 优化存储与推送性能

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.05（含清理）
    - **Region**: us-east-1（所有 Commercial + GovCloud Regions 可用）
    - **最后验证**: 2026-03-28

## 背景

在微服务架构中，多个服务往往共享相同的 base image（如 `python:3.12`、`node:20-alpine`）。传统方式下，每个 ECR Repository 独立存储所有 layer，即使 layer 内容完全相同。这意味着：

- **存储浪费**：10 个微服务共用 200MB 的 base image = 2GB 重复存储
- **推送冗余**：同一 layer 被反复上传到不同 Repository

2026 年 1 月，Amazon ECR 推出了 **Blob Mounting** 功能，允许同一 Registry 内的 Repository 共享相同的 image layer。启用后，push 操作会自动检测已有 layer 并直接引用（mount），而非重新上传。

## 前置条件

- AWS 账号（需要 `ecr:PutAccountSetting`、`ecr:GetAccountSetting`、`ecr:CreateRepository`、`ecr:GetDownloadUrlForLayer` 等 ECR 权限）
- AWS CLI v2 已配置
- Docker CLI 已安装（支持 OCI 标准的容器工具均可）

## 核心概念

### Blob Mounting 工作原理

```
传统 Push（DISABLED）：
┌─────────┐    upload    ┌──────────┐
│ Docker   │ ──────────> │ repo-a   │  layer A (200MB)
│ Client   │    upload    ├──────────┤
│          │ ──────────> │ repo-b   │  layer A (200MB) ← 重复！
└─────────┘              └──────────┘  总存储：400MB

Blob Mounting（ENABLED）：
┌─────────┐    upload    ┌──────────┐
│ Docker   │ ──────────> │ repo-a   │  layer A (200MB)
│ Client   │    mount     ├──────────┤
│          │ ──────────> │ repo-b   │  → 引用 repo-a 的 layer A
└─────────┘              └──────────┘  总存储：200MB ✅
```

### 关键限制

| 条件 | 说明 |
|------|------|
| 作用范围 | 仅同一 Registry（同 Account + 同 Region） |
| 加密要求 | Repository 必须使用**相同**加密类型和 KMS Key |
| Pull Through Cache | 不支持通过 Pull Through Cache 创建的镜像 |
| 禁用影响 | 禁用后已 mount 的 layer 继续有效，不回滚 |
| IAM 权限 | 需要 `ecr:GetDownloadUrlForLayer` 权限才能从其他 repo mount layer |
| 客户端行为 | OCI 兼容客户端（Docker 等）自动检测并请求 mount，无需额外配置 |

## 动手实践

### Step 1: 查看当前状态

```bash
# 查看 blob mounting 当前配置
aws ecr get-account-setting \
  --name BLOB_MOUNTING \
  --region us-east-1
```

输出（默认禁用）：
```json
{
    "name": "BLOB_MOUNTING",
    "value": "DISABLED"
}
```

### Step 2: 创建测试 Repository

```bash
# 创建两个使用默认加密(AES256)的 repo
for repo in blob-test-a blob-test-b; do
  aws ecr create-repository \
    --repository-name $repo \
    --region us-east-1
done
```

### Step 3: 准备测试镜像

```bash
# 创建一个包含 ~10MB 自定义层的测试镜像
cat > /tmp/Dockerfile.blob-test << 'EOF'
FROM alpine:3.19
RUN echo "blob-mounting-test" > /test-data.txt && \
    dd if=/dev/urandom of=/padding.bin bs=1M count=10 2>/dev/null
EOF

docker build -t blob-test-image:v1 -f /tmp/Dockerfile.blob-test /tmp/

# 登录 ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
```

### Step 4: 对照实验 — 禁用状态下 Push

```bash
ECR_URI=<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

# Push 到 repo-a
docker tag blob-test-image:v1 $ECR_URI/blob-test-a:v1
time docker push $ECR_URI/blob-test-a:v1

# Push 相同镜像到 repo-b（layer 完全相同）
docker tag blob-test-image:v1 $ECR_URI/blob-test-b:v1
time docker push $ECR_URI/blob-test-b:v1
```

观察输出 — 两个 repo 的所有 layer 都显示 `Pushed`，表示各自独立上传：

```
57ea160e8fdb: Pushed
e232af9fa757: Pushed
17a39c0ba978: Pushed
```

### Step 5: 启用 Blob Mounting

```bash
# 启用 blob mounting
aws ecr put-account-setting \
  --name BLOB_MOUNTING \
  --value ENABLED \
  --region us-east-1

# 验证
aws ecr get-account-setting \
  --name BLOB_MOUNTING \
  --region us-east-1
```

### Step 6: 实验组 — 启用状态下 Push

```bash
# 创建新 repo 用于启用后的测试
for repo in blob-test-c blob-test-d; do
  aws ecr create-repository \
    --repository-name $repo \
    --region us-east-1
done

# Push 到 repo-c
docker tag blob-test-image:v1 $ECR_URI/blob-test-c:v1
time docker push $ECR_URI/blob-test-c:v1

# Push 到 repo-d（关键：观察是否 mount）
docker tag blob-test-image:v1 $ECR_URI/blob-test-d:v1
time docker push $ECR_URI/blob-test-d:v1
```

观察 repo-d 的输出 — layer 显示 `Mounted from` 而非 `Pushed`：

```
57ea160e8fdb: Mounted from blob-test-c
e232af9fa757: Mounted from blob-test-c
17a39c0ba978: Pushed
```

**这就是 blob mounting 的核心效果**：已存在于 Registry 中的 layer 被直接引用，只有不存在的 layer（如 attestation manifest）需要上传。

### Step 7: 边界测试 — 不同加密类型

```bash
# 创建 KMS 加密的 repo
aws ecr create-repository \
  --repository-name blob-test-e \
  --encryption-configuration encryptionType=KMS \
  --region us-east-1

# Push 相同镜像
docker tag blob-test-image:v1 $ECR_URI/blob-test-e:v1
time docker push $ECR_URI/blob-test-e:v1
```

输出全部为 `Pushed`（无 `Mounted from`），**验证了加密类型不同时 layer 无法共享**。

## 测试结果

| Repository | Blob Mounting | 加密类型 | Push 耗时 | Mount 层数 | 行为 |
|-----------|--------------|---------|----------|-----------|------|
| blob-test-a | DISABLED | AES256 | 5.756s | 0/3 | 全部上传 |
| blob-test-b | DISABLED | AES256 | 5.788s | 0/3 | 全部上传 |
| blob-test-c | ENABLED | AES256 | 5.006s | 2/3 | 2 层 Mounted |
| blob-test-d | ENABLED | AES256 | 4.912s | 2/3 | 2 层 Mounted |
| blob-test-e | ENABLED | KMS | 5.796s | 0/3 | 全部上传（加密不同） |
| blob-test-f | DISABLED (恢复) | AES256 | 5.782s | 0/3 | 全部上传 |

**关键发现**：

1. **Blob mounting 立即生效**：启用后 Docker push 自动识别已有 layer 并 mount
2. **Push 耗时改善约 15%**：测试 image 较小（~13MB），实际生产中共享数百 MB base image 的改善会更显著
3. **加密类型是硬限制**：AES256 与 KMS 加密的 repo 之间**无法**共享 layer
4. **禁用安全无损**：禁用后已 mount 的 image 继续正常工作，新 push 恢复独立存储

## 踩坑记录

!!! warning "describe-images 不反映存储共享"
    `aws ecr describe-images` 返回的 `imageSizeInBytes` 是镜像的**逻辑大小**，无论 layer 是独立存储还是 mounted，显示值相同。要观察实际存储节省，需要查看 CloudWatch metrics 或 Cost Explorer。（⚠️ 实测发现，官方未明确记录）

!!! tip "OCI 客户端自动处理"
    用户不需要修改 Docker 命令或配置。OCI 兼容的容器客户端在检测到 blob 可能已存在时，会自动在 POST 请求中附加 mounting 参数。ECR 在收到带 mounting 参数的请求时执行 mount。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ECR 存储 | $0.10/GB/月 | ~0.1GB × 分钟级 | < $0.01 |
| ECR API 调用 | 包含在存储费中 | ~50 次 | $0.00 |
| **合计** | | | **< $0.05** |

启用 blob mounting **本身不产生额外费用**，反而通过减少重复存储降低成本。

## 清理资源

```bash
# 删除所有测试 Repository（--force 会一并删除其中的镜像）
for repo in blob-test-a blob-test-b blob-test-c blob-test-d blob-test-e blob-test-f; do
  aws ecr delete-repository \
    --repository-name $repo \
    --force \
    --region us-east-1
done

# 恢复 blob mounting 为禁用状态（按需）
aws ecr put-account-setting \
  --name BLOB_MOUNTING \
  --value DISABLED \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外的 ECR 存储费用。

## 结论与建议

### 适用场景

- **微服务架构**：多个服务共享 base image（Python/Node.js/Java runtime），启用后显著减少存储
- **CI/CD 流水线**：频繁构建和推送镜像到多个 repo，mount 减少上传时间
- **Mono-repo 多 image**：同一代码仓库产出多个镜像共享公共 layer

### 生产环境建议

1. **统一加密策略**：确保需要共享 layer 的 repo 使用相同加密类型和 KMS Key
2. **一键启用**：`put-account-setting` 是 Registry 级别的开关，启用后对所有 repo 生效，无需逐个配置
3. **IAM 权限检查**：确保 push 用户对源 repo 有 `ecr:GetDownloadUrlForLayer` 权限
4. **可安全回退**：禁用不影响已有镜像，随时可以关闭

### 存储成本估算

假设 10 个微服务共享 500MB base image：

- **禁用**：500MB × 10 = 5GB → $0.50/月
- **启用**：500MB × 1 + 10 × 增量层 ≈ 1GB → $0.10/月
- **节省**：约 **80%** 存储成本

## 参考链接

- [Blob mounting in Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/blob-mounting.html)
- [Private registry settings in Amazon ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry-settings.html)
- [PutAccountSetting API Reference](https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_PutAccountSetting.html)
- [AWS What's New - ECR Cross-Repository Layer Sharing](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-ecr-cross-repository-layer-sharing/)
