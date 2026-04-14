---
tags:
  - Containers
---

# Amazon EKS Capabilities 实战：全托管 Kubernetes 平台能力（ACK + KRO + Argo CD）

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $3-5（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-27

## 背景

Kubernetes 平台工程正面临一个核心矛盾：团队需要 GitOps 部署（Argo CD）、AWS 资源管理（ACK）、自定义 API 编排（KRO）等基础能力，但安装、维护、升级这些开源组件的运维负担巨大——CRD 版本冲突、控制器 OOM、升级向后兼容性问题层出不穷。

2025 年 11 月，AWS 发布了 **EKS Capabilities**，将这三个 Kubernetes 生态核心组件（Argo CD、ACK、KRO）作为全托管服务提供。关键区别在于：这些能力**运行在 AWS 服务侧基础设施上，不占用客户 worker node 资源**，由 AWS 负责扩缩、补丁和升级。

本文通过实际部署和测试，验证三个 Capability 的完整使用流程，重点测试 **KRO + ACK 集成**（一个 kubectl apply 同时创建 K8s 和 AWS 资源），并记录实测踩坑。

## 前置条件

- AWS 账号（需要 EKS、IAM、S3 权限）
- AWS CLI v2 已配置
- `eksctl` 和 `kubectl` 已安装
- 基础 Kubernetes 知识

## 核心概念

### 三个 Capability 对比

| 能力 | 用途 | IAM 权限需求 | K8s 权限需求 |
|------|------|-------------|-------------|
| **ACK** | 通过 K8s API 管理 AWS 资源（S3、RDS、Lambda 等 50+ 服务） | 需要管理的 AWS 服务权限 | 自动创建 Access Entry |
| **KRO** | 创建自定义 K8s API，将多个资源组合为高级抽象 | 不需要（仅需 trust policy） | 需要额外 RBAC 授权 |
| **Argo CD** | GitOps 持续部署，Git 仓库驱动应用同步 | 默认不需要（可选 Secrets Manager） | 需要注册集群 + Access Policy |

### 架构特点

- **运行在 AWS 侧**：Capability 控制器运行在 AWS 拥有的账户中，不在你的 worker node 上
- **独立可选**：三个 Capability 完全独立，按需启用
- **每集群每类型限一个**：不能在同一集群创建两个 ACK Capability
- **定价**：按 Capability 小时 + 管理资源数量计费，无预付

## 动手实践

### Step 1: 创建 IAM Capability Roles

每个 Capability 需要一个 IAM Role，trust policy 指向 `capabilities.eks.amazonaws.com`：

```bash
# 创建通用 trust policy
cat > capability-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "capabilities.eks.amazonaws.com"
      },
      "Action": ["sts:AssumeRole", "sts:TagSession"]
    }
  ]
}
EOF

# 创建三个角色
aws iam create-role --role-name EKSCapabilityACKRole \
  --assume-role-policy-document file://capability-trust-policy.json

aws iam create-role --role-name EKSCapabilityArgoCDRole \
  --assume-role-policy-document file://capability-trust-policy.json

aws iam create-role --role-name EKSCapabilityKRORole \
  --assume-role-policy-document file://capability-trust-policy.json
```

为 ACK Role 添加 S3 管理权限（其他两个不需要 IAM 权限）：

```bash
cat > ack-s3-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": ["arn:aws:s3:::eks-cap-test-*", "arn:aws:s3:::eks-cap-test-*/*"]
    }
  ]
}
EOF

aws iam put-role-policy --role-name EKSCapabilityACKRole \
  --policy-name S3Management --policy-document file://ack-s3-policy.json
```

!!! warning "ACK 权限注意"
    ACK 需要 `s3:ListAllMyBuckets` 权限来检查 bucket 是否存在。如果权限不足，ACK 会报 `ACK.Recoverable` 错误，但会持续重试直到权限生效。

### Step 2: 创建 EKS 集群

```bash
eksctl create cluster \
  --name eks-cap-test \
  --region us-west-2 \
  --version 1.31 \
  --nodegroup-name eks-cap-ng \
  --node-type t3.medium \
  --nodes 2
```

集群创建约需 15-20 分钟。

### Step 3: 启用三个 Capabilities

