# CloudWatch 跨区域启用规则实测：一条规则搞定全球 16 个 Region 的 VPC Flow Logs

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 40 分钟
    - **预估费用**: < $2（含清理）
    - **Region**: us-east-1（home） + us-west-2 / eu-west-1（spoke）
    - **最后验证**: 2026-04-17

## 背景

CloudWatch Telemetry Config 的 enablement rules 早先已发布，但有个明显短板：**规则是 Region 级别的**。一个中央安全团队要给全球 17 个 Region 的 VPC 都开启 Flow Logs，就得登 17 次 Console、跑 17 次 CLI，新开一个 Region 还得手动补。

2026 年 4 月，AWS 推出「**Cross-Region Telemetry Auditing and Enablement Rules**」直接把规则做成"home → spoke"复制模型：

- 一个 Region 作为 home，规则自动复制到其他 spoke Region
- `AllRegions=true` 一次覆盖所有支持 Region（新 Region opt-in 后自动扩展）
- Spoke 上不能编辑/删除，必须回 home（防止 drift）
- 复制行为走 `AwsServiceEvent`，全程有 CloudTrail 审计

我用 VPC Flow Logs 实测了这套跨区域复制机制，踩出 3 个官方文档没明说的坑。本文完整记录实验数据和用户体验陷阱，帮你在生产落地前避坑。

## 前置条件

- AWS 账号（需要 `observabilityadmin:*`、`ec2:*FlowLog*`、`logs:*` 权限）
- AWS CLI v2 已配置
- **注意**：home Region 和 spoke Region 都需要 `observabilityadmin:StartTelemetryEvaluation` 允许（spoke 管理员如果不启 evaluation，查不到 replica）

## 核心概念

### 术语速查

| 概念 | 含义 |
|------|------|
| **Home Region** | 创建规则的 Region，规则的唯一真源 |
| **Spoke Region** | 被复制到的 Region，只读视图 |
| **Replicated rule** | Spoke 上的规则副本（`IsReplicated=true`） |
| **RegionStatuses** | 每个 spoke 的状态列表：`{Region, Status, FailureReason, RuleArn}` |
| **AwsServiceEvent** | CloudTrail 事件类型，标记服务自身发起的复制动作 |

### API 新增字段

`create-telemetry-rule` 新增两个互斥字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `Regions` | list | 显式指定 spoke Region 列表 |
| `AllRegions` | bool | 覆盖所有 observabilityadmin 支持的默认 Region |

`get-telemetry-rule` 响应新增：

| 字段 | 视图 | 含义 |
|------|------|------|
| `HomeRegion` | 仅 spoke | 标识规则源 Region |
| `IsReplicated` | 仅 spoke | 标记为副本 |
| `RegionStatuses` | 仅 home | 每个 spoke 的状态和 ARN |

### 与单区域规则的差异对比

| 能力 | 单区域规则（旧） | 跨区域规则（本次） |
|------|------|------|
| Region 作用范围 | 1 个 | 1～N 个或"所有支持 Region" |
| 新 Region 自动加入 | ❌ 需手动 | ✅ `AllRegions=true` 可自动扩展 |
| Spoke 编辑 | N/A | ❌ 禁止，错误消息精确指向 home ARN |
| CloudTrail 审计 | 普通 API 事件 | 复制行为记录为 `AwsServiceEvent` |
| Tag 行为 | 单 Region | Home→spoke 单向复制；spoke 本地 tag 不回传 |

## 动手实践

### Step 1: 前置检查与环境准备

确保 home Region (us-east-1) 已经启用 telemetry evaluation：

```bash
aws observabilityadmin get-telemetry-evaluation-status \
  --region us-east-1
# { "Status": "RUNNING" }
```

如果是 NOT_STARTED：

```bash
aws observabilityadmin start-telemetry-evaluation --region us-east-1
```

**关键提示**：spoke Region **暂时不要**启用，我们要验证一个容易踩坑的场景。

### Step 2: 创建跨区域规则（显式 Regions 列表）

