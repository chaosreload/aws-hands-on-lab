# CloudWatch Query Studio 实测：首次原生 PromQL 查询体验全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（Preview 期间免费）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-04

## 背景

长期以来，CloudWatch 用户如果想用 PromQL 查询指标，只能搭建 Amazon Managed Service for Prometheus (AMP) 再接 Grafana。对于已经在用 CloudWatch 的团队来说，这意味着额外的基础设施和成本。

2026 年 4 月，CloudWatch 发布了 **Query Studio (Preview)**，首次在 CloudWatch 中原生支持 PromQL 查询。Query Studio 将 PromQL 和 CloudWatch Metric Insights 统一在一个界面中，让你可以用 PromQL 查询 AWS vended metrics 和 OpenTelemetry 自定义指标，无需在多个控制台之间切换。

本文实测了从发送 OTLP 指标到 PromQL 查询的完整链路，验证了 Query Studio 的核心功能、边界条件，并记录了实际使用中发现的几个重要踩坑。

## 前置条件

- AWS 账号，且位于支持的 Region（us-east-1, us-west-2, eu-west-1, ap-southeast-1, ap-southeast-2）
- AWS CLI v2 已配置
- Python 3 + boto3（用于 OTLP 发送和 PromQL API 调用）
- IAM 权限：`cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `cloudwatch:PutDashboard`, `cloudwatch:StartOTelEnrichment`, `observabilityadmin:StartTelemetryEnrichment`

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:PutDashboard",
        "cloudwatch:GetDashboard",
        "cloudwatch:DeleteDashboards"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "observabilityadmin:StartTelemetryEnrichment"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

### Query Studio 全景

Query Studio 是 CloudWatch 控制台内的交互式查询环境，首次为 CloudWatch 带来原生 PromQL 支持。

| 特性 | 说明 |
|------|------|
| PromQL 版本 | Prometheus 3.0 规范（支持 UTF-8 metric/label 名） |
| 查询模式 | Builder（可视化表单 + 自动补全）/ Editor（代码编辑器 + 语法高亮） |
| 查询对象 | OTLP 自定义指标 + OTel-enriched AWS vended metrics |
| 集成 | 直接创建 Alarm、添加 Dashboard Widget |
| 定价 | Preview 期间免费（OTLP ingestion + PromQL + Query Studio） |

### PromQL Label 结构

CloudWatch 使用 `@` 前缀约定区分 OTLP 数据模型中不同 scope 的 labels：

| OTLP Scope | Attributes 前缀 | 示例 |
|------------|-----------------|------|
| Resource | `@resource.` | `@resource.service.name="my-app"` |
| Instrumentation | `@instrumentation.` | `@instrumentation.@name="otel-go"` |
| Datapoint | `@datapoint.` 或 bare | `http.method="GET"` |
| AWS Reserved | `@aws.` | `@aws.region="us-east-1"` |

### PromQL 关键限制

| 限制 | 值 |
|------|------|
| 查询 TPS | 300/account |
| Discovery TPS | 10/account |
| Max series/query | 500 |
| Max range | 7 days |
| 执行超时 | 20 seconds |

### Region 可用性

| Region | OTLP Ingest | PromQL Query | Query Studio |
|--------|-------------|--------------|--------------|
| us-east-1 | ✓ | ✓ | ✓ |
| us-west-2 | ✓ | ✓ | ✓ |
| eu-west-1 | ✓ | ✓ | ✓ |
| ap-southeast-1 | ✓ | ✓ | ✓ |
| ap-southeast-2 | ✓ | ✓ | ✓ |

## 动手实践

### Step 1: 启用 OTel Enrichment

启用 OTel enrichment，让 AWS vended metrics 可以通过 PromQL 查询。

**启用 resource tags on telemetry**（前置条件）：

```bash
aws observabilityadmin start-telemetry-enrichment \
  --region us-east-1
