# Amazon ECR Archive 存储类：降低容器镜像存储成本的实战指南

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.50（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

容器镜像会随着 CI/CD 持续积累。很多团队为了合规或回滚需要保留历史镜像，但这些镜像大部分时间不会被拉取。之前只能选择：**保留（持续付标准存储费）**或**删除（丢失回滚能力）**。

2025 年 11 月，Amazon ECR 推出了 **Archive 存储类**，在"保留"和"删除"之间提供了第三个选项：**归档**。归档后的镜像不可拉取，但可以在约 10-20 分钟内恢复到可用状态。更重要的是，ECR Lifecycle Policy 新增了基于"最后拉取时间"的自动归档规则，实现了按使用模式自动优化存储成本。

## 前置条件

- AWS 账号（需要 `ecr:UpdateImageStorageClass`、`ecr:DescribeImages`、`ecr:PutLifecyclePolicy` 权限）
- AWS CLI v2 已配置
- Docker（推送测试镜像用）

## 核心概念

### 镜像生命周期的三个状态

| 状态 | 说明 | 可 Pull？ | 可归档？ | 可恢复？ |
|------|------|----------|---------|---------|
| **ACTIVE** | 标准状态，正常使用 | ✅ | ✅ | — |
| **ARCHIVED** | 归档状态，元数据可查，无法拉取 | ❌ | ❌（幂等报错） | ✅ |
| **ACTIVATING** | 恢复中，约 10-20 分钟 | ❌ | ❌ | — |

### Lifecycle Policy 新增功能

| 功能 | 说明 |
|------|------|
| `action.type: "transition"` | 新 action 类型，将镜像转移到 archive 存储 |
| `countType: "sinceImagePulled"` | 基于最后拉取时间（**只能配 transition**） |
| `countType: "sinceImageTransitioned"` | 基于归档时间（**只能配 expire + archive storageClass**） |
| `selection.storageClass` | 新字段，指定规则作用的存储类 |

### 关键限制

- 归档镜像**最少保留 90 天**才能通过 lifecycle policy 删除
- `sinceImagePulled` **不能**与 `expire` action 配合使用
- `describe-images` 默认只显示 ACTIVE 镜像，需加 `--filter '{"imageStatus":"ANY"}'` 查看所有状态

## 动手实践

### Step 1: 创建测试仓库并推送镜像

```bash
# 设置变量
REGION=us-east-1
REPO_NAME=ecr-archive-test
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region $REGION)
REPO_URI=$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME

# 创建仓库
aws ecr create-repository \
  --repository-name $REPO_NAME \
  --region $REGION

# 登录 ECR
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# 构建并推送 3 个测试镜像
for i in 1 2 3; do
  echo "FROM alpine:3.19
RUN echo \"version $i\" > /version.txt" > /tmp/Dockerfile.ecr-test
  docker build -f /tmp/Dockerfile.ecr-test -t $REPO_URI:v$i /tmp/
  docker push $REPO_URI:v$i
done
```

### Step 2: 手动归档镜像

```bash
# 归档 v1
aws ecr update-image-storage-class \
  --repository-name $REPO_NAME \
  --image-id imageTag=v1 \
  --target-storage-class ARCHIVE \
  --region $REGION
```

返回结果：
```json
{
    "imageStatus": "ARCHIVED"
}
```

归档是**即时**的 — API 返回时镜像已经处于 ARCHIVED 状态。

### Step 3: 验证归档效果

```bash
# 查看归档镜像元数据（需要 ANY filter 或指定 imageTag）
aws ecr describe-images \
  --repository-name $REPO_NAME \
  --filter '{"imageStatus":"ANY"}' \
  --region $REGION \
  --query 'imageDetails[?imageTags!=`null`].{Tag:imageTags[0],Status:imageStatus,ArchivedAt:lastArchivedAt}'
```

输出示例：
```json
[
    {"Tag": "v1", "Status": "ARCHIVED", "ArchivedAt": "2026-03-28T01:20:11.929Z"},
    {"Tag": "v2", "Status": "ACTIVE",   "ArchivedAt": null},
    {"Tag": "v3", "Status": "ACTIVE",   "ArchivedAt": null}
]
```

