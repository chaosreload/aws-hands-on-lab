---
tags:
  - Cloud Operations
---

# CloudWatch Pipelines 条件处理实测：用 `when` 参数实现日志的精准手术刀

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（CloudWatch Logs 标准摄取费率）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-11

## 背景

CloudWatch Logs 的日志成本一直是 DevOps 团队的痛点。传统的 Transformer 处理器对所有日志"一视同仁"——要么全部解析，要么全部添加字段，无法针对特定条件做差异化处理。

2026 年 4 月，AWS 宣布 CloudWatch Pipelines 支持**条件处理（Conditional Processing）**和 **Drop Events 处理器**。这意味着你现在可以：

- 根据日志内容决定某个处理器是否执行
- 为不同级别的日志添加不同的标签
- 按条件删除敏感字段
- 选择性地对特定日志做 Grok 解析

本文通过 API 层面的实测，揭示了条件处理的**实际工作方式**、**未文档化的参数名称**，以及**当前的限制**。

## 前置条件

- AWS 账号（需要 CloudWatch Logs 读写权限）
- AWS CLI v2 已配置
- Python 3 + boto3（用于绕过 CLI 客户端校验，直接调用 API）

## 核心概念

### CloudWatch Pipelines 架构

CloudWatch Pipelines 是一个统一的控制面，底层有两种执行引擎：

| 数据源类型 | 底层引擎 | 管理 API | 条件处理支持 |
|-----------|---------|---------|------------|
| CloudWatch Logs | Logs Transformer | `aws logs put-transformer` | ✅ `when` 参数（API 已支持） |
| 第三方/S3 源 | OSIS (Data Prepper) | `aws osis create-pipeline` | ✅ Data Prepper 原生条件语法 |

### 条件处理的两个层级

公告提到了两种条件：

| 条件类型 | 作用域 | 说明 |
|---------|--------|------|
| **run-when** | 整个处理器 | 条件不满足时跳过整个处理器 |
| **entry-level condition** | 处理器内的单个操作 | 控制每个 entry/action 是否对单条日志生效 |

### 条件表达式语法

条件表达式使用 JSON Pointer 引用日志字段，支持丰富的运算符：

| 运算符 | 示例 | 说明 |
|--------|------|------|
| `==`, `!=` | `/level == "ERROR"` | 等值/不等 |
| `<`, `<=`, `>`, `>=` | `/code >= 400` | 数值比较 |
| `and`, `or` | `/level == "ERROR" or /code >= 400` | 逻辑组合 |
| `not (...)` | `not (/level == "DEBUG")` | 逻辑否定（需括号） |
| `in {...}` | `/level in {"ERROR", "WARN"}` | 集合包含 |
| `== null` | `/field == null` | 空值检查 |

## 动手实践

### Step 1: 发现 API 参数名

!!! warning "关键发现：`when` 是条件参数名，但 CLI/SDK 尚未更新"
    截至 2026-04-11，AWS CLI 2.34.26 和 boto3/botocore 的 `Processor` 数据模型中**不包含**条件参数。需要绕过客户端校验才能使用。

由于 CLI 的客户端校验会拒绝未知参数，我们使用 Python 直接签名 HTTP 请求：

```python
import boto3, json
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request

session = boto3.Session()  # 使用你的 profile
credentials = session.get_credentials().get_frozen_credentials()
endpoint = "https://logs.us-east-1.amazonaws.com"

def call_logs_api(action, payload):
    """直接调用 CloudWatch Logs API，绕过 SDK 客户端校验"""
    headers = {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": f"Logs_20140328.{action}",
    }
    data = json.dumps(payload).encode("utf-8")
    request = AWSRequest(method="POST", url=endpoint, data=data, headers=headers)
    SigV4Auth(credentials, "logs", "us-east-1").add_auth(request)
    req = urllib.request.Request(endpoint, data=data, 
                                 headers=dict(request.headers), method="POST")
    try:
        resp = urllib.request.urlopen(req)
        body = resp.read()
        return json.loads(body) if body else {"status": "ok"}
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()}
```

