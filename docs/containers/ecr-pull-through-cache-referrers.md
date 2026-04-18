# Amazon ECR Pull Through Cache Referrer 自动同步实测：让 cosign 在缓存镜像上直接验签

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.01（ECR 存储 KB 级）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-18

## 背景

2026 年 4 月 17 日，Amazon ECR 为 Pull Through Cache（PTC）新增了一个看似低调但意义重大的能力：**自动发现并同步上游仓库的 OCI Referrer**（镜像签名、SBOM、attestation）。

之前的坑是这样：你的 EKS 集群用 PTC 从 `public.ecr.aws` 或 `ghcr.io` 拉镜像，签名验证策略（比如 Kyverno、Sigstore Policy Controller）想用 OCI 1.1 的 Referrers API 查签名 → ECR 只看本地 repo，返回空 → 验证失败。你必须写一个 workaround：先连 upstream 手动拉签名，再 push 到 ECR，或者绕开 PTC 直接连上游。

现在 ECR 让这一步 "消失" 了。用户侧只需要调 `ListImageReferrers`，ECR 的 service-linked role 会帮你去 upstream 把 referrer 拉回来缓存到私有 repo。**从此 cosign verify 可以直接指向 ECR 私有 URL，端到端签名验证走通。**

本文用 cosign + OCI Public 实测这个能力，覆盖核心功能、边界条件、CloudTrail 审计链，以及与非 PTC repo 的对比。

## 前置条件

- AWS 账号（需要 `ecr:*`、`ecr-public:*`、`cloudtrail:LookupEvents`）
- AWS CLI v2 ≥ 2.34（含 `ecr list-image-referrers` 命令）
- Docker ≥ 20
- **cosign v2.x**（v3 CLI 变化较大，本文用 v2.4.1）
- oras（可选，用于验证 upstream referrer）

## 核心概念

| 项 | 说明 |
|---|---|
| OCI Referrer | OCI Distribution Spec 1.1 引入的机制，用 `subject` 字段把一个 artifact（签名/SBOM/attestation）挂到另一个 artifact（subject image）上 |
| Referrers API | Registry 提供 `GET /v2/{name}/referrers/{digest}` 端点，列出所有以该 digest 为 subject 的 artifact |
| ECR `ListImageReferrers` API | ECR 的等价物；同时也是触发 PTC 自动同步的唯一入口 |
| 同步模型 | **Lazy pull on API call**：`docker pull` 不同步，`ListImageReferrers` 才触发 |
| 缓存窗口 | Referrer 6 小时（镜像本身是 24 小时） |
| IAM 权限 | `ecr:BatchGetImage`（**不是新 action**） |
| 支持 upstream | 所有 PTC upstream：ECR Public / K8s / Quay / Docker Hub / Azure CR / GitHub CR / GitLab CR / Chainguard / ECR |
| Region | 除 China + GovCloud 外，所有支持 ECR PTC 的 Region |
| 新异常 | `UnableToListUpstreamImageReferrersException`（主要 Secrets Manager 凭据问题） |

关键差异表：

| 场景 | PTC repo ListImageReferrers | 普通 private repo ListImageReferrers |
|---|---|---|
| 有 upstream referrer | ✅ 返回（首次拉取，后续走 6h 缓存） | ❌ 返回空（只查本地） |
| 无 upstream referrer | ✅ 返回空，不报错 | ✅ 返回空 |
| IAM action | `ecr:BatchGetImage` | `ecr:BatchGetImage` |

## 动手实践

### Step 1: 创建 ECR Public PTC 规则

```bash
REGION=us-east-1
PROFILE=weichaol-testenv2-awswhatsnewtest
ACCOUNT=595842667825

aws ecr create-pull-through-cache-rule \
  --ecr-repository-prefix ecr-public \
  --upstream-registry-url public.ecr.aws \
  --upstream-registry ecr-public \
  --region $REGION --profile $PROFILE
```

输出：

```json
{
    "ecrRepositoryPrefix": "ecr-public",
    "upstreamRegistryUrl": "public.ecr.aws",
    "createdAt": "2026-04-18T03:56:23.728Z",
    "registryId": "595842667825"
}
```

