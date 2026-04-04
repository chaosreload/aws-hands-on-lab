# CloudWatch 自动启用规则实战：一条命令为所有 CloudFront Distribution 开启 Access Logs

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1（CloudWatch Logs 摄入 + CloudFront 请求）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-04

## 背景

你有 50 个 CloudFront Distribution，领导突然要求"所有 CDN 都开启 Access Logs 到 CloudWatch"。以前这意味着逐个配置每个 Distribution 的 standard logging —— 手动操作容易遗漏，新建的 Distribution 更是难以保证合规。

2026 年 4 月，Amazon CloudWatch 的 **Telemetry Config Enablement Rules** 扩展支持了 **CloudFront Standard Access Logs**。一条规则，自动为账号内所有现有和未来的 CloudFront Distribution 配置 Access Logs 投递到 CloudWatch Logs，零手动操作。

本文通过实测验证这个新功能，覆盖：创建规则、验证自动配置、日志格式、macro 支持、规则删除后行为等关键场景。

## 前置条件

- AWS 账号，需要 `observabilityadmin:*` 和 `cloudfront:*` 权限
- AWS CLI v2.34+（旧版可能不支持 `observabilityadmin` 子命令）
- 至少一个 CloudFront Distribution（或在本文中创建）

## 核心概念

### CloudWatch Telemetry Config Enablement Rules

| 概念 | 说明 |
|------|------|
| **Enablement Rule** | 定义"哪些资源类型 + 什么遥测类型 + 投递到哪"的自动化规则 |
| **Scope** | Organization / OU / Account 三级作用域 |
| **前置条件** | 必须先启动 Telemetry Evaluation |
| **资源发现** | 通过 AWS Config Internal SLR（不额外收费）发现资源 |
| **发现延迟** | 文档说最多 24 小时，实测 ~10 分钟 |

### CloudFront 新增支持

| 参数 | 值 |
|------|-----|
| ResourceType | `AWS::CloudFront::Distribution` |
| TelemetryType | `Logs` |
| DestinationType | `cloud-watch-logs` |
| 支持的 Macro | `<accountId>`, `<resourceId>` |
| 操作 Region | 必须 us-east-1 |
| 日志格式 | JSON（v2 Standard Logging） |

### 同批次新增的其他资源类型

| 资源类型 | 支持的 Scope |
|----------|-------------|
| CloudFront Standard Access Logs | Organization / Account |
| Security Hub CSPM Findings | Organization / Account |
| Bedrock AgentCore Memory Logs | Account |
| Bedrock AgentCore Gateway Logs & Traces | Account |

## 动手实践

### Step 1: 启动 Telemetry Evaluation

Enablement Rules 需要先启动 Telemetry Evaluation，这是一个账号级别的一次性操作。

```bash
# 检查当前状态
aws observabilityadmin get-telemetry-evaluation-status \
  --region us-east-1

# 如果 Status 是 NOT_STARTED，启动它
aws observabilityadmin start-telemetry-evaluation \
  --region us-east-1
```

**实测输出**：
```json
// 启动前
{ "Status": "NOT_STARTED" }

// 启动后
{ "Status": "RUNNING" }
```

!!! tip "一次性操作"
    `start-telemetry-evaluation` 只需执行一次。它会启动 AWS Config Internal SLR 来发现账号中的资源。

### Step 2: 创建 CloudFront Access Logs 自动启用规则

```bash
# 创建规则配置文件
cat > /tmp/cf-rule.json << 'EOF'
{
    "ResourceType": "AWS::CloudFront::Distribution",
    "TelemetryType": "Logs",
    "DestinationConfiguration": {
        "DestinationType": "cloud-watch-logs",
        "DestinationPattern": "/aws/cloudfront/access-logs",
        "RetentionInDays": 7
    }
}
EOF

# 创建规则
aws observabilityadmin create-telemetry-rule \
  --rule-name cloudfront-access-logs-auto \
  --rule file:///tmp/cf-rule.json \
  --region us-east-1
```

**实测输出**：
```json
{
    "RuleArn": "arn:aws:observabilityadmin:us-east-1:595842667825:telemetry-rule/cloudfront-access-logs-auto"
}
```