我们首先通过 `TestTransformer` API 测试不同的参数名：

```python
# 测试 "when" 参数
result = call_logs_api("TestTransformer", {
    "transformerConfig": [
        {"parseJSON": {}},
        {"addKeys": {"entries": [
            {"key": "env", "value": "prod", "when": '/level == "INFO"'}
        ]}}
    ],
    "logEventMessages": [
        '{"level":"DEBUG","message":"test1"}',
        '{"level":"INFO","message":"test2"}'
    ]
})
```

**实测输出**：
```
Event 1: {"level":"DEBUG","message":"test1"}          ← 无 env 字段
Event 2: {"level":"INFO","message":"test2","env":"prod"}  ← env 已添加
```

✅ `when` 是正确的参数名。我们也测试了 `condition`、`addWhen`、`runWhen` — 都被 API 静默忽略。

### Step 2: 多条件差异化标签

为不同日志级别添加不同的 severity 标签——这是条件处理最经典的用例：

```python
result = call_logs_api("TestTransformer", {
    "transformerConfig": [
        {"parseJSON": {}},
        {"addKeys": {"entries": [
            {"key": "severity", "value": "low",     "when": '/level == "DEBUG"'},
            {"key": "severity", "value": "medium",  "when": '/level == "INFO"'},
            {"key": "severity", "value": "high",    "when": '/level == "ERROR"'},
            {"key": "severity", "value": "warning", "when": '/level == "WARN"'}
        ]}}
    ],
    "logEventMessages": [
        '{"level":"DEBUG","message":"debug msg"}',
        '{"level":"INFO","message":"info msg"}',
        '{"level":"ERROR","message":"error msg"}',
        '{"level":"WARN","message":"warn msg"}'
    ]
})
```

**实测输出**：
```
Event 1: {"level":"DEBUG","message":"debug msg","severity":"low"}
Event 2: {"level":"INFO","message":"info msg","severity":"medium"}
Event 3: {"level":"ERROR","message":"error msg","severity":"high"}
Event 4: {"level":"WARN","message":"warn msg","severity":"warning"}
```

每个事件精准匹配了对应的 severity ✅

### Step 3: 条件化 deleteKeys —— 选择性删除敏感字段

在某些场景下，你可能只想删除 DEBUG 日志中的详细信息（减少存储），同时保留非 DEBUG 日志的完整性：

```python
# 仅对 DEBUG 日志删除 message 字段
result = call_logs_api("TestTransformer", {
    "transformerConfig": [
        {"parseJSON": {}},
        {"deleteKeys": {"withKeys": ["message"], "when": '/level == "DEBUG"'}}
    ],
    "logEventMessages": [
        '{"level":"DEBUG","message":"verbose debug info","code":200}',
        '{"level":"INFO","message":"important info","code":200}',
        '{"level":"ERROR","message":"critical error","code":500}'
    ]
})
```

**实测输出**：
```
Event 1: {"level":"DEBUG","code":200}                          ← message 已删除
Event 2: {"level":"INFO","message":"important info","code":200}   ← 保留
Event 3: {"level":"ERROR","message":"critical error","code":500}  ← 保留
```

### Step 4: 条件化 Grok —— 仅对需要的日志做正则解析

Grok 解析是 CPU 密集操作，对不需要解析的日志跳过可以提高效率：

```python
# 仅对非 DEBUG 日志做 Grok 解析
result = call_logs_api("TestTransformer", {
    "transformerConfig": [
        {"parseJSON": {}},
        {"grok": {
            "match": "%{WORD:parsed_msg}", 
            "source": "message", 
            "when": '/level != "DEBUG"'
        }}
    ],
    "logEventMessages": [
        '{"level":"DEBUG","message":"hello world"}',
        '{"level":"INFO","message":"goodbye"}',
        '{"level":"ERROR","message":"critical"}'
    ]
})
```

