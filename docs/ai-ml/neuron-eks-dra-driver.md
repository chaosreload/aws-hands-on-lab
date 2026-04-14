---
description: "Deploy AWS Neuron DRA driver on EKS for topology-aware Trainium device scheduling with CEL filtering and per-workload LNC config."
tags:
  - Trainium
  - EKS
  - What's New
---
# Neuron DRA Driver：EKS 上 Trainium 设备的拓扑感知调度实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐⭐ 高级
    - **预估时间**: 60 分钟
    - **预估费用**: ~$30（Spot 实例 + EKS 集群，含清理）
    - **Region**: us-east-2 (Ohio)
    - **最后验证**: 2026-03-23

## 背景

在 EKS 上运行 ML 训练和推理工作负载时，如何高效分配 Neuron 设备一直是个痛点。传统的 Kubernetes device plugin 只能告诉调度器"这个节点有 N 个 Neuron 设备"——一个简单的整数。它不知道设备之间的拓扑关系，不知道哪些设备通过高速互连相连，也无法按工作负载动态配置 Logical NeuronCore (LNC)。

要实现拓扑感知调度，你需要额外部署 [Neuron Scheduler Extension](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/containers/tutorials/k8s-neuron-scheduler.html)，而 LNC 配置只能在 EC2 Launch Template 里预设——这意味着同一个节点组的所有工作负载共享相同的 LNC 设置。

**Neuron DRA (Dynamic Resource Allocation) Driver** 改变了这一切。它基于 Kubernetes 1.34 引入的 DRA 框架，让每个 Neuron 设备作为带有丰富属性的 `ResourceSlice` 对象发布到集群中。调度器不再只看到数字，而是看到完整的设备画像：拓扑坐标、驱动版本、实例类型、连接组 ID。

这篇文章将在 trn2.48xlarge（16 个 Neuron 设备）上完整验证 DRA driver 的三大核心能力：拓扑感知子集分配、CEL 属性过滤、和 per-workload LNC 配置。

## 前置条件

- AWS 账号（需要 EKS、EC2 权限，trn2 实例 quota）
- AWS CLI v2 + eksctl + kubectl + Helm
- trn2.48xlarge Spot 或 On-Demand quota（us-east-2，至少 192 vCPUs）

## 核心概念

### DRA vs Device Plugin：范式转换

| 特性 | Device Plugin | DRA Driver |
|------|--------------|------------|
| K8s 版本 | 所有 EKS 版本 | 1.34+ |
| 设备发现 | 整数计数（`aws.amazon.com/neuron: 16`） | 每个设备的完整属性（ResourceSlice） |
| 拓扑感知 | 需要 Scheduler Extension | 原生支持（matchAttribute） |
| LNC 配置 | Launch Template 预设 | 每个工作负载独立配置 |
| 设备过滤 | 不支持 | CEL 表达式 |
| Karpenter/Auto Mode | ✅ | ❌ |
| AMI | AL2023, Bottlerocket | 仅 AL2023 |

**关键取舍**：DRA driver 功能更强，但不支持 Karpenter/EKS Auto Mode 和 Bottlerocket。如果你依赖这些，目前仍需使用 device plugin。

### ResourceSlice：设备的"简历"

DRA driver 为每个 Neuron 设备发布一份 ResourceSlice，包含 12 种属性：

- **身份信息**：`deviceId`、`instanceType`、`resourceType`
- **驱动版本**：`neuronDriverVersion`、`draDriverVersion`
- **拓扑坐标**：`topology_x`、`topology_y`（2D 网格位置）
- **连接组 ID**：`devicegroup1_id`（PCI BDF）、`devicegroup4_id`、`devicegroup8_id`、`devicegroup16_id`
- **网络层**：`networkNodeLayer1/2/3`

调度器用这些属性做两件事：**过滤**（CEL 表达式选设备）和**约束**（matchAttribute 确保拓扑连接）。

### 三大核心能力

1. **Connected Device Subsets** — 用 `matchAttribute` 约束分配 1/4/8/16 个拓扑相连的设备，替代 Scheduler Extension
2. **CEL Attribute Selection** — 用表达式过滤设备属性，如按 instanceType 或 driverVersion 选择
3. **Per-workload LNC** — 通过 ResourceClaimTemplate 的 opaque parameters 为每个工作负载独立配置 Logical NeuronCore

## 动手实践

### Step 1: 创建 EKS 集群

创建 EKS 1.34 集群，包含一个 system 节点组和一个 trn2 节点组：

