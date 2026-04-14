---
tags:
  - Cloud Operations
---

# CloudWatch Logs Insights 参数化查询实测：告别重复查询模板的新方式

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-14

## 背景

如果你经常用 CloudWatch Logs Insights 排查问题，一定遇到过这个场景：同一条查询改个 log level 或 service name 就要保存一份新的 saved query，时间长了一堆 `ErrorsByLevel-ERROR`、`ErrorsByLevel-WARN` 之类的查询堆在那里。

2026 年 4 月 13 日，AWS 推出了 **Saved Queries with Parameters** 功能，你可以在查询模板中定义参数占位符 `{{paramName}}`，运行时通过 `$QueryName(param="value")` 语法传入不同的值。一个模板搞定所有变体。

本文从 CLI 角度完整实测这个功能，包括基础用法、多参数、默认值、20 参数上限、查询组合，以及两个官方文档未记录的行为。

## 前置条件

- AWS 账号（需要 `logs:PutQueryDefinition`、`logs:StartQuery`、`logs:GetQueryResults`、`logs:DescribeQueryDefinitions`、`logs:DeleteQueryDefinition` 权限）
- AWS CLI v2 已配置
- 一个 CloudWatch Log Group（本文会创建测试用的）

## 核心概念

| 概念 | 说明 |
|------|------|
| 参数占位符 | `{{parameterName}}`，写在 queryString 中 |
| 调用语法 | `$QueryName(param1="value1", param2="value2")` |
| 默认值 | 每个参数可选设置 `defaultValue`，不传参时使用 |
| 参数上限 | 每个查询最多 20 个参数 |
| 查询语言限制 | **仅支持 Logs Insights QL**（不支持 OpenSearch PPL/SQL） |
| 嵌套调用 | **不支持**（查询不能引用另一个参数化查询内部） |
| 查询组合 | 支持管道 `$Q1(...) \| $Q2(...)` 串联多个参数化查询 |
| 参数名规则 | 必须以字母或下划线开头 |
| 展开后长度 | 不超过 10,000 字符 |
| 含特殊字符的查询名 | 需用反引号包围：`` $`Query Name`(param="val") `` |

## 动手实践

### Step 1: 准备测试环境

创建 Log Group 并注入包含不同 level 和 service 的 JSON 日志：

```bash
# 创建 Log Group
aws logs create-log-group \
  --log-group-name "/aws/test/query-params-lab" \
  --region us-east-1

# 创建 Log Stream
aws logs create-log-stream \
  --log-group-name "/aws/test/query-params-lab" \
  --log-stream-name "test-stream-1" \
  --region us-east-1

# 注入 8 条测试日志（3 ERROR + 2 WARN + 3 INFO，分布在 3 个 service）
NOW=$(date +%s)000
aws logs put-log-events \
  --log-group-name "/aws/test/query-params-lab" \
  --log-stream-name "test-stream-1" \
  --region us-east-1 \
  --log-events "[
    {\"timestamp\":$NOW,\"message\":\"{\\\"level\\\":\\\"ERROR\\\",\\\"serviceName\\\":\\\"OrderService\\\",\\\"message\\\":\\\"Order processing failed\\\"}\"},
    {\"timestamp\":$((NOW+1000)),\"message\":\"{\\\"level\\\":\\\"WARN\\\",\\\"serviceName\\\":\\\"OrderService\\\",\\\"message\\\":\\\"Slow database response\\\"}\"},
    {\"timestamp\":$((NOW+2000)),\"message\":\"{\\\"level\\\":\\\"INFO\\\",\\\"serviceName\\\":\\\"OrderService\\\",\\\"message\\\":\\\"Order completed successfully\\\"}\"},
    {\"timestamp\":$((NOW+3000)),\"message\":\"{\\\"level\\\":\\\"ERROR\\\",\\\"serviceName\\\":\\\"PaymentService\\\",\\\"message\\\":\\\"Payment gateway timeout\\\"}\"},
    {\"timestamp\":$((NOW+4000)),\"message\":\"{\\\"level\\\":\\\"INFO\\\",\\\"serviceName\\\":\\\"PaymentService\\\",\\\"message\\\":\\\"Payment processed\\\"}\"},
    {\"timestamp\":$((NOW+5000)),\"message\":\"{\\\"level\\\":\\\"ERROR\\\",\\\"serviceName\\\":\\\"InventoryService\\\",\\\"message\\\":\\\"Stock check failed\\\"}\"},
    {\"timestamp\":$((NOW+6000)),\"message\":\"{\\\"level\\\":\\\"WARN\\\",\\\"serviceName\\\":\\\"InventoryService\\\",\\\"message\\\":\\\"Low stock warning\\\"}\"},
    {\"timestamp\":$((NOW+7000)),\"message\":\"{\\\"level\\\":\\\"INFO\\\",\\\"serviceName\\\":\\\"InventoryService\\\",\\\"message\\\":\\\"Inventory updated\\\"}\"} 
  ]"
```

