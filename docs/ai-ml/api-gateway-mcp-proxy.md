---
tags:
  - MCP
  - API Gateway
  - What's New
---

# 动手实践：将 API Gateway REST API 变成 AI Agent 可调用的 MCP 工具

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

你有一堆运行了多年的 REST API，现在 AI Agent 时代来了——这些 API 能不能直接让 Agent 调用？

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 是 Anthropic 在 2024 年底发布的开放协议，正在成为 AI Agent 与工具交互的事实标准。2025 年 12 月，AWS 宣布 **Amazon API Gateway 支持 MCP proxy**，通过 Amazon Bedrock AgentCore Gateway，你可以将现有的 REST API 自动转换为 MCP 兼容的工具——无需修改任何 API 代码。

**核心价值**：
- 零代码改造——现有 REST API 原封不动
- 双向认证——入站验证 Agent 身份，出站管理 API 访问
- 语义搜索——Agent 用自然语言发现最相关的 API 工具

本文将从零开始，创建一个 REST API，配置 AgentCore Gateway，并通过 MCP 协议实际调用它。

## 前置条件

- AWS 账号（需要 IAM、API Gateway、Bedrock AgentCore 权限）
- AWS CLI v2.34+ 已配置（需要支持 `bedrock-agentcore-control` 命令）
- curl（支持 `--aws-sigv4` 选项）

## 核心概念

### 架构概览

```
MCP Client (Agent)
       │
       │ MCP Protocol (JSON-RPC)
       ▼
AgentCore Gateway ──── 入站认证 (AWS_IAM / JWT / NONE)
       │
       │ HTTP (协议转换)
       ▼
API Gateway REST API ── 出站认证 (IAM Role / API Key)
       │
       ▼
  后端服务
```

### 工作原理

1. AgentCore Gateway 通过 API Gateway 的 **GetExport** API 获取 OpenAPI 3.0 规范
2. 每个 REST API 的 method + path 组合被转换为一个 **MCP 工具**
3. 工具名称格式：`{Target名称}___{operationId}`（三下划线分隔）
4. MCP Client 通过 `tools/list` 发现工具，`tools/call` 调用工具

### 关键限制（开始前必读）

| 限制项 | 详情 |
|--------|------|
| API 类型 | 仅支持 **REST API**（不支持 HTTP API 和 WebSocket API） |
| 端点类型 | 仅支持 **Public endpoint**（不支持 Private endpoint） |
| operationId | 每个暴露的 method 必须有 operationId 或 tool override |
| proxy resource | 不支持 `{proxy+}` 类型的资源路径 |
| 同 Account/Region | API 和 Gateway 必须在同一 AWS 账号和 Region |
| 认证冲突 | 使用 `AWS_IAM` + API Key 组合的 method 不支持 |

## 动手实践

### Step 1: 创建 Gateway 服务角色

AgentCore Gateway 需要一个 IAM Role 来访问 API Gateway 的 GetExport API。

```bash
# 创建信任策略文件
cat > /tmp/gateway-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# 创建 IAM Role
aws iam create-role \
  --role-name apigw-mcp-lab-gateway-role \
  --assume-role-policy-document file:///tmp/gateway-trust-policy.json \
  --region us-east-1

# 附加 API Gateway 读取权限
cat > /tmp/gateway-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["apigateway:GET"],
      "Resource": "arn:aws:apigateway:us-east-1::/restapis/*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name apigw-mcp-lab-gateway-role \
  --policy-name ApiGatewayGetExport \
  --policy-document file:///tmp/gateway-policy.json
```

### Step 2: 创建 REST API（Mock 后端）

为了演示，我们创建一个 PetStore 风格的 REST API，使用 Mock Integration（无需真实后端）。

```bash
# 创建 REST API
API_ID=$(aws apigateway create-rest-api \
  --name apigw-mcp-lab-petstore \
  --description "PetStore API for MCP proxy lab" \
  --endpoint-configuration types=REGIONAL \
  --region us-east-1 \
  --query "id" --output text)

echo "API ID: $API_ID"

# 获取根资源 ID
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id $API_ID \
  --region us-east-1 \
  --query "items[0].id" --output text)

# 创建 /pets 资源
PETS_ID=$(aws apigateway create-resource \
  --rest-api-id $API_ID \
  --parent-id $ROOT_ID \
  --path-part pets \
  --region us-east-1 \
  --query "id" --output text)

# 创建 /pets/{petId} 资源
PETID_ID=$(aws apigateway create-resource \
  --rest-api-id $API_ID \
  --parent-id $PETS_ID \
  --path-part "{petId}" \
  --region us-east-1 \
  --query "id" --output text)

echo "Resources: /pets=$PETS_ID, /pets/{petId}=$PETID_ID"
```