```bash
# 创建集群配置
cat > neuron-dra-cluster.yaml << 'EOF'
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: neuron-dra-test
  region: us-east-2
  version: "1.34"

managedNodeGroups:
  - name: system
    instanceType: m5.large
    desiredCapacity: 2
    minSize: 2
    maxSize: 2
EOF

eksctl create cluster -f neuron-dra-cluster.yaml
```

集群就绪后，添加 trn2 节点组。trn2.48xlarge 目前仅在 us-east-2 提供，On-Demand 容量可能不足，**建议使用 Spot**：

```bash
# 查看哪些 AZ 有 Spot 容量
aws ec2 describe-spot-price-history \
  --instance-types trn2.48xlarge \
  --product-descriptions "Linux/UNIX" \
  --region us-east-2 \
  --query 'SpotPriceHistory[0:3].{AZ:AvailabilityZone,Price:SpotPrice}' \
  --output table
```

```bash
# 获取 private subnet ID（选有 Spot 容量的 AZ）
CLUSTER_VPC=$(aws eks describe-cluster --name neuron-dra-test --region us-east-2 \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)

SUBNET_ID=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$CLUSTER_VPC" \
            "Name=availability-zone,Values=us-east-2c" \
            "Name=map-public-ip-on-launch,Values=false" \
  --query 'Subnets[0].SubnetId' --output text --region us-east-2)

# 获取现有节点组的 IAM role
NODE_ROLE=$(aws eks describe-nodegroup --cluster-name neuron-dra-test \
  --nodegroup-name system --region us-east-2 \
  --query 'nodegroup.nodeRole' --output text)

# 创建 Spot 节点组
aws eks create-nodegroup \
  --cluster-name neuron-dra-test \
  --nodegroup-name trn2-spot \
  --node-role "$NODE_ROLE" \
  --subnets "$SUBNET_ID" \
  --instance-types trn2.48xlarge \
  --ami-type AL2023_x86_64_NEURON \
  --scaling-config minSize=1,maxSize=1,desiredSize=1 \
  --capacity-type SPOT \
  --region us-east-2
```

等待节点就绪（约 5-10 分钟）：

```bash
aws eks update-kubeconfig --name neuron-dra-test --region us-east-2
kubectl get nodes -o wide
```

```
NAME                                            STATUS   ROLES    AGE   VERSION
ip-192-168-14-69.us-east-2.compute.internal     Ready    <none>   5h    v1.34.4-eks-f69f56f
ip-192-168-187-176.us-east-2.compute.internal   Ready    <none>   19m   v1.34.4-eks-f69f56f  # trn2
ip-192-168-57-87.us-east-2.compute.internal     Ready    <none>   5h    v1.34.4-eks-f69f56f
```

### Step 2: 安装 Neuron DRA Driver

```bash
# ECR public 登录（Helm OCI registry 需要）
aws ecr-public get-login-password --region us-east-1 | \
  helm registry login --username AWS --password-stdin public.ecr.aws

# 创建 healthcheck namespace（Helm chart 依赖）
kubectl create namespace neuron-healthcheck-system

# 安装 DRA driver
helm upgrade --install neuron-dra-driver \
  oci://public.ecr.aws/neuron/neuron-helm-chart \
  --set devicePlugin.enabled=false \
  --set npd.enabled=false \
  --set draDriver.enabled=true \
  --namespace neuron-dra-driver \
  --create-namespace
```

验证部署：

```bash
# DaemonSet 应该只在 trn2 节点上运行
kubectl get pods -n neuron-dra-driver -o wide
```

```
NAME                                     READY   STATUS    AGE   NODE
neuron-dra-driver-kubelet-plugin-prfn4   1/1     Running   28s   ip-192-168-187-176...
```

```bash
# DeviceClass 已创建
kubectl get deviceclass
```

```
NAME             AGE
neuron.aws.com   28s
```

```bash
# ResourceSlice 发布了所有 Neuron 设备
kubectl get resourceslice
```

```
NAME                                                              DRIVER           AGE
ip-192-168-187-176.us-east-2.compute.internal-neuron.aws.clm4fd   neuron.aws.com   23s
```

### Step 3: 查看 ResourceSlice 设备属性

```bash
kubectl get resourceslice -o jsonpath='{range .items[0].spec.devices[*]}{.name}: topology=({.attributes.topology_x.int},{.attributes.topology_y.int}) group4={.attributes.resource\.aws\.com/devicegroup4_id.string}{"\n"}{end}'
```

