---
tags:
  - Containers
---

# CloudWatch OTel Container Insights 实战：用 PromQL 解锁 EKS 可观测性新维度

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: ~$1.50（含清理，OTel 指标预览期免费）
    - **Region**: ap-southeast-1（支持的 5 个 Region 之一）
    - **最后验证**: 2026-04-03

## 背景

运行 EKS 集群的团队对 Container Insights 并不陌生 — Enhanced 模式已经提供了 cluster/namespace/pod 级别的预聚合指标和 CloudWatch 原生告警能力。但当你需要回答"backend team 的所有 pod 总共用了多少 CPU？"或者"按可用区统计网络流量"时，Enhanced 模式的 3-6 个固定维度就捉襟见肘了。

2026 年 4 月，CloudWatch 推出了 OTel Container Insights（Preview），这是一个基于 OpenTelemetry 协议的全新指标模式。它不替代 Enhanced，而是**并存**，为同一集群同时提供两种视角：Enhanced 负责快速概览和告警，OTel 负责深度排查和业务维度切片。

核心变化：每个指标从 3-6 个维度跃升到最多 **150 个标签**，包括所有 Kubernetes pod 和 node labels。这意味着你部署时打的 `team=backend`、`env=production`、`cost-center=engineering` 标签，都可以直接在 PromQL 查询中使用。

## 前置条件

- AWS 账号（需要 EKS、CloudWatch、IAM 权限）
- AWS CLI v2 已配置
- kubectl 已安装并配置
- 一个运行中的 EKS 集群（K8s 1.23+），或者按下面步骤创建

## 核心概念

### OTel vs Enhanced Container Insights 对比

| 特性 | Enhanced Container Insights | OTel Container Insights (Preview) |
|------|---------------------------|----------------------------------|
| 指标名 | CloudWatch 格式（`pod_cpu_utilization`）| 开源原生（`container_cpu_usage_seconds_total`）|
| 标签数/指标 | 3-6 个预定义维度 | **最多 150 个**（含所有 K8s labels）|
| 聚合 | 预聚合（cluster → namespace → pod）| 原始 per-resource，查询时 PromQL 聚合 |
| 查询语言 | CloudWatch Metrics API | **PromQL**（Prometheus 兼容）|
| 数据摄入 | CloudWatch Logs EMF 格式 | OTLP 端点 |
| 自定义 K8s Labels | ❌ 不支持 | ✅ 自动包含 |
| 可同时启用 | ✅ | ✅ |
| 费用 | 按 observation 计费 | **Preview 期间免费** |

### 指标来源

OTel Container Insights 从 8 个数据源采集指标：

| 来源 | 指标类型 | 前置条件 |
|------|---------|---------|
| cAdvisor | CPU/内存/网络/磁盘 | 无 |
| Prometheus Node Exporter | 节点级系统指标 | 无 |
| Kube State Metrics | K8s 资源状态（Deployment/Pod/Node 等）| 无 |
| K8s API Server | API Server 和 etcd 指标 | 无 |
| NVMe | SMART 磁盘指标 | 无 |
| NVIDIA DCGM | GPU 利用率/显存/功耗 | 需 NVIDIA device plugin |
| AWS Neuron Monitor | Trainium/Inferentia 加速器 | 需 Neuron driver |
| AWS EFA | 弹性网络适配器 | 需 EFA device plugin |

### 标签体系（三层）

每个 OTel 指标的标签来自三个层级：

1. **原始指标源标签** — cAdvisor 自带的 `pod`、`namespace`、`container` 等
2. **OTel Resource Attributes** — 遵循 OpenTelemetry 语义约定的统一属性（`k8s.pod.name`、`cloud.region`、`host.type` 等）
3. **K8s Pod/Node Labels** — 从 K8s API 发现的所有标签，前缀 `k8s.pod.label.` 和 `k8s.node.label.`

## 动手实践

### Step 1: 准备 EKS 集群和节点

如果已有 EKS 集群，跳到「更新 kubeconfig」步骤。否则，先创建集群和节点组：

```bash
# 创建 EKS 集群（约 15 分钟）
eksctl create cluster \
  --name my-cluster \
  --region ap-southeast-1 \
  --version 1.31 \
  --nodegroup-name default \
  --node-type m5.large \
  --nodes 2 \
  --managed
```

集群就绪后，更新 kubeconfig 并确认节点状态：

```bash
# 更新 kubeconfig
aws eks update-kubeconfig --name my-cluster --region ap-southeast-1

# 确认节点就绪
kubectl get nodes
```