```bash
# 尝试 pull 归档镜像（预期失败）
docker pull $REPO_URI:v1
# Error: not found
```

!!! warning "注意"
    归档镜像 pull 返回的是通用的 `not found` 错误，而不是 "image is archived" 之类的明确提示。如果你的自动化脚本依赖错误信息做判断，需要先 `describe-images` 检查状态。（实测发现，官方未记录）

### Step 4: 恢复镜像

```bash
# 恢复归档的 v1
aws ecr update-image-storage-class \
  --repository-name $REPO_NAME \
  --image-id imageTag=v1 \
  --target-storage-class STANDARD \
  --region $REGION
```

返回 `imageStatus: "ACTIVATING"` — 恢复是**异步**的。

```bash
# 轮询等待恢复完成
while true; do
  STATUS=$(aws ecr describe-images \
    --repository-name $REPO_NAME \
    --image-ids imageTag=v1 \
    --region $REGION \
    --query 'imageDetails[0].imageStatus' --output text)
  echo "$(date -u +%H:%M:%S) - Status: $STATUS"
  [ "$STATUS" = "ACTIVE" ] && break
  sleep 30
done
```

### Step 5: 配置 Lifecycle Policy 自动归档

**场景：90 天未 pull 的镜像自动归档，归档 365 天后自动删除**

```bash
cat > /tmp/lifecycle-policy.json << 'EOF'
{
    "rules": [
        {
            "rulePriority": 1,
            "description": "Archive images not pulled in 90 days",
            "selection": {
                "tagStatus": "any",
                "countType": "sinceImagePulled",
                "countUnit": "days",
                "countNumber": 90
            },
            "action": {
                "type": "transition",
                "targetStorageClass": "archive"
            }
        },
        {
            "rulePriority": 2,
            "description": "Expire archived images after 365 days",
            "selection": {
                "tagStatus": "any",
                "storageClass": "archive",
                "countType": "sinceImageTransitioned",
                "countUnit": "days",
                "countNumber": 365
            },
            "action": {
                "type": "expire"
            }
        }
    ]
}
EOF

# 先预览（不会实际执行）
aws ecr start-lifecycle-policy-preview \
  --repository-name $REPO_NAME \
  --lifecycle-policy-text file:///tmp/lifecycle-policy.json \
  --region $REGION

# 查看预览结果
aws ecr get-lifecycle-policy-preview \
  --repository-name $REPO_NAME \
  --region $REGION
```

!!! tip "Lifecycle Policy 规则组合"
    归档和删除必须分成两条规则：

    - **Rule 1**（`sinceImagePulled` + `transition`）：基于拉取时间归档
    - **Rule 2**（`sinceImageTransitioned` + `expire`）：基于归档时间删除

    不能用 `sinceImagePulled` 直接删除镜像，必须先归档再删除。归档后最少保留 90 天。

## 测试结果

### 归档与恢复时间

| 操作 | 耗时 | 备注 |
|------|------|------|
| 归档（ACTIVE → ARCHIVED） | **即时** | API 返回时已完成 |
| 恢复（ARCHIVED → ACTIVE） | **~9 分 10 秒** | 测试镜像 ~3.4MB，远低于 20 分钟承诺 |

### 状态转换行为

| 操作 | ACTIVE 镜像 | ARCHIVED 镜像 | ACTIVATING 镜像 |
|------|------------|--------------|----------------|
| Pull | ✅ 正常 | ❌ not found | ❌ not found |
| 归档 | ✅ 即时 | ❌ 报错 | ❌ 报错 |
| 恢复 | — | ✅ 异步 | — |
| describe-images（默认） | ✅ 显示 | ❌ 不显示 | ❌ 不显示 |
| describe-images（ANY filter） | ✅ 显示 | ✅ 显示 | ✅ 显示 |

### Lifecycle Policy 规则验证

| 规则组合 | 结果 |
|---------|------|
| `sinceImagePushed` + `transition` | ✅ 有效 |
| `sinceImagePulled` + `transition` | ✅ 有效 |
| `sinceImagePulled` + `expire` | ❌ 报错：只能配 transition |
| `sinceImageTransitioned` + `expire` + `archive` | ✅ 有效 |
| 组合规则（archive + expire） | ✅ 有效 |

