---
tags:
  - Cloud Operations
---

# CloudWatch OpenTelemetry Metrics 实战：原生 OTel 指标摄入与 PromQL 查询全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0（Preview 期间免费）
    - **Region**: us-east-1（Preview 支持 5 个 Region）
    - **最后验证**: 2026-04-04

## 背景

在 CloudWatch 原生支持 OpenTelemetry (OTel) metrics 之前，将 OTel 指标发送到 CloudWatch 需要借助 CloudWatch Agent 或 ADOT Collector 的 `awsemf` exporter 将 OTel 数据转换为 CloudWatch Embedded Metric Format——这意味着额外的转换逻辑、Namespace/Dimension 的手动映射，以及丢失 OTel 丰富的 label 语义。

2026 年 4 月，CloudWatch 推出了原生 OTLP metrics endpoint（Public Preview），你可以通过标准 OpenTelemetry Protocol 直接将指标发送到 CloudWatch，并使用 PromQL 查询——不需要自建 Prometheus，不需要转换逻辑，还能与 70+ AWS 服务的 vended metrics 联合查询。

本文通过实测验证从 OTel Collector 配置、指标发送、PromQL 查询到边界条件的完整链路，揭示了几个官方文档未明确提及的关键行为。

## 前置条件

- AWS 账号（Region 需在 Preview 支持范围内）
- AWS CLI v2 已配置
- `awscurl`（用于向 PromQL API 发送 SigV4 签名请求）

!!! note "Preview Region"
    目前支持：US East (N. Virginia)、US West (Oregon)、Asia Pacific (Sydney)、Asia Pacific (Singapore)、Europe (Ireland)

<details>
<summary>IAM 权限：CloudWatchAgentServerPolicy（点击展开）</summary>

OTel Collector 发送指标需要 `CloudWatchAgentServerPolicy` 托管策略。如果在 EC2 上运行，将该策略附加到实例的 IAM Role 即可。

</details>

## 核心概念

### 架构一览

```
应用（OTel SDK）→ OTel Collector → OTLP HTTP endpoint → CloudWatch → PromQL（Query Studio）
                                          ↓
                                   SigV4 认证（必须）
```

### 关键参数

| 参数 | 值 |
|------|-----|
| Metrics endpoint | `https://monitoring.{region}.amazonaws.com/v1/metrics` |
| 认证方式 | AWS SigV4（service=`monitoring`） |
| 协议 | HTTP only（不支持 gRPC） |
| 格式 | Binary (protobuf)、JSON |
| 压缩 | gzip、none |
| 最大 TPS | 500/账号 |
| 最大 datapoints/请求 | 1,000 |
| 最大 labels/datapoint | 150 |
| 最大请求大小 | 1 MB（未压缩） |
| 定价 | Preview 期间免费 |

### 三种接入方式

| 特性 | OTel Collector | Custom OTel Collector | ADOT SDK（无 Collector） |
|------|---------------|----------------------|--------------------------|
| 支持信号 | Logs, Metrics, Traces | Logs, Traces, Metrics | Metrics, Traces |
| AWS 基础设施属性增强 | ❌ | ✅ | ✅ |
| Runtime 指标关联 | ❌ | ✅ | ❌ |
| 部署复杂度 | 低 | 中 | 最低 |

### PromQL label 结构

OTel 指标进入 CloudWatch 后，PromQL 查询时的 label 遵循 UTF-8 命名规范：

| OTLP 作用域 | 前缀 | 示例 |
|-------------|------|------|
| Resource | `@resource.` | `@resource.service.name="myservice"` |
| Instrumentation Scope | `@instrumentation.` | `@instrumentation.@name="otel-go/metrics"` |
| Datapoint | 无前缀（向后兼容） | `method="GET"` |
| AWS 系统 | `@aws.` | `@aws.account="123456789"` |

## 动手实践

### Step 1: 安装并配置 OTel Collector

下载 OTel Collector Contrib 发行版：

```bash
curl -sL https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.120.0/otelcol-contrib_0.120.0_linux_amd64.tar.gz \
  -o otelcol-contrib.tar.gz
tar xzf otelcol-contrib.tar.gz otelcol-contrib
chmod +x otelcol-contrib
./otelcol-contrib --version
```

```
otelcol-contrib version 0.120.1
```

创建 Collector 配置文件 `otel-config-metrics.yaml`：

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    send_batch_size: 100
    timeout: 5s

exporters:
  otlphttp/metrics:
    compression: gzip
    metrics_endpoint: https://monitoring.us-east-1.amazonaws.com/v1/metrics
    auth:
      authenticator: sigv4auth/metrics
  debug:
    verbosity: detailed

extensions:
  sigv4auth/metrics:
    region: "us-east-1"
    service: "monitoring"