**实测输出**（使用已有集群 zeroboot-eks）：
```
NAME                                                 STATUS   ROLES    AGE   VERSION
ip-192-168-121-112.ap-southeast-1.compute.internal   Ready    <none>   51s   v1.31.14-eks-f69f56f
ip-192-168-161-148.ap-southeast-1.compute.internal   Ready    <none>   51s   v1.31.14-eks-f69f56f
```

### Step 2: 安装 CloudWatch Observability Add-on

确保节点 IAM 角色附加了 `CloudWatchAgentServerPolicy`：

```bash
# 附加 CW Agent 权限
aws iam attach-role-policy \
  --role-name my-node-role \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

# 安装 add-on（使用最新版本）
aws eks create-addon \
  --cluster-name my-cluster \
  --addon-name amazon-cloudwatch-observability \
  --region ap-southeast-1
```

!!! tip "版本说明"
    官方文档提到需要 v6.0.1-eksbuild.1，但在 ap-southeast-1 实测中，v6.0.0-eksbuild.1 是当前最新可用版本且已包含完整 OTel 支持。不指定版本会自动安装最新。

安装后验证组件：

```bash
kubectl get pods -n amazon-cloudwatch
```

**实测输出**：
```
NAME                                                              READY   STATUS
amazon-cloudwatch-observability-controller-manager-779fcc5qhc5s   1/1     Running
cloudwatch-agent-4lcwf                                            1/1     Running
cloudwatch-agent-cluster-scraper-54c7b4fbdb-sfz2r                 1/1     Running
kube-state-metrics-569d8b6df7-7mdl8                               1/1     Running
node-exporter-4bjxr                                               1/1     Running
fluent-bit-4sbvw                                                  1/1     Running
```

Add-on 自动部署了 6 类组件：CW Agent（DaemonSet）、Cluster Scraper、Kube State Metrics、Node Exporter、Fluent Bit 和 Controller Manager。OTel 指标所需的 **kube-state-metrics** 和 **node-exporter** 作为 add-on 的一部分自动安装，无需手动配置。

### Step 3: 部署带自定义标签的测试工作负载

部署一组带业务标签的 Pod，用于验证 OTel 的标签传播能力：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: otel-test
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-otel-test
  namespace: otel-test
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
        team: backend
        env: test
        cost-center: engineering
    spec:
      containers:
      - name: nginx
        image: nginx:latest
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 128Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: stress-otel-test
  namespace: otel-test
spec:
  replicas: 1
  selector:
    matchLabels:
      app: stress
  template:
    metadata:
      labels:
        app: stress
        team: platform
        env: test
    spec:
      containers:
      - name: stress
        image: alpine:latest
        command: ["sh", "-c"]
        args: ["apk add --no-cache stress-ng && stress-ng --cpu 1 --vm 1 --vm-bytes 64M --timeout 3600"]
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
          limits:
            cpu: 500m
            memory: 256Mi
```

等待约 **3 分钟**让 OTel 指标开始流入 CloudWatch。

### Step 4: 用 PromQL 查询 OTel 指标

OTel 指标通过 CloudWatch 的 Prometheus 兼容 API 查询。端点为：

```
POST https://monitoring.{region}.amazonaws.com/api/v1/query
Content-Type: application/x-www-form-urlencoded
Authorization: SigV4 (service=monitoring)
```

以下用 Python 演示（也可在 CloudWatch Console → Query Studio 中直接使用 PromQL）：

```python
import boto3, urllib.parse, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session(region_name="ap-southeast-1")
creds = session.get_credentials().get_frozen_credentials()