```bash
cat > /tmp/cross-region-rule.json <<'EOF'
{
  "ResourceType": "AWS::EC2::VPC",
  "TelemetryType": "Logs",
  "TelemetrySourceTypes": ["VPC_FLOW_LOGS"],
  "DestinationConfiguration": {
    "DestinationType": "cloud-watch-logs",
    "DestinationPattern": "/aws/vpc/xregion/<accountId>/<resourceId>",
    "RetentionInDays": 3,
    "VPCFlowLogParameters": {
      "LogFormat": "${version} ${vpc-id} ${account-id} ${action}",
      "TrafficType": "ALL",
      "MaxAggregationInterval": 600
    }
  },
  "Regions": ["us-west-2", "eu-west-1"]
}
EOF

aws observabilityadmin create-telemetry-rule \
  --rule-name xregion-vpc-flow-logs \
  --rule file:///tmp/cross-region-rule.json \
  --region us-east-1
```

返回：

```json
{
  "RuleArn": "arn:aws:observabilityadmin:us-east-1:595842667825:telemetry-rule/xregion-vpc-flow-logs"
}
```

立刻在 home 查状态（1 秒后）：

```bash
aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs --region us-east-1
```

**实测输出**：

```json
{
  "RuleName": "xregion-vpc-flow-logs",
  "TelemetryRule": { "Regions": ["eu-west-1", "us-west-2"] },
  "RegionStatuses": [
    { "Region": "eu-west-1", "Status": "ACTIVE", "RuleArn": "arn:aws:observabilityadmin:eu-west-1:..." },
    { "Region": "us-west-2", "Status": "ACTIVE", "RuleArn": "arn:aws:observabilityadmin:us-west-2:..." }
  ]
}
```

两个 spoke **1 秒内** ACTIVE。注意：home 视图不包含 `HomeRegion` 和 `IsReplicated` 字段。

### Step 3: 验证 spoke evaluation 未启用的陷阱

切到 spoke us-west-2 查 replica：

```bash
aws observabilityadmin list-telemetry-rules --region us-west-2
```

**实测输出**：

```
ValidationException: Telemetry evaluation is not enabled for the requester account
```

✋ 注意这个现象：**home 报 ACTIVE，但 spoke 完全查不到**。下文"踩坑 1"会展开。

启用 spoke evaluation：

```bash
aws observabilityadmin start-telemetry-evaluation --region us-west-2
sleep 10
aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs --region us-west-2
```

现在 spoke 视图出现了：

```json
{
  "RuleArn": "arn:aws:observabilityadmin:us-west-2:595842667825:telemetry-rule/xregion-vpc-flow-logs",
  "HomeRegion": "us-east-1",
  "IsReplicated": true,
  "TelemetryRule": { "DestinationConfiguration": { ... } }
}
```

**注意**：spoke 视图里 `TelemetryRule` 不含 `Regions` 和 `AllRegions` — 副本不应该知道同胞有谁。

### Step 4: 尝试在 spoke 编辑/删除 replica

```bash
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs \
  --region us-west-2
```

**实测输出**：

```
ValidationException: This rule is a replicated copy managed by the home region 'us-east-1'.
To modify this rule, call DeleteTelemetryRule in region 'us-east-1' using rule ARN
'arn:aws:observabilityadmin:us-east-1:595842667825:telemetry-rule/xregion-vpc-flow-logs'.
```

设计得很贴心 — 错误消息里直接告诉你 home Region 和 home ARN。`update-telemetry-rule` 报同样的错（只是动词换成 UpdateTelemetryRule）。

### Step 5: Tag 单向复制验证

先给 spoke 上的 replica 打本地 tag：

```bash
aws observabilityadmin tag-resource \
  --resource-arn arn:aws:observabilityadmin:us-west-2:595842667825:telemetry-rule/xregion-vpc-flow-logs \
  --tags SpokeLocal=yes,Environment=spoke-test \
  --region us-west-2
```

检查两边：