!!! warning "CLI Help 枚举滞后"
    `aws observabilityadmin create-telemetry-rule help` 的 ResourceType 枚举**不包含** `AWS::CloudFront::Distribution`，但 API 实际接受。这是因为 CLI help 文档尚未更新，功能已经可用。

### Step 3: 创建测试 CloudFront Distribution

```bash
# 创建 S3 origin bucket
aws s3api create-bucket \
  --bucket my-cf-test-bucket \
  --region us-east-1

# 上传测试页面
echo "<h1>Hello from CloudFront</h1>" | aws s3 cp - \
  s3://my-cf-test-bucket/index.html \
  --content-type text/html

# 创建 CloudFront OAC
aws cloudfront create-origin-access-control \
  --origin-access-control-config \
    Name=my-cf-test-oac,\
    Description="OAC for test",\
    SigningProtocol=sigv4,\
    SigningBehavior=always,\
    OriginAccessControlOriginType=s3

# 创建 Distribution（替换 OAC_ID）
cat > /tmp/cf-dist.json << 'EOF'
{
    "CallerReference": "my-cf-test-dist",
    "Comment": "Test for auto-enablement",
    "Enabled": true,
    "Origins": {
        "Quantity": 1,
        "Items": [{
            "Id": "s3-origin",
            "DomainName": "my-cf-test-bucket.s3.us-east-1.amazonaws.com",
            "S3OriginConfig": { "OriginAccessIdentity": "" },
            "OriginAccessControlId": "<YOUR_OAC_ID>"
        }]
    },
    "DefaultCacheBehavior": {
        "TargetOriginId": "s3-origin",
        "ViewerProtocolPolicy": "allow-all",
        "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
        "Compress": true
    }
}
EOF

aws cloudfront create-distribution \
  --distribution-config file:///tmp/cf-dist.json
```

### Step 4: 验证自动配置生效

创建 Distribution 后等待约 10 分钟，检查 CloudWatch Logs：

```bash
# 检查是否自动创建了 Log Group
aws logs describe-log-groups \
  --log-group-name-prefix /aws/cloudfront \
  --region us-east-1
```

**实测输出**：
```json
{
    "logGroups": [{
        "logGroupName": "/aws/cloudfront/access-logs",
        "retentionInDays": 7,
        "logGroupClass": "STANDARD"
    }]
}
```

```bash
# 检查 Delivery Source（由 enablement rule 自动创建）
aws logs describe-delivery-sources \
  --region us-east-1 \
  --query "deliverySources[?service=='cloudfront']"
```

**实测输出**：
```json
[{
    "name": "b592a26e-c45b-3a33-af86-df7f1f108fcc",
    "resourceArns": ["arn:aws:cloudfront::595842667825:distribution/E1ZNLS5CEYNWRO"],
    "service": "cloudfront",
    "logType": "ACCESS_LOGS"
}]
```

发送测试请求并查看日志：

```bash
# 发送请求
curl -s https://<YOUR_DISTRIBUTION_DOMAIN>/index.html

# 查看 Log Streams
aws logs describe-log-streams \
  --log-group-name /aws/cloudfront/access-logs \
  --region us-east-1

# 查看实际日志
aws logs get-log-events \
  --log-group-name /aws/cloudfront/access-logs \
  --log-stream-name CloudFront_<DISTRIBUTION_ID> \
  --region us-east-1 --limit 1
```

**实测日志（JSON 格式）**：
```json
{
    "date": "2026-04-04",
    "time": "17:48:32",
    "x-edge-location": "SIN2-P11",
    "sc-bytes": "357",
    "c-ip": "18.140.5.11",
    "cs-method": "GET",
    "cs(Host)": "d3pnvry5wvdfb5.cloudfront.net",
    "cs-uri-stem": "/index.html",
    "sc-status": "200",
    "x-edge-result-type": "Miss",
    "time-taken": "0.686",
    "ssl-protocol": "TLSv1.3",
    "x-edge-detailed-result-type": "Miss",
    "sc-content-type": "text/html"
}
```

!!! info "日志格式"
    自动启用的 Access Logs 使用 **JSON 格式**（v2 Standard Logging），包含 33+ 字段。Log stream 命名格式为 `CloudFront_{DistributionId}`。

### Step 5: 对比实验 — 使用 Macro 自定义 Log Group

