# AWS CloudTrail MCP Server 实战：用自然语言查询 AWS 安全事件

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $2-3（含 CloudTrail Lake，清理后停止计费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

安全团队做事件调查时，通常需要在 CloudTrail Console 中翻页、构造复杂的 CLI 命令，或者写 CloudTrail Lake SQL。这对非安全专家来说门槛不低。

AWS Labs 发布了 [CloudTrail MCP Server](https://github.com/awslabs/mcp/tree/main/src/cloudtrail-mcp-server)，让 AI 助手通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 直接查询 CloudTrail 数据。这意味着你可以用自然语言问 AI 助手："过去 24 小时谁删了 IAM Role？"——AI 在后台自动调用 CloudTrail API，返回结构化结果。

本文完整测试这个 MCP Server 的 5 个 Tools，包括 Event History 查询、CloudTrail Lake SQL 分析、以及无 Lake 时的降级体验。

## 前置条件

- AWS 账号，IAM 用户/角色需要以下权限：
    - `cloudtrail:LookupEvents`
    - `cloudtrail:ListEventDataStores`、`cloudtrail:GetEventDataStore`
    - `cloudtrail:StartQuery`、`cloudtrail:DescribeQuery`、`cloudtrail:GetQueryResults`
- Python 3.10+ 和 [uv](https://docs.astral.sh/uv/) 包管理器
- AWS CLI v2 已配置 Profile

## 核心概念

### CloudTrail MCP Server 做了什么？

它把 CloudTrail API 封装成 5 个 MCP Tools，AI 助手可以直接调用：

| MCP Tool | 对应能力 | 数据范围 |
|----------|---------|---------|
| `lookup_events` | 按属性搜索管理事件 | 最近 90 天，免费 |
| `list_event_data_stores` | 列出 CloudTrail Lake 数据存储 | — |
| `lake_query` | 执行 Trino SQL 查询 | 最长 10 年，付费 |
| `get_query_status` | 查询 Lake 查询状态 | — |
| `get_query_results` | 获取 Lake 查询结果（分页） | — |

### Event History vs CloudTrail Lake

| 特性 | Event History | CloudTrail Lake |
|------|:------------:|:---------------:|
| 保留期 | 90 天 | 最长 10 年（3,653 天） |
| 查询语言 | 属性过滤（单一属性） | Trino SQL（复杂聚合） |
| 跨 Region | ❌ | ✅ |
| 跨账号 | ❌ | ✅（Organizations） |
| 费用 | 免费 | 按摄入量 + 扫描量计费 |
| 需要配置 | 默认开启 | 需创建 Event Data Store |

## 动手实践

### Step 1: 安装 MCP Server

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（如已安装可跳过）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

验证 MCP Server 可以正常启动：

```bash
uvx awslabs.cloudtrail-mcp-server@latest --help
```

### Step 2: 配置 MCP Client

在你的 MCP Client（Kiro / Cursor / VS Code）的 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "awslabs.cloudtrail-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.cloudtrail-mcp-server@latest"],
      "env": {
        "AWS_PROFILE": "your-aws-profile",
        "FASTMCP_LOG_LEVEL": "ERROR"
      },
      "transportType": "stdio"
    }
  }
}
```

!!! tip "配置文件位置"
    - **Kiro**: `~/.kiro/settings/mcp.json`
    - **VS Code**: `.vscode/mcp.json` 或全局设置
    - **Cursor**: MCP 设置面板

### Step 3: 使用 lookup_events 查询最近事件

配置完成后，在 AI 助手中用自然语言提问即可。以下是背后实际调用的 MCP Tool 参数和返回值。

**查询最近 5 个管理事件：**

```json
// MCP Tool: lookup_events
{
  "max_results": 5,
  "region": "us-east-1"
}
```

返回示例（精简）：

```json
{
  "events": [
    {
      "EventId": "07dae4e3-f74a-44d9-...",
      "EventName": "GetResource",
      "EventTime": "2026-03-26T15:12:04Z",
      "EventSource": "cloudcontrolapi.amazonaws.com",
      "Username": "resource-explorer-2"
    }
  ],
  "next_token": "kbOt5LlZe++...",
  "query_params": {
    "start_time": "2026-03-25T15:15:17Z",
    "end_time": "2026-03-26T15:15:17Z"
  }
}
```

**按用户名过滤——"谁在操作我的账号？"：**

```json
// MCP Tool: lookup_events
{
  "attribute_key": "Username",
  "attribute_value": "awswhatsnewtest",
  "max_results": 10,
  "region": "us-east-1"
}
```

**按 API 名称过滤——"有人删了什么东西吗？"：**

```json
// MCP Tool: lookup_events
{
  "attribute_key": "EventName",
  "attribute_value": "DeleteRole",
  "max_results": 5,
  "region": "us-east-1"
}
```

`lookup_events` 支持 8 种过滤属性：`EventId`、`EventName`、`ReadOnly`、`Username`、`ResourceType`、`ResourceName`、`EventSource`、`AccessKeyId`。

### Step 4: 使用 CloudTrail Lake 进行高级分析

如果你需要更强大的查询（跨属性、聚合、长时间范围），需要先创建 CloudTrail Lake Event Data Store。

**创建 Event Data Store（CLI）：**

```bash
aws cloudtrail create-event-data-store \
  --name "my-security-analysis-eds" \
  --retention-period 30 \
  --region us-east-1 \
  --no-multi-region-enabled \
  --advanced-event-selectors '[{
    "Name": "Management events",
    "FieldSelectors": [{
      "Field": "eventCategory",
      "Equals": ["Management"]
    }]
  }]' \
  --profile your-aws-profile
