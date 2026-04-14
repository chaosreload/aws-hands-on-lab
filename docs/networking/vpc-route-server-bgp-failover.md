---
tags:
  - Networking
---

# Amazon VPC Route Server 动态路由实战：BGP 自动 Failover + BFD 快速切换

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60-90 分钟
    - **预估费用**: ~$4（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

在 VPC 中部署网络虚拟设备（NVA）如防火墙、NAT 网关时，主备切换一直是个痛点：

- **传统方案**：写 Lambda + CloudWatch 监控设备健康，检测到故障后调 API 更新路由表。运维复杂，切换慢（分钟级）。
- **Overlay 方案**：使用 VXLAN 等隧道协议绕过 VPC 路由限制。引入额外复杂度，排查困难。

2025 年 4 月，AWS 发布了 **VPC Route Server**——一个 VPC 内的原生 BGP Route Reflector。网络设备通过标准 BGP 协议向 Route Server 宣告路由，Route Server 自动将最优路由安装到 VPC 路由表。设备故障时，BFD 秒级检测、路由自动切换，无需任何自定义脚本。

本文通过 6 个递进实验，完整验证 VPC Route Server 的核心能力。

## 前置条件

- AWS 账号（需要 EC2、VPC 完整权限）
- AWS CLI v2 已配置
- 对 BGP 有基本了解（ASN、Peer、MED 概念）

## 核心概念

### 组件架构

```
Route Server (ASN 65000)
├── Association → VPC
├── Endpoint → 子网内的 BGP 邻居（AWS 托管 ENI）
├── Propagation → 指定哪些路由表接收动态路由
└── Peer → 与网络设备的 BGP 会话配置
```

| 组件 | 作用 | 关键参数 |
|------|------|---------|
| **Route Server** | 核心控制面，维护 RIB/FIB | ASN、Persist Routes |
| **Endpoint** | 子网内的 BGP 邻居 | 自动分配 IP，按小时计费 |
| **Propagation** | 将 FIB 路由安装到路由表 | 支持 VPC/Subnet/IGW 路由表 |
| **Peer** | BGP 会话配置 | 对端 ASN、BFD/BGP-Keepalive |

### 路由选择逻辑

Route Server 使用标准 BGP 路径选择算法。对于相同前缀的多条路由：

1. **MED（Multi-Exit Discriminator）值越低越优先** — MED=0 的设备是主设备
2. 最优路由进入 FIB → 安装到路由表（`in-fib`）
3. 备选路由留在 RIB（`in-rib`），主设备故障时自动提升

### 故障检测方式对比

| 方式 | 原理 | 检测速度 |
|------|------|---------|
| **BGP Keepalive** | BGP hold-timer 超时（默认 90s） | 慢（30-90s） |
| **BFD** | 专用轻量协议，毫秒级探测 | 快（亚秒级检测） |

## 动手实践

### 架构图

```
VPC 10.0.0.0/16
└── Subnet 10.0.1.0/24
    ├── Device-A (FRR, ASN 65001, MED=0) — 主 ← 10.0.1.10
    ├── Device-B (FRR, ASN 65002, MED=100) — 备 ← 10.0.1.20
    └── Route Server Endpoint (ASN 65000) ← 10.0.1.78
        └── Propagation → Subnet Route Table
```

两台 EC2 运行 FRRouting，分别以不同 ASN 和 MED 值向 Route Server 宣告同一前缀 `192.168.0.0/24`。

### Step 1: 创建 VPC 和基础网络

```bash
# 创建 VPC
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 \
  --region us-east-1 --query 'Vpc.VpcId' --output text)

# 创建子网
SUBNET_ID=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.0.1.0/24 --availability-zone us-east-1a \
  --region us-east-1 --query 'Subnet.SubnetId' --output text)

# 创建安全组（VPC 内部通信 + BGP + BFD）
SG_ID=$(aws ec2 create-security-group --group-name vpc-rs-lab \
  --description "Route Server Lab SG" --vpc-id $VPC_ID \
  --region us-east-1 --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol tcp --port 179 --cidr 10.0.0.0/16 --region us-east-1   # BGP
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol udp --port 3784 --cidr 10.0.0.0/16 --region us-east-1  # BFD Control
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol udp --port 3785 --cidr 10.0.0.0/16 --region us-east-1  # BFD Echo
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol icmp --port -1 --cidr 10.0.0.0/16 --region us-east-1   # ICMP
```

