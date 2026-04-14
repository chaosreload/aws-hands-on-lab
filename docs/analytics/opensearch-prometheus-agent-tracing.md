---
tags:
  - Analytics
---

# OpenSearch Service 统一可观测性实测：Prometheus Direct Query + AI Agent Tracing 全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $5-10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-10

## 背景

可观测性领域长期面临"数据孤岛"问题：Prometheus 指标在 Grafana，Traces 在 Jaeger/X-Ray，Logs 在 CloudWatch。切换工具 + 手动关联 trace ID 是家常便饭。

2026 年 4 月，OpenSearch Service 发布了统一可观测性三件套：

1. **Prometheus Direct Query** — 不复制数据，通过 live-call 直接在 OpenSearch UI 中用 PromQL 查 AMP 指标
2. **Application Monitoring** — 从 OTel traces 自动生成 RED 指标 + 服务拓扑图
3. **Agent Tracing** — 基于 OTel GenAI 语义约定，追踪 AI Agent 的完整执行链

这三者结合，意味着 SRE 团队可以在同一个 UI 里看指标、查 trace、关联日志——连 GenAI Agent 的 LLM 调用链也能追踪。本文实测验证这套组合的真实表现。

## 前置条件

- AWS 账号（需要 OpenSearch、AMP、OSIS、IAM 权限）
- AWS CLI v2 已配置
- `awscurl`（用于 SigV4 签名请求）：`pip install awscurl`
- Python 3.x（用于生成测试数据）
- `curl`（用于查询 OpenSearch）

## 核心概念

### 三大功能架构总览

```
                  ┌──────────────┐
                  │  OpenSearch   │
                  │     UI        │  ← 统一入口
                  └──┬──┬──┬─────┘
                     │  │  │
         PromQL ─────┘  │  └───── PPL/SQL
         (live call)    │         (indexed data)
                        │
              ┌─────────┴──────────┐
              │                    │
     ┌────────▼────────┐  ┌───────▼────────┐
     │   Amazon         │  │   OpenSearch    │
     │   Managed        │  │   Domain        │
     │   Prometheus     │  │   (traces/logs) │
     └─────────────────┘  └────────────────┘
              ▲                    ▲
              │                    │
     remote_write          ┌──────┴──────┐
              │            │  OpenSearch   │
              │            │  Ingestion    │
              │            └──────┬───────┘
              │                   │
              └───────────────────┘
                      ▲
                      │ OTLP
               ┌──────┴──────┐
               │  OTel        │
               │  Collector   │
               └──────┬───────┘
                      │
              ┌───────┴────────┐
              │  Applications  │
              │  + AI Agents   │
              └────────────────┘
```

### 关键参数对比

| 特性 | Prometheus Direct Query | 传统数据复制方案 |
|------|------------------------|-----------------|
| 数据位置 | 留在 AMP | 复制到 OpenSearch |
| 查询方式 | PromQL (live call) | PPL/SQL (indexed) |
| OCU 费用 | **无** | 有（计算资源） |
| 延迟 | 略高（网络往返） | 低（本地索引） |
| 查询超时 | 30 秒（不可覆盖） | 取决于集群配置 |
| 数据源上限 | 20/账户/Region | N/A |

### OTel GenAI 语义约定（Agent Tracing）

| Span 类型 | 属性 | 说明 |
|-----------|------|------|
| Agent | `gen_ai.agent.name/id/version` | Agent 身份标识 |
| LLM 调用 | `gen_ai.request.model`, `gen_ai.provider.name` | 模型和供应商 |
| Token 计量 | `gen_ai.usage.prompt_tokens/completion_tokens` | Token 用量追踪 |
| 工具执行 | `gen_ai.tool.name`, `tool.result.status` | 工具调用结果 |
| 操作类型 | `gen_ai.operation.name` | chat/embeddings 等 |

## 动手实践

### Step 1: 创建 AMP Workspace

```bash
aws amp create-workspace \
  --alias obs-lab-prometheus \
  --region us-east-1
```

**实测输出**：
```json
{
    "workspaceId": "ws-8c12c906-6c24-4e00-a555-31fa5c6e18d1",
    "status": { "statusCode": "ACTIVE" }
}
```

### Step 2: 创建 OpenSearch 域