配置 GET /pets（列出所有宠物）：

```bash
# 创建 GET 方法，注意 --operation-name 是 MCP 工具名称的来源
aws apigateway put-method \
  --rest-api-id $API_ID --resource-id $PETS_ID \
  --http-method GET --authorization-type NONE \
  --operation-name ListPets \
  --region us-east-1

# 配置 Mock Integration
aws apigateway put-integration \
  --rest-api-id $API_ID --resource-id $PETS_ID \
  --http-method GET --type MOCK \
  --request-templates '{"application/json": "{\"statusCode\": 200}"}' \
  --region us-east-1

aws apigateway put-method-response \
  --rest-api-id $API_ID --resource-id $PETS_ID \
  --http-method GET --status-code 200 \
  --response-models '{"application/json": "Empty"}' \
  --region us-east-1

aws apigateway put-integration-response \
  --rest-api-id $API_ID --resource-id $PETS_ID \
  --http-method GET --status-code 200 \
  --response-templates '{"application/json": "[{\"id\": 1, \"name\": \"Buddy\", \"type\": \"dog\"}, {\"id\": 2, \"name\": \"Whiskers\", \"type\": \"cat\"}]"}' \
  --region us-east-1
```

配置 GET /pets/{petId}（获取单个宠物）：

```bash
aws apigateway put-method \
  --rest-api-id $API_ID --resource-id $PETID_ID \
  --http-method GET --authorization-type NONE \
  --operation-name GetPet \
  --request-parameters '{"method.request.path.petId": true}' \
  --region us-east-1

aws apigateway put-integration \
  --rest-api-id $API_ID --resource-id $PETID_ID \
  --http-method GET --type MOCK \
  --request-templates '{"application/json": "{\"statusCode\": 200}"}' \
  --region us-east-1

aws apigateway put-method-response \
  --rest-api-id $API_ID --resource-id $PETID_ID \
  --http-method GET --status-code 200 \
  --response-models '{"application/json": "Empty"}' \
  --region us-east-1

aws apigateway put-integration-response \
  --rest-api-id $API_ID --resource-id $PETID_ID \
  --http-method GET --status-code 200 \
  --response-templates '{"application/json": "{\"id\": 1, \"name\": \"Buddy\", \"type\": \"dog\"}"}' \
  --region us-east-1
```

部署到 Stage：

```bash
aws apigateway create-deployment \
  --rest-api-id $API_ID \
  --stage-name prod \
  --description "Initial deployment" \
  --region us-east-1

# 验证 API 可用
curl -s "https://${API_ID}.execute-api.us-east-1.amazonaws.com/prod/pets"
```

### Step 3: 创建 AgentCore Gateway

```bash
# 替换为你的 Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)

aws bedrock-agentcore-control create-gateway \
  --name apigw-mcp-lab-gateway \
  --description "MCP Gateway for PetStore API" \
  --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/apigw-mcp-lab-gateway-role" \
  --protocol-type MCP \
  --protocol-configuration '{"mcp":{"supportedVersions":["2025-03-26"],"searchType":"SEMANTIC"}}' \
  --authorizer-type AWS_IAM \
  --exception-level DEBUG \
  --region us-east-1
```

!!! tip "入站认证选择"
    - **AWS_IAM**：推荐用于 AWS 内部 Agent 调用，使用 SigV4 签名
    - **CUSTOM_JWT**：推荐生产环境，支持 Cognito 或第三方 IdP
    - **NONE**：AWS 明确警告**不推荐用于测试/开发**，仅用于公开 Gateway

等待 Gateway 就绪：

```bash
# 获取 Gateway ID（从上一步输出中获取）
GATEWAY_ID="你的-gateway-id"

# 检查状态
aws bedrock-agentcore-control get-gateway \
  --gateway-identifier $GATEWAY_ID \
  --region us-east-1 \
  --query "status" --output text
# 预期：READY（通常 10 秒内）
```

### Step 4: 将 REST API 添加为 Gateway Target

```bash
# 创建 Target 配置文件
cat > /tmp/target-config.json << EOF
{
  "mcp": {
    "apiGateway": {
      "restApiId": "${API_ID}",
      "stage": "prod",
      "apiGatewayToolConfiguration": {
        "toolFilters": [
          {
            "filterPath": "/*",
            "methods": ["GET"]
          }
        ]
      }
    }
  }
}
EOF

aws bedrock-agentcore-control create-gateway-target \
  --gateway-identifier $GATEWAY_ID \
  --name petstore-api-target \
  --description "PetStore REST API target" \
  --target-configuration file:///tmp/target-config.json \
  --region us-east-1
```

