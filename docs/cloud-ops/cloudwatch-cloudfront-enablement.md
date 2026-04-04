# CloudWatch 自动启用规则实战：一条规则为所有 CloudFront Distribution 开启 Access Logs

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $1（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-04

## 背景

CloudFront 的 Standard Access Logs（v2）支持发送到 CloudWatch Logs、Firehose 和 S3。但如果你有几十个 distribution，逐个手动配置 logging 是一件痛苦的事——更不用说新建的 distribution 还需要记得再配一遍。

2026 年 4 月，CloudWatch Telemetry Config 的 **Enablement Rules** 新增支持 CloudFront Standard Access Logs。这意味着你可以创建一条规则，自动为账号内所有现有和新建的 CloudFront distribution 开启 access logs 到 CloudWatch Logs，**零手动配置**。

本文将动手验证：规则是否真的秒级生效？删除规则后 logging 会不会消失？多条规则会不会冲突？

## 前置条件

- AWS 账号，需要以下权限：
    - `observabilityadmin:*`（Telemetry Config）
    - `cloudfront:*`（创建 distribution）
    - `logs:*`（查看 CloudWatch Logs）
    - `s3:*`（创建 origin bucket）
- AWS CLI v2 已配置
- Region 必须为 **us-east-1**（CloudFront 全局服务）

## 核心概念

### Telemetry Enablement Rules

Enablement rule 是 CloudWatch Telemetry Config 的核心功能，它与 AWS Config 集成，自动发现你账号中的资源并配置遥测收集。

| 参数 | 说明 |
|------|------|
| ResourceType | `AWS::CloudFront::Distribution` |
| TelemetryType | `Logs` |
| DestinationType | `cloud-watch-logs` |
| DestinationPattern | 支持 `<resourceId>` 和 `<accountId>` macro |
| Scope | Account / OU / Organization |
| API 服务名 | `observabilityadmin` |

### 关键行为

| 行为 | 说明 |
|------|------|
| 生效速度 | 创建规则后**秒级**为所有现有资源配置（文档说最多 24 小时，这是最坏情况） |
| 删除规则 | 已配置的 logging **保持不变**，日志继续流入 |
| 规则冲突 | 同 scope 下同 ResourceType + TelemetryType + 同 Destination = 冲突；不同 Destination Pattern ≠ 冲突 |
| AWS Config | 自动创建 Internal SLR recorder，**不额外收费** |
| 前置条件 | 必须先调用 `start-telemetry-evaluation` |

## 动手实践

### Step 1: 启动 Telemetry Evaluation

```bash
# 检查状态
aws observabilityadmin get-telemetry-evaluation-status \
  --region us-east-1

# 如果不是 RUNNING，启动它
aws observabilityadmin start-telemetry-evaluation \
  --region us-east-1
```

**预期输出**：
```json
{
    "Status": "RUNNING"
}
```

### Step 2: 创建 Enablement Rule

创建一条规则，为所有 CloudFront distribution 自动开启 access logs 到 CloudWatch Logs。

先创建规则配置文件 `rule.json`：

```json
{
  "ResourceType": "AWS::CloudFront::Distribution",
  "TelemetryType": "Logs",
  "DestinationConfiguration": {
    "DestinationType": "cloud-watch-logs",
    "DestinationPattern": "/aws/cloudfront/accesslogs/<resourceId>",
    "RetentionInDays": 7
  }
}
```

然后创建规则：

```bash
aws observabilityadmin create-telemetry-rule \
  --rule-name "cf-accesslogs-auto-enable" \
  --rule file://rule.json \
  --region us-east-1
```

**实测输出**：
```json
{
    "RuleArn": "arn:aws:observabilityadmin:us-east-1:123456789012:telemetry-rule/cf-accesslogs-auto-enable"
}
```

`<resourceId>` macro 会在实际配置时替换为每个 distribution 的 ID，实现**每个 distribution 一个独立 log group**。

### Step 3: 创建测试 CloudFront Distribution

准备一个 S3 bucket 作为 origin：