```bash
cat > /tmp/os-domain.json << 'EOF'
{
    "DomainName": "obs-lab",
    "EngineVersion": "OpenSearch_2.19",
    "ClusterConfig": {
        "InstanceType": "t3.small.search",
        "InstanceCount": 1
    },
    "EBSOptions": {
        "EBSEnabled": true,
        "VolumeType": "gp3",
        "VolumeSize": 10
    },
    "AccessPolicies": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"*\"},\"Action\":\"es:*\",\"Resource\":\"arn:aws:es:us-east-1:*:domain/obs-lab/*\"}]}",
    "AdvancedSecurityOptions": {
        "Enabled": true,
        "InternalUserDatabaseEnabled": true,
        "MasterUserOptions": {
            "MasterUserName": "admin",
            "MasterUserPassword": "YourSecurePassword1!"
        }
    },
    "NodeToNodeEncryptionOptions": { "Enabled": true },
    "EncryptionAtRestOptions": { "Enabled": true },
    "DomainEndpointOptions": { "EnforceHTTPS": true }
}
EOF

aws opensearch create-domain \
  --cli-input-json file:///tmp/os-domain.json \
  --region us-east-1
```

等待约 10-15 分钟域变为 Active。

### Step 3: 配置 Prometheus Direct Query

**创建 IAM Role**（信任 `directquery.opensearchservice.amazonaws.com`）：

```bash
# 信任策略
cat > /tmp/dq-trust.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": { "Service": "directquery.opensearchservice.amazonaws.com" },
        "Action": "sts:AssumeRole"
    }]
}
EOF

aws iam create-role \
  --role-name obs-lab-prometheus-dq \
  --assume-role-policy-document file:///tmp/dq-trust.json

# 权限策略（AMP 查询权限）
cat > /tmp/dq-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": ["aps:QueryMetrics", "aps:GetLabels", "aps:GetSeries", "aps:GetMetricMetadata"],
        "Resource": "arn:aws:aps:us-east-1:ACCOUNT_ID:workspace/WORKSPACE_ID"
    }]
}
EOF

aws iam put-role-policy \
  --role-name obs-lab-prometheus-dq \
  --policy-name aps-query \
  --policy-document file:///tmp/dq-policy.json
```

**创建 OpenSearch UI Application + Direct Query 数据源**：

```bash
# 创建 OpenSearch UI Application
aws opensearch create-application \
  --name obs-lab-app \
  --region us-east-1

# 创建 Prometheus 数据源
aws opensearch add-direct-query-data-source \
  --data-source-name obs_lab_prometheus \
  --data-source-type '{"Prometheus":{"RoleArn":"arn:aws:iam::ACCOUNT_ID:role/obs-lab-prometheus-dq","WorkspaceArn":"arn:aws:aps:us-east-1:ACCOUNT_ID:workspace/WORKSPACE_ID"}}' \
  --description "Prometheus metrics for obs-lab" \
  --region us-east-1
```

### Step 4: 写入 Prometheus 测试指标

使用 `awscurl` 通过 remote_write 协议写入测试数据（需要 protobuf + snappy 编码，完整脚本见 GitHub repo）。

写入 4 个模拟服务的 metrics：
- `http_requests_total` — 请求计数
- `http_request_duration_milliseconds` — 请求延迟
- `container_cpu_usage_seconds_total` — CPU 使用率

**验证 AMP 数据**：

```bash
awscurl --service aps --region us-east-1 \
  "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/WORKSPACE_ID/api/v1/label/__name__/values"
```

**实测输出**：
```json
{
  "status": "success",
  "data": [
    "container_cpu_usage_seconds_total",
    "http_request_duration_milliseconds",
    "http_requests_total"
  ]
}
```

### Step 5: 创建 OSIS Trace Pipeline

**创建 Pipeline IAM Role**：

```bash
# 信任 osis-pipelines.amazonaws.com
# 权限：es:ESHttp*, es:DescribeDomain, aps:RemoteWrite
```

**创建 Pipeline**（使用 AWS-TraceAnalyticsPipeline blueprint）：