默认情况下所有 Distribution 的日志会合并到同一个 Log Group。使用 `<resourceId>` macro 可以为每个 Distribution 创建独立的 Log Group：

```bash
cat > /tmp/cf-rule-macro.json << 'EOF'
{
    "ResourceType": "AWS::CloudFront::Distribution",
    "TelemetryType": "Logs",
    "DestinationConfiguration": {
        "DestinationType": "cloud-watch-logs",
        "DestinationPattern": "/aws/cloudfront/accesslogs/<resourceId>",
        "RetentionInDays": 3
    }
}
EOF

aws observabilityadmin create-telemetry-rule \
  --rule-name cloudfront-per-dist-logs \
  --rule file:///tmp/cf-rule-macro.json \
  --region us-east-1
```

**结果**：每个 Distribution 自动获得独立的 Log Group：
```
/aws/cloudfront/accesslogs/E1ZNLS5CEYNWRO
/aws/cloudfront/accesslogs/EQFW2ILMYVVOX
```

### Step 6: 边界测试 — 错误的 Macro 格式

```bash
# 使用 kebab-case macro（错误）
cat > /tmp/cf-rule-bad.json << 'EOF'
{
    "ResourceType": "AWS::CloudFront::Distribution",
    "TelemetryType": "Logs",
    "DestinationConfiguration": {
        "DestinationType": "cloud-watch-logs",
        "DestinationPattern": "/aws/cloudfront/<account-id>/logs",
        "RetentionInDays": 7
    }
}
EOF

aws observabilityadmin create-telemetry-rule \
  --rule-name bad-macro-test \
  --rule file:///tmp/cf-rule-bad.json \
  --region us-east-1
```

**实测输出**：
```
ValidationException: Pattern can only contain alphabets, digits,
the macros <accountId> and <resourceId>, and the symbols: _, /, -
```

!!! warning "Macro 格式必须是 camelCase"
    只支持 `<accountId>` 和 `<resourceId>`（camelCase），**不支持** `<account-id>` 或 `<resource-id>`（kebab-case）。官方文档中其他服务的示例使用的 `<account-id>` 格式**不适用于 CLI API**。

### Step 7: 验证删除规则后行为

```bash
# 删除 enablement rule
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier cloudfront-access-logs-auto \
  --region us-east-1

# 验证 delivery source 是否保持
aws logs describe-delivery-sources \
  --region us-east-1 \
  --query "deliverySources[?service=='cloudfront']"
```

**结果**：
```json
// Delivery source 仍然存在 ✅
[{
    "name": "b592a26e-c45b-3a33-af86-df7f1f108fcc",
    "resourceArns": ["arn:aws:cloudfront::595842667825:distribution/E1ZNLS5CEYNWRO"],
    "service": "cloudfront",
    "logType": "ACCESS_LOGS"
}]
```

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | 创建 Rule → 新建 Distribution → 自动配置 | ✅ 通过 | 发现延迟 ~10 分钟 | Log group + delivery source 自动创建 |
| 2 | 已有 Distribution 是否被自动配置 | ✅ 通过 | 已有资源同样被配置 | 也发现延迟 ~10 分钟 |
| 3 | 使用 `<resourceId>` macro 自定义 Log Group | ✅ 通过 | 每个 Distribution 独立 Log Group | |
| 4 | 错误 macro 格式（kebab-case） | ✅ 预期报错 | ValidationException | 必须 camelCase |
| 5 | 删除 Rule → Logging 是否保持 | ✅ 保持 | Delivery source 和 Log Group 不受影响 | |

## 踩坑记录

!!! warning "踩坑 1: CLI Help 不列出 CloudFront ResourceType"
    运行 `aws observabilityadmin create-telemetry-rule help`，ResourceType 枚举中**不包含** `AWS::CloudFront::Distribution`。但 API 实际接受这个值并正常工作。
    
    这很可能是因为 CLI SDK 的模型定义尚未更新。如果你依赖 CLI help 来确认支持的资源类型，会误以为 CloudFront 不支持。
    
    <!-- 实测发现，官方 CLI 未更新 -->