```bash
# 启用 ACK（需指定 delete-propagation-policy）
aws eks create-capability \
  --cluster-name eks-cap-test \
  --capability-name ack-capability \
  --type ACK \
  --role-arn arn:aws:iam::ACCOUNT_ID:role/EKSCapabilityACKRole \
  --delete-propagation-policy RETAIN \
  --region us-west-2

# 启用 KRO
aws eks create-capability \
  --cluster-name eks-cap-test \
  --capability-name kro-capability \
  --type KRO \
  --role-arn arn:aws:iam::ACCOUNT_ID:role/EKSCapabilityKRORole \
  --delete-propagation-policy RETAIN \
  --region us-west-2

# 启用 Argo CD（需要先创建 IAM Identity Center 实例）
IDC_ARN=$(aws sso-admin create-instance --region us-west-2 \
  --query 'InstanceArn' --output text)

aws eks create-capability \
  --cluster-name eks-cap-test \
  --capability-name argocd-capability \
  --type ARGOCD \
  --role-arn arn:aws:iam::ACCOUNT_ID:role/EKSCapabilityArgoCDRole \
  --delete-propagation-policy RETAIN \
  --configuration "{\"argoCd\":{\"namespace\":\"argocd\",\"awsIdc\":{\"idcInstanceArn\":\"${IDC_ARN}\",\"idcRegion\":\"us-west-2\"}}}" \
  --region us-west-2
```

查看状态：

```bash
aws eks list-capabilities --cluster-name eks-cap-test --region us-west-2 \
  --query "capabilities[*].{Name:capabilityName,Type:type,Status:status}" \
  --output table
```

### Step 4: 验证 ACK — 用 kubectl 创建 S3 Bucket

ACK 启用后会自动安装 50+ AWS 服务的 CRD。创建 S3 bucket 只需一个 YAML：

```yaml
# s3-bucket.yaml
apiVersion: s3.services.k8s.aws/v1alpha1
kind: Bucket
metadata:
  name: my-test-bucket
  namespace: default
spec:
  name: eks-cap-test-demo-ACCOUNT_ID
  tagging:
    tagSet:
    - key: Environment
      value: test
    - key: ManagedBy
      value: ACK-EKS-Capability
```

```bash
kubectl apply -f s3-bucket.yaml

# 检查 ACK 同步状态
kubectl get bucket my-test-bucket -o jsonpath='{.status.conditions}' | python3 -m json.tool

# 验证 AWS 侧 bucket 创建
aws s3api head-bucket --bucket eks-cap-test-demo-ACCOUNT_ID --region us-west-2
```

### Step 5: 验证 KRO — 创建自定义 K8s API

KRO 让你定义 ResourceGraphDefinition（RGD）来创建自定义 API：

```yaml
# simple-app-rgd.yaml
apiVersion: kro.run/v1alpha1
kind: ResourceGraphDefinition
metadata:
  name: simple-app
spec:
  schema:
    apiVersion: v1alpha1
    kind: SimpleApp
    spec:
      name: string
      replicas: integer | default=1
  resources:
  - id: appns
    template:
      apiVersion: v1
      kind: Namespace
      metadata:
        name: ${schema.spec.name}
  - id: configmap
    template:
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: ${schema.spec.name}-config
        namespace: ${appns.metadata.name}
      data:
        app-name: ${schema.spec.name}
```

!!! warning "KRO 需要额外 RBAC"
    KRO 默认只有 RGD 管理权限，要创建 Namespace、ConfigMap 等资源需额外授权：

    ```yaml
    # kro-rbac.yaml
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRoleBinding
    metadata:
      name: kro-cluster-admin
    subjects:
    - kind: User
      name: arn:aws:sts::ACCOUNT_ID:assumed-role/EKSCapabilityKRORole/KRO
      apiGroup: rbac.authorization.k8s.io
    roleRef:
      kind: ClusterRole
      name: cluster-admin
      apiGroup: rbac.authorization.k8s.io
    ```

```bash
kubectl apply -f kro-rbac.yaml
kubectl apply -f simple-app-rgd.yaml

# 等待 RGD 就绪（KRO 自动创建 CRD simpleapps.kro.run）
kubectl get rgd simple-app
# NAME         APIVERSION   KIND        STATE   AGE
# simple-app   v1alpha1     SimpleApp   Active  20s

# 创建实例
cat <<EOF | kubectl apply -f -
apiVersion: kro.run/v1alpha1
kind: SimpleApp
metadata:
  name: my-test-app
spec:
  name: kro-demo
EOF

# 验证：Namespace 和 ConfigMap 自动创建
kubectl get simpleapp my-test-app
# NAME          STATE    READY   AGE
# my-test-app   ACTIVE   True    30s

kubectl get ns kro-demo
kubectl get cm -n kro-demo
```

### Step 6: 核心场景 — KRO + ACK 集成（一键创建 K8s + AWS 资源）

这是 EKS Capabilities 最强大的场景：用 KRO 组合 ACK 资源和 K8s 原生资源，一个 `kubectl apply` 同时创建 S3 bucket 和 ConfigMap：