### Step 2: 创建 Route Server

```bash
# 创建 Route Server（ASN 65000）
RS_ID=$(aws ec2 create-route-server --amazon-side-asn 65000 \
  --persist-routes enable --persist-routes-duration 1 \
  --sns-notifications-enabled \
  --region us-east-1 --query 'RouteServer.RouteServerId' --output text)

# 等待 available
aws ec2 describe-route-servers --route-server-ids $RS_ID \
  --region us-east-1 --query 'RouteServers[0].State'

# 关联 VPC
aws ec2 create-route-server-association --route-server-id $RS_ID \
  --vpc-id $VPC_ID --region us-east-1

# 创建 Endpoint（自动获取 IP）
RSE_ID=$(aws ec2 create-route-server-endpoint \
  --route-server-id $RS_ID --subnet-id $SUBNET_ID \
  --region us-east-1 \
  --query 'RouteServerEndpoint.RouteServerEndpointId' --output text)

# 设置路由传播到子网路由表
RTB_ID=$(aws ec2 describe-route-tables \
  --filters "Name=association.subnet-id,Values=$SUBNET_ID" \
  --region us-east-1 --query 'RouteTables[0].RouteTableId' --output text)

aws ec2 create-route-server-propagation --route-server-id $RS_ID \
  --route-table-id $RTB_ID --region us-east-1
```

### Step 3: 部署 EC2 + FRRouting

```bash
# 创建两台 EC2（AL2023, t3.micro）
# Device-A: 10.0.1.10, Device-B: 10.0.1.20
# 使用 SSM 管理，无需 SSH 入站规则

# 安装 FRRouting（AL2023 需要 rpm --nodeps 绕过依赖）
sudo rpm --nodeps -ivh https://rpm.frrouting.org/repo/frr-stable-repo-1-0.el9.noarch.rpm
sudo dnf install -y frr frr-pythontools libatomic --nobest --skip-broken
```

!!! warning "AL2023 + FRR 兼容性"
    AL2023 的 `libjson-c.so.5` 缺少 `JSONC_0.14` 符号版本，需要 `rpm --nodeps` 强制安装。运行时会有 warning 但不影响功能。

启用 BGP 和 BFD 守护进程：

```bash
sudo sed -i 's/bgpd=no/bgpd=yes/' /etc/frr/daemons
sudo sed -i 's/bfdd=no/bfdd=yes/' /etc/frr/daemons
sudo systemctl start frr
```

### Step 4: 配置 BGP 对等

**Device-A（主设备，MED=0）**:

```bash
vtysh -c "configure terminal" \
  -c "route-map SET-MED permit 10" \
  -c "set metric 0" \
  -c "exit" \
  -c "ip route 192.168.0.0/24 Null0" \
  -c "router bgp 65001" \
  -c "bgp router-id 10.0.1.10" \
  -c "no bgp ebgp-requires-policy" \
  -c "neighbor 10.0.1.78 remote-as 65000" \
  -c "neighbor 10.0.1.78 bfd" \
  -c "address-family ipv4 unicast" \
  -c "network 192.168.0.0/24" \
  -c "neighbor 10.0.1.78 route-map SET-MED out" \
  -c "exit-address-family" \
  -c "exit" \
  -c "exit" \
  -c "write memory"
```

**Device-B（备设备，MED=100）**:

```bash
vtysh -c "configure terminal" \
  -c "route-map SET-MED permit 10" \
  -c "set metric 100" \
  -c "exit" \
  -c "ip route 192.168.0.0/24 Null0" \
  -c "router bgp 65002" \
  -c "bgp router-id 10.0.1.20" \
  -c "no bgp ebgp-requires-policy" \
  -c "neighbor 10.0.1.78 remote-as 65000" \
  -c "address-family ipv4 unicast" \
  -c "network 192.168.0.0/24" \
  -c "neighbor 10.0.1.78 route-map SET-MED out" \
  -c "exit-address-family" \
  -c "exit" \
  -c "exit" \
  -c "write memory"
```

创建 Route Server Peers：