```bash
# spoke
aws observabilityadmin list-tags-for-resource \
  --resource-arn arn:aws:observabilityadmin:us-west-2:...:telemetry-rule/xregion-vpc-flow-logs \
  --region us-west-2
# { "Tags": { "Environment": "spoke-test", "SpokeLocal": "yes" } }

# home
aws observabilityadmin list-tags-for-resource \
  --resource-arn arn:aws:observabilityadmin:us-east-1:...:telemetry-rule/xregion-vpc-flow-logs \
  --region us-east-1
# { "Tags": {} }
```

Spoke 本地 tag **不回传** home。这个设计适合"spoke 团队给 replica 打本地成本 allocation 标签"的场景。

### Step 6: 实际跨区域 VPC Flow Logs 自动配置

在 home 和 spoke 各创建一个 VPC：

```bash
# us-east-1 VPC
aws ec2 create-vpc --cidr-block 10.99.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=xregion-test-use1}]' \
  --region us-east-1
# vpc-0320f0a69ff411a76

# us-west-2 VPC
aws ec2 create-vpc --cidr-block 10.98.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=xregion-test-usw2}]' \
  --region us-west-2
# vpc-02a3f81484672f1aa
```

等待自动发现。定期 poll：

```bash
aws ec2 describe-flow-logs \
  --filter Name=resource-id,Values=vpc-0320f0a69ff411a76 \
  --region us-east-1 --query 'FlowLogs[].[FlowLogId,LogGroupName,FlowLogStatus]' --output table
```

**实测生效时间线**（从 VPC 创建开始计时）：

| Region | 生效时间 | Flow Log ID | Log Group |
|--------|---------|-------------|-----------|
| us-east-1 (home) | **~2.5 min** | fl-00f8a7ed5c00f97b7 | /aws/vpc/xregion/595842667825/vpc-0320f0a69ff411a76 |
| us-west-2 (spoke) | **~4.5 min** | fl-057a69b4813f8ffd2 | /aws/vpc/xregion/595842667825/vpc-02a3f81484672f1aa |

Spoke 多出 2 分钟是"复制延迟 + AWS Config 发现"叠加的结果。远低于文档说的"最多 24 小时"。

### Step 7: Home 更新 → Spoke 同步速度测试

```bash
cat > /tmp/updated-rule.json <<'EOF'
{
  "ResourceType": "AWS::EC2::VPC",
  "TelemetryType": "Logs",
  "TelemetrySourceTypes": ["VPC_FLOW_LOGS"],
  "DestinationConfiguration": {
    "DestinationType": "cloud-watch-logs",
    "DestinationPattern": "/aws/vpc/xregion-v2/<accountId>/<resourceId>",
    "RetentionInDays": 7,
    "VPCFlowLogParameters": {
      "LogFormat": "${version} ${vpc-id} ${account-id} ${action}",
      "TrafficType": "ALL",
      "MaxAggregationInterval": 600
    }
  },
  "Regions": ["us-west-2", "eu-west-1"]
}
EOF

aws observabilityadmin update-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs \
  --rule file:///tmp/updated-rule.json \
  --region us-east-1

# 立即查 spoke
sleep 5
aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs \
  --region us-west-2 \
  --query 'TelemetryRule.DestinationConfiguration.DestinationPattern' --output text
# /aws/vpc/xregion-v2/<accountId>/<resourceId>
```

**5 秒内** spoke 已是新 pattern。实际日志仍会输出到老 log group（因为文档明确："update 只影响新资源"）。

### Step 8: CloudTrail AwsServiceEvent 审计

在 spoke us-west-2 查 CloudTrail，找 service 自己发起的事件：

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateTelemetryRule \
  --region us-west-2 \
  --query 'Events[?Username==null].[EventTime,EventId]' --output table
```

拿到事件 ID 后查完整 JSON：

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventId,AttributeValue=<EventId> \
  --region us-west-2 \
  --query 'Events[0].CloudTrailEvent' --output text | python3 -m json.tool
```

**实测输出**：