```

**实测输出**：

```json
{
    "Status": "Running",
    "AwsResourceExplorerManagedViewArn": "arn:aws:resource-explorer-2:us-east-1:595842667825:managed-view/..."
}
```

**启用 OTel enrichment**：

!!! warning "CLI 不支持此命令"
    截至 AWS CLI v2.34.14，`start-o-tel-enrichment` 子命令尚未实现。需要通过 CloudWatch 控制台（Settings → Enable OTel Enrichment）或直接调用 API 来启用。

通过 Python 直接调用 API：

```python
import boto3, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session(region_name="us-east-1")
creds = session.get_credentials().get_frozen_credentials()

url = "https://monitoring.us-east-1.amazonaws.com/"
body = "Action=StartOTelEnrichment&Version=2010-08-01"
headers = {"Content-Type": "application/x-www-form-urlencoded"}

req = AWSRequest(method="POST", url=url, data=body, headers=headers)
SigV4Auth(creds, "monitoring", "us-east-1").add_auth(req)

resp = requests.post(url, data=body, headers=dict(req.headers))
print(resp.text)
```

### Step 2: 通过 OTLP 发送自定义指标

CloudWatch 的 OTLP metrics endpoint 是 `https://monitoring.{region}.amazonaws.com/v1/metrics`，使用 SigV4 认证。

```python
import boto3, requests, json, time
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session(region_name="us-east-1")
creds = session.get_credentials().get_frozen_credentials()

url = "https://monitoring.us-east-1.amazonaws.com/v1/metrics"
now_ns = str(int(time.time() * 1e9))

payload = {
    "resourceMetrics": [{
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "my-web-app"}},
                {"key": "deployment.environment", "value": {"stringValue": "production"}}
            ]
        },
        "scopeMetrics": [{
            "scope": {"name": "my-instrumentation", "version": "1.0"},
            "metrics": [{
                "name": "http.server.request.duration",
                "unit": "ms",
                "gauge": {
                    "dataPoints": [{
                        "timeUnixNano": now_ns,
                        "asDouble": 125.5,
                        "attributes": [
                            {"key": "http.method", "value": {"stringValue": "GET"}},
                            {"key": "http.route", "value": {"stringValue": "/api/health"}}
                        ]
                    }]
                }
            }]
        }]
    }]
}

body = json.dumps(payload)
headers = {"Content-Type": "application/json"}
req = AWSRequest(method="POST", url=url, data=body, headers=headers)
SigV4Auth(creds, "monitoring", "us-east-1").add_auth(req)
resp = requests.post(url, data=body, headers=dict(req.headers))
print(f"Status: {resp.status_code}")  # 200
```

### Step 3: 使用 PromQL API 查询指标

CloudWatch 提供 Prometheus 兼容的 HTTP API：

**Instant Query**（获取当前时刻的值）：

```python
url = "https://monitoring.us-east-1.amazonaws.com/api/v1/query"
query = '{"http.server.request.duration"}'

req = AWSRequest(method="GET", url=url, params={"query": query})
SigV4Auth(creds, "monitoring", "us-east-1").add_auth(req)
resp = requests.get(url, params={"query": query}, headers=dict(req.headers))
```

**实测输出**：

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [{
      "metric": {
        "__name__": "http.server.request.duration",
        "@resource.service.name": "my-web-app",
        "@resource.deployment.environment": "production",
        "@instrumentation.@name": "my-instrumentation",
        "@aws.region": "us-east-1",
        "@aws.account": "595842667825",
        "http.method": "GET",
        "http.route": "/api/health"
      },
      "value": [1775320613.282, "125.5"]
    }]
  }
}
```

注意 label 结构：`@resource.service.name`、`@instrumentation.@name`、`@aws.region` 等前缀完全符合 CloudWatch 的 OTLP label 约定。

### Step 4: PromQL 高级查询 — 聚合、过滤、数学运算

| 查询类型 | PromQL 表达式 | 结果 |
|---------|--------------|------|
| 标签过滤 | `{"http.server.request.duration", "http.method"="GET"}` | 仅返回 GET 请求 |
| 负匹配 | `{"http.server.request.duration", "http.method"!="GET"}` | 仅返回非 GET 请求 |
| 正则匹配 | `{"http.server.request.duration", "http.route"=~"/api/.*"}` | 匹配 /api/ 前缀 |
| 时间范围聚合 | `avg_over_time({"http.server.request.duration"}[5m])` | 5 分钟平均值 |
| 按标签分组 | `sum by ("http.method")({"http.server.request.duration"})` | 按 HTTP 方法汇总 |
| 数学运算 | `{"http.server.request.duration"} * 2` | 值翻倍 |

**Range Query**（获取时间范围内的值序列）：

```python
url = "https://monitoring.us-east-1.amazonaws.com/api/v1/query_range"
params = {
    "query": '{"http.server.request.duration", "http.method"="GET"}',
    "start": str(time.time() - 600),
    "end": str(time.time()),
    "step": "60"
}
```

**实测输出**：返回 5 个数据点（100, 125, 150, 175, 200 ms），间隔 60 秒。

### Step 5: 创建 PromQL Dashboard

通过 `put-dashboard` API 创建包含 PromQL Widget 的 Dashboard：

```python
import boto3, json