```
neuron-device-0:  topology=(0,0) group4=13713e551cd37c19
neuron-device-1:  topology=(0,1) group4=13713e551cd37c19
neuron-device-2:  topology=(0,2) group4=13713e551cd37c19
neuron-device-3:  topology=(0,3) group4=13713e551cd37c19
neuron-device-4:  topology=(1,0) group4=671622785349fddf
neuron-device-5:  topology=(1,1) group4=671622785349fddf
neuron-device-6:  topology=(1,2) group4=671622785349fddf
neuron-device-7:  topology=(1,3) group4=671622785349fddf
neuron-device-8:  topology=(2,0) group4=237e3ef7b968b4d3
neuron-device-9:  topology=(2,1) group4=237e3ef7b968b4d3
neuron-device-10: topology=(2,2) group4=237e3ef7b968b4d3
neuron-device-11: topology=(2,3) group4=237e3ef7b968b4d3
neuron-device-12: topology=(3,0) group4=b3d6ce1919d29f8f
neuron-device-13: topology=(3,1) group4=b3d6ce1919d29f8f
neuron-device-14: topology=(3,2) group4=b3d6ce1919d29f8f
neuron-device-15: topology=(3,3) group4=b3d6ce1919d29f8f
```

可以清晰看到 trn2.48xlarge 的 4×4 拓扑结构：16 个设备排列成 4 行 4 列，每 4 个一组共享同一个 `devicegroup4_id`，每 8 个共享 `devicegroup8_id`，全部 16 个共享 `devicegroup16_id`。

### Step 4: 全设备分配（All Mode）

请求节点上的所有 Neuron 设备：

```yaml
# all-neurons.yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: all-neurons
spec:
  spec:
    devices:
      requests:
      - name: neurons
        exactly:
          deviceClassName: neuron.aws.com
          selectors:
          - cel:
              expression: "device.attributes['neuron.aws.com'].instanceType == 'trn2.48xlarge'"
          allocationMode: All
---
apiVersion: v1
kind: Pod
metadata:
  name: all-neurons-test
spec:
  nodeSelector:
    node.kubernetes.io/instance-type: trn2.48xlarge
  containers:
  - name: app
    image: public.ecr.aws/amazonlinux/amazonlinux:2023
    command: ["sleep", "300"]
    resources:
      claims:
      - name: neurons
  resourceClaims:
  - name: neurons
    resourceClaimTemplateName: all-neurons
```

```bash
kubectl apply -f all-neurons.yaml
kubectl exec all-neurons-test -- ls /dev/ | grep neuron
```

```
neuron0
neuron1
neuron2
...
neuron15
```

**全部 16 个 Neuron 设备**都挂载到了 Pod 中。ResourceClaim 状态确认分配了所有设备：

```bash
kubectl get resourceclaim -o wide
```

```
NAME                                  STATE
all-neurons-test-neurons-jvrdv        allocated,reserved
```

### Step 5: 连接设备子集分配（4 Connected Devices）

这是 DRA driver 最有价值的能力——不需要 Scheduler Extension 就能分配拓扑相连的设备：

```yaml
# connected-4.yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: connected-4-neurons
spec:
  spec:
    devices:
      requests:
      - name: neurons
        exactly:
          deviceClassName: neuron.aws.com
          allocationMode: ExactCount
          count: 4
          selectors:
          - cel:
              expression: "device.attributes['neuron.aws.com'].instanceType == 'trn2.48xlarge'"
      constraints:
      - requests: ["neurons"]
        matchAttribute: "resource.aws.com/devicegroup4_id"
---
apiVersion: v1
kind: Pod
metadata:
  name: connected-4-test
spec:
  nodeSelector:
    node.kubernetes.io/instance-type: trn2.48xlarge
  containers:
  - name: app
    image: public.ecr.aws/amazonlinux/amazonlinux:2023
    command: ["sleep", "300"]
    resources:
      claims:
      - name: neurons
  resourceClaims:
  - name: neurons
    resourceClaimTemplateName: connected-4-neurons
```

```bash
kubectl apply -f connected-4.yaml

# 查看分配了哪些设备
CLAIM=$(kubectl get resourceclaim -o jsonpath='{.items[?(@.metadata.name!="")].metadata.name}' | tr ' ' '\n' | grep connected)
kubectl get resourceclaim $CLAIM -o jsonpath='{range .status.allocation.devices.results[*]}{.device} {end}'
```

```
neuron-device-0 neuron-device-1 neuron-device-2 neuron-device-3
```