ECR Public 不需要凭据，这是最简单的 upstream。

### Step 2: 在 upstream 准备一个带 cosign 签名的镜像

为了确定性地验证功能（而不是依赖某个第三方镜像碰巧有签名），我们自己做一个 upstream 镜像。

**2.1 创建 ECR Public repo 并 push 一个 alpine 镜像**：

```bash
# 创建 ECR Public repo（获得 public.ecr.aws/<alias>/<repo> URI）
aws ecr-public create-repository \
  --repository-name archie-referrer-test \
  --region us-east-1 --profile $PROFILE
# → uri: public.ecr.aws/l1j0r8q7/archie-referrer-test（你的 alias 会不同）

UPSTREAM=public.ecr.aws/l1j0r8q7/archie-referrer-test

# 构建一个简单镜像
cat > Dockerfile <<'EOF'
FROM public.ecr.aws/docker/library/alpine:3.19
LABEL test=ecr-ptc-referrers
EOF
docker build -t $UPSTREAM:v1 .

# Push 到 ECR Public
aws ecr-public get-login-password --region us-east-1 --profile $PROFILE | \
  docker login --username AWS --password-stdin public.ecr.aws
docker push $UPSTREAM:v1
# → Digest: sha256:dcda66a37dd711038d97a31244940909aa4946fe4bda4a528f8f77034f73415b
```

**2.2 用 cosign 签名（OCI 1.1 referrer 模式）**：

```bash
export COSIGN_PASSWORD=""
cosign generate-key-pair  # 生成 cosign.key + cosign.pub

export COSIGN_EXPERIMENTAL=1  # cosign v2 的 OCI 1.1 模式仍在 experimental
DIGEST=sha256:dcda66a37dd711038d97a31244940909aa4946fe4bda4a528f8f77034f73415b

cosign sign \
  --key cosign.key \
  --tlog-upload=false \
  --yes \
  --registry-referrers-mode oci-1-1 \
  $UPSTREAM@$DIGEST
```

输出关键行：

```
Pushing signature to: public.ecr.aws/l1j0r8q7/archie-referrer-test
Uploading signature for [...@sha256:dcda66a3...] to [...@sha256:3bc7fe5f...]
  with config.mediaType [application/vnd.dev.cosign.artifact.sig.v1+json]
```

签名被作为一个独立 manifest（digest `sha256:3bc7fe5f...`）推送，并通过 `subject` 字段指向 `sha256:dcda66a3...`。

**2.3 确认 upstream 确实有这个 referrer**：

```bash
oras discover $UPSTREAM@$DIGEST
```

```
public.ecr.aws/l1j0r8q7/archie-referrer-test@sha256:dcda66a3...
└── application/vnd.dev.cosign.artifact.sig.v1+json
    └── sha256:3bc7fe5f...
```

### Step 3: 通过 PTC 拉镜像（观察 referrer 是否随 pull 同步）

```bash
PRIVATE=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/ecr-public/l1j0r8q7/archie-referrer-test

# 登录私有 ECR
aws ecr get-login-password --region $REGION --profile $PROFILE | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# 第一次 pull，触发 PTC 创建 repo + 缓存镜像
docker pull $PRIVATE:v1
```

拉取成功后查看私有 repo 的 images：

```bash
aws ecr describe-images \
  --repository-name ecr-public/l1j0r8q7/archie-referrer-test \
  --region $REGION --profile $PROFILE
```

结果只有 3 个 image 对象（index + 2 个 child manifest），**没有 referrer 被下载**：

```json
{
  "imageDetails": [
    {"imageDigest": "sha256:52163a1aaf8978...", "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"},
    {"imageDigest": "sha256:59a3cff5c2f4ff...", "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"},
    {"imageDigest": "sha256:dcda66a37dd711...", "imageTags": ["v1"], "imageManifestMediaType": "application/vnd.oci.image.index.v1+json"}
  ]
}
```

**这是预期行为**：docker pull 不触发 referrer 同步。官方文档明确：同步的触发点是 `ListImageReferrers` API 调用。

### Step 4: 核心验证 — 首次 ListImageReferrers 触发自动同步