!!! warning "Tool Filter 注意事项"
    `toolFilters` 的 `filterPath` 支持精确路径（`/pets`）和通配符（`/*`）。使用通配符时，所有匹配的 method **都必须有 operationId**，否则整个 Target 创建会失败。可通过 `toolOverrides` 为缺少 operationId 的 method 指定名称。

等待 Target 就绪：

```bash
# 获取 Target ID（从上一步输出中获取）
TARGET_ID="你的-target-id"

aws bedrock-agentcore-control get-gateway-target \
  --gateway-identifier $GATEWAY_ID \
  --target-id $TARGET_ID \
  --region us-east-1 \
  --query "status" --output text
# 预期：READY
```

### Step 5: 通过 MCP 协议调用 API

获取 Gateway URL：

```bash
GATEWAY_URL=$(aws bedrock-agentcore-control get-gateway \
  --gateway-identifier $GATEWAY_ID \
  --region us-east-1 \
  --query "gatewayUrl" --output text)

echo "Gateway MCP URL: $GATEWAY_URL"
```

发现可用工具（tools/list）：

```bash
curl -s --aws-sigv4 "aws:amz:us-east-1:bedrock-agentcore" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$GATEWAY_URL" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}'
```

预期输出（格式化后）：

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "tools": [
      {
        "name": "x_amz_bedrock_agentcore_search",
        "description": "A special tool that returns a trimmed down list of tools given a context.",
        "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
      },
      {
        "name": "petstore-api-target___ListPets",
        "description": "ListPets",
        "inputSchema": {"type":"object","properties":{"basePath":{"type":"string"}}}
      },
      {
        "name": "petstore-api-target___GetPet",
        "description": "GetPet",
        "inputSchema": {"type":"object","properties":{"petId":{"type":"string"},"basePath":{"type":"string"}},"required":["petId"]}
      }
    ]
  }
}
```

调用工具（tools/call）：

```bash
# 列出所有宠物
curl -s --aws-sigv4 "aws:amz:us-east-1:bedrock-agentcore" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$GATEWAY_URL" \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/call","params":{"name":"petstore-api-target___ListPets","arguments":{}}}'
```

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "isError": false,
    "content": [{"type":"text","text":"[{\"id\":1,\"name\":\"Buddy\",\"type\":\"dog\"},{\"id\":2,\"name\":\"Whiskers\",\"type\":\"cat\"}]"}]
  }
}
```

```bash
# 按 ID 查询宠物
curl -s --aws-sigv4 "aws:amz:us-east-1:bedrock-agentcore" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$GATEWAY_URL" \
  -d '{"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"petstore-api-target___GetPet","arguments":{"petId":"1"}}}'
```

### Step 6: 测试语义搜索

启用语义搜索后，Gateway 会自动添加 `x_amz_bedrock_agentcore_search` 工具。Agent 可以用自然语言描述需求，Gateway 返回最相关的工具：

```bash
curl -s --aws-sigv4 "aws:amz:us-east-1:bedrock-agentcore" \
  --user "$(aws configure get aws_access_key_id):$(aws configure get aws_secret_access_key)" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$GATEWAY_URL" \
  -d '{"jsonrpc":"2.0","id":"4","method":"tools/call","params":{"name":"x_amz_bedrock_agentcore_search","arguments":{"query":"find all pets"}}}'
```

语义搜索会根据查询内容对工具进行排序——"find all pets" 会把 `ListPets` 排在第一位。

## 测试结果

### 延迟测试（us-east-1，5 次采样）

| 操作 | Min | Max | Avg | 说明 |
|------|-----|-----|-----|------|
| tools/list | 739ms | 807ms | 761ms | 工具发现 |
| tools/call | 804ms | 851ms | 828ms | 工具调用 |
| 语义搜索 | 1014ms | 1159ms | 1108ms | 自然语言搜索 |

**分析**：Gateway 中间层增加了约 700-800ms 延迟（包括 MCP 协议解析 + REST API 转发）。语义搜索因向量化和检索额外增加约 300ms。对于 AI Agent 场景，这个延迟完全可以接受——Agent 自身的 LLM 推理通常需要数秒到十几秒。

### Tool Filter 对比