!!! warning "踩坑 2: Macro 格式 camelCase vs kebab-case 不一致"
    官方文档中 VPC/Route53 的示例使用 `<vpc-id>`, `<account-id>` 格式，但 CLI API 只接受 `<accountId>` 和 `<resourceId>`（camelCase）。
    
    ```
    ValidationException: Pattern can only contain alphabets, digits,
    the macros <accountId> and <resourceId>, and the symbols: _, /, -
    ```
    
    <!-- 实测发现，文档与 API 行为不一致 -->

!!! info "发现: CONNECTION_LOGS 也会被配置"
    创建 ACCESS_LOGS 的 enablement rule 后，系统还自动创建了 `CONNECTION_LOGS` 类型的 delivery source。这意味着 enablement rule 可能会自动配置额外的日志类型。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudFront 请求 | $0.0075/10K | ~30 请求 | < $0.01 |
| CloudWatch Logs 摄入 | $0.50/GB | < 1KB | < $0.01 |
| CloudWatch Logs 存储（7天） | $0.03/GB/月 | < 1KB | < $0.01 |
| S3 存储 | $0.023/GB | < 1KB | < $0.01 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 1. 删除 Enablement Rules
aws observabilityadmin list-telemetry-rules --region us-east-1
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier <RULE_NAME> --region us-east-1

# 2. 删除 CloudWatch Delivery（需要先删 delivery，再删 source 和 destination）
aws logs describe-deliveries --region us-east-1
aws logs delete-delivery --id <DELIVERY_ID> --region us-east-1
aws logs delete-delivery-source --name <SOURCE_NAME> --region us-east-1
aws logs delete-delivery-destination --name <DEST_NAME> --region us-east-1

# 3. 删除 CloudWatch Log Groups
aws logs delete-log-group \
  --log-group-name /aws/cloudfront/access-logs --region us-east-1

# 4. 禁用 + 删除 CloudFront Distribution
aws cloudfront get-distribution-config --id <DIST_ID>
# 修改 Enabled: false，然后 update-distribution，等待 Deployed 后
aws cloudfront delete-distribution --id <DIST_ID> --if-match <ETAG>

# 5. 删除 OAC
aws cloudfront delete-origin-access-control --id <OAC_ID> --if-match <ETAG>

# 6. 删除 S3 Bucket
aws s3 rb s3://my-cf-test-bucket --force
```

!!! danger "务必清理"
    虽然费用极低，但 CloudFront Distribution 开启后每月有少量固定费用。CloudWatch Logs 按保留天数持续收费。请按上述步骤清理。

## 结论与建议

### 使用场景推荐

| 场景 | DestinationPattern 建议 | 理由 |
|------|------------------------|------|
| 小规模（<10 个 Distribution） | `/aws/cloudfront/access-logs` | 集中管理，Logs Insights 一次查全部 |
| 大规模（10+ Distribution） | `/aws/cloudfront/accesslogs/<resourceId>` | 隔离日志，避免单一 Log Group 过大 |
| 多账号组织 | `/aws/cloudfront/<accountId>/accesslogs` | 按账号分隔，便于成本分摊 |

### 生产环境建议

1. **先创建 Rule，再创建 Distribution** — 确保新资源从一开始就有日志覆盖
2. **避免创建多条同类型 Rule** — 虽然 API 不拒绝，但多条规则可能导致重复日志和额外费用
3. **设置合理的 RetentionInDays** — 生产环境建议 30-90 天，测试环境 3-7 天
4. **组合 Logs Insights** — CloudFront 日志进入 CloudWatch 后，可以用 SQL/PPL 实时分析访问模式

### 与手动配置的对比

| 维度 | 手动配置（CloudFront Console） | Enablement Rule |
|------|------------------------------|-----------------|
| 覆盖范围 | 逐个 Distribution | 自动覆盖所有现有 + 新建 |
| 新资源保障 | 需要运维流程保证 | 自动合规 |
| 日志目标 | S3 / CloudWatch / Firehose | 仅 CloudWatch Logs |
| 字段选择 | 可自定义 | 全量字段 |
| 操作复杂度 | N 个 Distribution = N 次操作 | 1 条规则 |

## 参考链接

- [What's New: Amazon CloudWatch expands auto-enablement to Amazon CloudFront logs and 3 additional resource types](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-cloudfront-enablement/)
- [Working with telemetry enablement rules](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/telemetry-config-rules.html)
- [CloudFront Standard Logging (v2)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging.html)
- [CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)