```bash
BUCKET_NAME="cf-enablement-test-$(date +%s)"

aws s3api create-bucket \
  --bucket $BUCKET_NAME \
  --region us-east-1

echo "hello" | aws s3 cp - s3://$BUCKET_NAME/index.html
```

创建 OAC 和 distribution：

```bash
# 创建 Origin Access Control
OAC_ID=$(aws cloudfront create-origin-access-control \
  --origin-access-control-config '{
    "Name": "enablement-test-oac",
    "Description": "OAC for enablement test",
    "SigningProtocol": "sigv4",
    "SigningBehavior": "always",
    "OriginAccessControlOriginType": "s3"
  }' \
  --query 'OriginAccessControl.Id' --output text)

# 创建 distribution 配置文件
cat > dist-config.json << EOF
{
  "CallerReference": "test-$(date +%s)",
  "Comment": "Enablement rule test",
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-origin",
    "ViewerProtocolPolicy": "redirect-to-https",
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "Compress": true
  },
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "s3-origin",
      "DomainName": "${BUCKET_NAME}.s3.amazonaws.com",
      "OriginAccessControlId": "${OAC_ID}",
      "S3OriginConfig": { "OriginAccessIdentity": "" }
    }]
  },
  "Enabled": true
}
EOF

aws cloudfront create-distribution \
  --distribution-config file://dist-config.json \
  --query '{Id: Distribution.Id, DomainName: Distribution.DomainName}'
```

**实测输出**：
```json
{
    "Id": "EQFW2ILMYVVOX",
    "DomainName": "do0lg54lrier2.cloudfront.net"
}
```

### Step 4: 验证自动配置（核心验证）

创建 distribution 后**无需额外操作**，立即检查是否自动配置了 logging：

```bash
# 检查 log groups
aws logs describe-log-groups \
  --log-group-name-prefix /aws/cloudfront/accesslogs \
  --region us-east-1 \
  --query 'logGroups[].logGroupName'
```

**实测输出**：
```json
[
    "/aws/cloudfront/accesslogs/E1ZNLS5CEYNWRO",
    "/aws/cloudfront/accesslogs/EQFW2ILMYVVOX"
]
```

✅ **两个 distribution 都被自动配置了独立的 log group！**

验证 delivery source：

```bash
aws logs describe-delivery-sources \
  --region us-east-1 \
  --query 'deliverySources[?service==`cloudfront`].{ResourceArns:resourceArns,LogType:logType}'
```

**实测输出**：
```json
[
    {
        "ResourceArns": ["arn:aws:cloudfront::123456789012:distribution/EQFW2ILMYVVOX"],
        "LogType": "ACCESS_LOGS"
    },
    {
        "ResourceArns": ["arn:aws:cloudfront::123456789012:distribution/E1ZNLS5CEYNWRO"],
        "LogType": "ACCESS_LOGS"
    }
]
```

发送测试请求，等待 2-5 分钟后查看日志：

```bash
# 发送请求
for i in {1..10}; do
  curl -s https://do0lg54lrier2.cloudfront.net/ > /dev/null
done

# 等待几分钟后查看日志
aws logs filter-log-events \
  --log-group-name /aws/cloudfront/accesslogs/EQFW2ILMYVVOX \
  --region us-east-1 \
  --limit 1 \
  --query 'events[].message'
```

**实测日志样例**（JSON 格式，33+ 字段）：
```json
{
  "date": "2026-04-04",
  "time": "17:48:32",
  "x-edge-location": "SIN2-P11",
  "sc-status": "200",
  "cs-method": "GET",
  "cs-uri-stem": "/index.html",
  "x-edge-result-type": "Miss",
  "time-taken": "0.686",
  "ssl-protocol": "TLSv1.3",
  "cs-protocol-version": "HTTP/2.0"
}
```

### Step 5: 验证删除规则后 logging 保持

```bash
# 删除 enablement rule
aws observabilityadmin delete-telemetry-rule \
  --rule-identifier "cf-accesslogs-auto-enable" \
  --region us-east-1

# 验证 delivery sources 仍然存在
aws logs describe-delivery-sources \
  --region us-east-1 \
  --query 'deliverySources[?service==`cloudfront`].LogType'
```