def promql_query(query):
    url = "https://monitoring.ap-southeast-1.amazonaws.com/api/v1/query"
    body = urllib.parse.urlencode({"query": query})
    req = AWSRequest(method="POST", url=url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    SigV4Auth(creds, "monitoring", "ap-southeast-1").add_auth(req)
    return requests.post(url, data=body, headers=dict(req.headers)).json()

# 查询所有 CPU 指标
result = promql_query("container_cpu_usage_seconds_total")
print(f"时间序列数: {len(result['data']['result'])}")
```

**实测结果**：查询 `container_cpu_usage_seconds_total` 返回 **145 条时间序列**。

#### 查看单个指标的完整标签

```python
result = promql_query(
    'container_cpu_usage_seconds_total{"@resource.k8s.pod.label.team"="backend"}'
)
labels = result['data']['result'][0]['metric']
print(f"标签总数: {len(labels)}")
```

**实测输出 — 单个 cAdvisor CPU 指标携带 52 个标签**：

```yaml
# OTel Resource Attributes（跨所有指标源一致）
@resource.cloud.provider: aws
@resource.cloud.region: ap-southeast-1
@resource.cloud.availability_zone: ap-southeast-1b
@resource.cloud.account.id: 595842667825
@resource.cloud.resource_id: arn:aws:eks:...:cluster/zeroboot-eks
@resource.host.type: t3.medium
@resource.host.id: i-0e2593468e4549a4c

# K8s Metadata
@resource.k8s.cluster.name: zeroboot-eks
@resource.k8s.namespace.name: otel-test
@resource.k8s.deployment.name: nginx-otel-test
@resource.k8s.pod.name: nginx-otel-test-fcd8d4fdc-qrqt4
@resource.k8s.workload.name: nginx-otel-test
@resource.k8s.workload.type: Deployment

# 自定义 K8s Pod Labels（你部署时打的标签全部可用！）
@resource.k8s.pod.label.app: nginx
@resource.k8s.pod.label.team: backend
@resource.k8s.pod.label.env: test
@resource.k8s.pod.label.cost-center: engineering
@resource.k8s.pod.label.version: v1

# Node Labels
@resource.k8s.node.label.eks.amazonaws.com/capacityType: ON_DEMAND
@resource.k8s.node.label.eks.amazonaws.com/nodegroup: otel-ci-ng

# 原始指标源信息
@instrumentation.cloudwatch.solution: k8s-otel-container-insights
@instrumentation.@name: github.com/google/cadvisor
```

### Step 5: OTel vs Enhanced 对比实验

同一集群同时运行两种指标模式，对比查询体验：

**OTel 查询 — 按 team 标签聚合 CPU**：
```python
result = promql_query(
    'sum by ("@resource.k8s.pod.label.team")'
    '(rate(container_cpu_usage_seconds_total[5m]))'
)
for r in result['data']['result']:
    team = r['metric'].get('@resource.k8s.pod.label.team', '(system)')
    cpu = float(r['value'][1])
    print(f"  team={team}: {cpu:.4f} cores")
```

**实测输出**：
```
  team=(system): 0.1894 cores
  team=backend:  0.0000 cores  # nginx 空闲
  team=platform: 0.9430 cores  # stress 工作负载
```

**Enhanced 查询 — 相同的聚合需求**：
```bash
aws cloudwatch get-metric-data \
  --metric-data-queries '[{
    "Id": "cpu",
    "MetricStat": {
      "Metric": {
        "Namespace": "ContainerInsights",
        "MetricName": "pod_cpu_utilization",
        "Dimensions": [
          {"Name": "ClusterName", "Value": "zeroboot-eks"},
          {"Name": "Namespace", "Value": "otel-test"}
        ]
      },
      "Period": 60,
      "Stat": "Average"
    }
  }]' \
  --start-time $(date -u -d '5 min ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --region ap-southeast-1
```

**结果**: Enhanced 只能聚合到 Namespace 级（维度仅有 ClusterName + Namespace），**无法按 team 标签切片** — 因为 Enhanced 根本不包含自定义 K8s labels。

### Step 6: PromQL Range Query 性能测试

```python
import time, urllib.parse

def promql_range_query(query, range_seconds, step=30):
    url = "https://monitoring.ap-southeast-1.amazonaws.com/api/v1/query_range"
    end = time.time()
    start = end - range_seconds
    body = urllib.parse.urlencode({
        "query": query, "start": str(start),
        "end": str(end), "step": str(step)
    })
    # ... SigV4 签名同上
```

**实测结果**：

| 时间范围 | 时间序列数 | 数据点数 | 查询延迟 |
|---------|-----------|---------|---------|
| 5 分钟 | 13 | 135 | **69ms** |
| 15 分钟 | 13 | 221 | **70ms** |
| 1 小时 | 13 | 221 | **139ms** |

PromQL 查询性能极佳，即使 1 小时范围也在 **140ms 以内**。

### Step 7: Kube State Metrics 验证

```python
# Pod 状态查询
result = promql_query('kube_pod_status_phase')
print(f"kube_pod_status_phase: {len(result['data']['result'])} 条")

# Deployment 副本数查询
result = promql_query(
    'kube_deployment_status_replicas{"@resource.k8s.namespace.name"="otel-test"}'
)
```

**实测输出**：
```
kube_pod_status_phase: 205 条（26-28 labels/metric）
  nginx-otel-test: 3 replicas
  stress-otel-test: 1 replicas