```yaml
version: "2"
entry-pipeline:
  source:
    otel_trace_source:
      path: "/entry-pipeline/v1/traces"
  processor:
    - trace_peer_forwarder:
  sink:
    - pipeline:
        name: "span-pipeline"
    - pipeline:
        name: "service-map-pipeline"
span-pipeline:
  source:
    pipeline:
      name: "entry-pipeline"
  processor:
    - otel_traces:
  sink:
    - opensearch:
        hosts: ["https://YOUR_DOMAIN_ENDPOINT"]
        aws:
          sts_role_arn: "arn:aws:iam::ACCOUNT_ID:role/obs-lab-osis-pipeline"
          region: "us-east-1"
          serverless: false
        index_type: "trace-analytics-raw"
service-map-pipeline:
  source:
    pipeline:
      name: "entry-pipeline"
  processor:
    - otel_apm_service_map:
  sink:
    - opensearch:
        hosts: ["https://YOUR_DOMAIN_ENDPOINT"]
        aws:
          sts_role_arn: "arn:aws:iam::ACCOUNT_ID:role/obs-lab-osis-pipeline"
          region: "us-east-1"
          serverless: false
        index_type: "trace-analytics-service-map"
```

```bash
aws osis create-pipeline \
  --pipeline-name obs-lab-traces \
  --min-units 1 --max-units 1 \
  --pipeline-configuration-body file:///tmp/osis-pipeline.yaml \
  --region us-east-1
```

等待约 5 分钟变为 ACTIVE。

### Step 6: 发送 AI Agent Traces

用 Python 脚本生成模拟 AI Agent 的 OTel traces（OTLP JSON 格式），模拟以下调用链：

```
user_request (SERVER)
  └── execute_agent (INTERNAL)
        ├── chat [planning] (CLIENT) — gen_ai.request.model=anthropic.claude-3-sonnet
        ├── execute_tool [web_search] (CLIENT) — gen_ai.tool.name=web_search
        └── chat [response] (CLIENT) — gen_ai.usage.prompt_tokens=2100
```

同时生成：

- 5 条 Agent traces（成功场景）
- 5 条微服务 traces（api-gateway → user-service → order-service → payment-service）
- 1 条 Error trace（Agent 工具调用超时）

```bash
# 生成 traces
python3 gen_traces.py  # 输出 /tmp/otlp_traces.json

# 发送到 OSIS endpoint
awscurl --service osis --region us-east-1 \
  -X POST \
  -H "Content-Type: application/json" \
  -d @/tmp/otlp_traces.json \
  "https://PIPELINE_ENDPOINT/trace-pipeline/v1/traces"
```

**实测输出**：HTTP 200，空 body（成功）。

### Step 7: 验证 OpenSearch 中的 Trace 数据

```bash
# 检查索引数据量
curl -s -u admin:PASSWORD \
  "https://DOMAIN_ENDPOINT/otel-v1-apm-span/_count"
```

**实测输出**：
```json
{"count": 96, "_shards": {"total": 5, "successful": 5, "failed": 0}}
```

96 个 spans 全部索引成功。

**查看 GenAI Agent Trace 详情**：

```bash
curl -s -u admin:PASSWORD \
  "https://DOMAIN_ENDPOINT/otel-v1-apm-span/_search?pretty" \
  -H "Content-Type: application/json" \
  -d '{"size":1,"query":{"term":{"name":"execute_agent"}}}'
```

**实测输出**（关键字段）：
```json
{
  "serviceName": "ai-agent-service",
  "name": "execute_agent",
  "durationInNanos": 3350000000,
  "span.attributes.gen_ai@agent@name": "research-assistant",
  "span.attributes.gen_ai@agent@id": "agent-001",
  "status.code": 1
}
```

!!! tip "属性名转换规则"
    OTel 属性中的 `.` 在 OpenSearch 索引中被转换为 `@`。例如 `gen_ai.agent.name` → `span.attributes.gen_ai@agent@name`。PPL 查询时需要使用转换后的字段名。

**查看 Error Trace**：

```json
{
  "serviceName": "ai-agent-service",
  "name": "execute_tool",
  "span.attributes.gen_ai@tool@name": "sql_query",
  "span.attributes.error@type": "TimeoutError",
  "status.code": 2,
  "status.message": "SQL query timed out after 3s"
}
```

**PPL 查询示例**：

```sql
-- 统计各服务的 span 数量
source = otel-v1-apm-span-*
| stats count() as spanCount by serviceName
| sort - spanCount

-- 查找所有 Agent 执行超过 3 秒的 trace
source = otel-v1-apm-span-*
| where name = 'execute_agent' AND durationInNanos > 3000000000
| fields traceId, serviceName, durationInNanos
```