分配了 device 0-3，它们都共享 `devicegroup4_id: 13713e551cd37c19`，确认是拓扑相连的 4 个设备。

`matchAttribute` 支持的连接子集大小：

| matchAttribute | 子集大小 | 说明 |
|---|---|---|
| `resource.aws.com/devicegroup1_id` | 1 | 单个设备 |
| `resource.aws.com/devicegroup4_id` | 4 | 一个 4 设备连接组 |
| `resource.aws.com/devicegroup8_id` | 8 | 一个 8 设备连接组 |
| `resource.aws.com/devicegroup16_id` | 16 | 全部设备 |

### Step 6: Per-workload LNC 配置

Logical NeuronCore (LNC) 决定每个 NeuronCore 对应几个逻辑核心。传统方式需要在 EC2 Launch Template 中预配置，DRA driver 可以在每个工作负载的 ResourceClaimTemplate 中独立指定：

```yaml
# lnc-config.yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: neurons-lnc-1
spec:
  spec:
    devices:
      requests:
      - name: neurons
        exactly:
          deviceClassName: neuron.aws.com
          allocationMode: ExactCount
          count: 1
      config:
      - requests: ["neurons"]
        opaque:
          driver: neuron.aws.com
          parameters:
            apiVersion: neuron.aws.com/v1
            kind: NeuronConfig
            logicalNeuronCore: 1
---
apiVersion: v1
kind: Pod
metadata:
  name: lnc-test
spec:
  nodeSelector:
    node.kubernetes.io/instance-type: trn2.48xlarge
  containers:
  - name: app
    image: public.ecr.aws/g4h4h0b5/neuron-monitor:1.0.0
    command: ["sleep", "300"]
    resources:
      claims:
      - name: neurons
  resourceClaims:
  - name: neurons
    resourceClaimTemplateName: neurons-lnc-1
```

```bash
kubectl apply -f lnc-config.yaml
# 等待 Pod Running（镜像较大，首次拉取约 1-2 分钟）
kubectl exec lnc-test -- neuron-ls
```

```
+--------+--------+--------+-------------+---------+
| NEURON | NEURON | NEURON |  CONNECTED  |   PCI   |
| DEVICE | CORES  | MEMORY |   DEVICES   |   BDF   |
+--------+--------+--------+-------------+---------+
| 0      | 2      | 32 GB  | 12, 1, 3, 4 | cc:00.0 |
+--------+--------+--------+-------------+---------+
```

LNC=1 配置生效，Pod 获得 1 个 Neuron 设备，2 个 NeuronCore，32GB 内存。不同的训练和推理工作负载可以在同一节点上使用不同的 LNC 设置，无需重建节点组。

### Step 7: CEL 属性过滤

```yaml
# cel-filter.yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: cel-filter-neurons
spec:
  spec:
    devices:
      requests:
      - name: neurons
        exactly:
          deviceClassName: neuron.aws.com
          allocationMode: ExactCount
          count: 1
          selectors:
          - cel:
              expression: "device.attributes['neuron.aws.com'].instanceType == 'trn2.48xlarge'"
---
apiVersion: v1
kind: Pod
metadata:
  name: cel-filter-test
spec:
  containers:
  - name: app
    image: public.ecr.aws/amazonlinux/amazonlinux:2023
    command: ["sleep", "60"]
    resources:
      claims:
      - name: neurons
  resourceClaims:
  - name: neurons
    resourceClaimTemplateName: cel-filter-neurons
```

CEL 表达式可以使用 ResourceSlice 中的任何属性。在混合实例类型的集群中（如 trn2 + inf2 节点），这让你能精确指定工作负载运行在哪种设备上。

### Step 8: 边界测试

**超额请求**——请求 17 个设备（节点只有 16 个）：

```bash
# Pod 将保持 Pending 状态
kubectl get events --field-selector reason=FailedScheduling
```

```
Warning  FailedScheduling  pod/overrequest  0/3 nodes are available: 1 timed out trying to allocate devices...
```

**不存在的 instanceType 过滤**——CEL 表达式指定不存在的实例类型：

```
Warning  FailedScheduling  pod/wrong-type  0/3 nodes are available: 3 cannot allocate all claims.
```

两个边界场景都按预期处理：Pod 保持 Pending，事件清晰说明原因。

## 踩坑记录