```

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | OTel 指标基础验证 | ✅ 通过 | 145 条 CPU 时间序列 | ~3min 首条指标出现 |
| 2 | OTel vs Enhanced 对比 | ✅ 通过 | 52 labels vs 2-4 dims | OTel 标签丰富度提升 10x+ |
| 3 | 自定义 K8s Label 查询 | ✅ 通过 | team/env/cost-center 全部可查 | 使用 `@resource.k8s.pod.label.` 前缀 |
| 4 | PromQL 按标签聚合 | ✅ 通过 | 3 个 team 分组结果正确 | Enhanced 无法实现 |
| 5 | PromQL 查询性能 | ✅ 通过 | 5m: 69ms, 1h: 139ms | 亚秒级响应 |
| 6 | Kube State Metrics | ✅ 通过 | 205 条 pod_status_phase | 含完整 K8s metadata |
| 7 | 指标出现延迟 | ✅ 通过 | ≤ 3 分钟 | 30s 采集粒度 |

## 踩坑记录

!!! warning "踩坑 1: Add-on 版本号与文档不一致"
    官方文档提到需要 v6.0.1-eksbuild.1，但在 ap-southeast-1（2026-04-03）最新可用版本是 **v6.0.0-eksbuild.1**，且已包含完整 OTel 功能。其他 Region（如 us-east-1）最新版本还停在 v5.3.0。
    
    建议：安装时不指定版本号，让 EKS 自动选择当前 Region 可用的最新版本。

!!! warning "踩坑 2: PromQL API 端点路径"
    CloudWatch 的 Prometheus 兼容 API 路径是 `/api/v1/query`，**不是** `/prometheus/api/v1/query`。后者返回 404。使用 SigV4 签名，service 名称为 `monitoring`。
    
    建议使用 POST 方法避免 GET URL 中特殊字符导致的签名问题。

!!! info "发现: 标签自动去重"
    Agent 配置了 `awsattributelimit` processor，会自动移除冗余标签（如 `pod-template-hash`、`controller-revision-hash`、各种 beta/alpha node labels），保持标签集干净。实测单个 cAdvisor 指标 52 个标签，远低于 150 上限。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EKS 集群 | $0.10/hr | 1 hr | $0.10 |
| EC2 节点 (t3.medium × 2) | $0.042/hr | 1 hr × 2 | $0.08 |
| OTel Container Insights 指标 | 免费 (Preview) | - | $0.00 |
| Enhanced Container Insights | 按 observation | ~50 obs | ~$0.05 |
| **合计** | | | **~$0.23** |

## 清理资源

```bash
# 1. 删除测试工作负载
kubectl delete namespace otel-test

# 2. 删除 CloudWatch Observability add-on
aws eks delete-addon \
  --cluster-name my-cluster \
  --addon-name amazon-cloudwatch-observability \
  --region ap-southeast-1

# 3. 删除节点组（如果是本实验创建的）
aws eks delete-nodegroup \
  --cluster-name my-cluster \
  --nodegroup-name otel-ci-ng \
  --region ap-southeast-1

# 4. 移除 IAM Policy（如果是本实验附加的）
aws iam detach-role-policy \
  --role-name my-node-role \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

# 5. 清理 CloudWatch Log Groups
for lg in application dataplane host performance; do
  aws logs delete-log-group \
    --log-group-name /aws/containerinsights/my-cluster/$lg \
    --region ap-southeast-1
done
```

!!! danger "务必清理"
    EKS 集群 + EC2 节点持续计费。Lab 完成后请立即执行清理步骤。OTel 指标预览期免费，但 Enhanced Container Insights 的 observation 费用和 CloudWatch Logs 存储费用仍然适用。

## 结论与建议

### 什么时候该用 OTel Container Insights？

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 快速健康检查、基础告警 | Enhanced | 预聚合指标即查即用，与 CW Alarms 原生集成 |
| 按业务标签（team/env/cost-center）排查 | **OTel** | Enhanced 没有自定义 K8s labels |
| 成本分摊分析 | **OTel** | 按 cost-center 标签聚合资源消耗 |
| Prometheus 迁移/混合监控 | **OTel** | 开源指标名 + PromQL，无缝对接现有 Grafana dashboard |
| GPU/Neuron 加速器监控 | **OTel** | 自动关联加速器指标到具体 Pod |
| 两者都需要 | **同时启用** | 互不干扰，Preview 期间 OTel 零额外成本 |

### 生产建议

1. **现在就启用 OTel Preview** — 免费且与 Enhanced 并存，零风险
2. **规划 K8s Label 策略** — OTel 的价值与你的标签质量成正比。确保 Pod 有 team、env、cost-center 等业务标签
3. **用 PromQL 构建业务视角 Dashboard** — 按团队、环境、可用区切片，这是 Enhanced 做不到的
4. **关注 GA 定价** — Preview 免费，但 GA 后 OTel 指标可能产生费用，提前规划预算

## 参考链接

- [Container Insights with OpenTelemetry metrics for Amazon EKS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/container-insights-otel-metrics.html)
- [PromQL querying in CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL-Querying.html)
- [Install the CloudWatch Observability EKS add-on](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/install-CloudWatch-Observability-EKS-addon.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/cloudwatch-otel-container-insights-eks/)