```

!!! warning "CloudTrail Lake 按量计费"
    Event Data Store 创建后即开始按摄入量计费。测试完成后务必删除。

**列出 Event Data Stores（确认 EDS ID）：**

```json
// MCP Tool: list_event_data_stores
{
  "region": "us-east-1"
}
```

返回：

```json
[
  {
    "event_data_store_arn": "arn:aws:cloudtrail:us-east-1:123456789012:eventdatastore/6f69f0eb-...",
    "name": "my-security-analysis-eds",
    "multi_region_enabled": false
  }
]
```

**执行 SQL 查询——按 API 调用统计：**

```json
// MCP Tool: lake_query
{
  "sql": "SELECT eventname, count(*) as cnt FROM <YOUR-EDS-ID> WHERE eventtime > TIMESTAMP '2026-03-26 00:00:00' GROUP BY eventname ORDER BY cnt DESC LIMIT 10",
  "region": "us-east-1"
}
```

返回：

```json
{
  "query_id": "1604b8f2-32e3-4b05-...",
  "query_status": "FINISHED",
  "query_statistics": {
    "EventsMatched": 3,
    "EventsScanned": 5,
    "BytesScanned": 23446,
    "ExecutionTimeInMillis": 1213
  },
  "query_result_rows": [
    [{"eventname": "DescribeQuery"}, {"cnt": "3"}],
    [{"eventname": "GetQueryResults"}, {"cnt": "1"}],
    [{"eventname": "StartQuery"}, {"cnt": "1"}]
  ]
}
```

**执行复杂查询——用户活动详情：**

```json
// MCP Tool: lake_query
{
  "sql": "SELECT useridentity.principalid, eventsource, eventname, eventtime, sourceipaddress FROM <YOUR-EDS-ID> WHERE eventtime > TIMESTAMP '2026-03-26 00:00:00' ORDER BY eventtime DESC LIMIT 5",
  "region": "us-east-1"
}
```

返回包含完整的用户身份、来源 IP、时间戳等信息，AI 助手可以直接分析这些数据给出安全建议。

## 测试结果

### MCP Server vs AWS CLI 性能对比

| 指标 | MCP Server（含冷启动） | AWS CLI | 差异 |
|------|:---------------------:|:-------:|:----:|
| 执行时间 | ~1.69s | ~2.24s | MCP 快 25% |
| 数据一致性 | ✅ 完全一致 | ✅ 基准 | — |

!!! note "实测说明"
    MCP Server 通过 uvx 启动有约 1.5 秒冷启动时间。在持久化 MCP Client 连接中（Kiro/Cursor），后续查询延迟会更低。

### 各 Tool 测试结果

| Tool | 场景 | 结果 |
|------|------|------|
| `lookup_events` | 无过滤查询 | ✅ 返回最近 24h 事件 |
| `lookup_events` | Username 过滤 | ✅ 精确过滤 |
| `lookup_events` | EventName 过滤 | ✅ 精确过滤 |
| `list_event_data_stores` | 无 EDS 存在 | ✅ 返回空列表，无报错 |
| `list_event_data_stores` | 有 EDS | ✅ 返回完整配置 |
| `lake_query` | 无效 EDS ID | ✅ 返回 InvalidQueryStatementException |
| `lake_query` | GROUP BY 聚合 | ✅ 正确统计 |
| `lake_query` | 多列 + 嵌套字段 | ✅ 支持 useridentity.principalid |

### 关键发现

1. **零基础设施部署**：纯客户端 MCP Server，`uvx` 一条命令安装，不需要在 AWS 上部署任何资源
2. **渐进式体验**：没有 CloudTrail Lake 也能用 `lookup_events` 查 90 天免费事件；需要高级分析再开 Lake
3. **结构化输出**：同时返回 `text` 和 `structuredContent`，AI Agent 可以直接解析结构化数据
4. **分页支持完善**：`lookup_events` 返回 `query_params`，分页时必须用完全相同的参数

## 踩坑记录

!!! warning "CloudTrail Lake 数据延迟"
    新建 Event Data Store 后，需要 **5-10 分钟** 才会开始有数据可查。初次查询返回空结果不要慌，等几分钟再试。（实测发现，官方文档提到"CloudTrail typically delivers events within an average of about 5 minutes"，已查文档确认。）

!!! warning "lake_query SQL 语法"
    CloudTrail Lake 使用 Trino SQL 语法。时间条件需要用 `TIMESTAMP '2026-01-01 00:00:00'` 格式，不能用普通字符串。EDS ID 直接放在 FROM 子句中，不需要引号。

!!! warning "LookupEvents API 限流"
    LookupEvents API 限制为 **2 次/秒/账号/Region**。如果用 AI Agent 频繁查询，可能触发 ThrottlingException。建议在 MCP Client 中设置合理的查询间隔。（已查文档确认。）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudTrail Event History 查询 | 免费 | — | $0 |
| CloudTrail Lake EDS 摄入 | $2.50/GB | ~0.02 GB | ~$0.05 |
| CloudTrail Lake 查询扫描 | $0.005/GB | ~0.02 GB | ~$0.01 |
| MCP Server | 开源免费 | — | $0 |
| **合计** | | | **~$0.06** |

## 清理资源

```bash
# 1. 关闭 Event Data Store 的终止保护
aws cloudtrail update-event-data-store \
  --event-data-store <YOUR-EDS-ID> \
  --no-termination-protection-enabled \
  --region us-east-1 \
  --profile your-aws-profile

