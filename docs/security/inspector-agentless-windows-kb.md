# Amazon Inspector Agentless 扫描与 Windows KB Findings 实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $0.20（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

Windows 服务器的漏洞管理是企业安全运维的核心痛点之一。传统方式下，Amazon Inspector 对每个 CVE 生成独立的 finding，一台 Windows Server 2022 实例可能同时有几十个 CVE findings，运维人员需要逐个分析才能确定需要安装哪个补丁。

2026 年 3 月，Amazon Inspector 发布了两项重要更新：

1. **Agentless EC2 扫描范围扩展**：新增对 WordPress、Apache HTTP Server、Python packages、Ruby gems 等应用软件包的检测，无需安装 SSM Agent
2. **Windows KB-based Findings**：将同一 Microsoft 补丁覆盖的多个 CVE 合并为单个 KB finding，直接告诉你需要安装哪个补丁

本文通过实测验证 KB-based findings 的实际效果，对比 KB 与 CVE findings 的差异，并演示如何通过 CLI 管理这些 findings。

## 前置条件

- AWS 账号（需要 Inspector、EC2、IAM、SSM 权限）
- AWS CLI v2 已配置
- 了解 Amazon Inspector 基本概念

## 核心概念

### KB-based Findings vs CVE-based Findings

| 对比维度 | CVE-based Findings | KB-based Findings |
|---------|-------------------|-------------------|
| **粒度** | 每个 CVE 一条 finding | 每个 Microsoft 补丁 (KB) 一条 finding |
| **示例** | CVE-2024-43608, CVE-2024-43607... | KB5044281 |
| **CVSS 分数** | 单个 CVE 的分数 | 所有关联 CVE 中的**最高**分数 |
| **行动指引** | 需要自行查找对应补丁 | 直接链接到 Microsoft KB 文章 |
| **来源标识** | source: `NVD` | source: `WINDOWS_SERVER_2022` |
| **适用范围** | 所有平台 | 仅 Windows EC2 实例 |

### Agent-based vs Agentless 扫描

| 对比维度 | Agent-based | Agentless |
|---------|------------|-----------|
| **依赖** | SSM Agent + Instance Profile | 无需任何 Agent |
| **原理** | SSM 收集软件 inventory | 通过 EBS 快照分析 |
| **扫描频率** | 持续（SSM inventory 30 分钟同步） | 周期性（首次可能需要数小时） |
| **出结果速度** | ~8 分钟 | 数小时到 24 小时 |
| **扫描范围** | 默认路径 + 自定义路径 | 所有可用路径 |
| **限制** | 实例需联网 + SSM 权限 | 卷数 <8，总大小 ≤120GB |

## 动手实践

### Step 1: 启用 Amazon Inspector EC2 扫描

```bash
# 启用 EC2 扫描
aws inspector2 enable \
  --resource-types EC2 \
  --region us-east-1

# 确认启用状态
aws inspector2 batch-get-account-status \
  --region us-east-1 \
  --query 'accounts[0].resourceState.ec2.status'
# 输出: "ENABLED"
```

### Step 2: 配置 Hybrid 扫描模式

Hybrid 模式同时启用 agent-based 和 agentless 扫描，确保覆盖所有实例：

```bash
# 设置为 Hybrid 扫描模式（启用 agentless）
aws inspector2 update-configuration \
  --ec2-configuration '{"scanMode": "EC2_HYBRID"}' \
  --region us-east-1

# 验证扫描模式
aws inspector2 get-configuration \
  --region us-east-1 \
  --query 'ec2Configuration.scanModeState'
# 输出: {"scanMode": "EC2_HYBRID", "scanModeStatus": "SUCCESS"}
```

启用后 Inspector 会自动创建以下资源：

- 2 个 Service-linked Roles（Inspector + Inspector Agentless）
- 5 个 SSM Associations（inventory 收集 + Windows/Linux plugin）

### Step 3: 创建测试安全组

```bash
# 创建仅出站的安全组（无任何入站规则）
SG_ID=$(aws ec2 create-security-group \
  --group-name inspector-test-sg \
  --description 'Inspector test - egress only' \
  --vpc-id <your-vpc-id> \
  --region us-east-1 \
  --query 'GroupId' --output text)

# 替换默认全出站规则为仅 HTTPS
aws ec2 revoke-security-group-egress \
  --group-id $SG_ID \
  --protocol all --port all --cidr 0.0.0.0/0 \
  --region us-east-1

aws ec2 authorize-security-group-egress \
  --group-id $SG_ID \
  --protocol tcp --port 443 --cidr 0.0.0.0/0 \
  --region us-east-1
```

### Step 4: 创建 IAM Instance Profile

Agent-based 扫描需要 SSM 权限：