```yaml
# app-with-storage.yaml
apiVersion: kro.run/v1alpha1
kind: ResourceGraphDefinition
metadata:
  name: app-with-storage
spec:
  schema:
    apiVersion: v1alpha1
    kind: AppWithStorage
    spec:
      appName: string
      environment: string | default=test
  resources:
  - id: bucket
    template:
      apiVersion: s3.services.k8s.aws/v1alpha1
      kind: Bucket
      metadata:
        name: ${schema.spec.appName}-bucket
      spec:
        name: eks-cap-test-kro-${schema.spec.appName}-ACCOUNT_ID
        tagging:
          tagSet:
          - key: Environment
            value: ${schema.spec.environment}
          - key: ManagedBy
            value: KRO-ACK-Combo
  - id: appconfig
    template:
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: ${schema.spec.appName}-config
      data:
        bucket-name: eks-cap-test-kro-${schema.spec.appName}-ACCOUNT_ID
        environment: ${schema.spec.environment}
```

```bash
kubectl apply -f app-with-storage.yaml

# 创建实例 — 一个命令同时创建 S3 + ConfigMap
cat <<EOF | kubectl apply -f -
apiVersion: kro.run/v1alpha1
kind: AppWithStorage
metadata:
  name: myapp
spec:
  appName: myapp
  environment: staging
EOF

# 验证：KRO + ACK 协作完成
kubectl get appwithstorage myapp
# NAME    STATE    READY   AGE
# myapp   ACTIVE   True    31s

# S3 bucket 在 AWS 侧创建成功
aws s3api head-bucket --bucket eks-cap-test-kro-myapp-ACCOUNT_ID --region us-west-2

# ConfigMap 也创建成功，包含 bucket 名称
kubectl get cm myapp-config -o yaml
```

### Step 7: 验证 Argo CD — GitOps 部署

Argo CD 需要先注册目标集群：

```bash
# 注册本地集群（Argo CD 不自动注册！）
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: in-cluster
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
stringData:
  name: in-cluster
  server: arn:aws:eks:us-west-2:ACCOUNT_ID:cluster/eks-cap-test
  project: default
EOF

# 授予 Argo CD 部署权限
aws eks associate-access-policy \
  --region us-west-2 \
  --cluster-name eks-cap-test \
  --principal-arn arn:aws:iam::ACCOUNT_ID:role/EKSCapabilityArgoCDRole \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster
```

!!! warning "关键注意事项"
    - 使用 **EKS 集群 ARN** 作为 server，不支持 `kubernetes.default.svc`
    - 注册后 Argo CD 需要 **3-4 分钟**才能识别新集群

部署应用：

```yaml
# guestbook-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: guestbook
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/argoproj/argocd-example-apps.git
    targetRevision: HEAD
    path: guestbook
  destination:
    name: in-cluster
    namespace: guestbook
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
    - CreateNamespace=true
```

```bash
kubectl apply -f guestbook-app.yaml

# 等待同步完成（3-5 分钟）
kubectl get application guestbook -n argocd
# NAME        SYNC STATUS   HEALTH STATUS
# guestbook   Synced        Healthy

kubectl get pods -n guestbook
# NAME                            READY   STATUS    RESTARTS   AGE
# guestbook-ui-6cb57c694d-rbnhm   1/1     Running   0          40s
```

## 测试结果

### Capability 创建耗时

| Capability | 耗时 | 说明 |
|-----------|------|------|
| **KRO** | ~50 秒 | 最快，仅安装 RGD CRD |
| **ACK** | ~3.5 分钟 | 安装 50+ 服务 CRD |
| **Argo CD** | ~8 分钟 | 最复杂，含 IAM IDC 集成 |

### 功能验证总结

| 测试项 | 结果 | 关键发现 |
|--------|------|---------|
| ACK 创建 S3 Bucket | ✅ | 需 ListAllMyBuckets 权限 |
| KRO 自定义 API | ✅ | 需额外 RBAC，schema 用简写格式 |
| KRO + ACK 组合 | ✅ | 一个 kubectl apply 创建 S3 + ConfigMap |
| Argo CD GitOps 部署 | ✅ | 需注册集群，不支持 kubernetes.default.svc |
| 重复创建同类型 | ❌ 预期报错 | ResourceLimitExceededException |

## 踩坑记录

!!! warning "踩坑 1: ACK 权限不足"
    ACK 需要 `s3:ListAllMyBuckets` 权限来检查 bucket 是否存在。首次创建 bucket 时如果缺少此权限，会报 `ACK.Recoverable` 错误。**已查文档确认**：这是 ACK S3 controller 的实现细节。

!!! warning "踩坑 2: KRO 保留关键字"
    KRO 的 resource id 不能使用 `namespace` —— 这是保留关键字，会报 "naming convention violation"。**实测发现，官方未记录**。

!!! warning "踩坑 3: KRO schema 格式"
    KRO schema 必须使用简写格式 `name: string`，不能用 `name: {type: string}`。**已查文档确认**：官方示例统一使用简写。