**实测输出**：
```
Event 1: {"level":"DEBUG","message":"hello world"}                   ← 无 parsed_msg
Event 2: {"level":"INFO","message":"goodbye","parsed_msg":"goodbye"}    ← Grok 生效
Event 3: {"level":"ERROR","message":"critical","parsed_msg":"critical"} ← Grok 生效
```

### Step 5: 部署到实际 Log Group 验证持久化

最关键的验证——条件处理在实际 `PutTransformer` + `PutLogEvents` 流程中是否生效：

```python
import boto3
logs = boto3.Session().client("logs", region_name="us-east-1")

LOG_GROUP = "/test/pipeline-conditional-base"

# 创建 Log Group
logs.create_log_group(logGroupName=LOG_GROUP)
logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=1)

# 部署带条件的 Transformer（通过直接 API 调用）
call_logs_api("PutTransformer", {
    "logGroupIdentifier": LOG_GROUP,
    "transformerConfig": [
        {"parseJSON": {}},
        {"addKeys": {"entries": [
            {"key": "severity", "value": "low",     "when": '/level == "DEBUG"'},
            {"key": "severity", "value": "medium",  "when": '/level == "INFO"'},
            {"key": "severity", "value": "high",    "when": '/level == "ERROR"'},
            {"key": "severity", "value": "warning", "when": '/level == "WARN"'}
        ]}}
    ]
})

# 发送测试日志
logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName="test")
logs.put_log_events(
    logGroupName=LOG_GROUP, logStreamName="test",
    logEvents=[
        {"timestamp": ts,   "message": '{"level":"DEBUG","message":"debug msg"}'},
        {"timestamp": ts+1, "message": '{"level":"INFO","message":"info msg"}'},
        {"timestamp": ts+2, "message": '{"level":"ERROR","message":"error msg"}'},
        {"timestamp": ts+3, "message": '{"level":"WARN","message":"warn msg"}'},
    ]
)
```

通过 CloudWatch Logs Insights 查询验证：

```
fields @timestamp, level, severity, message | sort @timestamp asc
```

**查询结果**：

| level | severity | message |
|-------|----------|---------|
| DEBUG | low | debug msg |
| INFO | medium | info msg |
| ERROR | high | error msg |
| WARN | warning | warn msg |

✅ 条件处理在实际部署中完全生效！

### Step 6: 复杂条件表达式测试

```python
# OR 条件 —— 任一条件满足即触发
{"addKeys": {"entries": [
    {"key": "alert", "value": "yes", 
     "when": '/level == "ERROR" or /code >= 400'}
]}}
# 结果：DEBUG(code=200)无alert, INFO(code=404)有alert, ERROR(code=500)有alert ✅

# 数值比较
{"addKeys": {"entries": [
    {"key": "is_error", "value": "true", "when": '/code >= 400'}
]}}
# 结果：code=200无标记, code=404和code=500有标记 ✅

# Null 检查
{"addKeys": {"entries": [
    {"key": "has_code", "value": "true", "when": '/code != null'}
]}}
# 结果：有code字段的事件被标记, 无code字段的不标记 ✅

# 集合包含
{"addKeys": {"entries": [
    {"key": "important", "value": "yes", 
     "when": '/level in {"ERROR", "WARN"}'}
]}}
# 结果：仅 ERROR 事件被标记 ✅
```

### Step 7: 边界测试 —— 不存在的字段和 NOT 运算符

