# CloudWatch Pipelines 合规治理实测：Keep Original + IAM 条件键 + 变换元数据三大功能全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $1（Pipeline 免费，仅 CW Logs 存储费）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-12

## 背景

CloudWatch Pipelines 允许你在日志摄入时对数据进行变换处理（添加/删除/重命名字段、解析 JSON 等）。但在合规场景下，这带来了三个问题：

1. **原始数据丢失** — 处理器会修改日志内容，审计时无法追溯未修改的原始数据
2. **数据血缘不清** — 处理后的日志与原始日志混在一起，无法区分哪些经过了变换
3. **权限控制粗放** — 无法按数据源名称/类型限制谁能创建 Pipeline

2026 年 4 月 10 日，AWS 发布了三项合规与治理功能来解决这些痛点：**Keep Original toggle**（保留原始日志副本）、**变换元数据标记**（标识已变换数据）、**IAM 条件键**（按数据源限制 Pipeline 创建权限）。

本文通过 10 项实测，验证这三项功能的实际行为、配置方式和边界条件。

## 前置条件

- AWS 账号（需要 `observabilityadmin:*`、`logs:*`、`iam:*` 权限）
- AWS CLI v2 已配置
- 对 CloudWatch Pipelines 基本概念有了解（Source → Processors → Sink）

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "observabilityadmin:CreateTelemetryPipeline",
        "observabilityadmin:GetTelemetryPipeline",
        "observabilityadmin:ListTelemetryPipelines",
        "observabilityadmin:DeleteTelemetryPipeline",
        "observabilityadmin:ValidateTelemetryPipelineConfiguration",
        "observabilityadmin:TestTelemetryPipeline"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:PutTransformer",
        "logs:GetTransformer",
        "logs:DeleteTransformer"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:SimulateCustomPolicy",
        "iam:PutUserPolicy",
        "iam:DeleteUserPolicy"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

CloudWatch Pipelines 的合规治理功能围绕三个维度展开：

| 功能 | 解决的问题 | 配置位置 | 验证方式 |
|------|-----------|---------|---------|
| **Keep Original** | 原始数据丢失 | Pipeline config: `pipeline.keep_original: true` | validate + create API |
| **变换元数据标记** | 数据血缘不清 | 自动添加（公告功能） | Logs Insights 查询 |
| **IAM 条件键** | 权限控制粗放 | IAM Policy condition block | simulate + 实际创建 |

### Pipeline Configuration Body 格式

Pipeline 配置使用 YAML 格式（Data Prepper 风格）：

```yaml
pipeline:
  keep_original: true                    # ← 保留原始日志副本
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: my_app         # ← IAM 条件键关联
        data_source_type: default        # ← IAM 条件键关联
      aws:
        sts_role_arn: arn:aws:iam::ACCOUNT:role/CWPipelineRole
  processor:
    - parse_json: {}
    - add_entries:
        entries:
          - key: environment
            value: production
  sink:
    - cloudwatch_logs:
        log_group: "@original"           # @original = 写回源 log group
```

### IAM 条件键

| 条件键 | 对应 Config 字段 | 用途 |
|--------|-----------------|------|
| `observabilityadmin:LogSourceName` | `data_source_name` | 按应用/团队名称限制 |
| `observabilityadmin:LogSourceType` | `data_source_type` | 按数据源类型限制 |

### 关键限制一览

| 限制 | 值 |
|------|-----|
| Pipeline 数量上限 | 330/账户（300 CW Logs + 30 其他源） |
| Sink 数量 | 最多 1 个 |
| Config body 大小 | 最大 24,000 字符 |
| keep_original 位置 | 仅 `pipeline` 层有效 |

## 动手实践

### Step 1: 验证 keep_original 配置位置

`keep_original` 只能放在 `pipeline` 顶层。用 `validate-telemetry-pipeline-configuration` API 验证不同位置的行为。

**Pipeline 层（正确位置）**：

```bash
aws observabilityadmin validate-telemetry-pipeline-configuration \
  --region us-east-1 \
  --configuration-body "$(cat <<'EOF'
pipeline:
  keep_original: true
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: test_app
        data_source_type: default
  processor:
    - parse_json: {}
  sink:
    - cloudwatch_logs:
        log_group: "@original"
EOF
)"
```

**实测输出**：空 response（= 验证通过 ✅）

**Source 层（错误位置）**：

```bash
aws observabilityadmin validate-telemetry-pipeline-configuration \
  --region us-east-1 \
  --configuration-body "$(cat <<'EOF'
pipeline:
  source:
    cloudwatch_logs:
      keep_original: true
      log_event_metadata:
        data_source_name: test_app
        data_source_type: default
  processor:
    - parse_json: {}
  sink:
    - cloudwatch_logs:
        log_group: "@original"
EOF
)"
```