```bash
aws ecr list-image-referrers \
  --repository-name ecr-public/l1j0r8q7/archie-referrer-test \
  --subject-id imageDigest=$DIGEST \
  --region $REGION --profile $PROFILE
```

**第一次调用**就返回了上游的签名：

```json
{
  "referrers": [
    {
      "digest": "sha256:3bc7fe5fb93a2fd5d3ded192dd009758786382d464ff2a669f2d39d22de9f052",
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "artifactType": "application/vnd.dev.cosign.artifact.sig.v1+json",
      "size": 725
    }
  ]
}
```

Digest `sha256:3bc7fe5f...` 与 Step 2.2 上游签名完全一致。**ECR 在这次 API 调用时，用 service-linked role 到 public.ecr.aws 查了 referrer，拉下来，缓存到私有 repo，并返回给你。**

### Step 5: cosign verify 直接指向 ECR 私有 URL

这是读者真正关心的场景：部署时用 ECR 私有 URL，能不能直接验签？

```bash
# cosign v2 的 OCI 1.1 模式需要显式开启
export COSIGN_EXPERIMENTAL=1

cosign verify \
  --key cosign.pub \
  --insecure-ignore-tlog=true \
  --experimental-oci11 \
  $PRIVATE@$DIGEST
```

输出：

```
Verification for 595842667825.dkr.ecr.us-east-1.amazonaws.com/ecr-public/l1j0r8q7/archie-referrer-test@sha256:dcda66a3... --
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - The signatures were verified against the specified public key

[{"critical":{"identity":{"docker-reference":"public.ecr.aws/l1j0r8q7/archie-referrer-test"},
  "image":{"docker-manifest-digest":"sha256:dcda66a3..."},
  "type":"cosign container image signature"},"optional":null}]
```

**验证通过。** 注意 `docker-reference` 字段仍然是 upstream 的 URL（签名创建时记录的），这符合 cosign 的设计 — 它只比对 digest，不强制 URL 一致。

### Step 6: 边界测试 — upstream 没有 referrer 的镜像

```bash
# 通过 PTC 拉一个完全没签名的镜像（hello-world）
docker pull $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/ecr-public/docker/library/hello-world:latest

HELLO_DIGEST=$(aws ecr describe-images \
  --repository-name ecr-public/docker/library/hello-world \
  --query 'imageDetails[?imageManifestMediaType==`application/vnd.oci.image.index.v1+json`]|[0].imageDigest' \
  --output text --region $REGION --profile $PROFILE)

aws ecr list-image-referrers \
  --repository-name ecr-public/docker/library/hello-world \
  --subject-id imageDigest=$HELLO_DIGEST \
  --region $REGION --profile $PROFILE
```

输出：

```json
{ "referrers": [] }
```

**无 referrer 的镜像返回空数组，不报错**。这保证了调用方可以无脑调 API，不用先判断镜像是否有签名。

### Step 7: 对比 — 同 digest 在非 PTC repo 上的行为

创建一个普通 private repo，直接 push 同一个镜像：

```bash
aws ecr create-repository --repository-name archie-plain-referrer-test \
  --region $REGION --profile $PROFILE

docker tag $UPSTREAM:v1 $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/archie-plain-referrer-test:v1
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/archie-plain-referrer-test:v1
# 同 digest: sha256:dcda66a37dd711...

aws ecr list-image-referrers \
  --repository-name archie-plain-referrer-test \
  --subject-id imageDigest=$DIGEST \
  --region $REGION --profile $PROFILE
```

输出：

```json
{ "referrers": [] }
```

**同样的 digest，非 PTC repo 不会去 upstream 查，返回空。** 这个对比直接证实了 "PTC + ListImageReferrers" 是新能力的触发条件。

### Step 8: 从 CloudTrail 看自动同步的服务内部调用链

```bash
START=$(date -u -d "15 minutes ago" +"%Y-%m-%dT%H:%M:%SZ")
END=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

aws cloudtrail lookup-events \
  --start-time "$START" --end-time "$END" \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ListImageReferrers \
  --region $REGION --profile $PROFILE
```

在事件里你会看到两类 `ListImageReferrers`：