# 2. 删除 Event Data Store
aws cloudtrail delete-event-data-store \
  --event-data-store <YOUR-EDS-ID> \
  --region us-east-1 \
  --profile your-aws-profile
```

!!! danger "务必清理"
    CloudTrail Lake Event Data Store 持续计费（按摄入量）。Lab 完成后请删除 EDS 避免产生意外费用。

## 结论与建议

**CloudTrail MCP Server 适合谁？**

- ✅ 安全团队日常事件调查——用自然语言快速定位"谁做了什么"
- ✅ DevOps 排查——"过去 1 小时哪些 API 调用失败了？"
- ✅ 合规审计——结合 CloudTrail Lake SQL 做定期合规检查
- ✅ SA/架构师 Demo——展示 MCP 如何简化安全运维

**vs 现有方案：**

| 方案 | 易用性 | 灵活性 | 成本 |
|------|:-----:|:------:|:---:|
| CloudTrail Console | ⭐⭐⭐ | ⭐⭐ | 免费 |
| AWS CLI | ⭐⭐ | ⭐⭐⭐ | 免费 |
| **CloudTrail MCP Server** | **⭐⭐⭐⭐⭐** | **⭐⭐⭐⭐** | **免费/Lake 付费** |
| CloudTrail Lake Console | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 付费 |

**生产环境建议：**

1. **最小权限原则**：MCP Server 使用的 IAM 角色只赋予 6 个必要权限
2. **仅本地运行**：MCP Server 设计为本地运行，不要暴露到网络
3. **CloudTrail Lake 按需开启**：90 天内的简单查询用免费的 Event History 足够

## 参考链接

- [CloudTrail MCP Server GitHub](https://github.com/awslabs/mcp/tree/main/src/cloudtrail-mcp-server)
- [CloudTrail MCP Server 文档](https://awslabs.github.io/mcp/servers/cloudtrail-mcp-server)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/09/aws-cloudtrail-mcp-server-enhanced-security-analysis/)
- [CloudTrail Event History 文档](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html)
- [CloudTrail Lake 文档](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-lake.html)