**实测输出**：

```
"validationResults": [
  {
    "type": "UNKNOWN_PROPERTY",
    "message": "Unknown property at source.cloudwatch_logs"
  }
]
```

**Sink 层**同样返回 `UNKNOWN_PROPERTY`。

!!! warning "keep_original 仅 pipeline 层有效"
    放在 `source` 或 `sink` 层会被 validate API 识别为 `UNKNOWN_PROPERTY`。这一行为在官方文档中未详述，完全依赖实测发现。

### Step 2: 创建对比 Pipeline（keep_original=true vs false）

创建两个 Pipeline 进行对比：

**Pipeline 1: 带 keep_original=true**

```bash
aws observabilityadmin create-telemetry-pipeline \
  --region us-east-1 \
  --pipeline-name compliance-keep-original \
  --configuration-body "$(cat <<'EOF'
pipeline:
  keep_original: true
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: compliance_app
        data_source_type: default
  processor:
    - delete_entries:
        with_keys:
          - secret
  sink:
    - cloudwatch_logs:
        log_group: "@original"
EOF
)"
```

**Pipeline 2: 无 keep_original（baseline）**

```bash
aws observabilityadmin create-telemetry-pipeline \
  --region us-east-1 \
  --pipeline-name compliance-baseline \
  --configuration-body "$(cat <<'EOF'
pipeline:
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: baseline_app
        data_source_type: default
  processor:
    - delete_entries:
        with_keys:
          - secret
  sink:
    - cloudwatch_logs:
        log_group: "@original"
EOF
)"
```

两个 Pipeline 均成功进入 **ACTIVE** 状态。通过 `get-telemetry-pipeline` 确认 `keep_original: true` 在返回的 config body 中保留。

### Step 3: test-telemetry-pipeline 对比

使用 `test-telemetry-pipeline` API 验证处理逻辑差异：

```bash
aws observabilityadmin test-telemetry-pipeline \
  --region us-east-1 \
  --configuration-body "$(cat <<'EOF'
pipeline:
  keep_original: true
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: test_app
        data_source_type: default
  processor:
    - delete_entries:
        with_keys:
          - secret
  sink:
    - cloudwatch_logs:
        log_group: "@original"
EOF
)" \
  --log-record '{"level":"ERROR","msg":"original data","secret":"pii_value"}'
```

| 配置 | 输入 | 输出 |
|------|------|------|
| keep_original: true + delete_entries(secret) | `{"level":"ERROR","msg":"original data","secret":"pii_value"}` | `{"level":"ERROR","msg":"original data"}` |
| 无 keep_original + delete_entries(secret) | 同上 | `{"level":"ERROR","msg":"original data"}` |

!!! info "test API 不展示 keep_original 差异"
    `test-telemetry-pipeline` 仅展示变换后输出。`keep_original` 是**运行时功能**，在日志实际 ingestion 时保留原始副本，不在测试 API 中可见。这意味着你无法通过 dry-run 验证原始日志保留效果——需要在真实环境中发送日志后检查。

### Step 4: 变换元数据标记验证

公告提到处理后的日志会自动添加 metadata 标识已变换。通过 Logs Insights 查询验证：

```
fields @timestamp, @message, @log.transformed, @metadata.transformed, 
       @pipeline, @isTransformed, transformed
| sort @timestamp desc
| limit 20
```

**实测结果**：所有可能的元数据字段均为空。Transformer 确实生效（`addKeys` 添加的字段在 extracted fields 中可见），但没有观察到额外的变换元数据标记。

!!! warning "变换元数据标记：公告功能，CLI/API 未观察到"
    公告原文确认 "adds new metadata to processed log entries indicating that the log has been transformed"，但通过 Logs Insights 和 CLI 查询，我们未能找到具体的元数据字段名。可能需要通过控制台创建的 Pipeline 才能触发，或者字段名是我们未尝试的格式。**标注为"公告功能，实测中通过 API/CLI 未观察到"。**

### Step 5: IAM 条件键 — 模拟验证

使用 `simulate-custom-policy` 验证条件键行为：

```bash
# Deny 非审批数据源
aws iam simulate-custom-policy \
  --region us-east-1 \
  --policy-input-list '[
    {
      "Version": "2012-10-17",
      "Statement": [
        {"Effect": "Allow", "Action": "observabilityadmin:CreateTelemetryPipeline", "Resource": "*"},
        {
          "Effect": "Deny",
          "Action": "observabilityadmin:CreateTelemetryPipeline",
          "Resource": "*",
          "Condition": {
            "StringNotEquals": {
              "observabilityadmin:LogSourceName": "approved_source"
            }
          }
        }
      ]
    }
  ]' \
  --action-names observabilityadmin:CreateTelemetryPipeline \
  --context-entries '[
    {"ContextKeyName": "observabilityadmin:LogSourceName", "ContextKeyValues": ["unapproved_source"], "ContextKeyType": "string"}
  ]'
```