```bash
# Device-A: BFD 模式（快速检测）
aws ec2 create-route-server-peer \
  --route-server-endpoint-id $RSE_ID \
  --peer-address 10.0.1.10 \
  --bgp-options PeerAsn=65001,PeerLivenessDetection=bfd \
  --region us-east-1

# Device-B: BGP Keepalive 模式（标准检测）
aws ec2 create-route-server-peer \
  --route-server-endpoint-id $RSE_ID \
  --peer-address 10.0.1.20 \
  --bgp-options PeerAsn=65002,PeerLivenessDetection=bgp-keepalive \
  --region us-east-1
```

## 测试结果

### 实验 1: BGP 路由自动传播

两台设备 BGP Established 后，查看 Route Server 路由数据库：

```bash
aws ec2 get-route-server-routing-database \
  --route-server-id $RS_ID --region us-east-1
```

```
+-----+-------------+------------------+---------+
| MED |   NextHop   |     Prefix       | Status  |
+-----+-------------+------------------+---------+
|  0  |  10.0.1.10  |  192.168.0.0/24  |  in-fib |  ← 主（已安装到路由表）
|  100|  10.0.1.20  |  192.168.0.0/24  |  in-rib |  ← 备（仅在 RIB 中）
+-----+-------------+------------------+---------+
```

VPC 路由表自动出现：

```
192.168.0.0/24 → eni-xxx (10.0.1.10)  Origin: Advertisement
```

**结论**：Route Server 根据 MED 值自动选择最优路由，无需手动配置路由表。

### 实验 2: MED 主备切换

- Device-A (MED=0) 的路由状态为 `in-fib`，已安装到路由表
- Device-B (MED=100) 的路由状态为 `in-rib`，作为备选
- VPC 路由表中 `192.168.0.0/24` 的 next-hop 指向 Device-A

**结论**：MED 属性正确控制路由优先级，低 MED 值的设备成为主路径。

### 实验 3 & 4: 故障切换速度对比

这是本文的核心实验。分别测试两种故障检测方式的切换速度：

| 检测方式 | 操作 | 切换耗时 | 说明 |
|---------|------|---------|------|
| **BGP Keepalive** | 停止 Device-A FRR | **~33 秒** | 等待 BGP hold-timer 超时 |
| **BFD** | 停止 Device-A FRR | **~5-7 秒** | BFD 亚秒检测 + Route Server 处理 |

切换过程中，Route Server 自动：

1. 检测到 Device-A 不可达
2. 从 RIB 撤回 Device-A 的路由
3. 将 Device-B (MED=100) 从 `in-rib` 提升为 `in-fib`
4. 更新 VPC 路由表 next-hop 到 Device-B

**结论**：BFD 比 BGP Keepalive 快 5-6 倍。生产环境强烈建议启用 BFD。

### 实验 5: Persist Routes

启用 `persist-routes-duration=1`，同时停止两台设备的 FRR：

```bash
aws ec2 modify-route-server --route-server-id $RS_ID \
  --persist-routes enable --persist-routes-duration 1 \
  --region us-east-1
```

结果：

- `AreRoutesPersisted: true` — 路由进入持久化状态
- VPC 路由表中的路由**持续存在**，所有 BGP 断开期间不会消失
- 路由保持了 4+ 分钟（整个 BGP 断开期间）

!!! warning "Persist Routes Duration 的真正含义"
    **容易误解**：`persist-routes-duration=1` 不是"BGP 断开后路由保持 1 分钟"。
    
    **正确理解**：路由在所有 BGP 断开期间**无限期保持**。`duration` 是 BGP **重新建立后**的等待时间——给网络设备重新学习路由的缓冲期。1 分钟后 Route Server 恢复正常功能（用新收到的路由替换持久化路由）。

### 实验 6: BGP 恢复

重启两台设备的 FRR 后：

- BGP 会话自动重建
- Device-A (MED=0) 恢复为 `in-fib`（主路由）
- Device-B (MED=100) 回到 `in-rib`（备用）
- `AreRoutesPersisted: false` — 退出持久化模式

**结论**：故障恢复完全自动，路由优先级正确恢复。

## 踩坑记录

!!! warning "踩坑 1: RSE 安全组缺少 BFD Echo 端口"
    创建 BFD 模式的 Peer 时，AWS 自动为 RSE 的安全组添加 UDP 3784（BFD Control）入站规则，但**不会自动添加 UDP 3785（BFD Echo）**。需要手动添加，否则 BFD 无法建立。
    
    *实测发现，官方文档未明确记录。*