| 配置 | 暴露工具数 | 行为 |
|------|-----------|------|
| `filterPath: "/*"`, methods: GET,POST,DELETE | 4 个工具 | 匹配所有子路径的指定方法 |
| `filterPath: "/pets"`, methods: GET | 1 个工具（ListPets） | 仅精确匹配 /pets 路径 |

### 边界条件测试

| 测试 | 结果 |
|------|------|
| 无 operationId + 无 override | Target 创建 **FAILED**（不是跳过该方法，而是整体失败） |
| 无 operationId + 有 override | ✅ Target 创建成功，使用 override 的名称 |
| 调用不存在的 tool | JSON-RPC 错误码 -32602: "Unknown tool: xxx" |

## 踩坑记录

!!! warning "踩坑 1：operationId 缺失导致整个 Target 失败"
    如果用通配符 `/*` 匹配所有路径，**任何一个**匹配到的 method 缺少 operationId 且没有对应的 toolOverride，都会导致整个 Target 创建失败。不会只跳过那个 method。
    
    **解决方案**：确保所有暴露的 method 都有 operationId（推荐），或者使用 toolOverrides 为缺少的 method 提供名称。
    
    **已查文档确认**：官方文档明确说明 "If both the operationId and the override name are missing, target creation and updates will fail validation."

!!! warning "踩坑 2：入站认证枚举值"
    `authorizerType` 的值是 `AWS_IAM`，不是 `IAM`。第一次尝试如果写成 `IAM` 会收到 ValidationException。
    
    有效值：`AWS_IAM`、`CUSTOM_JWT`、`NONE`

!!! warning "踩坑 3：NONE 认证的定位"
    直觉上可能会选 `NONE` 来快速测试，但 AWS 明确警告：**不推荐将 NONE 用于测试/开发**。NONE 类型是为已实施自定义安全措施的公开 Gateway 设计的。测试/开发请使用 `AWS_IAM`。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| API Gateway REST API | $3.50/百万请求 | ~20 请求 | < $0.01 |
| AgentCore Gateway | 按调用计费 | ~20 调用 | < $0.01 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除 Gateway Targets
aws bedrock-agentcore-control delete-gateway-target \
  --gateway-identifier $GATEWAY_ID \
  --target-id $TARGET_ID \
  --region us-east-1

# 2. 删除 AgentCore Gateway
aws bedrock-agentcore-control delete-gateway \
  --gateway-identifier $GATEWAY_ID \
  --region us-east-1

# 3. 删除 REST API（会同时删除所有资源、方法和部署）
aws apigateway delete-rest-api \
  --rest-api-id $API_ID \
  --region us-east-1

# 4. 删除 IAM Role
aws iam delete-role-policy \
  --role-name apigw-mcp-lab-gateway-role \
  --policy-name ApiGatewayGetExport

aws iam delete-role \
  --role-name apigw-mcp-lab-gateway-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然费用极低，但 AgentCore Gateway 保持运行可能产生持续费用。

## 结论与建议

### 适合的场景

- **已有大量 REST API 的企业**：零改造成本，快速让 AI Agent 调用现有 API
- **需要统一 Agent 工具发现**：一个 Gateway 可以聚合多个 REST API Target，Agent 通过 tools/list 或语义搜索发现所有可用工具
- **需要安全管控 Agent 访问**：入站认证 + 出站认证的双层安全模型

### 不适合的场景

- 使用 HTTP API 或 WebSocket API（目前不支持）
- 私有 VPC 内的 API（需要用 Public endpoint + Private Integration 间接实现）
- 需要极低延迟的实时交互（Gateway 中间层增加 ~800ms）

### 生产环境建议

1. **入站认证**：使用 CUSTOM_JWT（Cognito）而非 AWS_IAM，更灵活的客户端管理
2. **operationId 规范**：在 API 设计时就为每个 method 定义清晰的 operationId
3. **Tool Filter 最小权限**：仅暴露 Agent 需要的方法，避免用 `/*` 全量暴露
4. **语义搜索**：工具数量多（>10）时开启，帮助 Agent 精准选择工具
5. **Debug 模式**：开发测试阶段开启 `--exception-level DEBUG`，生产关闭

## 参考链接

- [AWS What's New: Amazon API Gateway adds MCP proxy support](https://aws.amazon.com/about-aws/whats-new/2025/12/api-gateway-mcp-proxy-support/)
- [API Gateway 文档: Add an API Gateway REST API as a target for AgentCore Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/mcp-server.html)
- [AgentCore Gateway 文档: API Gateway REST API stages as targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-api-gateway.html)
- [AgentCore Gateway Quick Start](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-quick-start.html)
- [Amazon Bedrock AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
