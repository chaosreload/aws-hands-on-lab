# Amazon OpenSearch Service Agentic Search 实战：自然语言驱动搜索

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $2-4（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

传统 OpenSearch 搜索需要用户构建复杂的 Query DSL（域特定语言）才能精准查询数据。对于非技术用户甚至是经验丰富的工程师，编写包含 `bool`、`range`、`aggs` 的嵌套查询都是一个门槛。

**Agentic Search** 改变了这一范式：用户直接用自然语言提问，AI Agent 自动理解意图、分析索引结构、生成并执行 DSL 查询，最终返回结果和推理过程。这是 LLM + 搜索引擎深度融合的典型场景。

## 前置条件

- AWS 账号（需要 OpenSearch Service、Bedrock、IAM 权限）
- AWS CLI v2 已配置
- [awscurl](https://github.com/okigan/awscurl) 已安装（用于 SigV4 签名的 HTTP 请求）
- Bedrock Claude 模型访问已开通（us-east-1）

## 核心概念

### 架构全景

```
用户自然语言查询
    ↓
agentic query clause（搜索请求）
    ↓
agentic_query_translator（搜索管线 request processor）
    ↓
Agent（Conversational / Flow）
    ↓
QueryPlanningTool（LLM 生成 DSL）
    ↓
OpenSearch 执行 DSL → 返回结果
```

### 两种 Agent 类型

| 特性 | Conversational Agent | Flow Agent |
|------|---------------------|------------|
| 工具 | 多工具（QueryPlanning + ListIndex + IndexMapping 等） | 仅 QueryPlanningTool |
| 对话记忆 | ✅ 通过 `memory_id` 跨查询维持上下文 | ❌ |
| 推理轨迹 | ✅ 详细 step-by-step reasoning | ❌ 仅返回生成的 DSL |
| 智能索引选择 | ✅ Agent 自动发现索引 | ❌ 必须指定索引 |
| 延迟 | 较高（多轮 LLM 调用） | 较低（单轮） |
| 适用场景 | 复杂查询、多索引探索、需要对话上下文 | 简单查询、已知索引、高吞吐低延迟 |

**选择建议**：先用 Flow Agent 满足大部分查询需求，对复杂场景再升级到 Conversational Agent。

## 动手实践

### Step 1: 创建 OpenSearch 域

创建一个 OpenSearch 3.3 域，启用 FGAC（细粒度访问控制）：

```bash
# 创建域配置文件
cat > /tmp/opensearch-domain.json << 'EOF'
{
  "DomainName": "agentic-search-lab",
  "EngineVersion": "OpenSearch_3.3",
  "ClusterConfig": {
    "InstanceType": "r6g.large.search",
    "InstanceCount": 1,
    "DedicatedMasterEnabled": false,
    "ZoneAwarenessEnabled": false
  },
  "EBSOptions": {
    "EBSEnabled": true,
    "VolumeType": "gp3",
    "VolumeSize": 10,
    "Iops": 3000,
    "Throughput": 125
  },
  "AccessPolicies": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"*\"},\"Action\":\"es:*\",\"Resource\":\"arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/agentic-search-lab/*\"}]}",
  "EncryptionAtRestOptions": { "Enabled": true },
  "NodeToNodeEncryptionOptions": { "Enabled": true },
  "DomainEndpointOptions": {
    "EnforceHTTPS": true,
    "TLSSecurityPolicy": "Policy-Min-TLS-1-2-PFS-2023-10"
  },
  "AdvancedSecurityOptions": {
    "Enabled": true,
    "InternalUserDatabaseEnabled": true,
    "MasterUserOptions": {
      "MasterUserName": "admin",
      "MasterUserPassword": "YourStrongPassword123!"
    }
  }
}
EOF

aws opensearch create-domain \
  --cli-input-json file:///tmp/opensearch-domain.json \
  --region us-east-1
```

!!! note "等待域就绪"
    域创建约需 15 分钟。用以下命令检查状态：
    ```bash
    aws opensearch describe-domain --domain-name agentic-search-lab \
      --region us-east-1 \
      --query 'DomainStatus.{Processing:Processing,Endpoint:Endpoint}'
    ```
    当 `Processing` 为 `false` 且 `Endpoint` 不为 null 时，域已就绪。

### Step 2: 准备测试数据

创建产品索引并灌入样本数据：

```bash
ENDPOINT="your-domain-endpoint"  # 替换为你的域端点

# 创建索引
curl -s -u admin:'YourStrongPassword123!' \
  -X PUT "https://${ENDPOINT}/products-index" \
  -H 'Content-Type: application/json' -d '{
  "settings": {"number_of_shards": 1, "number_of_replicas": 0},
  "mappings": {
    "properties": {
      "product_name": {"type": "text"},
      "description": {"type": "text"},
      "price": {"type": "float"},
      "currency": {"type": "keyword"},
      "rating": {"type": "float"},
      "review_count": {"type": "integer"},
      "in_stock": {"type": "boolean"},
      "color": {"type": "keyword"},
      "category": {"type": "keyword"},
      "brand": {"type": "keyword"},
      "tags": {"type": "keyword"}
    }
  }
}'

# 灌入数据（12 条跨品类产品）
curl -s -u admin:'YourStrongPassword123!' \
  -X POST "https://${ENDPOINT}/_bulk" \
  -H 'Content-Type: application/x-ndjson' -d '
{"index":{"_index":"products-index","_id":"1"}}
{"product_name":"Nike Air Max 270","description":"Comfortable running shoes with Air Max technology","price":150.0,"currency":"USD","rating":4.5,"review_count":1200,"in_stock":true,"color":"white","category":"shoes","brand":"Nike","tags":["running","athletic"]}
{"index":{"_index":"products-index","_id":"2"}}
{"product_name":"Adidas Ultraboost 22","description":"Premium running shoes with Boost midsole","price":180.0,"currency":"USD","rating":4.7,"review_count":850,"in_stock":true,"color":"black","category":"shoes","brand":"Adidas","tags":["running","premium"]}
{"index":{"_index":"products-index","_id":"3"}}
{"product_name":"Converse Chuck Taylor","description":"Classic canvas sneakers since 1917","price":65.0,"currency":"USD","rating":4.2,"review_count":2100,"in_stock":true,"color":"white","category":"shoes","brand":"Converse","tags":["casual","classic"]}
{"index":{"_index":"products-index","_id":"4"}}
{"product_name":"Puma RS-X","description":"Retro-inspired running shoes with modern comfort","price":120.0,"currency":"USD","rating":4.3,"review_count":750,"in_stock":true,"color":"black","category":"shoes","brand":"Puma","tags":["retro","running"]}
{"index":{"_index":"products-index","_id":"5"}}
{"product_name":"Samsung Galaxy S25 Ultra","description":"Flagship smartphone with 200MP camera","price":1299.0,"currency":"USD","rating":4.6,"review_count":3500,"in_stock":true,"color":"black","category":"electronics","brand":"Samsung","tags":["smartphone","flagship"]}
{"index":{"_index":"products-index","_id":"6"}}
{"product_name":"Sony WH-1000XM6","description":"Noise cancelling wireless headphones with 40hr battery","price":349.0,"currency":"USD","rating":4.8,"review_count":5200,"in_stock":true,"color":"black","category":"electronics","brand":"Sony","tags":["headphones","wireless"]}
{"index":{"_index":"products-index","_id":"7"}}
{"product_name":"Apple MacBook Pro M4","description":"Professional laptop with M4 chip","price":1999.0,"currency":"USD","rating":4.9,"review_count":4100,"in_stock":false,"color":"silver","category":"electronics","brand":"Apple","tags":["laptop","professional"]}
{"index":{"_index":"products-index","_id":"8"}}
{"product_name":"Levi 501 Original Jeans","description":"Classic straight-fit jeans since 1873","price":69.0,"currency":"USD","rating":4.4,"review_count":8900,"in_stock":true,"color":"blue","category":"clothing","brand":"Levis","tags":["jeans","classic"]}
{"index":{"_index":"products-index","_id":"9"}}
{"product_name":"North Face Nuptse Jacket","description":"Puffer jacket with 700-fill goose down","price":320.0,"currency":"USD","rating":4.7,"review_count":1800,"in_stock":true,"color":"black","category":"clothing","brand":"North Face","tags":["jacket","winter"]}
{"index":{"_index":"products-index","_id":"10"}}
{"product_name":"Dyson V15 Detect","description":"Cordless vacuum with laser dust detection","price":749.0,"currency":"USD","rating":4.5,"review_count":2300,"in_stock":true,"color":"yellow","category":"home","brand":"Dyson","tags":["vacuum","cordless"]}
{"index":{"_index":"products-index","_id":"11"}}
{"product_name":"KitchenAid Stand Mixer","description":"Professional 5-quart stand mixer","price":449.0,"currency":"USD","rating":4.8,"review_count":12000,"in_stock":true,"color":"red","category":"home","brand":"KitchenAid","tags":["mixer","baking"]}
{"index":{"_index":"products-index","_id":"12"}}
{"product_name":"New Balance 990v6","description":"Premium made-in-USA running shoe","price":200.0,"currency":"USD","rating":4.8,"review_count":620,"in_stock":true,"color":"grey","category":"shoes","brand":"New Balance","tags":["premium","stability"]}
'
```

### Step 3: 创建 Bedrock 连接器

首先创建 IAM 角色，让 OpenSearch 能调用 Bedrock：

```bash
# 创建信任策略
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "opensearchservice.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "YOUR_ACCOUNT_ID"},
      "ArnLike": {"aws:SourceArn": "arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/agentic-search-lab"}
    }
  }]
}
EOF

aws iam create-role \
  --role-name opensearch-bedrock-connector-role \
  --assume-role-policy-document file:///tmp/trust-policy.json \
  --region us-east-1

# 添加 Bedrock 调用权限
cat > /tmp/bedrock-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:us-east-1:YOUR_ACCOUNT_ID:inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0",
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
    ]
  }]
}
EOF

aws iam put-role-policy \
  --role-name opensearch-bedrock-connector-role \
  --policy-name bedrock-invoke \
  --policy-document file:///tmp/bedrock-policy.json \
  --region us-east-1
```

!!! warning "重要：使用 Inference Profile ID"
    Bedrock Claude Sonnet 4 不支持直接用 Foundation Model ID 调用（会报 "on-demand throughput isn't supported"），必须使用 **Inference Profile ID**：`us.anthropic.claude-sonnet-4-20250514-v1:0`。这是实测踩坑发现的关键点。

接下来在 OpenSearch 中配置 FGAC 角色映射和创建连接器：

```bash
# 映射 IAM 用户和连接器角色到 FGAC
curl -s -u admin:'YourStrongPassword123!' \
  -X PUT "https://${ENDPOINT}/_plugins/_security/api/rolesmapping/ml_full_access" \
  -H 'Content-Type: application/json' -d '{
  "backend_roles": ["arn:aws:iam::YOUR_ACCOUNT_ID:role/opensearch-bedrock-connector-role"],
  "users": ["admin"]
}'

# 创建 Bedrock 连接器（使用 awscurl 进行 SigV4 签名）
cat > /tmp/bedrock-connector.json << 'EOF'
{
  "name": "Bedrock Claude Sonnet 4 Connector",
  "description": "Connector to Amazon Bedrock Claude Sonnet 4 for agentic search",
  "version": 1,
  "protocol": "aws_sigv4",
  "parameters": {
    "region": "us-east-1",
    "service_name": "bedrock",
    "model": "us.anthropic.claude-sonnet-4-20250514-v1:0"
  },
  "credential": {
    "roleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/opensearch-bedrock-connector-role"
  },
  "actions": [{
    "action_type": "predict",
    "method": "POST",
    "url": "https://bedrock-runtime.${parameters.region}.amazonaws.com/model/${parameters.model}/converse",
    "headers": {"content-type": "application/json"},
    "request_body": "{ \"system\": [{\"text\": \"${parameters.system_prompt}\"}], \"messages\": [${parameters._chat_history:-}{\"role\":\"user\",\"content\":[{\"text\":\"${parameters.user_prompt}\"}]}${parameters._interactions:-}]${parameters.tool_configs:-} }"
  }]
}
EOF

awscurl --region us-east-1 --service es \
  -X POST "https://${ENDPOINT}/_plugins/_ml/connectors/_create" \
  -H 'Content-Type: application/json' -d @/tmp/bedrock-connector.json
# 输出示例: {"connector_id":"your-connector-id"}
```

### Step 4: 注册并部署模型

```bash
CONNECTOR_ID="your-connector-id"  # 替换为上一步的输出

# 注册模型
awscurl --region us-east-1 --service es \
  -X POST "https://${ENDPOINT}/_plugins/_ml/models/_register" \
  -H 'Content-Type: application/json' -d "{
  \"name\": \"Bedrock Claude Sonnet 4\",
  \"function_name\": \"remote\",
  \"description\": \"Claude Sonnet 4 via Bedrock for agentic search\",
  \"connector_id\": \"${CONNECTOR_ID}\"
}"
# 输出: {"task_id":"...","status":"CREATED","model_id":"your-model-id"}

MODEL_ID="your-model-id"  # 替换为输出的 model_id

# 部署模型
awscurl --region us-east-1 --service es \
  -X POST "https://${ENDPOINT}/_plugins/_ml/models/${MODEL_ID}/_deploy"
# 输出: {"task_id":"...","task_type":"DEPLOY_MODEL","status":"COMPLETED"}
```

### Step 5: 创建 Agent 和搜索管线

#### 方式 A：Conversational Agent（推荐初次体验）

```bash
# 创建 Conversational Agent
cat > /tmp/conv-agent.json << EOF
{
  "name": "Conversational Agent for Agentic Search",
  "type": "conversational",
  "description": "Multi-tool conversational agent with memory",
  "llm": {
    "model_id": "${MODEL_ID}",
    "parameters": {"max_iteration": 15}
  },
  "memory": {"type": "conversation_index"},
  "parameters": {"_llm_interface": "bedrock/converse/claude"},
  "tools": [
    {"type": "ListIndexTool", "name": "ListIndexTool"},
    {"type": "IndexMappingTool", "name": "IndexMappingTool"},
    {"type": "QueryPlanningTool"}
  ],
  "app_type": "os_chat"
}
EOF

awscurl --region us-east-1 --service es \
  -X POST "https://${ENDPOINT}/_plugins/_ml/agents/_register" \
  -H 'Content-Type: application/json' -d @/tmp/conv-agent.json
# 输出: {"agent_id":"your-conv-agent-id"}

CONV_AGENT_ID="your-conv-agent-id"

# 创建搜索管线（含 response processor 获取推理轨迹）
awscurl --region us-east-1 --service es \
  -X PUT "https://${ENDPOINT}/_search/pipeline/agentic-conv-pipeline" \
  -H 'Content-Type: application/json' -d "{
  \"request_processors\": [{
    \"agentic_query_translator\": {\"agent_id\": \"${CONV_AGENT_ID}\"}
  }],
  \"response_processors\": [{
    \"agentic_context\": {\"agent_steps_summary\": true, \"dsl_query\": true}
  }]
}"
```

#### 方式 B：Flow Agent（高性能场景）

```bash
# 创建 Flow Agent
awscurl --region us-east-1 --service es \
  -X POST "https://${ENDPOINT}/_plugins/_ml/agents/_register" \
  -H 'Content-Type: application/json' -d "{
  \"name\": \"Flow Agent for Agentic Search\",
  \"type\": \"flow\",
  \"description\": \"Streamlined flow agent for fast query planning\",
  \"tools\": [{
    \"type\": \"QueryPlanningTool\",
    \"parameters\": {
      \"model_id\": \"${MODEL_ID}\",
      \"response_filter\": \"\$.output.message.content[0].text\"
    }
  }]
}"
# 输出: {"agent_id":"your-flow-agent-id"}

FLOW_AGENT_ID="your-flow-agent-id"

# 创建搜索管线
awscurl --region us-east-1 --service es \
  -X PUT "https://${ENDPOINT}/_search/pipeline/agentic-flow-pipeline" \
  -H 'Content-Type: application/json' -d "{
  \"request_processors\": [{
    \"agentic_query_translator\": {\"agent_id\": \"${FLOW_AGENT_ID}\"}
  }]
}"
```

### Step 6: 运行 Agentic Search

#### 基础自然语言查询

```bash
# Conversational Agent 查询
awscurl --region us-east-1 --service es \
  -X GET "https://${ENDPOINT}/products-index/_search?search_pipeline=agentic-conv-pipeline" \
  -H 'Content-Type: application/json' -d '{
  "query": {
    "agentic": {
      "query_text": "Find running shoes under 160 dollars",
      "query_fields": ["product_name", "description", "price", "category", "tags"]
    }
  }
}'
```

返回结果：Agent 自动生成含 `bool` + `range` 过滤的 DSL，返回 Puma RS-X ($120) 和 Nike Air Max 270 ($150)。

#### 聚合查询

```bash
awscurl --region us-east-1 --service es \
  -X GET "https://${ENDPOINT}/products-index/_search?search_pipeline=agentic-conv-pipeline" \
  -H 'Content-Type: application/json' -d '{
  "query": {
    "agentic": {
      "query_text": "What is the average price per category?",
      "query_fields": ["category", "price"]
    }
  }
}'
```

Agent 自动生成 `terms` + `avg` 聚合 DSL，返回每个品类的平均价格。

#### 对话上下文（Conversational Agent 专属）

```bash
# 第一轮：查找电子产品
# 响应的 ext 字段中包含 memory_id（需 agentic_context response processor）

# 第二轮：利用上下文追问
awscurl --region us-east-1 --service es \
  -X GET "https://${ENDPOINT}/products-index/_search?search_pipeline=agentic-conv-pipeline" \
  -H 'Content-Type: application/json' -d '{
  "query": {
    "agentic": {
      "query_text": "Which of those are under 500 dollars?",
      "query_fields": ["product_name", "category", "price"],
      "memory_id": "your-memory-id-from-first-query"
    }
  }
}'
```

Agent 记住了上一轮的 "electronics" 上下文，自动生成 `category=electronics AND price<500` 的 DSL。

## 测试结果

### Conversational Agent vs Flow Agent 延迟对比

同一查询 "Find black shoes" 各跑 5 次：

| Agent 类型 | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | **平均** |
|-----------|-------|-------|-------|-------|-------|--------|
| Conversational | 14.0s | 13.9s | 15.8s | 17.0s | 10.4s | **14.2s** |
| Flow | 1.9s | 2.7s | 4.5s | 1.6s | 2.0s | **2.5s** |

**Flow Agent 平均快 5.6 倍**，因为它只调用一次 LLM（QueryPlanningTool），而 Conversational Agent 需要多轮 LLM 调用来编排工具。

### 功能测试结果

| 场景 | 查询 | 结果 | 耗时 |
|------|------|------|------|
| 基础筛选 | "Find running shoes under 160 dollars" | ✅ 2 hits（正确过滤） | 24.8s |
| 聚合统计 | "What is the average price per category?" | ✅ 4 个品类聚合 | 13.0s |
| 中文查询 | "找到价格低于200美元的高评分产品" | ✅ 5 hits（跨语言理解） | 11.3s |
| 对话上下文 | "Electronics" → "Which under $500?" | ✅ 1 hit（上下文保持） | 12.4s |
| 无结果边界 | "$10000+ Antarctic products" | ✅ 0 hits（无报错） | 10.1s |

## 踩坑记录

!!! warning "踩坑 1：Bedrock 模型调用必须使用 Inference Profile ID"
    直接使用 Foundation Model ID（如 `anthropic.claude-sonnet-4-20250514-v1:0`）会报错：
    > "Invocation of model ID ... with on-demand throughput isn't supported"
    
    必须使用 Inference Profile ID：`us.anthropic.claude-sonnet-4-20250514-v1:0`。
    **状态**：⚠️ 实测发现，OpenSearch 官方文档示例中未明确说明此要求。

!!! warning "踩坑 2：FGAC 角色映射中 IAM 用户 ARN 的正确位置"
    IAM 用户 ARN 必须放在 `users` 字段中，而不是 `backend_roles`。`backend_roles` 用于 IAM Role 的 ARN。
    **状态**：已查文档确认，这是 FGAC 的设计逻辑。

!!! warning "踩坑 3：必须添加 agentic_context response processor"
    仅配置 `agentic_query_translator` request processor 时，响应只返回搜索结果。要获取 `memory_id`（对话上下文）、`dsl_query`（生成的 DSL）和 `agent_steps_summary`（推理轨迹），必须在 pipeline 中添加 `agentic_context` response processor。
    **状态**：已查文档确认，这是设计分离。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch r6g.large.search | ~$0.167/hr | 2 hr | ~$0.33 |
| EBS gp3 10GB | ~$0.08/GB-month | prorated | ~$0.01 |
| Bedrock Claude Sonnet 4 | ~$3/M input + $15/M output tokens | ~20 queries | ~$0.50 |
| IAM Role | 免费 | - | $0 |
| **合计** | | | **~$0.84** |

## 清理资源

```bash
# 1. 删除 OpenSearch 域
aws opensearch delete-domain --domain-name agentic-search-lab --region us-east-1

# 2. 删除 IAM 角色和策略
aws iam delete-role-policy \
  --role-name opensearch-bedrock-connector-role \
  --policy-name bedrock-invoke \
  --region us-east-1

aws iam delete-role \
  --role-name opensearch-bedrock-connector-role \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。OpenSearch 域按小时计费（r6g.large.search ~$0.167/hr），不删除会持续产生费用。

## 结论与建议

### 适用场景

- **内部搜索工具**：让非技术团队用自然语言查询业务数据（订单、产品、日志）
- **客户支持系统**：自然语言驱动的知识库搜索
- **数据探索**：分析师用自然语言做聚合统计，免去学习 DSL 的门槛

### 生产环境建议

1. **Agent 选型**：80% 的场景用 Flow Agent 即可（低延迟 + 低成本），复杂多索引查询再用 Conversational Agent
2. **搜索模板（Search Templates）**：生产环境强烈建议配置搜索模板，引导 LLM 使用经过测试的查询模式
3. **安全性**：始终启用 FGAC，限制 connector role 的 Bedrock 权限范围到特定模型
4. **成本控制**：监控 Bedrock API 调用量，Flow Agent 的单次 LLM 调用远低于 Conversational Agent 的多轮调用
5. **索引指定**：在搜索请求中明确指定目标索引，避免全集群扫描

### 与传统搜索对比

| 维度 | 传统 DSL 查询 | Agentic Search |
|------|-------------|----------------|
| 入门门槛 | 需学习 Query DSL 语法 | 自然语言即可 |
| 延迟 | 毫秒级 | 秒级（1.6-17s） |
| 灵活性 | 完全精确控制 | LLM 理解意图后生成 |
| 适用用户 | 开发者/工程师 | 所有人 |
| 成本 | 仅 OpenSearch 集群费用 | 额外 LLM API 调用费 |

**Agentic Search 不是替代传统搜索，而是互补**。高性能、精确控制的场景继续用 DSL；用户友好、探索性查询用 Agentic Search。

## 参考链接

- [AWS What's New: OpenSearch Service Agentic Search](https://aws.amazon.com/about-aws/whats-new/2025/11/opensearch-service-agentic-search/)
- [Agentic Search 官方文档（AWS）](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/agentic-search.html)
- [Agentic Search 技术文档（OpenSearch）](https://docs.opensearch.org/latest/vector-search/ai-search/agentic-search/index/)
- [AI Search Flows 插件](https://docs.opensearch.org/latest/vector-search/ai-search/building-agentic-search-flows/)
- [Agent 配置详解](https://docs.opensearch.org/latest/vector-search/ai-search/agentic-search/agent-customization/)