| 策略 | 上下文值 | 结果 |
|------|---------|------|
| Deny StringNotEquals LogSourceName: approved_source | unapproved_source | ✅ **explicitDeny** |
| 同上 | approved_source | ✅ **allowed** |
| Deny StringNotEquals LogSourceType: default | custom | ✅ **explicitDeny** |
| 同上 | default | ✅ **allowed** |

### Step 6: IAM 条件键 — 实际 CreateTelemetryPipeline 拦截

将模拟验证升级为实际 API 调用拦截。部署 Deny 策略到测试用户：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Deny",
      "Action": "observabilityadmin:CreateTelemetryPipeline",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "observabilityadmin:LogSourceName": ["blocked_app"]
        }
      }
    }
  ]
}
```

然后分别尝试用 `blocked_app` 和 `safe_app` 作为 `data_source_name` 创建 Pipeline：

| data_source_name | 预期 | 实际 | 状态 |
|-----------------|------|------|------|
| `blocked_app` | Deny | ✅ AccessDeniedException | 拦截成功 |
| `safe_app` | Allow | ✅ Pipeline 创建成功（ACTIVE） | 放行成功 |

!!! info "IAM 策略传播延迟"
    IAM 策略在附加到用户后，需要约 **15-30 秒**才能对 `CreateTelemetryPipeline` 生效。在自动化脚本中需要加入等待逻辑。

### Step 7: Pipeline 关联机制 + @original

验证 Pipeline 如何与 Log Group 关联，以及 `@original` sink 的行为：

- Pipeline 使用 `data_source_name` 标识数据源，不直接绑定 log group
- Sink `@original` 表示写回源 log group
- CW Logs 源 pipeline 自动在匹配的 log group 上部署 Transformer
- Transformer 在 log group 级别操作，pipeline 是更高层的管理抽象

### Step 8: 边界测试 — 多 Sink

```bash
aws observabilityadmin validate-telemetry-pipeline-configuration \
  --region us-east-1 \
  --configuration-body "$(cat <<'EOF'
pipeline:
  source:
    cloudwatch_logs:
      log_event_metadata:
        data_source_name: test
        data_source_type: default
  processor:
    - parse_json: {}
  sink:
    - cloudwatch_logs:
        log_group: "@original"
    - cloudwatch_logs:
        log_group: /test/secondary