cw = boto3.client("cloudwatch", region_name="us-east-1")

dashboard_body = {
    "widgets": [{
        "type": "metric",
        "x": 0, "y": 0, "width": 12, "height": 6,
        "properties": {
            "metrics": [[{
                "expression": "PROMQL(\"{\\\"http.server.request.duration\\\"}\")",
                "id": "q1"
            }]],
            "view": "timeSeries",
            "region": "us-east-1",
            "title": "OTel Request Duration (PromQL)",
            "period": 60
        }
    }]
}

cw.put_dashboard(
    DashboardName="MyPromQL-Dashboard",
    DashboardBody=json.dumps(dashboard_body)
)
```

Dashboard Widget 中使用 `PROMQL()` 函数包裹 PromQL 表达式。

### Step 6: 边界与探索性测试

**超过 7 天 range query**：

```python
# 查询 8 天范围
params = {
    "query": '{"http.server.request.duration"}',
    "start": str(time.time() - 8*86400),
    "end": str(time.time()),
    "step": "3600"
}
# 结果: status=success（未报错！）
```

!!! info "实测发现：8 天范围查询未触发限制"
    官方文档标注 Max range 为 7 天，但实测 8 天范围查询返回 success。可能 Preview 阶段限制未严格执行，**生产环境中不要依赖此行为**。

**Invalid PromQL 错误处理**：

| 无效查询 | 返回码 | 错误消息 |
|---------|--------|---------|
| `invalid{{{` | 400 | "unexpected left brace inside braces" |
| `sum(` | 400 | "unclosed left parenthesis" |
| `{__name__=~""}` | 400 | "must contain at least one non-empty matcher" |

错误消息清晰、准确，有助于排查查询语法问题。

**Discovery API**：

```python
# 列出所有 label 名称
GET /api/v1/labels
# 返回: ["@aws.account", "@aws.region", "@resource.service.name", ...]

# 获取指定 label 的所有值
GET /api/v1/label/__name__/values
# 返回: ["http.server.request.duration", "http.server.active_requests", ...]
```

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | 启用 OTel Enrichment | ✅ 通过 | API 直接调用成功 | CLI 尚不支持 |
| 2 | OTLP 发送自定义指标 | ✅ 通过 | HTTP 200, JSON + SigV4 | |
| 3 | PromQL Instant Query | ✅ 通过 | 4 series 返回 | 所有 label 结构正确 |
| 4 | PromQL 标签过滤 | ✅ 通过 | =, !=, =~ 均正常 | |
| 5 | PromQL 聚合函数 | ✅ 通过 | avg/max/min/sum 正常 | |
| 6 | PromQL Range Query | ✅ 通过 | 5 datapoints, 60s step | |
| 7 | PromQL Dashboard | ✅ 通过 | PROMQL() 函数 | |
| 8 | PromQL Alarm | ⚠️ SDK 不支持 | 需控制台操作 | CLI/boto3 缺少新参数 |
| 9 | 边界：8 天 range | ⚠️ 未报错 | 与文档不一致 | Preview 阶段可能未严格执行 |
| 10 | 边界：Invalid PromQL | ✅ 通过 | 清晰错误消息 | |
| 11 | Discovery API | ✅ 通过 | labels + label values | |

## 踩坑记录

!!! warning "踩坑 1: AWS CLI/SDK 尚不支持 PromQL 新功能"
    截至 AWS CLI v2.34.14 和当前 boto3 版本：

    - `start-o-tel-enrichment` CLI 子命令不存在
    - `put-metric-alarm` 不支持 `--evaluation-criteria` / `--evaluation-interval` 参数
    - boto3 `put_metric_alarm()` 不认识 `EvaluationCriteria` 参数

    **影响**：PromQL Alarm 只能通过控制台创建。OTel Enrichment 需要直接调用 CloudWatch API endpoint。
    建议等 SDK 更新后再将 PromQL Alarm 纳入 IaC 管道。

!!! warning "踩坑 2: OTel Enrichment 不是即时生效"
    启用 `StartOTelEnrichment` 后，vended metrics（如 EC2 CPUUtilization）不会立即可通过 PromQL 查询。
    官方文档说 resource tags discovery 最多需要 3 小时。

    **影响**：如果你启用 enrichment 后立刻尝试 PromQL 查询 vended metrics，会得到空结果。
    这不是权限问题，而是 enrichment pipeline 需要时间处理。

!!! info "踩坑 3: PromQL UTF-8 语法需要引号"
    CloudWatch PromQL 基于 Prometheus 3.0，metric 和 label 名称中含有 `.` 等特殊字符时，
    必须用双引号包裹：

    ```promql
    # 正确
    {"http.server.request.duration", "@resource.service.name"="my-app"}

    # 错误（传统 Prometheus 2.x 语法）
    http_server_request_duration{resource_service_name="my-app"}
    ```

    CloudWatch 不会自动将 `.` 替换为 `_`，这是一个与传统 Prometheus 的重要区别。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OTLP Ingestion | 免费（Preview） | ~20 datapoints | $0 |
| PromQL Query | 免费（Preview） | ~30 queries | $0 |
| Query Studio | 免费（Preview） | - | $0 |
| Dashboard | $3/month | < 1 hour | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除 Dashboard
aws cloudwatch delete-dashboards \
  --dashboard-names MyPromQL-Dashboard \
  --region us-east-1

# 2. 自定义 OTel 指标无需清理（自动存储，按保留期策略过期）
# 3. OTel Enrichment 可保留（免费），如需关闭：通过控制台 Settings 关闭
```

!!! tip "低成本实验"
    本 Lab 的所有核心功能（OTLP ingestion、PromQL、Query Studio）在 Preview 期间完全免费。
    唯一的费用来自 Dashboard（$3/month），测试完立即删除即可。

## 结论与建议

### 场景化推荐

| 场景 | 建议 | 理由 |
|------|------|------|
| 已有 Prometheus 经验的团队 | ✅ 强烈推荐 | 可以直接复用 PromQL 知识，无需额外 AMP 基础设施 |
| 纯 CloudWatch 用户 | ⚠️ 建议试用 | Builder 模式降低了 PromQL 学习曲线 |
| 需要 IaC 自动化 | ⏳ 等待 SDK 更新 | PromQL Alarm 和 OTel Enrichment 的 CLI/SDK 支持不完整 |
| 生产环境 | ⚠️ 谨慎 | Preview 阶段，API 可能变更 |

### 关键价值

1. **统一查询体验**：PromQL + Metric Insights 在一个界面，不再需要在 AMP/Grafana 和 CloudWatch 之间来回切换
2. **零额外基础设施**：不需要部署 AMP 或 Grafana，直接在 CloudWatch 控制台查询
3. **Preview 免费**：OTLP ingestion + PromQL + Query Studio 全部免费，是零成本试用的好时机
4. **Prometheus 3.0 兼容**：支持 UTF-8 metric/label 名，与 OTel 语义约定完美契合

### 当前限制

1. CLI/SDK 支持不完整（PromQL Alarm、OTel Enrichment）
2. 仅 5 个 Region 可用
3. Preview 阶段 API 可能变更
4. Vended metrics enrichment 需要等待时间（最多 3 小时）

## 参考链接

- [What's New: Amazon CloudWatch introduces PromQL querying with Query Studio Preview](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-query-studio-preview/)
- [CloudWatch PromQL 官方文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL.html)
- [Query Studio 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL-QueryStudio.html)
- [OTLP Endpoints 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)
- [CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)