```json
{
  "eventVersion": "1.11",
  "userIdentity": {
    "accountId": "595842667825",
    "invokedBy": "observabilityadmin.amazonaws.com"
  },
  "eventTime": "2026-04-17T01:17:10Z",
  "eventSource": "observabilityadmin.amazonaws.com",
  "eventName": "CreateTelemetryRule",
  "awsRegion": "us-west-2",
  "sourceIPAddress": "observabilityadmin.amazonaws.com",
  "userAgent": "observabilityadmin.amazonaws.com",
  "resources": [{
    "accountId": "595842667825",
    "type": "AWS::ObservabilityAdmin::TelemetryRule",
    "ARN": "arn:aws:observabilityadmin:us-west-2:595842667825:telemetry-rule/xregion-vpc-flow-logs"
  }],
  "eventType": "AwsServiceEvent",
  "managementEvent": true
}
```

这是合规团队的金矿 — 可以做一个 EventBridge rule：

```json
{
  "source": ["aws.observabilityadmin"],
  "detail-type": ["AWS Service Event via CloudTrail"],
  "detail": {
    "eventSource": ["observabilityadmin.amazonaws.com"],
    "eventType": ["AwsServiceEvent"]
  }
}
```

触发到 Lambda，实时监控跨 Region 复制行为。

### Step 9: AllRegions=true 展开实测

```bash
cat > /tmp/allregions-rule.json <<'EOF'
{
  "ResourceType": "AWS::EC2::VPC",
  "TelemetryType": "Logs",
  "TelemetrySourceTypes": ["VPC_FLOW_LOGS"],
  "DestinationConfiguration": {
    "DestinationType": "cloud-watch-logs",
    "DestinationPattern": "/aws/vpc/allregions/<resourceId>",
    "RetentionInDays": 3,
    "VPCFlowLogParameters": {
      "LogFormat": "${version} ${vpc-id}",
      "TrafficType": "ALL",
      "MaxAggregationInterval": 600
    }
  },
  "AllRegions": true
}
EOF

aws observabilityadmin create-telemetry-rule \
  --rule-name xregion-allregions-test \
  --rule file:///tmp/allregions-rule.json \
  --region us-east-1
```

立刻查 `RegionStatuses`：

```bash
aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-allregions-test \
  --region us-east-1 \
  --query 'TelemetryRule.Regions' --output json
```

**实测输出**（16 个 Region）：

```json
[
  "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-south-1",
  "ap-southeast-1", "ap-southeast-2", "ca-central-1",
  "eu-central-1", "eu-north-1", "eu-west-1", "eu-west-2", "eu-west-3",
  "sa-east-1", "us-east-2", "us-west-1", "us-west-2"
]
```

⚠️ **重点**：`AllRegions=true` 实际只展开 **16 个 default-enabled Region**，**不含 opt-in Region**（如 `me-south-1`、`ap-east-1`、`af-south-1`、`il-central-1`、`me-central-1`、`ca-west-1` 等）。详见踩坑 2。

初始状态 14 个 PENDING，60 秒内全部变 ACTIVE：

```
aws observabilityadmin get-telemetry-rule ...
# 01:24:06 UTC — 14 PENDING + 2 ACTIVE
# 01:25:07 UTC — 16 ACTIVE
```

### Step 10: 删除 home → Spoke 自动清除

```bash
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier xregion-allregions-test --region us-east-1

sleep 5

# 两边查
aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-allregions-test --region us-east-1
# ResourceNotFoundException

aws observabilityadmin get-telemetry-rule \
  --rule-identifier xregion-allregions-test --region us-west-2
# ResourceNotFoundException
```

**5 秒内**两边清除。但注意 — **已配置的 Flow Logs 不会被删除**，仍保留（文档明确声明，也符合 #102 结论）。清理需要手动。