```bash
# 创建 IAM Role
aws iam create-role \
  --role-name inspector-test-ec2-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 附加 SSM 管理策略
aws iam attach-role-policy \
  --role-name inspector-test-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# 创建并关联 Instance Profile
aws iam create-instance-profile \
  --instance-profile-name inspector-test-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name inspector-test-profile \
  --role-name inspector-test-ec2-role
```

### Step 5: 启动 Windows Server 2022 测试实例

```bash
# 查找最新的 Windows Server 2022 AMI
AMI_ID=$(aws ec2 describe-images \
  --owners amazon \
  --filters 'Name=name,Values=Windows_Server-2022-English-Full-Base-*' \
            'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
  --output text \
  --region us-east-1)

echo "Using AMI: $AMI_ID"

# 启动实例（带 SSM 权限，用于 agent-based 扫描验证）
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type t3.small \
  --security-group-ids $SG_ID \
  --subnet-id <your-subnet-id> \
  --associate-public-ip-address \
  --iam-instance-profile Name=inspector-test-profile \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=inspector-test-windows}]' \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance ID: $INSTANCE_ID"
```

### Step 6: 等待扫描完成并查看结果

SSM Agent 注册和首次扫描约需 5-10 分钟：

```bash
# 检查 SSM Agent 状态
aws ssm describe-instance-information \
  --region us-east-1 \
  --query 'InstanceInformationList[?InstanceId==`'$INSTANCE_ID'`].{
    PingStatus:PingStatus,
    Platform:PlatformType,
    AgentVersion:AgentVersion}'

# 检查 Inspector 覆盖状态
aws inspector2 list-coverage \
  --region us-east-1 \
  --query 'coveredResources[?resourceId==`'$INSTANCE_ID'`].{
    Status:scanStatus.statusCode,
    Reason:scanStatus.reason,
    Mode:scanMode,
    LastScanned:lastScannedAt}'
```

当 `Status` 变为 `ACTIVE` 后，查看 findings：

```bash
# 查看所有 findings 的严重性分布
aws inspector2 list-findings \
  --region us-east-1 \
  --query '{
    Total: length(findings),
    Critical: length(findings[?severity==`CRITICAL`]),
    High: length(findings[?severity==`HIGH`]),
    Medium: length(findings[?severity==`MEDIUM`]),
    Low: length(findings[?severity==`LOW`])}'
```

### Step 7: 筛选 KB-based Findings

```bash
# 按 title 前缀过滤 KB findings
aws inspector2 list-findings \
  --filter-criteria '{"title":[{"comparison":"PREFIX","value":"KB"}]}' \
  --region us-east-1 \
  --query 'findings[*].{
    Title:title,
    Severity:severity,
    Score:inspectorScore,
    RelatedCVEs:length(packageVulnerabilityDetails.relatedVulnerabilities),
    Source:packageVulnerabilityDetails.source,
    FixAvailable:fixAvailable}'
```

## 测试结果

### Findings 统计（Windows Server 2022，2026-03-25 实测）

| 类别 | 数量 | 说明 |
|------|------|------|
| **KB-based findings** | 1 | KB5044281 (2024-10 安全更新) |
| **CVE-based findings** | 31 | 主要为 Chrome/Edge 相关漏洞 |
| **总计** | 32 | |

### 严重性分布

| 严重性 | 数量 | 示例 |
|--------|------|------|
| CRITICAL | 2 | KB5044281 (CVSS 9.0) |
| HIGH | 17 | CVE-2026-3918 (Chrome, CVSS 8.8) |
| MEDIUM | 12 | CVE-2026-3923 (Chrome, CVSS 6.5) |
| LOW | 1 | CVE-2026-3929 (Chrome, CVSS 3.1) |

### KB5044281 Finding 详情

这是本次实测的核心发现 — 一个 KB finding 替代了 68 个独立 CVE findings：