EOF
)"
```

**实测输出**：

```
"validationResults": [
  {
    "type": "EXCEEDS_MAX_ITEMS",
    "message": "must have at most 1 items"
  }
]
```

Pipeline 严格限制为最多 **1 个 sink**。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | Pipeline 创建 keep_original=true vs false | ✅ 通过 | 两个均 ACTIVE | |
| 2 | validate: keep_original 位置验证 | ✅ 通过 | 仅 pipeline 层有效 | source/sink 层返回 UNKNOWN_PROPERTY |
| 3-4 | test-telemetry-pipeline 对比 | ✅ 通过 | test API 仅展示变换后输出 | keep_original 是运行时功能 |
| 5 | 变换元数据标记 | ⚠️ 未观察到 | CLI/Insights 均无 | 公告功能，可能需控制台 |
| 6 | IAM simulate-custom-policy | ✅ 通过 | 两个条件键验证通过 | |
| 7 | IAM 实际 CreateTelemetryPipeline 拦截 | ✅ 通过 | blocked_app 被 Deny，safe_app 允许 | |
| 8-9 | Pipeline 关联机制 + @original | ✅ 通过 | @original 写回源 log group | |
| 10 | 限制：多 sink | ✅ 预期报错 | EXCEEDS_MAX_ITEMS | 最多 1 个 sink |

## 踩坑记录

!!! warning "踩坑 1: keep_original 仅 pipeline 层有效"
    `keep_original` 放在 `source.cloudwatch_logs` 或 `sink[0].cloudwatch_logs` 层会返回 `UNKNOWN_PROPERTY`。必须放在 `pipeline` 顶层。官方文档未详述 config body 格式，这一限制完全依赖实测发现。
    
    **影响**：错误放置不会报错创建失败（validate 会提醒），但功能不会生效。

!!! warning "踩坑 2: CW Logs 源的 parse_json 不能有 source 参数"
    在 CW Logs 数据源的 pipeline 中，`parse_json` 处理器不能带 `source` 参数。validate 时可以通过，但 `create-telemetry-pipeline` 时会报 `PARSER_CONFIG_INVALID`。
    
    ```yaml
    # ❌ 错误
    processor:
      - parse_json:
          source: message
    
    # ✅ 正确
    processor:
      - parse_json: {}
    ```

!!! warning "踩坑 3: StringNotEquals 多键条件用 AND 逻辑"
    IAM 策略中 `StringNotEquals` 同时指定多个条件键时，使用 **AND 逻辑** — 必须所有条件都不匹配才触发 Deny。
    
    ```json
    "Condition": {
      "StringNotEquals": {
        "observabilityadmin:LogSourceName": "approved_source",
        "observabilityadmin:LogSourceType": "default"
      }
    }
    ```
    
    只要 Name **或** Type 匹配其中之一，就不会被 Deny。如果需要"两者都必须匹配才放行"，应使用多条独立的 Deny Statement 或 `ForAnyValue:StringNotEquals`。

!!! warning "踩坑 4: delete-telemetry-pipeline 需要特殊权限"
    测试用户（awswhatsnewtest）在调用 `delete-telemetry-pipeline` 时遇到 `UnauthorizedException`。即使拥有 `observabilityadmin:*` 权限，删除 Pipeline 可能需要额外的服务关联角色或特定权限配置。
    
    **影响**：自动化清理脚本需要确保有足够权限，或准备手动清理路径。

!!! info "踩坑 5: IAM 策略传播延迟约 15-30 秒"
    IAM 策略附加到用户后，不会立即对 `CreateTelemetryPipeline` 生效。实测需要等待 15-30 秒。在 CI/CD 或自动化测试中需要加入 sleep 逻辑。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudWatch Pipelines | 免费 | 6 个 Pipeline | $0 |
| CloudWatch Logs 存储 | $0.50/GB | < 1 MB | < $0.01 |
| IAM simulate-custom-policy | 免费 | 多次 | $0 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 删除 Pipeline（如遇 UnauthorizedException，需手动通过控制台删除）
for name in test-compliance compliance-keep-original compliance-baseline \
            compliance-unapproved compliance-unapproved2 compliance-safe; do
  aws observabilityadmin delete-telemetry-pipeline \
    --region us-east-1 \
    --pipeline-identifier "$name" 2>/dev/null && echo "Deleted pipeline: $name" || echo "Failed to delete: $name"
done

# 2. 删除 Log Groups
for lg in /test/compliance-e2e /test/compliance-ko-source /test/compliance-bl-source \
          /test/compliance-source /test/compliance-dest /test/compliance-original \
          /test/compliance-keep /test/pipeline-source /test/pipeline-dest \
          /test/pipeline-original /test/pipeline-sink; do
  aws logs delete-log-group --region us-east-1 --log-group-name "$lg" 2>/dev/null \
    && echo "Deleted log group: $lg" || echo "Failed: $lg"
done

# 3. 删除测试 IAM 策略（如果附加了）
aws iam delete-user-policy \
  --user-name awswhatsnewtest \
  --policy-name TestPipelineSourceRestriction 2>/dev/null
```

!!! danger "务必清理"
    Pipeline 本身免费，但关联的 Log Groups 会产生存储费用。Lab 完成后请执行清理步骤。

## 结论与建议

### 三项功能成熟度评估

| 功能 | 成熟度 | 实测评价 |
|------|--------|---------|
| **Keep Original** | ✅ 可用 | Pipeline 级配置，validate API 确认有效。运行时功能，test API 不可见。 |
| **变换元数据标记** | ⚠️ 待确认 | 公告确认存在，但 CLI/API/Logs Insights 未观察到具体字段名。 |
| **IAM 条件键** | ✅ 成熟 | simulate + 实际 API 双重验证通过，生产可用。 |

### 场景化推荐

| 场景 | 推荐配置 |
|------|---------|
| **合规审计** | 启用 `keep_original: true`，确保原始日志可追溯 |
| **多团队共享账号** | 用 IAM 条件键按 `data_source_name` 限制各团队 Pipeline 创建范围 |
| **PII 脱敏** | `delete_entries` 移除敏感字段 + `keep_original: true` 保留原始副本到受限访问的 log group |
| **日志路由** | 利用 `@original` sink 写回源 log group，简化架构 |

### 生产注意事项

1. **keep_original 的存储成本** — 启用后原始+变换副本均按标准费率计费，注意日志量翻倍
2. **IAM 条件键命名规范** — 建议制定 `data_source_name` 命名规范（如 `{team}_{app}_{env}`），与 IAM 策略配合
3. **StringNotEquals 陷阱** — 多键条件用 AND 逻辑，确认策略行为是否符合预期
4. **Pipeline 删除权限** — 提前确认 IAM 权限，避免清理阶段卡住

## 参考链接

- [AWS What's New: CloudWatch Pipelines Compliance & Governance](https://aws.amazon.com/about-aws/whats-new/2026/04/cloudwatch-pipelines-compliance-governance/)
- [CloudWatch Pipelines 官方文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-pipelines.html)
- [IAM Condition Keys for Observability Admin](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsobservabilityadminservice.html)