## 踩坑记录

!!! warning "踩坑 1: describe-images 默认不显示归档镜像"
    不加 `--filter '{"imageStatus":"ANY"}'` 的 `describe-images` 只返回 ACTIVE 镜像。如果你的脚本用 describe-images 做镜像盘点，归档后可能"看不见"这些镜像，以为它们被删了。（实测发现，官方未明确记录）

!!! warning "踩坑 2: Pull 归档镜像的错误信息不明确"
    归档镜像 pull 返回 `not found`，和镜像真的不存在是同一个错误。建议在 CI/CD 中加一层 `describe-images` 检查。（实测发现，官方未记录）

!!! warning "踩坑 3: 重复归档不是幂等的"
    对已经 ARCHIVED 的镜像再次调用 `update-image-storage-class ARCHIVE` 会报 `ImageStorageClassUpdateNotSupportedException`。自动化脚本需要先检查状态。（已查文档确认）

!!! warning "踩坑 4: ACTIVATING 状态全锁定"
    恢复过程中（ACTIVATING），镜像既不能 pull，也不能重新归档。需要等恢复完成才能操作。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ECR 标准存储 | $0.10/GB/月 | ~17MB × 数小时 | < $0.01 |
| ECR 归档存储 | 低于标准存储（具体费率见定价页） | ~3.4MB × 数小时 | < $0.01 |
| 数据传输（同 Region） | $0 | — | $0 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 先恢复所有归档镜像（不能直接删除归档镜像所在的仓库）
# 查看所有镜像状态
aws ecr describe-images \
  --repository-name $REPO_NAME \
  --filter '{"imageStatus":"ANY"}' \
  --region $REGION \
  --query 'imageDetails[?imageStatus!=`ACTIVE`].{Tag:imageTags[0],Digest:imageDigest,Status:imageStatus}'

# 如有 ARCHIVED 镜像，先恢复
aws ecr update-image-storage-class \
  --repository-name $REPO_NAME \
  --image-id imageTag=v3 \
  --target-storage-class STANDARD \
  --region $REGION

# 等待恢复完成...

# 2. 删除仓库（--force 删除所有镜像）
aws ecr delete-repository \
  --repository-name $REPO_NAME \
  --force \
  --region $REGION
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。归档镜像虽然费率低，但长期积累仍会产生费用。

## 结论与建议

### 适合场景

- **合规保留**：需要保留历史镜像但不常拉取（如审计要求保留 1-3 年）
- **CI/CD 镜像积累**：大量构建产物持续占用存储
- **灾备镜像**：老版本镜像作为回滚储备，但概率很低会用到

### 生产环境建议

1. **优先使用 Lifecycle Policy 自动归档** — 基于 `sinceImagePulled` 的规则最实用，按实际使用模式自动归档
2. **设计两段式生命周期** — 不常用 → 归档（Rule 1）→ 过期删除（Rule 2），归档最少 90 天
3. **更新监控脚本** — 加 `--filter '{"imageStatus":"ANY"}'` 确保能看到所有镜像
4. **CI/CD 适配** — Pull 前检查 imageStatus，避免 "not found" 误判
5. **恢复时间预期** — 小镜像 ~10 分钟，大镜像可能更长，做容量规划时预留 20 分钟窗口

### 与 S3 Glacier 的对比

ECR Archive 的定位类似 S3 的 Glacier 层 — 不常访问的数据以更低成本保留。但 ECR Archive 恢复时间（~10-20 分钟）远快于 S3 Glacier（数小时），更适合需要快速回滚的容器场景。

## 参考链接

- [Amazon ECR Lifecycle Policies 文档](https://docs.aws.amazon.com/AmazonECR/latest/userguide/LifecyclePolicies.html)
- [Lifecycle Policy Examples](https://docs.aws.amazon.com/AmazonECR/latest/userguide/lifecycle_policy_examples.html)
- [Lifecycle Policy Parameters](https://docs.aws.amazon.com/AmazonECR/latest/userguide/lifecycle_policy_parameters.html)
- [ECR Pricing](https://aws.amazon.com/ecr/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-ecr-archive-storage-class-container-images/)