### Step 2: 创建单参数查询 + 传参执行

创建一个按 log level 过滤的参数化查询：

```bash
aws logs put-query-definition \
  --name "FilterByLevel" \
  --query-string "fields @timestamp, level, serviceName, message | filter level = \"{{logLevel}}\" | sort @timestamp desc" \
  --log-group-names "/aws/test/query-params-lab" \
  --parameters '[{"name":"logLevel","defaultValue":"ERROR","description":"Log level to filter"}]' \
  --region us-east-1
```

**实测输出**：
```json
{
    "queryDefinitionId": "d640e8c8-1f00-4a52-874f-988d44dd2aee"
}
```

运行查询，过滤 ERROR 日志：

```bash
START=$(($(date +%s) - 3600))
END=$(date +%s)

QID=$(aws logs start-query \
  --log-group-names "/aws/test/query-params-lab" \
  --start-time $START --end-time $END \
  --query-string '$FilterByLevel(logLevel=ERROR)' \
  --region us-east-1 \
  --query queryId --output text)

sleep 5
aws logs get-query-results --query-id "$QID" --region us-east-1
```

**实测输出**（关键部分）：
```json
{
    "statistics": {
        "recordsMatched": 3.0,
        "recordsScanned": 8.0
    },
    "status": "Complete"
}
```

切换参数查 WARN 日志 — **同一个查询模板，不同参数值**：

```bash
QID=$(aws logs start-query \
  --log-group-names "/aws/test/query-params-lab" \
  --start-time $START --end-time $END \
  --query-string '$FilterByLevel(logLevel=WARN)' \
  --region us-east-1 \
  --query queryId --output text)

sleep 5
aws logs get-query-results --query-id "$QID" --region us-east-1
```

**结果**：`recordsMatched: 2.0` — 正确匹配 2 条 WARN 日志。

### Step 3: 多参数 + 默认值

创建一个包含 3 个参数的查询，测试部分传参时的默认值行为：

```bash
aws logs put-query-definition \
  --name "MultiParamFilter" \
  --query-string "fields @timestamp, level, serviceName, message | filter level = \"{{logLevel}}\" | filter serviceName = \"{{svcName}}\" | sort @timestamp desc | limit {{maxResults}}" \
  --log-group-names "/aws/test/query-params-lab" \
  --parameters '[
    {"name":"logLevel","defaultValue":"ERROR","description":"Log level to filter"},
    {"name":"svcName","defaultValue":"OrderService","description":"Service name"},
    {"name":"maxResults","defaultValue":"10","description":"Max results"}
  ]' \
  --region us-east-1
```

只传 `logLevel` 和 `svcName`，让 `maxResults` 使用默认值 10：

```bash
QID=$(aws logs start-query \
  --log-group-names "/aws/test/query-params-lab" \
  --start-time $START --end-time $END \
  --query-string '$MultiParamFilter(logLevel=WARN,svcName=InventoryService)' \
  --region us-east-1 \
  --query queryId --output text)

sleep 5
aws logs get-query-results --query-id "$QID" --region us-east-1
```

**结果**：`recordsMatched: 1.0` — 正确匹配 InventoryService 的 WARN 日志，`maxResults` 默认值 10 生效。

### Step 4: 边界测试 — 20 参数上限

创建一个包含 20 个参数的查询（上限）：