## 测试结果汇总

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | `Regions=[us-west-2, eu-west-1]` 创建 | ✅ | 1 秒 ACTIVE | 独立 ARN 每个 spoke |
| 2 | Spoke 未 evaluation 查 replica | ⚠️ 拒绝 | ValidationException | **踩坑 1** |
| 3 | Spoke delete/update replica | ✅ 预期拒绝 | 错误消息含 home ARN | 好设计 |
| 4 | Spoke 本地 tag | ✅ | home Tags={} | 单向复制 |
| 5 | VPC Flow Logs 自动配置 | ✅ | home 2.5min, spoke 4.5min | 远低于文档 24h |
| 6 | Home update → spoke 同步 | ✅ | ~5 秒 | 近实时 |
| 7 | CloudTrail AwsServiceEvent | ✅ | 与文档示例 100% 一致 | 合规可用 |
| 8 | AllRegions=true 展开 | ⚠️ | 16 Region，**不含 opt-in** | **踩坑 2** |
| 9 | 删除 home → spoke 同步清除 | ✅ | ~5 秒 | 但 Flow Logs 保留 |

## 踩坑记录

!!! warning "踩坑 1：Spoke 管理员看不到 replicated rule（实测发现，官方未明说）"
    如果中央团队从 home Region 创建规则，spoke Region 的本地账号管理员打开 Console 或调 CLI，**会完全看不到 replicated rule 的存在**，错误消息是 "Telemetry evaluation is not enabled for the requester account"。
    
    但规则其实已经在 spoke Region 生效 — 新建资源会被自动配置。
    
    **影响**：Spoke 账号团队排查"为什么我的 VPC 突然有 Flow Logs"时找不到源头。
    
    **解决**：每个 spoke Region 都应主动 `aws observabilityadmin start-telemetry-evaluation --region <spoke>`，并给本地团队培训"看 `IsReplicated=true` 表示来自 home"。
    
    ```bash
    # 看到这个错就先启 evaluation
    aws observabilityadmin list-telemetry-rules --region us-west-2
    # ValidationException: Telemetry evaluation is not enabled for the requester account
    ```

!!! warning "踩坑 2：AllRegions=true 不含 opt-in Region（实测发现，文档描述含糊）"
    官方文档说 `AllRegions=true` 会"replicate to all Amazon Web Services Regions where Amazon CloudWatch Observability Admin is available in the current partition" — 听起来像"全部 Region"。
    
    **实测展开只有 16 个 default-enabled Region**。以下现有的 opt-in region 全部缺失：
    
    ```
    af-south-1 (Cape Town)       ap-east-1 (Hong Kong)
    ap-south-2 (Hyderabad)       ap-southeast-3 (Jakarta)
    ap-southeast-4 (Melbourne)   ca-west-1 (Calgary)
    eu-south-1 (Milan)           eu-south-2 (Spain)
    il-central-1 (Tel Aviv)      me-central-1 (UAE)
    me-south-1 (Bahrain)
    ```
    
    **影响**：如果你在中东/非洲/Hong Kong 有分公司 VPC，`AllRegions=true` 不会覆盖它们。
    
    **解决**：手动检查 `RegionStatuses` 列表，对缺失 Region 单独创建 rule 或在 `Regions` 里显式追加。

!!! info "踩坑 3：删除 rule 后 Flow Logs 保留（与 #102 结论一致）"
    删除 home rule → spoke 5 秒内同步清除。但 VPC Flow Logs 对象（`fl-xxx`）及其 CW Log Group **仍然保留并产生费用**。
    
    官方文档明确："telemetry config only creates new flow logs... It does not delete or impact previously established Amazon VPC Flow logs"。
    
    清理方案见下文。

## 费用明细

| 资源 | 用量 | 费用 |
|------|------|------|
| VPC Flow Logs ingestion (2 Region, 极小流量) | ~100KB | < $0.01 |
| CW Log Group 保留 (3 天) | 3 个 group | < $0.10 |
| AWS Config Internal SLR | 3 Region | $0（Internal SLR 免费） |
| Telemetry rules | 2 个 | $0 |
| **合计** | | **< $2** |

## 清理资源