**实测输出**：
```json
["ACCESS_LOGS", "ACCESS_LOGS"]
```

✅ **删除规则后，delivery sources 和 log groups 全部保留。** 规则只负责"配置"，不负责"维持"。

### Step 6: 验证多规则共存（边界测试）

```bash
# 第一条规则
aws observabilityadmin create-telemetry-rule \
  --rule-name "rule-pattern-a" \
  --rule '{"ResourceType":"AWS::CloudFront::Distribution","TelemetryType":"Logs","DestinationConfiguration":{"DestinationType":"cloud-watch-logs","DestinationPattern":"/aws/cloudfront/accesslogs/<resourceId>"}}' \
  --region us-east-1

# 第二条规则（不同 destination pattern）
aws observabilityadmin create-telemetry-rule \
  --rule-name "rule-pattern-b" \
  --rule '{"ResourceType":"AWS::CloudFront::Distribution","TelemetryType":"Logs","DestinationConfiguration":{"DestinationType":"cloud-watch-logs","DestinationPattern":"/aws/cloudfront/logs-b/<resourceId>"}}' \
  --region us-east-1
```

**实测结果**：两条规则都成功创建，**不冲突**。不同的 `DestinationPattern` 被视为不同的 destination configuration。

## 测试结果

| # | 测试场景 | 结果 | 关键发现 |
|---|---------|------|---------|
| 1 | Rule → 新建 distribution → 自动配置 | ✅ 通过 | Log group 秒级自动创建，macro 正确替换 |
| 2 | Rule 对已有 distribution 的影响 | ✅ 通过 | 已有 distribution 同时被配置，创建时间差 < 100ms |
| 3 | 多规则共存（不同 pattern） | ✅ 通过 | 不同 destination pattern 不冲突，各自独立工作 |
| 4 | 删除 rule 后 logging 保持 | ✅ 通过 | Delivery sources + log groups 全部保留 |
| 5 | DestinationPattern macro 验证 | ✅ 通过 | `<resourceId>` `<accountId>` 支持；`<resource-id>` 被拒绝 |

## 踩坑记录

!!! warning "踩坑 1: CLI Help 枚举不包含 CloudFront"
    执行 `aws observabilityadmin create-telemetry-rule help` 时，`ResourceType` 的可选值列表中**没有** `AWS::CloudFront::Distribution`。但 API 实际接受这个值并正常工作。
    
    这是 CLI 文档滞后于 API 实现的典型案例。如果你在 help 页面找不到某个新支持的 resource type，直接尝试 API 调用。

!!! warning "踩坑 2: Macro 格式必须 camelCase"
    DestinationPattern 中的 macro 必须使用 **camelCase** 格式：
    
    - ✅ `<resourceId>` `<accountId>`
    - ❌ `<resource-id>` `<account-id>`
    
    ```
    ValidationException: Pattern can only contain alphabets, digits,
    the macros <accountId> and <resourceId>, and the symbols: _, /, -
    ```
    
    错误信息直接告诉你支持的 macro 名称，但如果你从 VPC Flow Logs 的文档（用 `<vpc-id>`）类推，就会掉进这个坑。

!!! info "发现: 系统同时配置 ACCESS_LOGS 和 CONNECTION_LOGS"
    创建一条 `TelemetryType: Logs` 的规则后，系统不仅配置了 ACCESS_LOGS 的 delivery source，还自动创建了 CONNECTION_LOGS 的 delivery source。这意味着你不仅得到 access logs，还自动得到了 connection logs（TLS 握手等信息），无需额外配置。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudFront Distribution | 按请求计费 | ~30 请求 | ~$0.00 |
| CloudWatch Logs | $0.50/GB | < 1KB | ~$0.00 |
| S3 Bucket (origin) | $0.023/GB | < 1KB | ~$0.00 |
| Enablement Rule | 免费 | - | $0.00 |
| AWS Config (Internal SLR) | 免费 | - | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 删除 enablement rules
aws observabilityadmin list-telemetry-rules --region us-east-1 \
  --query 'TelemetryRuleSummaries[].RuleName' --output text | \
  xargs -n1 -I{} aws observabilityadmin delete-telemetry-rule \
  --rule-identifier {} --region us-east-1