```bash
# 准备 20 个参数的 JSON（保存到文件避免 CLI 转义）
cat > /tmp/20params.json << EOF
[
  {"name":"p1","defaultValue":"v1"},{"name":"p2","defaultValue":"v2"},
  {"name":"p3","defaultValue":"v3"},{"name":"p4","defaultValue":"v4"},
  {"name":"p5","defaultValue":"v5"},{"name":"p6","defaultValue":"v6"},
  {"name":"p7","defaultValue":"v7"},{"name":"p8","defaultValue":"v8"},
  {"name":"p9","defaultValue":"v9"},{"name":"p10","defaultValue":"v10"},
  {"name":"p11","defaultValue":"v11"},{"name":"p12","defaultValue":"v12"},
  {"name":"p13","defaultValue":"v13"},{"name":"p14","defaultValue":"v14"},
  {"name":"p15","defaultValue":"v15"},{"name":"p16","defaultValue":"v16"},
  {"name":"p17","defaultValue":"v17"},{"name":"p18","defaultValue":"v18"},
  {"name":"p19","defaultValue":"v19"},{"name":"p20","defaultValue":"v20"}
]
EOF

aws logs put-query-definition \
  --name "TwentyParamTest" \
  --query-string "fields @timestamp | filter @message like /{{p1}}/ | filter @message like /{{p2}}/ or @message like /{{p3}}/ or @message like /{{p4}}/ or @message like /{{p5}}/ or @message like /{{p6}}/ or @message like /{{p7}}/ or @message like /{{p8}}/ or @message like /{{p9}}/ or @message like /{{p10}}/ or @message like /{{p11}}/ or @message like /{{p12}}/ or @message like /{{p13}}/ or @message like /{{p14}}/ or @message like /{{p15}}/ or @message like /{{p16}}/ or @message like /{{p17}}/ or @message like /{{p18}}/ or @message like /{{p19}}/ or @message like /{{p20}}/" \
  --log-group-names "/aws/test/query-params-lab" \
  --parameters file:///tmp/20params.json \
  --region us-east-1
```

**结果**：成功创建 ✅

尝试 21 个参数：

```bash
# 添加第 21 个参数
cat > /tmp/21params.json << EOF
[...(前 20 个),{"name":"p21","defaultValue":"v21"}]
EOF

aws logs put-query-definition \
  --name "TwentyOneParamTest" \
  --query-string "... | filter @message like /{{p21}}/" \
  --parameters file:///tmp/21params.json \
  --region us-east-1
```

**报错**：
```
InvalidParameterException: Member must have length less than or equal to 20
```

### Step 5: 查询组合（管道串联）

创建第二个参数化查询用于计数统计：

```bash
aws logs put-query-definition \
  --name "CountByLevel" \
  --query-string "fields level | filter level = \"{{logLevel}}\" | stats count(*) as cnt" \
  --log-group-names "/aws/test/query-params-lab" \
  --parameters '[{"name":"logLevel","defaultValue":"ERROR","description":"Log level to count"}]' \
  --region us-east-1
```

用管道串联两个参数化查询：

```bash
QID=$(aws logs start-query \
  --log-group-names "/aws/test/query-params-lab" \
  --start-time $START --end-time $END \
  --query-string '$FilterByLevel(logLevel=ERROR) | $CountByLevel(logLevel=ERROR)' \
  --region us-east-1 \
  --query queryId --output text)

sleep 5
aws logs get-query-results --query-id "$QID" --region us-east-1
```

**实测输出**：
```json
{
    "results": [[{"field": "cnt", "value": "3"}]],
    "statistics": {"recordsMatched": 3.0, "recordsScanned": 8.0},
    "status": "Complete"
}
```

Logs Insights 引擎先展开 `$FilterByLevel` 过滤出 ERROR 日志，再展开 `$CountByLevel` 做统计。**两个参数化查询的管道组合正常工作**。

### Step 6: 更新已有查询添加参数

可以在已有的非参数化查询上添加参数，`queryDefinitionId` 保持不变：

```bash
# 先创建无参数查询
QDEF_ID=$(aws logs put-query-definition \
  --name "UpdateTest" \
  --query-string "fields @timestamp, level | sort @timestamp desc" \
  --log-group-names "/aws/test/query-params-lab" \
  --region us-east-1 \
  --query queryDefinitionId --output text)

# 更新为参数化查询
aws logs put-query-definition \
  --name "UpdateTest" \
  --query-definition-id "$QDEF_ID" \
  --query-string "fields @timestamp, level, message | filter level = \"{{logLevel}}\" | sort @timestamp desc" \
  --log-group-names "/aws/test/query-params-lab" \
  --parameters '[{"name":"logLevel","defaultValue":"ERROR","description":"Added via update"}]' \
  --region us-east-1
```