!!! warning "LNC 参数名是 `logicalNeuronCore` 不是 `logicalNeuronCoreCount`"
    opaque parameters 中的字段名必须严格匹配。如果写成 `logicalNeuronCoreCount`，Pod 会卡在 ContainerCreating 状态，kubelet 报 `strict decoding error: unknown field`。错误信息不太直观——你需要查看 `kubectl describe pod` 的 Events 才能看到具体的 decoding error。

!!! warning "Helm chart 需要手动创建 `neuron-healthcheck-system` namespace"
    Neuron Helm chart v1.5.0 引用了 `neuron-healthcheck-system` namespace 但不会自动创建它。如果不预先创建，`helm install` 会失败。

!!! warning "trn2.48xlarge On-Demand 容量极度稀缺"
    在 us-east-2 的三个 AZ 中，On-Demand 全部 `InsufficientInstanceCapacity`。**Spot 实例可以解决**——us-east-2c 有稳定的 Spot 容量，当前价格约 $8.60/hr。对于测试/Lab 场景，Spot 是更现实的选择。

!!! warning "DRA driver 与 device plugin 不能共存"
    两者不能在同一节点上同时运行（参考 [KEP-5004](https://github.com/kubernetes/enhancements/issues/5004)）。迁移时需要先在目标节点上卸载 device plugin，再安装 DRA driver。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EKS 集群 | $0.10/hr | ~8hr | $0.80 |
| trn2.48xlarge (Spot) | ~$8.60/hr | ~2hr | ~$17.20 |
| m5.large × 2 (system) | $0.096/hr × 2 | ~8hr | $1.54 |
| EBS + 网络 | 杂项 | - | ~$2.00 |
| **合计** | | | **~$21.54** |

## 清理资源

```bash
# 方法 1：eksctl 一键清理（推荐，处理所有关联资源）
eksctl delete cluster --name neuron-dra-test --region us-east-2 --wait

# 方法 2：手动逐步清理
# 1. 删除测试 Pods 和 ResourceClaims
kubectl delete pod --all
kubectl delete resourceclaimtemplate --all
kubectl delete resourceclaim --all

# 2. 卸载 DRA driver
helm uninstall neuron-dra-driver -n neuron-dra-driver
kubectl delete namespace neuron-dra-driver neuron-healthcheck-system

# 3. 删除节点组
aws eks delete-nodegroup --cluster-name neuron-dra-test \
  --nodegroup-name trn2-spot --region us-east-2

# 4. 等待节点组删除完成后删除集群
aws eks delete-cluster --name neuron-dra-test --region us-east-2
```

!!! danger "务必清理"
    trn2.48xlarge 即使是 Spot 也是 ~$8.60/hr。Lab 完成后请立即清理，或至少先删除 trn2 节点组。

## 结论与建议

### 什么时候用 DRA Driver？

- ✅ **新部署 + EKS 1.34+** — 没有历史包袱，直接上 DRA
- ✅ **需要拓扑感知调度** — 替代 Scheduler Extension，原生支持连接设备子集
- ✅ **混合实例类型集群** — CEL 表达式精确控制设备分配
- ✅ **不同工作负载需要不同 LNC** — per-workload 配置，无需多个节点组

### 什么时候继续用 Device Plugin？

- ❌ 使用 Karpenter 或 EKS Auto Mode
- ❌ 使用 Bottlerocket AMI
- ❌ EKS 版本低于 1.34
- ❌ 简单场景（只需要按数量分配设备）

### 生产环境建议

1. **迁移策略**：由于 DRA driver 和 device plugin 不能共存，建议按节点组逐步迁移。新节点组用 DRA，旧的保持 device plugin
2. **Spot 与 On-Demand**：trn2 On-Demand 容量紧张，生产环境建议 Reserved Instances 或 Savings Plans。测试环境用 Spot
3. **监控 ResourceSlice**：把 `kubectl get resourceslice` 纳入集群监控，确保设备发现正常
4. **LNC 测试**：生产使用 LNC 前，先在测试环境验证你的模型在 LNC=1 vs LNC=2 下的性能差异

## 参考链接

- [Manage Neuron devices on Amazon EKS](https://docs.aws.amazon.com/eks/latest/userguide/device-management-neuron.html)
- [Neuron DRA Driver Documentation](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/containers/neuron-dra.html)
- [What's New: Neuron EKS DRA Support](https://aws.amazon.com/about-aws/whats-new/2026/03/neuron-eks-dra-support/)
- [KEP-5004: DRA and Device Plugin coexistence](https://github.com/kubernetes/enhancements/issues/5004)
- [Kubernetes DRA Documentation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