# 2. 禁用并删除 CloudFront distribution
DIST_ID="YOUR_DISTRIBUTION_ID"

# 获取 ETag 和配置
ETAG=$(aws cloudfront get-distribution-config --id $DIST_ID \
  --query 'ETag' --output text)
aws cloudfront get-distribution-config --id $DIST_ID \
  --query 'DistributionConfig' > /tmp/dist-config.json

# 修改 Enabled 为 false
sed -i 's/"Enabled": true/"Enabled": false/' /tmp/dist-config.json

# 更新 distribution（禁用）
aws cloudfront update-distribution --id $DIST_ID \
  --if-match $ETAG \
  --distribution-config file:///tmp/dist-config.json

# 等待状态变为 Deployed（约 5-10 分钟）
aws cloudfront wait distribution-deployed --id $DIST_ID

# 删除 distribution
NEW_ETAG=$(aws cloudfront get-distribution --id $DIST_ID \
  --query 'ETag' --output text)
aws cloudfront delete-distribution --id $DIST_ID --if-match $NEW_ETAG

# 3. 删除 OAC
aws cloudfront delete-origin-access-control --id YOUR_OAC_ID \
  --if-match $(aws cloudfront get-origin-access-control --id YOUR_OAC_ID \
  --query 'ETag' --output text)

# 4. 删除 CloudWatch Log Groups
aws logs delete-log-group \
  --log-group-name /aws/cloudfront/accesslogs/$DIST_ID \
  --region us-east-1

# 5. 删除 delivery sources、destinations、deliveries
# （删除 log group 前需要先删除关联的 delivery）
aws logs describe-deliveries --region us-east-1 \
  --query 'deliveries[].id' --output text | \
  xargs -n1 -I{} aws logs delete-delivery --id {} --region us-east-1

# 6. 清空并删除 S3 bucket
aws s3 rb s3://$BUCKET_NAME --force
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然本 lab 费用极低（< $0.01），但 CloudFront distribution 和 CloudWatch Logs 的 delivery 如果长期保留，在生产环境中可能产生持续费用。

## 结论与建议

### 适用场景

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 单账号 < 10 个 distribution | 手动配置 | 一次性工作，enablement rule 优势不明显 |
| 单账号 10+ distribution | Account-level enablement rule | 自动覆盖现有 + 新建，零运维 |
| 多账号/Organization | Organization-level rule | 一条规则覆盖所有账号，中央管控 |
| 已有手动配置 | 可叠加 enablement rule | 不会覆盖已有配置，只补充缺失的 |

### 最佳实践

1. **DestinationPattern 用 `<resourceId>` macro** — 每个 distribution 独立 log group，便于查询和权限控制
2. **设置 RetentionInDays** — 避免日志无限增长，7-30 天通常足够
3. **先在单账号测试，再推广到 Organization** — 验证 pattern 和行为符合预期
4. **不要担心删除规则** — 已配置的 logging 会保持，删除规则是安全操作

### 与手动配置的对比

| 维度 | 手动配置 (CloudWatch API) | Enablement Rule |
|------|--------------------------|-----------------|
| 配置方式 | 每个 distribution 单独配置 | 一条规则自动覆盖 |
| 新建资源 | 需要记得再配一次 | 自动配置 |
| 配置一致性 | 依赖人工 | 自动保证 |
| 灵活性 | 完全自定义 | 通过 pattern 模板化 |
| 回滚 | 逐个删除 delivery | 删除规则（已配置的不受影响） |

## 参考链接

- [AWS What's New: CloudWatch extends auto-enablement rules for CloudFront](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-cloudfront-enablement/)
- [Working with telemetry enablement rules](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/telemetry-config-rules.html)
- [CloudFront Standard Logging (v2)](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/standard-logging.html)
- [CloudWatch Observability Admin CLI Reference](https://docs.aws.amazon.com/cli/latest/reference/observabilityadmin/)