!!! warning "踩坑 4: KRO 需要额外 RBAC"
    KRO 默认只有 RGD 管理权限（`AmazonEKSKROPolicy`），创建 Namespace、ConfigMap 等资源需要额外的 ClusterRoleBinding。**已查文档确认**。

!!! warning "踩坑 5: Argo CD 本地集群未自动注册"
    与自部署 Argo CD 不同，EKS 托管 Argo CD **不自动注册本地集群**。必须手动创建 cluster Secret（使用 EKS ARN，不支持 `kubernetes.default.svc`）并关联 Access Policy。**已查文档确认**。

!!! warning "踩坑 6: ACK Drift 检测的边界"
    手动通过 AWS CLI 给 S3 bucket 添加额外 tag（spec 中未定义的），ACK 不会删除它。ACK 只管理 CR spec 中声明的字段，不会强制清除 spec 外的属性。**实测发现，官方未记录**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EKS 集群 | $0.10/hr | 2 hr | $0.20 |
| EC2 (2×t3.medium) | $0.0832/hr ×2 | 2 hr | $0.33 |
| EKS Capabilities (3个) | ~$0.10/hr ×3 | 2 hr | $0.60 |
| S3 | - | 微量 | ~$0.00 |
| **合计** | | | **~$1.13** |

## 清理资源

```bash
# 1. 删除 K8s 资源
kubectl delete appwithstorage myapp
kubectl delete simpleapp my-test-app
kubectl delete application guestbook -n argocd
kubectl delete rgd app-with-storage simple-app
kubectl delete bucket my-test-bucket

# 2. 手动清理 S3 bucket（delete-propagation-policy=RETAIN 时 ACK 不删 AWS 资源）
aws s3 rb s3://eks-cap-test-demo-ACCOUNT_ID --region us-west-2
aws s3 rb s3://eks-cap-test-kro-myapp-ACCOUNT_ID --region us-west-2

# 3. 删除 Capabilities
aws eks delete-capability --cluster-name eks-cap-test \
  --capability-name ack-capability --region us-west-2
aws eks delete-capability --cluster-name eks-cap-test \
  --capability-name kro-capability --region us-west-2
aws eks delete-capability --cluster-name eks-cap-test \
  --capability-name argocd-capability --region us-west-2

# 4. 删除 EKS 集群（含 VPC 等 CloudFormation 资源）
eksctl delete cluster --name eks-cap-test --region us-west-2

# 5. 删除 IAM Roles
aws iam delete-role-policy --role-name EKSCapabilityACKRole --policy-name S3Management
aws iam delete-role --role-name EKSCapabilityACKRole
aws iam delete-role --role-name EKSCapabilityArgoCDRole
aws iam delete-role --role-name EKSCapabilityKRORole

# 6. 删除 IAM Identity Center 实例
aws sso-admin delete-instance --instance-arn IDC_INSTANCE_ARN --region us-west-2
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。EKS 集群 + Capabilities 按小时计费，不清理每天约 $5+。

## 结论与建议

### 适用场景

- **平台工程团队**：用 KRO + ACK 构建自助服务平台，开发者通过简单的自定义 API 获取完整基础设施
- **GitOps 实践者**：Argo CD 提供全托管 GitOps，无需维护 Argo CD 基础设施
- **多集群管理**：Argo CD 支持跨集群、跨账户、跨 Region 部署

### 生产环境建议

1. **权限最小化**：ACK 使用 IAM Role Selectors 做 namespace 级别权限隔离，不要给 `s3:*`
2. **KRO RBAC**：生产环境不要用 `cluster-admin`，按需授权具体资源类型
3. **Argo CD**：生产环境使用 namespace-scoped 权限，不要用 `AmazonEKSClusterAdminPolicy`
4. **监控成本**：启用 Cost Explorer 标签跟踪 Capability 费用

### 与自管理方案对比

| 维度 | 自管理 (Helm/Operator) | EKS Capabilities |
|------|----------------------|-----------------|
| 安装维护 | 自己安装、升级、调参 | AWS 全托管 |
| 资源占用 | 占用 worker node 资源 | 运行在 AWS 侧 |
| 版本控制 | 自主选择版本 | AWS 控制升级节奏 |
| 灵活性 | 完全自定义 | 按 AWS 提供的配置 |
| 适合 | 有专职平台团队 | 希望减少运维负担 |

## 参考链接

- [EKS Capabilities 用户指南](https://docs.aws.amazon.com/eks/latest/userguide/capabilities.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-eks-capabilities/)
- [AWS News Blog](https://aws.amazon.com/blogs/aws/announcing-amazon-eks-capabilities-for-workload-orchestration-and-cloud-resource-management/)
- [EKS 定价](https://aws.amazon.com/eks/pricing/)
- [EKS Capabilities 安全配置](https://docs.aws.amazon.com/eks/latest/userguide/capabilities-security.html)