**结果**：`queryDefinitionId` 不变，参数已添加。通过 `describe-query-definitions` 可以确认参数定义完整返回。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | 单参数创建+执行 | ✅ 通过 | ERROR: 3条, WARN: 2条 | |
| 2 | 多参数+部分传参 | ✅ 通过 | 未传参使用默认值 | |
| 3 | 20 参数上限 | ✅ 通过 | 成功创建 | |
| 4 | 21 参数超限 | ✅ 预期报错 | `length <= 20` | |
| 5 | 查询组合（管道） | ✅ 通过 | cnt: 3 | 两个参数化查询串联 |
| 6 | 更新已有查询添加参数 | ✅ 通过 | ID 不变 | |
| 7 | 无默认值不传参 | ⚠️ 行为记录 | 0 结果，无报错 | 见踩坑 1 |
| 8 | 未使用的参数定义 | ✅ 预期报错 | `Required parameters not found` | |

## 踩坑记录

!!! warning "踩坑 1: 无默认值 + 不传参 = 静默返回空结果"
    如果参数没有设置 `defaultValue`，且运行时也没有传入参数值，查询**不会报错**。参数会被替换为空字符串，导致查询语义错误但静默完成，返回 0 条结果。
    
    **影响**：用户可能误以为"没有匹配的日志"，实际上是查询条件出了问题。
    
    **建议**：为所有参数设置合理的 `defaultValue`。
    
    *实测发现，官方未记录。*

!!! warning "踩坑 2: 所有定义的参数必须在 queryString 中使用"
    如果在 `--parameters` 中定义了参数，但 `--query-string` 中没有对应的 `{{paramName}}` 占位符，会收到：
    
    ```
    InvalidParameterException: Required parameters not found in queryString: {{paramName}}
    ```
    
    注意错误信息中说的是"Required parameters not found **in queryString**"，意味着参数定义和查询模板必须完全对应。

!!! info "注意: 引号归属规则"
    参数值中的引号是查询语法的一部分。对于字符串比较：
    
    - 模板中写 `filter level = "{{logLevel}}"`（引号包围占位符）
    - 调用时传 `logLevel=ERROR`（无引号）
    
    或者按官方示例：
    
    - 模板中写 `filter level = {{logLevel}}`
    - 调用时传 `logLevel="ERROR"`（引号在参数值中）
    
    两种方式都能工作，但建议保持一致。对于数值型参数（如 `limit {{maxResults}}`），则不需要引号。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Logs Insights 查询 | $0.0076/GB | ~0.001 GB | < $0.01 |
| PutLogEvents | $0.57/GB | < 0.001 GB | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除所有测试用的 saved queries
for QDEF_ID in $(aws logs describe-query-definitions \
  --region us-east-1 \
  --query "queryDefinitions[?contains(name,FilterByLevel) || contains(name,MultiParam) || contains(name,TwentyParam) || contains(name,SimpleFilter) || contains(name,CountByLevel) || contains(name,UpdateTest) || contains(name,NoDefault)].queryDefinitionId" \
  --output text); do
  echo "Deleting $QDEF_ID"
  aws logs delete-query-definition \
    --query-definition-id "$QDEF_ID" \
    --region us-east-1
done

# 2. 删除 Log Group
aws logs delete-log-group \
  --log-group-name "/aws/test/query-params-lab" \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 saved queries 本身不产生费用，但 Log Group 中的日志存储会持续收费。

## 结论与建议

| 场景 | 建议 |
|------|------|
| 日常排障 | 创建 2-3 个通用参数化查询（按 level、service、时间窗口），替代几十个硬编码查询 |
| 团队协作 | 参数化查询对同 Region 同 Account 的所有用户可见，统一模板减少重复 |
| 自动化 | 结合 `start-query` + 参数化语法，在脚本中动态传参查询 |
| 复杂分析 | 用管道 `$Q1(...) \| $Q2(...)` 组合多个参数化查询 |

**关键限制提醒**：

- 仅支持 Logs Insights QL，不支持 OpenSearch PPL/SQL
- 参数上限 20 个，展开后 queryString 不超过 10,000 字符
- 嵌套调用不支持（查询不能引用另一个参数化查询）
- 为所有参数设置默认值，避免静默空结果
- 运行参数化查询除了 `logs:StartQuery` 还需要 `logs:DescribeQueryDefinitions` 权限

## 参考链接

- [官方文档 - Save and re-run CloudWatch Logs Insights queries](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_Insights-Saving-Queries.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/cloudwatch-logs-insights-query-params/)
- [PutQueryDefinition API Reference](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutQueryDefinition.html)