**① 用户发起**（accessKey 是 AKIA...，userAgent 是 aws-cli）：
```
eventName: ListImageReferrers
userIdentity.type: IAMUser
userIdentity.userName: awswhatsnewtest
userAgent: aws-cli/2.34.29 ...
sourceIPAddress: <your-IP>
```

**② ECR 服务内部发起**（accessKey 是 ASIA... 临时会话，关键字段全指向 ECR 自己）：
```
eventName: ListImageReferrers
userIdentity.invokedBy: ecr.amazonaws.com
userIdentity.sessionContext: { creationDate, mfaAuthenticated: false }
userAgent: ecr.amazonaws.com
sourceIPAddress: ecr.amazonaws.com
```

在同一时间窗口，你还会看到 ECR 服务自动发起的 `BatchGetImage`、`GetDownloadUrlForLayer`、`PutImage`（把 referrer manifest 写入私有 repo）— 它们都带 `invokedBy: ecr.amazonaws.com`。

**这是 PTC 自动同步行为的完整审计链**，对合规场景非常有价值。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---|---|---|---|
| T1 | 创建 ECR Public PTC rule | ✅ 成功 | `ecrRepositoryPrefix=ecr-public` | |
| T2 | docker pull 不同步 referrer | ✅ 符合预期 | describe-images 无 referrer | API 触发模型 |
| T3 | 首次 list-image-referrers 同步 cosign 签名 | ✅ | digest `sha256:3bc7...`，size 725 B | 核心功能 |
| T4 | cosign verify 指向 ECR 私有 URL | ✅ 通过 | 需要 `--experimental-oci11` | 端到端验证 |
| T5 | 二次 list-image-referrers 走缓存 | ✅ | 返回一致，耗时 ~1.6s | 6h 缓存窗口 |
| T6 | upstream 无 referrer 的镜像 | ✅ 空数组不报错 | `{"referrers": []}` | 优雅降级 |
| T7 | 非 PTC repo 同 digest | ✅ 空数组 | 与 PTC repo 形成对比 | 功能边界清晰 |
| T8 | CloudTrail 审计链 | ✅ | 2 类事件（用户 + ECR 服务） | `invokedBy=ecr.amazonaws.com`（实测发现，官方未明示） |

## 踩坑记录

!!! warning "踩坑 1：docker pull 不触发 referrer 同步"

    直觉上以为 "pull 镜像 = 同步完整供应链 artifact"，实际不是。Referrer 只有在 `ListImageReferrers` API 被调用时才同步。
    
    **对读者的影响**：如果你只用 docker pull + 运行镜像，signature/SBOM 不会自动出现在私有 repo（因此也不占存储）。但如果你用 Kyverno / Sigstore Policy Controller / Admission Webhook 验证签名，它们会调 Referrers API，这时同步自动发生。
    
    → 已查文档确认。官方 `pull-through-cache.html`：*"Calling the `ListImageReferrers` API to a pull through cache created repository returns the OCI-compliant referrer artifacts to the private cache."*

!!! warning "踩坑 2：cosign v3 的 `--tlog-upload=false` 不能单独用"

    cosign v3 把 transparency log 配置移到了 `--signing-config` 文件里，直接用 `--tlog-upload=false` 会报错：
    ```
    Error: --tlog-upload=false is not supported with --signing-config
    ```
    → 实测发现，cosign 工具链变化，与 ECR 无关。
    
    **解决**：离线签名（不打 Rekor）的最简办法是用 cosign v2.4.1；或者给 v3 提供自定义 `signing-config`。

!!! warning "踩坑 3：cosign verify 默认走 legacy `.sig` tag，访问 ECR 时 404"

    不加 `--experimental-oci11` 时，cosign 会尝试访问 `sha256-<digest>.sig` 这种老格式 tag：
    ```
    Error: GET .../v2/.../manifests/sha256-dcda...sig: DENIED / MANIFEST_UNKNOWN
    ```
    ECR PTC 同步的是 OCI 1.1 referrer（独立 manifest + subject 字段），不是 `.sig` tag。
    
    **解决**：`export COSIGN_EXPERIMENTAL=1 && cosign verify --experimental-oci11 ...`
    
    → 实测发现，cosign 工具默认行为问题，与 ECR 无关。