service:
  extensions: [sigv4auth/metrics]
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp/metrics, debug]
```

!!! warning "sigv4auth 是必需的"
    CloudWatch OTLP endpoint 只支持 SigV4 认证。`sigv4authextension` 会自动从 AWS credentials chain 获取凭证（EC2 Instance Profile、环境变量或 `~/.aws/credentials`）。

启动 Collector：

```bash
AWS_PROFILE=your-profile ./otelcol-contrib --config otel-config-metrics.yaml
```

```
2026-04-04T17:02:25.956Z  info  service@v0.120.0/service.go:258  Starting otelcol-contrib...
2026-04-04T17:02:25.956Z  info  otlpreceiver  Starting HTTP server  {"endpoint": "0.0.0.0:4318"}
2026-04-04T17:02:25.956Z  info  Everything is ready. Begin running and processing data.
```

### Step 2: 发送 OTel Metrics（Counter + Gauge + Histogram）

通过 OTLP HTTP 协议发送三种指标类型：

```python
import json, time, subprocess, random

ts_ns = str(int(time.time() * 1e9))

payload = {
    "resourceMetrics": [{
        "resource": {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "otel-hands-on-test"}},
                {"key": "host.name", "value": {"stringValue": "dev-server"}}
            ]
        },
        "scopeMetrics": [{
            "scope": {"name": "hands-on-lab", "version": "1.0.0"},
            "metrics": [
                {
                    "name": "http_requests_total",
                    "unit": "1",
                    "sum": {
                        "dataPoints": [{
                            "attributes": [
                                {"key": "method", "value": {"stringValue": "GET"}},
                                {"key": "status_code", "value": {"stringValue": "200"}}
                            ],
                            "startTimeUnixNano": ts_ns,
                            "timeUnixNano": ts_ns,
                            "asInt": "42"
                        }],
                        "aggregationTemporality": 2,
                        "isMonotonic": True
                    }
                },
                {
                    "name": "cpu_utilization",
                    "unit": "%",
                    "gauge": {
                        "dataPoints": [{
                            "attributes": [{"key": "cpu", "value": {"stringValue": "cpu0"}}],
                            "timeUnixNano": ts_ns,
                            "asDouble": 65.5
                        }]
                    }
                }
            ]
        }]
    }]
}

# 发送到 Collector 的 OTLP HTTP receiver
result = subprocess.run(
    ["curl", "-s", "-X", "POST", "http://localhost:4318/v1/metrics",
     "-H", "Content-Type: application/json",
     "-d", json.dumps(payload)],
    capture_output=True, text=True
)
print(result.stdout)
```

```json
{"partialSuccess":{}}
```

`partialSuccess` 为空对象 = 所有指标都成功接收。

### Step 3: 用 PromQL 查询 OTel 指标

CloudWatch 提供 Prometheus 兼容的 PromQL API，endpoint 格式：

```
https://monitoring.{region}.amazonaws.com/api/v1/query
```

!!! tip "awscurl 使用"
    PromQL API 同样需要 SigV4 签名。使用 `awscurl` 或 AWS SDK 的 SigV4 签名模块发送请求。

**即时查询 — Counter 指标**：

```bash
awscurl --service monitoring --region us-east-1 \
  -X POST "https://monitoring.us-east-1.amazonaws.com/api/v1/query" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "query=http_requests_total"
```

```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [{
      "metric": {
        "__name__": "http_requests_total",
        "@resource.service.name": "otel-hands-on-test",
        "@resource.host.name": "dev-server",
        "@aws.account": "595842667825",
        "@aws.region": "us-east-1",
        "method": "GET",
        "status_code": "200",
        "__type__": "Sum",
        "__temporality__": "cumulative"
      },
      "value": [1775322356.053, "92"]
    }]
  }
}
```

注意 label 结构：OTel resource attributes 用 `@resource.` 前缀，datapoint attributes（`method`、`status_code`）直接使用原名。

**Rate 计算**：

```bash
# 计算 5 分钟内的请求速率
awscurl ... -d "query=rate(http_requests_total[5m])"
```

```json
{"value": [1775322368.161, "0.18333333333333332"]}
```

**Label 过滤查询**：

```bash
awscurl ... -d 'query=http_requests_total{method="GET"}'
```

**Range 查询**：

```bash
awscurl ... -X POST ".../api/v1/query_range" \
  -d "query=cpu_utilization&start=1775321580&end=1775322360&step=60"