| 属性 | 值 |
|------|-----|
| **KB ID** | KB5044281 |
| **描述** | Windows Server 2022 Security Update (2024-10) |
| **严重性** | CRITICAL |
| **CVSS 分数** | 9.0 (CVSS:3.1/AV:A/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H) |
| **关联 CVE 数量** | 68 个 |
| **补丁链接** | [support.microsoft.com/help/5044281](https://support.microsoft.com/help/5044281) |
| **Microsoft Catalog** | [catalog.update.microsoft.com](https://catalog.update.microsoft.com/v7/site/Search.aspx?q=KB5044281) |

**关键价值**：运维人员无需逐个分析 68 个 CVE，直接根据 KB5044281 安装对应补丁即可。

### KB Findings 与 CVE Findings 的共存关系

实测发现一个重要细节：**KB findings 和 CVE findings 在同一实例上共存**。

- **Windows OS 漏洞** → KB-based finding (source: `WINDOWS_SERVER_2022`)
- **第三方软件漏洞** (Chrome/Edge 等) → CVE-based finding (source: `NVD`)

这意味着 KB findings 只合并 Windows OS 级别的漏洞，第三方软件漏洞仍然以 CVE 形式报告。

## 踩坑记录

!!! warning "Agentless 首次扫描延迟"
    启用 Hybrid 模式后，agentless 扫描不会立即触发。实测 30+ 分钟仍未看到 EBS 快照创建（`InspectorScan` 标签）。官方文档未明确说明首次扫描的延迟时间，推测可能需要数小时到 24 小时。
    
    **建议**：如果需要快速获取 findings，优先使用 agent-based 扫描（配置 SSM Agent），agentless 作为补充覆盖。

!!! warning "Coverage 状态显示"
    即使已启用 Hybrid 模式，`list-coverage` API 中 `scanMode` 字段可能仍显示 `EC2_SSM_AGENT_BASED`，而非 `EC2_HYBRID`。这是因为该字段反映的是**实际使用的扫描方式**，不是账号级别的配置。未被 agentless 扫描过的实例会显示 agent-based。
    
    实测发现，官方未记录此行为。

!!! warning "SSM Agent 网络要求"
    Agent-based 扫描需要实例能访问 SSM 端点（HTTPS 443）。如果实例在私有子网且无 NAT Gateway 或 VPC Endpoint，SSM Agent 无法注册，Inspector 会将实例标记为 `UNMANAGED_EC2_INSTANCE`。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.small (Windows) | $0.0208/hr | ~1 hr | $0.02 |
| EC2 t3.micro (Linux) | $0.0104/hr | ~1 hr | $0.01 |
| EBS gp3 30GB × 2 | $0.08/GB/月 | ~1 hr | $0.01 |
| Inspector EC2 扫描 | $1.258/实例/月 | 免费试用期 | $0.00 |
| **合计** | | | **~$0.04** |

!!! tip "Inspector 免费试用"
    Amazon Inspector 提供 15 天免费试用期（每种扫描类型独立计算），适合用于评估和 Lab 练习。

## 清理资源

```bash
# 1. 终止 EC2 实例
aws ec2 terminate-instances \
  --instance-ids <instance-id-1> <instance-id-2> \
  --region us-east-1

# 等待实例终止
aws ec2 wait instance-terminated \
  --instance-ids <instance-id-1> <instance-id-2> \
  --region us-east-1

# 2. 禁用 Inspector EC2 扫描
aws inspector2 disable --resource-types EC2 \
  --region us-east-1

# 3. 清理 IAM 资源
aws iam remove-role-from-instance-profile \
  --instance-profile-name inspector-test-profile \
  --role-name inspector-test-ec2-role

aws iam delete-instance-profile \
  --instance-profile-name inspector-test-profile

aws iam detach-role-policy \
  --role-name inspector-test-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam delete-role \
  --role-name inspector-test-ec2-role

# 4. 检查安全组是否有残留 ENI
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=<sg-id> \
  --region us-east-1 \
  --query 'NetworkInterfaces[*].{Id:NetworkInterfaceId,Status:Status}'

# 确认无残留后删除安全组
aws ec2 delete-security-group \
  --group-id <sg-id> \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 Inspector 有 15 天免费试用，但 EC2 实例和 EBS 卷会持续产生费用。

## 结论与建议

### 核心发现

1. **KB-based findings 显著简化 Windows 补丁管理**：一个 KB finding（KB5044281）替代了 68 个独立 CVE findings，直接提供补丁链接
2. **KB 与 CVE findings 共存**：Windows OS 漏洞用 KB findings，第三方软件漏洞（Chrome 等）仍用 CVE findings
3. **Agent-based 扫描显著更快**：约 8 分钟出结果，而 agentless 首次扫描可能需要数小时
4. **Agentless 覆盖更广**：不需要 SSM Agent，适合无法安装 Agent 的实例

### 生产环境建议

- **推荐 Hybrid 模式**：兼顾速度（agent-based）和覆盖范围（agentless）
- **Windows 补丁管理**：结合 KB findings + AWS Systems Manager Patch Manager，实现自动化补丁部署
- **告警策略**：优先关注 KB findings 中 CRITICAL/HIGH 的补丁（已是合并后的最高分数）
- **Agentless 适用场景**：遗留系统、第三方管理实例、安全要求不允许安装 Agent 的环境

## 参考链接

- [What's New: Amazon Inspector expands agentless EC2 scanning and introduces Windows KB-based findings](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-inspector-agentless-ec2-scanning-windows/)
- [Amazon Inspector - Scanning EC2 instances](https://docs.aws.amazon.com/inspector/latest/user/scanning-ec2.html)
- [Amazon Inspector - Finding types](https://docs.aws.amazon.com/inspector/latest/user/findings-types.html)
- [Amazon Inspector Pricing](https://aws.amazon.com/inspector/pricing/)