!!! warning "踩坑 2: AL2023 安装 FRRouting 需要 --nodeps"
    FRR 的 el9 RPM 要求 `redhat-release >= 9` 和特定版本的 `libjson-c.so.5`。AL2023 两者都不满足，需要 `rpm --nodeps` 强制安装。运行时有 `no version information available` warning，但 BGP/BFD 功能正常。
    
    *已查文档确认：FRR 官方不提供 AL2023 专用包。*

!!! warning "踩坑 3: Peer Liveness Detection 创建后不可修改"
    Route Server Peer 的 `PeerLivenessDetection`（BFD 或 BGP-Keepalive）在创建时指定，**不能通过 modify 修改**。要切换检测方式，必须删除重建 Peer。
    
    *已查 API 文档确认：ModifyRouteServerPeer 不支持修改 BgpOptions。*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Route Server Endpoint | $0.75/hr | ~4 hr | $3.00 |
| EC2 t3.micro × 2 | $0.0104/hr × 2 | ~4 hr | $0.08 |
| Route Server | 免费 | - | $0.00 |
| **合计** | | | **~$3.08** |

## 清理资源

```bash
# 1. 删除 Peers
aws ec2 delete-route-server-peer --route-server-peer-id <peer-a-id> --region us-east-1
aws ec2 delete-route-server-peer --route-server-peer-id <peer-b-id> --region us-east-1

# 2. 删除 Propagation
aws ec2 delete-route-server-propagation --route-server-id <rs-id> \
  --route-table-id <rtb-id> --region us-east-1

# 3. 删除 Endpoint（等待删除完成）
aws ec2 delete-route-server-endpoint --route-server-endpoint-id <rse-id> --region us-east-1

# 4. 删除 Association
aws ec2 delete-route-server-association --route-server-id <rs-id> \
  --vpc-id <vpc-id> --region us-east-1

# 5. 删除 Route Server
aws ec2 delete-route-server --route-server-id <rs-id> --region us-east-1

# 6. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids <device-a-id> <device-b-id> --region us-east-1

# 7. 检查 ENI 残留后删除安全组
aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=<sg-id>" --region us-east-1
aws ec2 delete-security-group --group-id <sg-id> --region us-east-1

# 8. 删除子网、IGW、VPC
aws ec2 delete-subnet --subnet-id <subnet-id> --region us-east-1
aws ec2 detach-internet-gateway --internet-gateway-id <igw-id> --vpc-id <vpc-id> --region us-east-1
aws ec2 delete-internet-gateway --internet-gateway-id <igw-id> --region us-east-1
aws ec2 delete-vpc --vpc-id <vpc-id> --region us-east-1
```

!!! danger "务必清理"
    Route Server Endpoint 按小时计费（$0.75/hr），Lab 完成后请立即清理。

## 结论与建议

### 适用场景

- **VPC 内防火墙主备切换** — 替代 Lambda + CloudWatch 方案
- **SD-WAN 集成** — 第三方网络设备与 VPC 路由表原生对接
- **多路径负载分担** — 配合 ECMP（需要 equal MED）

### 生产环境建议

1. **必须启用 BFD** — 切换速度差 5-6 倍，没有理由不用
2. **设置 Persist Routes** — 防止所有设备同时重启时流量黑洞（建议 duration=2-3 min）
3. **双 Endpoint 冗余** — 生产环境在不同 AZ 各部署一个 Endpoint
4. **SNS 通知** — 启用 BGP/BFD 状态变更通知，对接告警系统

### 限制

- 不支持 VGW 路由表（使用 Transit Gateway Connect 替代）
- 每个 VPC 最多 5 个 Route Server
- Peer 的检测模式创建后不可修改，需删除重建

## 参考链接

- [VPC Route Server 文档](https://docs.aws.amazon.com/vpc/latest/userguide/dynamic-routing-route-server.html)
- [How Amazon VPC Route Server works](https://docs.aws.amazon.com/vpc/latest/userguide/route-server-how-it-works.html)
- [API Reference: CreateRouteServer](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateRouteServer.html)
- [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-vpc-route-server/)