```python
# 引用不存在的字段
{"addKeys": {"entries": [
    {"key": "tag", "value": "matched", "when": '/nonexistent == "value"'}
]}}
# 结果：条件不满足，entry 被跳过，不报错 ✅

# NOT 运算符 —— 注意优先级陷阱！
{"addKeys": {"entries": [
    {"key": "keep", "value": "true", "when": 'not /level == "DEBUG"'}
]}}
# 结果：❌ 所有事件都未添加！（解析为 (not /level) == "DEBUG"）

# 正确写法：
{"addKeys": {"entries": [
    {"key": "keep", "value": "true", "when": '/level != "DEBUG"'}
]}}
# 或者：
{"addKeys": {"entries": [
    {"key": "keep", "value": "true", "when": 'not (/level == "DEBUG")'}
]}}
# 结果：✅ DEBUG 不添加，INFO 和 ERROR 添加
```

!!! danger "NOT 运算符优先级陷阱"
    `not /level == "DEBUG"` 不等于 `not (/level == "DEBUG")`！
    
    - `not /level == "DEBUG"` → 解析为 `(not /level) == "DEBUG"` → 始终为 false
    - `not (/level == "DEBUG")` → 正确的否定逻辑
    - **建议**：直接用 `!=` 更安全：`/level != "DEBUG"`

## 测试结果

| # | 测试场景 | 处理器 | 结果 | 关键发现 |
|---|---------|--------|------|---------|
| 1 | entry-level `when` on addKeys | addKeys | ✅ | 每个 entry 独立条件判断 |
| 2 | `when` on deleteKeys | deleteKeys | ✅ | 仅匹配的事件被删除字段 |
| 3 | `when` on grok | grok | ✅ | 跳过不匹配的事件 |
| 4 | `when` on copyValue | copyValue | ✅ | 仅匹配的事件复制字段 |
| 5 | `when` on renameKeys | renameKeys | ✅ | 仅匹配的事件重命名 |
| 6 | OR 条件 | addKeys | ✅ | `or` 运算正常 |
| 7 | AND 条件 | addKeys | ✅ | `and` 运算正常 |
| 8 | 数值比较 (>=) | addKeys | ✅ | 数值运算正确 |
| 9 | Null 检查 | addKeys | ✅ | 存在性检查可用 |
| 10 | 集合包含 (in) | addKeys | ✅ | `in {}` 语法可用 |
| 11 | 不存在字段 | addKeys | ✅ | 静默跳过，不报错 |
| 12 | NOT 优先级 | addKeys | ⚠️ | 需要括号或用 `!=` |
| 13 | 跨处理器引用 | addKeys→deleteKeys | ✅ | 前面处理器添加的字段可引用 |
| 14 | 同类型处理器 | addKeys×2 | ❌ | 同类型限制 1 个 |
| 15 | PutTransformer 持久化 | addKeys | ✅ | `when` 在 ingestion 时生效 |
| 16 | GetTransformer 返回 | — | ⚠️ | 响应中 `when` 字段被剥离 |
| 17 | dropEvents 处理器 | dropEvents | ❌ | API 不支持此处理器类型 |

## 踩坑记录

!!! warning "踩坑 1: CLI/SDK 不支持条件参数，需直接签名 API 请求"
    AWS CLI 2.34.26 和 boto3 的 `Processor` 模型不包含 `when` 参数。客户端校验会拒绝。
    
    **解决方案**：使用 `botocore.auth.SigV4Auth` 直接签名 HTTP 请求，绕过客户端校验。
    
    **影响**：所有通过 CLI 或 SDK 管理 transformer 的自动化脚本需要更新。
    
    *实测发现，SDK 模型尚未更新（2026-04-11）*

!!! warning "踩坑 2: GetTransformer 响应不返回 when 条件"
    `GetTransformer` API 返回的 transformer 配置中，`when` 字段被去除。
    但条件**实际在 ingestion 时生效**（通过 Logs Insights 查询确认）。
    
    **影响**：无法通过 GetTransformer 审计已部署的条件规则。需要在 IaC 或外部文档中记录条件配置。
    
    *实测发现，官方未记录*

