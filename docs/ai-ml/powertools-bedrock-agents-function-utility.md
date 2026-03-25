# 用 Powertools for Lambda 简化 Bedrock Agent 开发：代码量减少 44% 的实战验证

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.01
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

构建 Amazon Bedrock Agent 的 action group 时，开发者需要编写 Lambda 函数来处理 Agent 的请求。传统方式下，你需要：

1. 手动解析 Bedrock Agent 发来的事件结构
2. 从 `parameters` 数组中逐个提取参数
3. 编写函数路由逻辑（判断调用哪个 function）
4. 按照 Bedrock Agent 要求的格式构建响应

这些**样板代码**占据了 Lambda 函数的大部分篇幅，却与业务逻辑无关。

2025 年 6 月，AWS 发布了 **Powertools for AWS Lambda 的 Bedrock Agents Function utility**，通过 `@app.tool()` 装饰器，将上述样板代码全部自动化。本文通过实际部署对比，验证它到底能省多少代码。

## 前置条件

- AWS 账号（需要 Bedrock、Lambda、IAM 权限）
- AWS CLI v2 已配置
- Python 3.12
- Amazon Nova Lite 模型已启用（在 Bedrock Model access 中开启）

## 核心概念

### Powertools for Lambda 是什么？

[Powertools for AWS Lambda](https://docs.powertools.aws.dev/lambda/python/latest/) 是 AWS 官方开源的 Lambda 开发工具包，提供 Logger、Tracer、Metrics 等实用工具。新增的 **Bedrock Agents Function utility** 扩展了其事件处理能力。

### 两种 Action Group 模式

| 方面 | OpenAPI-based | Function-based |
|------|--------------|----------------|
| 定义方式 | `@app.get("/path")` | `@app.tool(name="")` |
| 参数来源 | Path/Query/Body | 函数参数 |
| 需要 Schema | OpenAPI JSON | 在 Bedrock 控制台定义 |
| 适用场景 | 复杂 REST API | 简单函数调用 |
| Powertools 类 | `BedrockAgentResolver` | `BedrockAgentFunctionResolver` |

本文聚焦 **Function-based** 模式，这是更简洁的方案。

## 动手实践

### Step 1: 创建 IAM 角色

```bash
# Lambda 执行角色
aws iam create-role \
  --role-name lambda-role-powertools-test \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region us-east-1

aws iam attach-role-policy \
  --role-name lambda-role-powertools-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Bedrock Agent 服务角色
aws iam create-role \
  --role-name bedrock-agent-role-powertools-test \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "bedrock.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {"aws:SourceAccount": "<YOUR_ACCOUNT_ID>"},
        "ArnLike": {"AWS:SourceArn": "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:agent/*"}
      }
    }]
  }' \
  --region us-east-1

# 允许 Agent 调用 Nova Lite 模型
aws iam put-role-policy \
  --role-name bedrock-agent-role-powertools-test \
  --policy-name BedrockInvokeModel \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": ["arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"]
    }]
  }'
```

### Step 2: 编写 Lambda 函数（Powertools 版）

```python
# lambda_function.py — 仅 53 行有效代码
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import BedrockAgentFunctionResolver
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
app = BedrockAgentFunctionResolver()

WEATHER_DATA = {
    "seattle": {"temp": 12, "condition": "Rainy", "humidity": 85},
    "tokyo": {"temp": 22, "condition": "Sunny", "humidity": 60},
    "london": {"temp": 15, "condition": "Cloudy", "humidity": 75},
}

CITY_DATA = {
    "seattle": {"country": "USA", "population": 737015, "timezone": "PST"},
    "tokyo": {"country": "Japan", "population": 13960000, "timezone": "JST"},
    "london": {"country": "UK", "population": 8982000, "timezone": "GMT"},
}

@app.tool(description="Get current weather for a city")
def get_weather(city: str) -> str:
    city_lower = city.lower()
    if city_lower in WEATHER_DATA:
        data = WEATHER_DATA[city_lower]
        return f"Weather in {city}: {data['temp']}°C, {data['condition']}, Humidity: {data['humidity']}%"
    return f"Weather data not available for {city}"

@app.tool(description="Get city information including country and timezone")
def get_city_info(city: str, include_population: bool = False) -> str:
    city_lower = city.lower()
    if city_lower in CITY_DATA:
        data = CITY_DATA[city_lower]
        result = f"{city}: Country={data['country']}, Timezone={data['timezone']}"
        if include_population:
            result += f", Population={data['population']:,}"
        return result
    return f"City data not available for {city}"

@app.tool(description="Convert temperature between Celsius and Fahrenheit")
def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    from_unit, to_unit = from_unit.upper(), to_unit.upper()
    if from_unit == "C" and to_unit == "F":
        return f"{value}°C = {(value * 9/5) + 32:.1f}°F"
    elif from_unit == "F" and to_unit == "C":
        return f"{value}°F = {(value - 32) * 5/9:.1f}°C"
    return "Invalid units. Use C or F."

@logger.inject_lambda_context
def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
```

**关键点**：

- `@app.tool(description=...)` — 注册 tool，Powertools 自动处理参数解析和响应构建
- `app.resolve(event, context)` — 一行搞定事件路由
- 函数签名就是参数定义 — `city: str`, `value: float` 等类型注解直接生效

### Step 3: 对比——没有 Powertools 的写法

```python
# vanilla_lambda.py — 同样功能需要 117 行
import json
import logging

logger = logging.getLogger()

# ... 同样的数据定义 ...

def get_weather(params):
    city = None
    for p in params:                    # 手动遍历参数数组
        if p.get("name") == "city":
            city = p.get("value")
    if not city:
        return "Error: city parameter is required"
    # ... 业务逻辑 ...

def get_city_info(params):
    city = None
    include_population = False
    for p in params:                    # 每个函数都要重复这段
        if p.get("name") == "city":
            city = p.get("value")
        elif p.get("name") == "include_population":
            include_population = str(p.get("value", "false")).lower() == "true"
    # ... 业务逻辑 ...

FUNCTION_MAP = {                        # 手写路由表
    "get_weather": get_weather,
    "get_city_info": get_city_info,
    "convert_temperature": convert_temperature,
}

def lambda_handler(event, context):
    function_name = event.get("function", "")
    parameters = event.get("parameters", [])
    action_group = event.get("actionGroup", "")

    handler = FUNCTION_MAP.get(function_name)  # 手动路由
    result = handler(parameters) if handler else "Unknown function"

    return {                            # 手动构建响应格式
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "function": function_name,
            "functionResponse": {
                "responseBody": {
                    "TEXT": {"body": result}
                }
            }
        }
    }
```

### Step 4: 打包部署 Lambda

```bash
# 安装依赖并打包
mkdir -p /tmp/powertools-pkg && cd /tmp/powertools-pkg
pip install aws-lambda-powertools -t .
cp lambda_function.py .
zip -r ../powertools-lambda.zip .

# 创建 Lambda 函数
aws lambda create-function \
  --function-name powertools-bedrock-agent-fn \
  --runtime python3.12 \
  --role arn:aws:iam::<YOUR_ACCOUNT_ID>:role/lambda-role-powertools-test \
  --handler lambda_function.lambda_handler \
  --zip-file fileb:///tmp/powertools-lambda.zip \
  --timeout 30 \
  --memory-size 256 \
  --environment "Variables={POWERTOOLS_SERVICE_NAME=weather-agent,LOG_LEVEL=INFO}" \
  --region us-east-1

aws lambda wait function-active \
  --function-name powertools-bedrock-agent-fn \
  --region us-east-1
```

### Step 5: 创建 Bedrock Agent

```bash
# 创建 Agent
AGENT_ID=$(aws bedrock-agent create-agent \
  --agent-name powertools-test-agent \
  --agent-resource-role-arn arn:aws:iam::<YOUR_ACCOUNT_ID>:role/bedrock-agent-role-powertools-test \
  --foundation-model "amazon.nova-lite-v1:0" \
  --instruction "You are a helpful weather and city information assistant." \
  --region us-east-1 \
  --query 'agent.agentId' --output text)

echo "Agent ID: $AGENT_ID"

# 添加 Lambda 调用权限
aws lambda add-permission \
  --function-name powertools-bedrock-agent-fn \
  --statement-id AllowBedrockAgent \
  --action lambda:InvokeFunction \
  --principal bedrock.amazonaws.com \
  --source-account <YOUR_ACCOUNT_ID> \
  --source-arn "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:agent/$AGENT_ID" \
  --region us-east-1
```

### Step 6: 创建 Action Group

将以下 function schema 保存为 `function-schema.json`：

```json
{
  "functions": [
    {
      "name": "get_weather",
      "description": "Get current weather for a city",
      "parameters": {
        "city": {
          "type": "string",
          "description": "The name of the city",
          "required": true
        }
      }
    },
    {
      "name": "get_city_info",
      "description": "Get city information including country and timezone",
      "parameters": {
        "city": {
          "type": "string",
          "description": "The city name",
          "required": true
        },
        "include_population": {
          "type": "boolean",
          "description": "Whether to include population data",
          "required": false
        }
      }
    },
    {
      "name": "convert_temperature",
      "description": "Convert temperature between Celsius and Fahrenheit",
      "parameters": {
        "value": {
          "type": "number",
          "description": "Temperature value",
          "required": true
        },
        "from_unit": {
          "type": "string",
          "description": "From unit (C or F)",
          "required": true
        },
        "to_unit": {
          "type": "string",
          "description": "To unit (C or F)",
          "required": true
        }
      }
    }
  ]
}
```

```bash
aws bedrock-agent create-agent-action-group \
  --agent-id $AGENT_ID \
  --agent-version DRAFT \
  --action-group-name "CityWeatherTools" \
  --action-group-executor "{\"lambda\": \"arn:aws:lambda:us-east-1:<YOUR_ACCOUNT_ID>:function:powertools-bedrock-agent-fn\"}" \
  --function-schema file://function-schema.json \
  --region us-east-1

# 准备 Agent（必须执行）
aws bedrock-agent prepare-agent --agent-id $AGENT_ID --region us-east-1
sleep 10
```

### Step 7: 测试 Agent

```python
import boto3, time

client = boto3.client('bedrock-agent-runtime', region_name='us-east-1')

response = client.invoke_agent(
    agentId='<YOUR_AGENT_ID>',
    agentAliasId='TSTALIASID',   # DRAFT 别名
    sessionId='test-001',
    inputText='What is the weather in Tokyo?'
)

for event in response.get('completion', []):
    if 'chunk' in event:
        print(event['chunk']['bytes'].decode('utf-8'))
# 输出: The weather in Tokyo is 22°C, Sunny, with a humidity of 60%.
```

## 测试结果

### 代码量对比

| 指标 | Powertools 版 | 原生版 | 差异 |
|------|-------------|--------|------|
| 总行数 | 66 行 | 117 行 | **-44%** |
| 参数解析 | 0 行（自动） | ~30 行 | 完全消除 |
| 响应构建 | 0 行（自动） | ~15 行 | 完全消除 |
| 路由逻辑 | 0 行（自动） | ~6 行 | 完全消除 |
| 纯业务逻辑 | ~50 行 | ~50 行 | 相同 |

**核心结论**：Powertools 消除了 51 行样板代码，开发者只需关注业务逻辑。

### 功能验证

| 测试场景 | 查询 | 结果 | 延迟 |
|---------|------|------|------|
| 基础天气查询 | "What is the weather in Tokyo?" | ✅ 22°C, Sunny, 60% | 3.90s（冷启动） |
| 城市信息+可选参数 | "Tell me about London, include population" | ✅ UK, GMT, 8,982,000 | 2.71s |
| 温度转换 | "Convert 100°F to Celsius" | ✅ 37.8°C | 2.73s |
| 多工具单次对话 | "Weather in Seattle and city info?" | ✅ 自动调用两个 tool | 3.80s |
| 不存在的城市 | "Weather in Berlin?" | ✅ 正确返回不可用 | 2.83s |
| 无效参数值 | "Convert 50K to C" | ⚠️ Lambda 正确返回错误 | 3.64s |

### 延迟分析（10 次热调用）

| 指标 | 值 |
|------|-----|
| 最快 | 1.51s |
| p50 | 1.80s |
| p90 | 2.53s |
| 平均 | 1.80s |
| Lambda 执行时间 | 35-41ms |

> **延迟分析**：端到端延迟 ~1.8s，其中 Lambda 执行仅 35-41ms。延迟主要来自 Nova Lite 模型的推理时间，Powertools 的开销可忽略不计。

## 踩坑记录

!!! warning "aws_xray_sdk 依赖"
    如果使用 Powertools 的 `Tracer` 功能，必须在部署包中包含 `aws-xray-sdk`。否则会报 `No module named 'aws_xray_sdk'` 错误。如果不需要 X-Ray 追踪，可以只使用 `Logger` 而跳过 `Tracer`。
    
    **状态**：实测发现，官方文档未显式提及此打包要求。

!!! warning "Agent 可能不转发错误响应"
    当 Lambda 返回业务错误（如"Invalid units"），Agent 可能不会将错误信息传递给用户，而是返回"Sorry I cannot answer"。这是 Bedrock Agent 的行为，不是 Powertools 的问题。
    
    **状态**：已查文档确认，这是 Agent 的 orchestration 逻辑决定的。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda | $0 | 免费层内 | $0 |
| Bedrock Nova Lite (input) | $0.06/1M tokens | ~15K tokens | $0.0009 |
| Bedrock Nova Lite (output) | $0.24/1M tokens | ~5K tokens | $0.0012 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
AGENT_ID=<YOUR_AGENT_ID>

# 1. 删除 Bedrock Agent
aws bedrock-agent delete-agent --agent-id $AGENT_ID --region us-east-1

# 2. 删除 Lambda 函数
aws lambda delete-function --function-name powertools-bedrock-agent-fn --region us-east-1
aws lambda delete-function --function-name vanilla-bedrock-agent-fn --region us-east-1

# 3. 删除 IAM 角色
aws iam detach-role-policy \
  --role-name lambda-role-powertools-test \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name lambda-role-powertools-test

aws iam delete-role-policy \
  --role-name bedrock-agent-role-powertools-test \
  --policy-name BedrockInvokeModel
aws iam delete-role --role-name bedrock-agent-role-powertools-test

# 4. 删除 CloudWatch 日志组
aws logs delete-log-group \
  --log-group-name /aws/lambda/powertools-bedrock-agent-fn --region us-east-1
aws logs delete-log-group \
  --log-group-name /aws/lambda/vanilla-bedrock-agent-fn --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。Bedrock Agent 本身不产生费用（仅 invoke 时按 LLM token 计费），但 Lambda 和 CloudWatch Logs 可能累积费用。

## 结论与建议

### Powertools Bedrock Agents Function Utility 值得用吗？

**毫无疑问，值得。** 核心价值：

1. **代码量减少 44%** — 51 行样板代码被完全消除
2. **开发体验一致** — 如果你已经用 Powertools 的 API Gateway Event Handler，写法几乎一样
3. **零性能开销** — Lambda 执行时间无明显差异
4. **内建与 Logger/Tracer 集成** — 开箱即用的可观测性

### 适用场景

- ✅ 新建 Bedrock Agent 项目 — 直接用 Powertools
- ✅ 已使用 Powertools 的项目 — 无缝扩展
- ⚠️ 极简 Lambda（单函数） — 原生写法也可接受，但 Powertools 仍更清晰

### 生产建议

1. 使用 **Lambda Layer** 部署 Powertools，避免每个函数都打包一份
2. 如果不需要 X-Ray 追踪，可以不安装 `aws-xray-sdk`，减小包体积
3. 为 Agent 的 service role 使用**最小权限**，只授权需要的 foundation model
4. 活用 `app.current_event.session_id` 做日志关联，方便调试完整对话流

## 参考链接

- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/06/powertools-lambda-bedrock-agents-function-utility/)
- [Powertools Python 文档 — Bedrock Agents](https://docs.powertools.aws.dev/lambda/python/latest/core/event_handler/bedrock_agents/)
- [Bedrock Agents 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/agents.html)
- [AWS 示例代码仓库](https://github.com/aws-samples/sample-bedrock-agent-powertools-for-aws)