!!! info "观察 1：Referrer 同步延迟可忽略"

    实测首次 `list-image-referrers` 返回耗时 ~1 秒（包含 ECR 从 upstream 拉 referrer manifest），二次调用在 6h 缓存窗口内，差异不明显，因为 cosign bundle 只有几百字节。对于大型 SBOM（可能 MB 级），首次延迟会更显著 — 建议在生产场景预热（pre-fetch）关键镜像的 referrer。
    
    → 实测数据，官方未记录性能基线。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|---|---|---|---|
| ECR 私有存储 | $0.10/GB/月 | ~10 MB（3 repos） | < $0.001/月 |
| ECR Public 存储 | 免费（50GB/月配额） | <10 MB | $0 |
| ECR API 调用 | 前 100 万免费 | ~80 次 | $0 |
| 数据传输（ECR Public → ECR 私有，同 Region） | 免费 | <50 MB | $0 |
| **合计** | | | **< $0.01** |

Referrer artifact 本身（cosign bundle ~1KB，SLSA provenance ~1KB，SBOM 通常几十 KB 到 MB）按 ECR 存储正常计费，无额外费用类别。

## 清理资源

```bash
REGION=us-east-1
PROFILE=weichaol-testenv2-awswhatsnewtest

# 1. 删除 PTC rule（rule 删除不会清掉已缓存的 repo，要单独删）
aws ecr delete-pull-through-cache-rule \
  --ecr-repository-prefix ecr-public \
  --region $REGION --profile $PROFILE

# 2. 删除 PTC 自动创建的私有 repo（用 --force 连同镜像一起）
aws ecr delete-repository --force \
  --repository-name ecr-public/l1j0r8q7/archie-referrer-test \
  --region $REGION --profile $PROFILE

aws ecr delete-repository --force \
  --repository-name ecr-public/docker/library/hello-world \
  --region $REGION --profile $PROFILE

# 3. 删除对比用的普通 repo
aws ecr delete-repository --force \
  --repository-name archie-plain-referrer-test \
  --region $REGION --profile $PROFILE

# 4. 删除 ECR Public 上的 upstream repo
aws ecr-public delete-repository --force \
  --repository-name archie-referrer-test \
  --region us-east-1 --profile $PROFILE
```

!!! danger "务必清理"
    虽然费用微乎其微，ECR Public repo 长期留存会在你的 account alias 下公开可见，建议清理。

## 场景化建议

| 场景 | 建议 |
|---|---|
| Kyverno / Sigstore Policy Controller 验签 | ✅ 直接升级到支持 Referrers API 的版本，无需改 workflow |
| EKS Pod 准入控制 | ✅ Admission webhook 内调 ListImageReferrers 即可，不需要先拉 upstream |
| 大型 SBOM/attestation | ⚠️ 首次调用会有秒级延迟，生产场景建议预热关键镜像 |
| 跨账号 ECR PTC + referrer | ✅ 同样适用，依赖 IAM role 跨账号访问 |
| Chainguard / GHCR 私有镜像 | ✅ 同样适用，需要正确配置 Secrets Manager 凭据 |
| 不想让 referrer 占用私有 repo 存储 | ⛔ 目前没有 opt-out 选项，调了 API 就会缓存 |

## 一句话总结

ECR PTC 现在把 OCI 1.1 Referrer 的 "upstream 寻址" 对用户透明化了：你调 `ListImageReferrers`，ECR 就帮你跑一趟 upstream 把签名/SBOM/attestation 拉回来。**cosign verify、Kyverno 策略、Admission Webhook 这些签名验证工作流，可以第一次在 ECR PTC 模式下无 workaround 地运行。**

## 参考链接

- [Amazon ECR Pull Through Cache Now Supports Referrer Discovery and Sync](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-ecr-pull-through-cache-referrers/)
- [Sync an upstream registry with an Amazon ECR private registry](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html)
- [ListImageReferrers API Reference](https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_ListImageReferrers.html)
- [OCI Distribution Spec 1.1 Referrers API](https://github.com/opencontainers/distribution-spec/blob/v1.1.0/spec.md#listing-referrers)
- [Cosign — OCI 1.1+ Experimental Mode](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/)