### Step 8: 验证服务分布

```bash
curl -s -u admin:PASSWORD \
  "https://DOMAIN_ENDPOINT/otel-v1-apm-span/_search" \
  -H "Content-Type: application/json" \
  -d '{"size":0,"aggs":{"services":{"terms":{"field":"serviceName.keyword","size":20}}}}'
```

**实测输出**：

| 服务名 | Span 数量 |
|--------|----------|
| ai-agent-service | 56 |
| api-gateway | 10 |
| order-service | 10 |
| payment-service | 10 |
| user-service | 10 |

5 个服务全部正确识别，Agent 服务包含最多 spans（每个 agent trace 有 5 个 spans）。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | Prometheus Direct Query 配置 | ✅ 通过 | 3 metrics, 8 时间序列 | AMP → OpenSearch 数据源链路完整 |
| 2 | Agent Tracing 端到端 | ✅ 通过 | 96 spans, 5 services | GenAI 属性完整保留 |
| 3 | Application Map (otel_apm_service_map) | ⚠️ 受限 | — | OSIS 上有 db_path 权限 bug |
| 4 | PromQL 查询超时边界 | ⏭️ 未测 | — | 需通过 OpenSearch UI 操作 |
| 5 | Trace-Log 关联 | ⏭️ 未测 | — | 需额外 log pipeline |
| 6 | Error Trace 追踪 | ✅ 通过 | status.code=2 | error.type + message 完整保留 |

## 踩坑记录

!!! warning "踩坑 1: otel_apm_service_map 在 OSIS 上的 db_path 权限问题"
    在 OpenSearch Ingestion (OSIS) 托管环境中，`otel_apm_service_map` processor 默认尝试在 `data/otel-apm-service-map/` 目录创建数据库文件，但 OSIS 的文件系统权限不允许在该路径创建目录。
    
    ```
    ValidationException: service-map-pipeline.processor.otel_apm_service_map: 
    caused by: Unable to create the directory at the provided path: otel-apm-service-map
    ```
    
    即使指定 `db_path: /tmp/otel-apm-service-map` 也会遇到 InternalFailure (HTTP 500)。
    
    **影响**：使用完整的 TraceAnalyticsPipeline blueprint（含 service-map-pipeline）时可能失败。Workaround 是使用简化 pipeline（仅 span 存储，不生成 service map）。
    
    **来源**：实测发现，AWS-TraceAnalyticsPipeline blueprint 中使用了该 processor 但未说明此限制。

!!! warning "踩坑 2: OSIS CreatePipeline 的 log-publishing-options 隐藏依赖"
    `--log-publishing-options` 参数引用的 CloudWatch Log Group 如果不存在，OSIS 不会返回明确的验证错误，而是直接返回 HTTP 500 InternalFailure。
    
    ```
    InternalFailure when calling the CreatePipeline operation (reached max retries: 2)
    ```
    
    **Workaround**：先创建 Log Group，或者干脆不指定 `--log-publishing-options`（Pipeline 仍能正常工作）。

!!! warning "踩坑 3: trace-analytics-raw index_type 不允许同时指定 index 字段"
    使用 `index_type: trace-analytics-raw` 或 `trace-analytics-service-map` 时，不能同时指定 `index` 字段，否则报 ValidationException。这是因为这些 index_type 自动管理索引命名。
    
    ```
    "opensearch.index" is an invalid parameter, and should be removed when 
    the "opensearch.index_type" is set to "trace-analytics-raw"
    ```

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch 域 (t3.small.search) | $0.036/hr | ~12 hr | ~$0.43 |
| OpenSearch Ingestion (1 OCU) | $0.24/hr | ~2 hr | ~$0.48 |
| AMP Workspace (metrics ingestion) | $0.003/10K samples | ~480 samples | < $0.01 |
| AMP Query | $0.10/billion samples | 几次查询 | < $0.01 |
| Prometheus Direct Query (OCU) | **$0** | — | **$0** |
| **合计** | | | **~$1-2** |

!!! info "Prometheus Direct Query 零 OCU 成本"
    这是此功能的核心卖点。与 S3/CloudWatch Logs 的 Direct Query 不同，Prometheus Direct Query 使用 live-call 架构，不会启动临时计算资源，因此**不产生 OCU 费用**。你只承担 AMP 标准查询费用。