!!! warning "踩坑 3: NOT 运算符的优先级问题"
    `not /field == "value"` 不等同于 `not (/field == "value")`。
    
    前者解析为 `(not /field) == "value"`，这会将字段值取逻辑非后再比较，几乎总是返回 false。
    
    **建议**：使用 `!=` 或显式括号 `not (...)`。

!!! info "踩坑 4: 同类型处理器限制 1 个"
    一个 Transformer 中不能有两个相同类型的处理器（如两个 addKeys）。
    API 返回：`Transformer config should have a maximum of 1 addKeys processors`。
    
    **解决方案**：在单个 addKeys 处理器中用多个 entries（最多 5 个），配合不同的 `when` 条件。

!!! info "发现：dropEvents 处理器在 Logs Transformer API 中不可用"
    尽管 What's New 公告宣布了 Drop Events 处理器，但 `aws logs` API 端点
    目前不支持 `dropEvents` 处理器类型（返回 `Processor cannot be null`）。
    
    Drop Events 可能仅在 CloudWatch Pipelines 控制台的 OSIS/Data Prepper 层（第三方源 pipeline）中可用。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudWatch Logs 摄取 (Standard) | $0.50/GB | < 1 KB 测试数据 | < $0.01 |
| CloudWatch Logs 存储 (1天保留) | $0.03/GB | < 1 KB | < $0.01 |
| Pipeline/Transformer | 免费 | — | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 删除所有测试 Transformer
for lg in /test/pipeline-conditional-base \
          /test/pipeline-conditional-delete \
          /test/pipeline-conditional-grok \
          /test/pipeline-conditional-copy; do
    aws logs delete-transformer \
        --log-group-identifier "$lg" \
        --region us-east-1 2>/dev/null
    aws logs delete-log-group \
        --log-group-name "$lg" \
        --region us-east-1 2>/dev/null
    echo "Deleted: $lg"
done
```

!!! danger "务必清理"
    Log Group 设置了 1 天保留期，即使忘记清理也会自动过期。
    但建议手动删除以保持账号整洁。

## 结论与建议

### 条件处理适用场景

| 场景 | 推荐方式 | 示例 |
|------|---------|------|
| 按日志级别差异化标签 | addKeys + 多 entry `when` | severity low/medium/high |
| 仅对生产日志做深度解析 | grok + `when` | `/env == "prod"` 时才 Grok |
| 选择性删除敏感字段 | deleteKeys + `when` | DEBUG 日志删除详情 |
| 仅备份错误日志的原始内容 | copyValue + `when` | ERROR 时复制到 backup 字段 |

### 使用建议

1. **用 `!=` 代替 `not`** — 避免 NOT 运算符优先级问题
2. **在单个处理器中用多 entry + 不同 when** — 绕过同类型处理器限制
3. **不要依赖 GetTransformer 审计条件** — `when` 字段不在响应中返回
4. **使用 SigV4 直接签名** — 在 CLI/SDK 更新前，这是唯一的 API 自动化方式
5. **优先用 TestTransformer 验证** — 在部署前用 TestTransformer 内存测试条件逻辑

### 当前限制

- **dropEvents 处理器**：在 Logs Transformer API 中不可用，可能需要通过 Pipelines 控制台操作
- **CLI/SDK 支持**：需要等待 SDK 模型更新
- **GetTransformer 审计**：无法查看已部署的条件配置
- **同类型处理器**：每种处理器限 1 个（但每个可含最多 5 个条件化 entry）

## 参考链接

- [What's New: CloudWatch Pipelines Conditional Processing](https://aws.amazon.com/about-aws/whats-new/2026/04/amazon-cloudwatch-pipelines-conditional/)
- [CloudWatch Pipelines 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-pipelines.html)
- [CloudWatch Logs Transformation 文档](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CloudWatch-Logs-Transformation.html)
- [Data Prepper Expression Syntax](https://docs.opensearch.org/latest/data-prepper/pipelines/expression-syntax/)（条件表达式语法参考）