```

```json
{
  "resultType": "matrix",
  "result": [{
    "metric": {"__name__": "cpu_utilization", "cpu": "cpu0", ...},
    "values": [
      [1775322180.0, "65.5"],
      [1775322240.0, "87.3"],
      [1775322300.0, "87.3"],
      [1775322360.0, "87.3"]
    ]
  }]
}
```

### Step 4: 查看统一命名空间 — OTel + AWS Vended Metrics

查询所有可用 metric 名称：

```bash
awscurl --service monitoring --region us-east-1 \
  "https://monitoring.us-east-1.amazonaws.com/api/v1/label/__name__/values"
```

**实测输出**（部分）：

```json
{
  "status": "success",
  "data": [
    "ApproximateAgeOfOldestMessage",
    "CPUUtilization",
    "NetworkIn",
    "NetworkOut",
    "StatusCheckFailed",
    "cpu_utilization",
    "http_requests_total",
    "request_duration_seconds"
  ]
}
```

AWS vended metrics（`CPUUtilization`、`NetworkIn`）和自定义 OTel metrics（`http_requests_total`、`cpu_utilization`）出现在同一个 PromQL 命名空间中。这验证了公告中 "combine your custom OpenTelemetry metrics with AWS vended metrics" 的声明。

!!! info "启用 Vended Metrics in PromQL"
    要在 PromQL 中查询 AWS vended metrics 的数据，需要先启用 OTel Enrichment：
    
    ```bash
    # 方式 1：CloudWatch Console → Settings → Enable OTel Enrichment for AWS Metrics
    # 方式 2：API 调用
    awscurl --service monitoring --region us-east-1 \
      -X POST "https://monitoring.us-east-1.amazonaws.com" \
      -d "Action=StartOTelEnrichment&Version=2010-08-01"
    ```
    
    启用后还需要开启 Resource Tags：
    
    ```bash
    awscurl --service monitoring --region us-east-1 \
      -X POST "https://monitoring.us-east-1.amazonaws.com" \
      -d "Action=PutManagedInsightRules&ManagedRules.member.1.TemplateName=CloudWatch-Resource-Tags&Version=2010-08-01"
    ```

### Step 5: 边界测试 — 超过 150 个 Labels

构造一个包含 151 个 attribute 的 metric datapoint：

```python
attrs = [{"key": f"label_{i:03d}", "value": {"stringValue": f"value_{i}"}} for i in range(151)]
# ... 构造 payload 并发送
```

OTel Collector 本地接收成功（HTTP 200），但转发到 CloudWatch 时被拒绝：

```
error  Exporting failed. Dropping data.
error: "Permanent error: HTTP Status Code 400, 
  Message=Maximum number of attributes exceeded: 150 
  [ResourceMetrics.1.ScopeMetrics.1.Metrics.1.Datapoint.1]."
dropped_items: 1
```

!!! warning "Collector 不做 label 数量校验"
    OTel Collector 的 OTLP receiver 会接受任意数量的 attributes（返回 200），但 CloudWatch endpoint 会在服务端拒绝超过 150 个 labels 的 datapoint。这意味着数据在 Collector 端看似成功，实际已被丢弃。建议在 Collector 配置中添加 `attributes` processor 来提前限制 label 数量。

### Step 6: Histogram 指标的特殊行为

发送 OTel Histogram 类型指标后，查询结果揭示了一个重要差异：

```bash
awscurl ... -d "query=request_duration_seconds"
```

```json
{
  "result": [{
    "metric": {
      "__name__": "request_duration_seconds",
      "__type__": "Histogram",
      ...
    },
    "histogram": [1775322587.471, {
      "count": "40",
      "sum": "10",
      "buckets": [
        [0, "0.00506...", "0.00552...", "3"],
        [0, "0.01562...", "0.01703...", "2"],
        ...
      ]
    }]
  }]
}
```

!!! warning "OTel Histogram ≠ Prometheus Histogram"
    CloudWatch 将 OTel Histogram 转换为**原生直方图格式**，而非标准 Prometheus 的 `_bucket`/`_count`/`_sum` 后缀 convention。查询时使用原始 metric name（`request_duration_seconds`），不能使用 `request_duration_seconds_bucket`。返回的 `histogram` 字段包含 exponential bucket boundaries，而非 Prometheus 的固定边界 `le` label。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | OTel Collector → OTLP endpoint（3 种指标类型） | ✅ 通过 | 11 轮全部 HTTP 200 | Counter/Gauge/Histogram 均支持 |
| 2 | PromQL 查询（即时/范围/rate/过滤） | ✅ 通过 | 5 种查询类型全部返回正确数据 | 标准 PromQL 语法兼容 |
| 3 | 超 150 labels 边界测试 | ✅ 预期拒绝 | HTTP 400, "Maximum number of attributes exceeded" | Collector 不校验，CloudWatch 端拒绝 |
| 4 | OTel + AWS vended 统一命名空间 | ✅ 通过 | label values API 返回两类 metrics | 需启用 OTel Enrichment |
| 5 | CloudWatch Alarm on OTel metrics | ⚠️ 部分 | Alarm 创建成功，但 Metrics Insights SQL 无法读取 OTel 数据 | OTel metrics 只在 PromQL 域 |
| 6 | Histogram 查询格式 | ✅ 通过 | 返回原生直方图格式 | 非 Prometheus `_bucket` convention |

## 踩坑记录

!!! warning "踩坑 1: OTel 指标不出现在 CloudWatch 传统指标 API 中"
    OTel OTLP 发送的指标存在于一个**独立的数据平面**，不能通过 `aws cloudwatch get-metric-data` 或 Metrics Insights SQL 查询。这意味着：
    
    - 现有的 CloudWatch Dashboard widgets 如果使用 Metrics Insights SQL，无法查询 OTel 指标
    - 基于 Metrics Insights SQL 的 Alarm 无法触发 OTel 指标
    - 必须使用 PromQL API（`/api/v1/query`）或 Query Studio 查询
    
    **实测发现，官方未明确记录。**

!!! warning "踩坑 2: Histogram 指标格式与 Prometheus 标准不同"
    如果你习惯了 Prometheus 的 `histogram_quantile(0.99, rate(xxx_bucket[5m]))` 查询模式，在 CloudWatch 中需要调整——这里没有 `_bucket` 后缀的 metric，Histogram 作为原生类型存储，bucket 边界是指数分布而非固定边界。
    
    **实测发现，官方未明确记录。**

!!! info "踩坑 3: CLI 尚未支持 PromQL 相关操作"
    截至 AWS CLI v2.34.14，尚无 PromQL 查询或 `start-otel-enrichment` 等命令。需要使用 `awscurl` 或直接调用 API endpoint。Preview 阶段的功能 CLI 支持通常会滞后。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OTel Metrics 摄入 | $0（Preview） | ~60 datapoints | $0 |
| PromQL 查询 | $0（Preview） | ~20 次查询 | $0 |
| **合计** | | | **$0** |

## 清理资源

```bash
# 1. 停止 OTel Collector
pkill -f otelcol-contrib