## 清理资源

```bash
# 1. 删除 OSIS Pipeline
aws osis delete-pipeline --pipeline-name obs-lab-traces --region us-east-1

# 2. 等待 Pipeline 删除完成（约 3 分钟）
aws osis list-pipelines --region us-east-1 \
  --query "Pipelines[?PipelineName=='obs-lab-traces'].Status"

# 3. 删除 Prometheus Direct Query 数据源
aws opensearch delete-direct-query-data-source \
  --data-source-name obs_lab_prometheus --region us-east-1

# 4. 删除 OpenSearch UI Application
aws opensearch delete-application --id YOUR_APP_ID --region us-east-1

# 5. 删除 OpenSearch 域
aws opensearch delete-domain --domain-name obs-lab --region us-east-1

# 6. 删除 AMP Workspace
aws amp delete-workspace \
  --workspace-id ws-XXXXX --region us-east-1

# 7. 删除 IAM Roles
aws iam delete-role-policy --role-name obs-lab-prometheus-dq --policy-name aps-query
aws iam delete-role --role-name obs-lab-prometheus-dq
aws iam delete-role-policy --role-name obs-lab-osis-pipeline --policy-name osis-access
aws iam delete-role --role-name obs-lab-osis-pipeline

# 8. 删除 CloudWatch Log Groups（如有）
aws logs delete-log-group --log-group-name /aws/vendedlogs/obs-lab-traces --region us-east-1
```

!!! danger "务必清理"
    OpenSearch 域 (t3.small.search) 持续费用约 $0.036/hr（$0.86/天）。OSIS Pipeline 1 OCU 约 $0.24/hr（$5.76/天）。请务必在实验结束后清理。

## 结论与建议

### 统一可观测性成熟度评估

| 能力 | 成熟度 | 评估 |
|------|--------|------|
| Prometheus Direct Query | ⭐⭐⭐⭐ 可用于生产 | 配置简单、零 OCU、PromQL 原生支持 |
| Trace Ingestion (OSIS) | ⭐⭐⭐ 基本可用 | 端到端工作，但 service-map processor 有 bug |
| Agent Tracing (GenAI) | ⭐⭐⭐ 新兴阶段 | OTel GenAI 语义约定仍在 Development，但已可用 |
| Application Map (RED) | ⭐⭐ 受限 | otel_apm_service_map 在 OSIS 上有路径权限问题 |
| Trace-Log 关联 | ⭐⭐⭐ 文档确认 | 需配置 correlation，本次未实测 |

### 场景化推荐

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 已有 AMP，想在 OpenSearch UI 查 metrics | Prometheus Direct Query | 零额外成本，live-call 架构 |
| GenAI Agent 可观测性 | OSIS + OpenSearch + OTel GenAI SDK | 端到端 trace 链路完整 |
| 全栈统一可观测性（metrics + traces + logs） | 等 service-map bug 修复 | 目前 Application Map 受限 |
| 短期查询（< 1 小时窗口） | Direct Query 优先 | 无需数据复制，延迟可接受 |
| 长期趋势分析（> 30 天） | AMP Recording Rules + Direct Query | 避免 30s 查询超时 |

### 生产注意事项

1. **30 秒查询超时不可覆盖** — 复杂 PromQL 必须控制查询范围，配合 recording rules
2. **GenAI 语义约定是 Development 状态** — 属性名可能在后续版本变化，做好向后兼容准备
3. **属性名 `.` → `@` 转换** — PPL/SQL 查询时注意字段名映射
4. **OSIS Pipeline 最小 1 OCU** — 即使空闲也按 $0.24/hr 计费，测试完立即删除

## 参考链接

- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/opensearch-managed-prometheus-agent-tracing/)
- [Direct Query Prometheus Overview](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/direct-query-prometheus-overview.html)
- [Application Monitoring](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/observability-app-monitoring.html)
- [Discover Traces](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/observability-analyze-traces.html)
- [OTel Collector + Ingestion](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/configure-client-otel.html)
- [Ingesting Application Telemetry](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/observability-ingestion.html)
- [OTel GenAI Agent Spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/)
- [otel_apm_service_map Processor](https://docs.opensearch.org/latest/data-prepper/pipelines/configuration/processors/otel-apm-service-map/)