```bash
# 1. 删除 home rule（spoke replica 自动清理）
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier xregion-vpc-flow-logs --region us-east-1

# 2. 关键：删除 rule 后 Flow Logs 保留，必须手动
aws ec2 delete-flow-logs \
  --flow-log-ids fl-00f8a7ed5c00f97b7 --region us-east-1

aws ec2 delete-flow-logs \
  --flow-log-ids fl-057a69b4813f8ffd2 --region us-west-2

# 3. 删除 CW Log Groups
aws logs delete-log-group \
  --log-group-name /aws/vpc/xregion/595842667825/vpc-0320f0a69ff411a76 \
  --region us-east-1
aws logs delete-log-group \
  --log-group-name /aws/vpc/xregion/595842667825/vpc-02a3f81484672f1aa \
  --region us-west-2

# 4. 删除 VPC
aws ec2 delete-vpc --vpc-id vpc-0320f0a69ff411a76 --region us-east-1
aws ec2 delete-vpc --vpc-id vpc-02a3f81484672f1aa --region us-west-2

# 5. 可选：保留 spoke evaluation 状态；如不需要可停用
# aws observabilityadmin stop-telemetry-evaluation --region us-west-2
```

!!! danger "务必清理 Flow Logs"
    Rule 删除后 Flow Logs 不会自动删。长期运行会持续产生 CW Logs 摄入和存储费用。

## 结论与建议

### 场景推荐

| 场景 | 推荐配置 | 理由 |
|------|---------|------|
| 组织级强制合规（所有 default-enabled Region） | `AllRegions=true` | 一次配置，未来新默认 Region 自动扩展 |
| 有 opt-in Region 的跨国客户 | `Regions=[a,b,c]` 显式列出 + 独立 rule 覆盖 opt-in Region | AllRegions 不含 opt-in |
| 单 Region 场景 | 不用 Regions/AllRegions | 退回单区域规则（zero overhead） |
| Prod + DR 双 Region | `Regions=[prod-region, dr-region]` | 精确控制 |

### 生产落地建议

1. **Home Region 选 us-east-1 或 eu-west-1**：容量大、稳定、大多数全球服务的主 Region。
2. **所有 spoke Region 都要 `start-telemetry-evaluation`**：否则本地团队看不到 replica（踩坑 1）。
3. **手动覆盖 opt-in Region**：`AllRegions=true` 之后，检查 `RegionStatuses` 列表，缺什么手动补。
4. **AwsServiceEvent 做审计**：EventBridge 监听 `eventType=AwsServiceEvent` + `eventSource=observabilityadmin.amazonaws.com`，把跨 Region 复制动作送到 SIEM。
5. **删除 rule 前先想清楚 "保留还是清理"**：rule 一删、flow logs 就成"孤儿"（继续产生费用）。生产上建议先把 `RetentionInDays` 降到最低后再考虑删除。
6. **Tag 策略**：home 用 tag 做 rule 分类（如 `Owner=SecTeam`, `Compliance=PCI`）；spoke 的 replica 可以打本地成本 tag（不会污染 home）。

### 与其他 CloudWatch 跨区域功能的组合

- **CloudWatch Pipelines 合规治理**（2026-04）：跨 Region 摄入前做 PII 脱敏
- **CloudWatch Query Studio PromQL**（2026-04）：跨 Region 指标统一查询
- 本次 cross-region rules：跨 Region 自动采集

三者叠加 = 中央安全团队 one-stop 合规平台。

## 参考链接

- [What's New: Cross-region telemetry auditing and enablement rules](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-cross-region-enablement-rules/)
- [Telemetry enablement rules](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/telemetry-config-rules.html)
- [Setting up telemetry configuration](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/telemetry-config-turn-on.html)
- [create_telemetry_rule API Reference](https://docs.aws.amazon.com/observabilityadmin/2018-05-10/APIReference/API_CreateTelemetryRule.html)
- 前篇：[CloudWatch 自动启用规则实战：一条规则为所有 CloudFront Distribution 开启 Access Logs](../cloud-ops/cloudwatch-cloudfront-enablement.md)