# 2. 删除测试告警（如已创建）
aws cloudwatch delete-alarms \
  --region us-east-1 \
  --alarm-names "otel-http-requests-high"

# 3. 清理临时文件
rm -f otel-config-metrics.yaml otelcol-contrib.tar.gz otelcol-contrib
rm -f /tmp/otel-metrics-*.json /tmp/otel-boundary-test.json
```

!!! tip "OTel 指标数据无需清理"
    Preview 期间的 OTel 指标数据不产生费用，且没有手动删除 PromQL 指标的 API。数据会按照保留策略自动过期。

## 结论与建议

### 适用场景推荐

| 场景 | 是否推荐 | 理由 |
|------|---------|------|
| 已有 OTel 基础设施，想统一到 CloudWatch | ✅ 强烈推荐 | 零转换逻辑，直接发送 |
| 需要 PromQL + AWS 原生指标联合查询 | ✅ 推荐 | 唯一能在同一 PromQL 中查 OTel + AWS vended metrics 的方案 |
| 替代自建 Prometheus | ✅ 推荐 | 免运维，Preview 免费，GA 后按量付费 |
| 需要 CloudWatch Alarm on OTel metrics | ⚠️ 等 GA | 目前 PromQL-native alarm 支持有限 |
| 需要 gRPC 协议 | ❌ 不适用 | 仅支持 HTTP |

### 从 EMF 方案迁移的建议

如果你目前使用 `awsemf` exporter 将 OTel 指标转换为 CloudWatch Embedded Metric Format：

1. **新项目**：直接使用 OTLP endpoint，享受原生 label 语义和 PromQL 查询
2. **迁移中**：可以同时保留两个 exporter（`awsemf` + `otlphttp`），逐步验证 PromQL 查询等价性
3. **暂不迁移**：如果依赖 Metrics Insights SQL 查询或现有 Dashboard，保持 EMF 方案直到 GA

### 生产部署注意事项

1. **TPS 限制**：500 TPS/账号，高吞吐场景需要提前规划 batch 策略
2. **Label 数量**：150 上限比 Prometheus 默认宽松，但仍需在 Collector 端做好过滤
3. **仅 HTTP**：如果你的 SDK 默认使用 gRPC，需要修改 exporter 配置
4. **Region 限制**：Preview 仅 5 个 Region，生产部署前确认你的 Region 在列

## 参考链接

- [CloudWatch OpenTelemetry 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OpenTelemetry-Sections.html)
- [OTLP Endpoints 限制](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)
- [PromQL 查询语法](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-PromQL-Querying.html)
- [启用 Vended Metrics in PromQL](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTelEnrichment.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-opentelemetry-metrics/)
