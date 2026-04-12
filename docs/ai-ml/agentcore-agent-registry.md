# AWS Agent Registry 实测：用 CLI 走通 Agent 资产治理全生命周期

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $0.01（纯 API 调用，Preview 阶段）
    - **Region**: us-west-2
    - **最后验证**: 2026-04-12

## 背景

当组织内的 AI Agent 从几个扩展到几十上百个，最先崩溃的不是计算资源，而是发现和治理。团队 A 花两周搭了一个文档解析工具，团队 B 不知道这个工具存在，又花两周重新造了一个。没有人知道组织内到底有多少 Agent、谁在维护、是否合规。

AWS Agent Registry 是 Amazon Bedrock AgentCore 新增的 Preview 功能，提供一个**私有的、受治理的资产目录**。你可以把组织内的 MCP Server、Agent、Skill 和任意自定义资源注册到 Registry，通过审批工作流控制哪些资源可被发现，用语义搜索让开发者快速找到已有能力——而且 Registry 本身也是一个 MCP Server，任何 MCP 客户端都能直接查询。

本文用 AWS CLI 从零走通完整生命周期：创建 Registry → 注册四种资源类型 → 审批工作流 → 搜索发现 → MCP endpoint 调用 → 清理，并记录实测中发现的搜索排序特征、延迟数据和文档未提及的边界行为。

## 前置条件

- AWS 账号，IAM 用户/角色具备 `bedrock-agentcore:*` 权限
- **AWS CLI v2.34.29+**（旧版本无 Registry 命令，需 `curl + install --update` 升级）
- 操作 Region：us-west-2（也可用 us-east-1、ap-northeast-1、ap-southeast-2、eu-west-1）

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateRegistry",
        "bedrock-agentcore:GetRegistry",
        "bedrock-agentcore:ListRegistries",
        "bedrock-agentcore:DeleteRegistry",
        "bedrock-agentcore:CreateRegistryRecord",
        "bedrock-agentcore:GetRegistryRecord",
        "bedrock-agentcore:ListRegistryRecords",
        "bedrock-agentcore:UpdateRegistryRecord",
        "bedrock-agentcore:DeleteRegistryRecord",
        "bedrock-agentcore:SubmitRegistryRecordForApproval",
        "bedrock-agentcore:UpdateRegistryRecordStatus",
        "bedrock-agentcore:SearchRegistryRecords",
        "bedrock-agentcore:InvokeRegistryMcp"
      ],
      "Resource": "arn:aws:bedrock-agentcore:*:*:*"
    }
  ]
}
```

</details>

## 核心概念

### 架构全景

Agent Registry 围绕两个核心资源构建：

| 资源 | 说明 |
|------|------|
| **Registry** | 目录实例。每个 Registry 有独立的认证配置（IAM 或 JWT）和审批设置 |
| **Registry Record** | 注册的单个资源元数据。支持 MCP Server、Agent (A2A)、Agent Skills、Custom 四种类型 |

### Record 生命周期

```
Create → DRAFT → Submit → PENDING_APPROVAL → Approve → APPROVED（可搜索）
                               │                          │
                               │ Reject                   │ Edit → 新 DRAFT 修订版
                               ▼                          │       （旧版仍可搜索）
                          REJECTED ── Approve（直接批准）──┘
                               │
                               └── Edit → DRAFT

         任意状态 → DEPRECATED（终态，不可恢复）
```

### 关键限制一览

| 参数 | 限制 |
|------|------|
| Registry 名称 | 最长 64 字符，`[a-zA-Z0-9][a-zA-Z0-9_\-\.\/]*` |
| Record 名称 | 最长 255 字符 |
| Description | 1-4,096 字符 |
| 搜索查询 | 1-256 字符 |
| 搜索结果 | 最多 20 条（默认 10） |
| Auth type | 创建后不可更改（IAM 或 JWT 二选一） |

### 三种访问方式

| 方式 | 适用场景 |
|------|---------|
| **Console** | 管理员日常操作 |
| **AWS CLI / SDK** | 自动化流程、CI/CD 集成 |
| **MCP Endpoint** | IDE 集成（Kiro、Claude Code）、Agent-to-Agent 发现 |

## 动手实践

> 以下命令均指定 `--region us-west-2`。请将 `--profile` 替换为你的 AWS Profile。

### Step 1: 创建 Registry

创建两个 Registry 来对比审批行为：一个手动审批，一个自动审批。

```bash
# 创建手动审批 Registry
aws bedrock-agentcore-control create-registry \
  --name "my-agent-registry" \
  --description "Production registry with manual approval" \
  --region us-west-2
```

```json
{
    "registryArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:registry/yBXelhf77WOcBvZM"
}
```

```bash
# 创建自动审批 Registry
aws bedrock-agentcore-control create-registry \
  --name "dev-agent-registry" \
  --description "Development registry with auto-approval" \
  --approval-configuration '{"autoApproval": true}' \
  --region us-west-2
```

Registry 状态从 `CREATING` 变为 `READY` 约需 **60 秒**。查询状态：

```bash
aws bedrock-agentcore-control get-registry \
  --registry-id <registryId> \
  --region us-west-2 \
  --query '{status:status, auth:authorizerType, autoApproval:approvalConfiguration.autoApproval}'
```

```json
{
    "status": "READY",
    "auth": "AWS_IAM",
    "autoApproval": false
}
```

### Step 2: 手动注册 MCP Server Record

手动构造 MCP Server 的 server definition 和 tool definitions：

```bash
cat > /tmp/create-mcp-record.json << 'EOF'
{
    "registryId": "<registryId>",
    "name": "weather-forecast-server",
    "description": "An MCP server providing weather forecasting tools for various locations",
    "descriptorType": "MCP",
    "descriptors": {
        "mcp": {
            "server": {
                "schemaVersion": "2025-12-11",
                "inlineContent": "{\"name\": \"my-org/weather-forecast-server\", \"description\": \"Weather data and forecasts\", \"version\": \"1.0.0\"}"
            },
            "tools": {
                "protocolVersion": "2025-11-25",
                "inlineContent": "{\"tools\": [{\"name\": \"get_current_weather\", \"description\": \"Get current weather conditions for a location\", \"inputSchema\": {\"type\": \"object\", \"properties\": {\"location\": {\"type\": \"string\"}}, \"required\": [\"location\"]}}, {\"name\": \"get_weather_forecast\", \"description\": \"Get multi-day weather forecast\", \"inputSchema\": {\"type\": \"object\", \"properties\": {\"location\": {\"type\": \"string\"}, \"days\": {\"type\": \"integer\"}}, \"required\": [\"location\"]}}]}"
            }
        }
    },
    "recordVersion": "1.0.0"
}
EOF

aws bedrock-agentcore-control create-registry-record \
  --cli-input-json file:///tmp/create-mcp-record.json \
  --region us-west-2
```

```json
{
    "recordArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:registry/.../record/BdsnMRNLLsx4",
    "status": "CREATING"
}
```

Record 几秒后变为 `DRAFT`。

!!! tip "关键细节"
    `descriptors.mcp.server.inlineContent` 和 `tools.inlineContent` 都是 **JSON 字符串**（不是 JSON 对象）。tool definitions 的顶层必须是 `{"tools": [...]}` 对象，不能直接传数组。

### Step 3: URL-based 自动发现（对比手动注册）

Agent Registry 可以从 MCP Server endpoint 自动拉取元数据——这是比手动注册更强大的方式：

```bash
aws bedrock-agentcore-control create-registry-record \
  --registry-id <registryId> \
  --name "aws-knowledge-mcp" \
  --description "AWS Knowledge MCP server via URL auto-discovery" \
  --descriptor-type MCP \
  --synchronization-type URL \
  --synchronization-configuration '{"fromUrl": {"url": "https://knowledge-mcp.global.api.aws"}}' \
  --region us-west-2
```

几秒后查看 Record 详情，可以看到自动拉取的结果：

```bash
aws bedrock-agentcore-control get-registry-record \
  --registry-id <registryId> \
  --record-id <recordId> \
  --region us-west-2 \
  --query '{name:name,status:status,serverSchema:descriptors.mcp.server.schemaVersion}'
```

```json
{
    "name": "AWSDocumentationMCPProdGateway",
    "status": "DRAFT",
    "serverSchema": "2025-12-11"
}
```

**关键发现**：自动发现会覆盖你指定的 `--name`，改用从 endpoint 拉取的名称。在实测中，`--name "aws-knowledge-mcp"` 被替换为 `"AWSDocumentationMCPProdGateway"`。同时自动拉取了全部 6 个 tools 的完整 description 和 inputSchema。

| 对比项 | 手动注册 | URL-based 自动发现 |
|--------|---------|-------------------|
| 元数据来源 | 人工编写 | 自动从 endpoint 提取 |
| Tool definitions | 需自己构造 JSON | 自动提取所有 tools |
| 准确性 | 取决于编写者 | 与运行中的 server 一致 |
| 适用场景 | 尚未部署的资源 | 已部署的 MCP Server / A2A Agent |

### Step 4: 注册其他资源类型

**Agent (A2A) Record**:

```bash
cat > /tmp/create-agent-record.json << 'EOF'
{
    "registryId": "<registryId>",
    "name": "travel-booking-agent",
    "description": "AI agent for booking flights, hotels, and car rentals",
    "descriptorType": "A2A",
    "descriptors": {
        "a2a": {
            "agentCard": {
                "schemaVersion": "0.3",
                "inlineContent": "{\"name\": \"Travel Booking Agent\", \"description\": \"Book flights, hotels, and car rentals\", \"version\": \"2.0.0\", \"protocolVersion\": \"0.3.0\", \"url\": \"https://api.example.com/travel-agent/a2a\", \"capabilities\": {}, \"defaultInputModes\": [\"text/plain\"], \"defaultOutputModes\": [\"text/plain\"], \"skills\": [{\"id\": \"flight-booking\", \"name\": \"Flight Booking\", \"description\": \"Search and book flights\", \"tags\": [\"travel\"]}]}"
            }
        }
    },
    "recordVersion": "2.0.0"
}
EOF

aws bedrock-agentcore-control create-registry-record \
  --cli-input-json file:///tmp/create-agent-record.json \
  --region us-west-2
```

**Agent Skills Record**:

```bash
cat > /tmp/create-skills-record.json << 'EOF'
{
    "registryId": "<registryId>",
    "name": "document-processing-skill",
    "description": "Extract structured data from PDF documents, invoices, and receipts",
    "descriptorType": "AGENT_SKILLS",
    "descriptors": {
        "agentSkills": {
            "skillMd": {
                "inlineContent": "---\nname: document-processing\ndescription: Extract structured data from PDF documents using OCR and LLM.\n---\n\n# Document Processing Skill\n\nThis skill extracts tables, key-value pairs, and text from documents."
            },
            "skillDefinition": {
                "schemaVersion": "0.1.0",
                "inlineContent": "{\"websiteUrl\": \"https://example.com/doc-processing\", \"repository\": {\"url\": \"https://github.com/example/doc-processing\", \"source\": \"github\"}}"
            }
        }
    },
    "recordVersion": "1.2.0"
}
EOF

aws bedrock-agentcore-control create-registry-record \
  --cli-input-json file:///tmp/create-skills-record.json \
  --region us-west-2
```

**Custom Resource Record**:

```bash
cat > /tmp/create-custom-record.json << 'EOF'
{
    "registryId": "<registryId>",
    "name": "customer-knowledge-base",
    "description": "Bedrock Knowledge Base for customer support FAQs and product docs",
    "descriptorType": "CUSTOM",
    "descriptors": {
        "custom": {
            "inlineContent": "{\"type\": \"knowledge-base\", \"provider\": \"Amazon Bedrock\", \"embeddingModel\": \"amazon.titan-embed-text-v2\", \"documentCount\": 15000}"
        }
    },
    "recordVersion": "3.1.0"
}
EOF

aws bedrock-agentcore-control create-registry-record \
  --cli-input-json file:///tmp/create-custom-record.json \
  --region us-west-2
```

### Step 5: 审批工作流——手动 vs 自动

**手动审批流程（3 步）**：

```bash
# 1. 提交审批
aws bedrock-agentcore-control submit-registry-record-for-approval \
  --registry-id <registryId> \
  --record-id <recordId> \
  --region us-west-2
# → status: "PENDING_APPROVAL"

# 2. 批准（Curator 操作）
aws bedrock-agentcore-control update-registry-record-status \
  --registry-id <registryId> \
  --record-id <recordId> \
  --status APPROVED \
  --status-reason "Reviewed and approved for production use" \
  --region us-west-2
# → status: "APPROVED"
```

**自动审批流程（1 步）**：在 auto-approval Registry 中：

```bash
aws bedrock-agentcore-control submit-registry-record-for-approval \
  --registry-id <autoApproveRegistryId> \
  --record-id <recordId> \
  --region us-west-2
# → status: "APPROVED"  # 直接跳过 PENDING_APPROVAL！
```

!!! info "auto-approval 行为"
    `autoApproval: true` 的 Registry 中，`submit-for-approval` 会直接将 Record 状态设为 `APPROVED`，跳过 `PENDING_APPROVAL` 阶段。适合开发环境快速迭代。

### Step 6: 搜索——语义搜索 vs 关键词搜索

全部 Record 批准后（等待 1-2 分钟让搜索索引生效），测试不同搜索策略：

**精确名称搜索（关键词优势）**：

```bash
aws bedrock-agentcore search-registry-records \
  --search-query "weather-forecast-server" \
  --registry-ids "<registryARN>" \
  --region us-west-2 \
  --query 'registryRecords[].{name:name,type:descriptorType}'
```

```json
[
    {"name": "weather-forecast-server", "type": "MCP"},
    {"name": "AWSDocumentationMCPProdGateway", "type": "MCP"}
]
```

**自然语言搜索（语义优势）**：

```bash
aws bedrock-agentcore search-registry-records \
  --search-query "extract data from PDF documents and invoices" \
  --registry-ids "<registryARN>" \
  --max-results 5 \
  --region us-west-2 \
  --query 'registryRecords[].{name:name,type:descriptorType}'
```

```json
[
    {"name": "document-processing-skill", "type": "AGENT_SKILLS"},
    {"name": "AWSDocumentationMCPProdGateway", "type": "MCP"},
    {"name": "weather-forecast-server", "type": "MCP"},
    {"name": "customer-knowledge-base", "type": "CUSTOM"},
    {"name": "travel-booking-agent", "type": "A2A"}
]
```

**Metadata Filter 搜索**：

```bash
# 只搜索 MCP 类型资源
aws bedrock-agentcore search-registry-records \
  --search-query "find tools" \
  --registry-ids "<registryARN>" \
  --filters '{"descriptorType": {"$eq": "MCP"}}' \
  --region us-west-2 \
  --query 'registryRecords[].{name:name,type:descriptorType}'
```

```json
[
    {"name": "AWSDocumentationMCPProdGateway", "type": "MCP"},
    {"name": "weather-forecast-server", "type": "MCP"}
]
```

支持的 filter 操作符：`$eq`、`$ne`、`$in`，逻辑操作符：`$and`、`$or`。可过滤字段：`name`、`descriptorType`、`version`。

### Step 7: 双修订版行为——安全迭代 APPROVED 记录

编辑已批准的 Record，验证搜索不受影响：

```bash
cat > /tmp/update-record.json << 'EOF'
{
    "registryId": "<registryId>",
    "recordId": "<recordId>",
    "description": {"optionalValue": "Updated: now with weather alerts and historical data"},
    "recordVersion": "2.0.0"
}
EOF

aws bedrock-agentcore-control update-registry-record \
  --cli-input-json file:///tmp/update-record.json \
  --region us-west-2
```

编辑后状态对比：

| API | 返回版本 | 状态 |
|-----|---------|------|
| `GetRegistryRecord` | v2.0.0 | DRAFT（新修订版） |
| `SearchRegistryRecords` | v1.0.0 | APPROVED（旧修订版，仍可搜索） |

**这意味着**：在生产环境中，你可以安全地迭代 Record 内容，用户搜到的始终是最后一个 APPROVED 版本，直到新版本也通过审批。

### Step 8: 通过 MCP Endpoint 直接交互

Registry 本身就是一个 MCP Server！用 curl + SigV4 签名调用：

```bash
# 获取临时凭证
CREDS=$(aws sts get-session-token --output json)
export AWS_ACCESS_KEY_ID=$(echo $CREDS | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $CREDS | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $CREDS | jq -r '.Credentials.SessionToken')

# MCP Initialize
curl -s -X POST \
  "https://bedrock-agentcore.us-west-2.amazonaws.com/registry/<registryId>/mcp" \
  -H "Content-Type: application/json" \
  -H "X-Amz-Security-Token: ${AWS_SESSION_TOKEN}" \
  --aws-sigv4 "aws:amz:us-west-2:bedrock-agentcore" \
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"my-client","version":"1.0.0"}}}' | jq .
```

```json
{
  "result": {
    "protocolVersion": "2025-11-25",
    "serverInfo": {"name": "bedrock-agentcore-registry", "version": "1.0.0"},
    "capabilities": {"tools": {"listChanged": false}}
  }
}
```

```bash
# 列出可用工具
curl -s -X POST ... \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | jq '.result.tools[].name'
# → "search_registry_records"

# 通过 MCP 搜索
curl -s -X POST ... \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_registry_records","arguments":{"searchQuery":"weather"}}}' | jq .
```

Registry 的 MCP Endpoint 暴露一个工具 `search_registry_records`，支持 `searchQuery`、`maxResults`、`filter` 三个参数。这意味着任何 MCP 客户端（如 Kiro、Claude Code）都可以直接查询 Registry。

!!! warning "MCP 协议版本"
    必须使用 `protocolVersion: "2025-11-25"`。使用旧版本（如 `2024-11-05`）会收到 `Unsupported protocol version` 错误。

### Step 9: 边界测试

```bash
# Registry 名称 64 字符（上限）→ ✅ 成功
aws bedrock-agentcore-control create-registry \
  --name "$(python3 -c 'print("a"*64)')" \
  --region us-west-2

# Registry 名称 65 字符 → ❌ ValidationException
aws bedrock-agentcore-control create-registry \
  --name "$(python3 -c 'print("a"*65)')" \
  --region us-west-2
# → "Member must have length less than or equal to 64"

# 特殊字符名称 → ❌ 被拒
aws bedrock-agentcore-control create-registry \
  --name "test@registry!" \
  --region us-west-2
# → "Member must satisfy regular expression pattern"

# 重复名称 → ✅ 允许！（无唯一性约束）
aws bedrock-agentcore-control create-registry \
  --name "my-agent-registry" \
  --region us-west-2
# → 成功创建第二个同名 Registry
```

## 测试结果

| # | 测试场景 | 结果 | 关键数据 |
|---|---------|------|---------|
| T1 | IAM Registry 完整生命周期 | ✅ | 全流程跑通：CREATING→READY→DRAFT→PENDING→APPROVED→DEPRECATED |
| T2 | URL-based 自动发现 | ✅ | 自动提取 server name + 6 tools 完整元数据 |
| T3 | Auto-approval vs Manual | ✅ | Auto-approval 直接跳到 APPROVED |
| T4 | 语义搜索 vs 关键词搜索 | ✅ | 精确名称搜准确；语义搜索受 tool description 量影响排序 |
| T5 | 四种 Record 类型 | ✅ | MCP/A2A/AGENT_SKILLS/CUSTOM 全部支持 |
| T6 | 双修订版行为 | ✅ | GetRecord 返回 DRAFT v2.0，Search 返回 APPROVED v1.0 |
| T7 | 搜索可见延迟 | ✅ | Approve 后 ~100 秒搜索可见 |
| T8 | 边界条件 | ✅ | 64 字符上限、重复名称允许、特殊字符拒绝 |
| T9 | MCP Endpoint 调用 | ✅ | Initialize + tools/list + search 全部正常 |
| T10 | Metadata filter | ✅ | $eq/$in/$or 均有效 |

## 踩坑记录

!!! warning "踩坑 1: CLI 版本必须升级到 2.34.29+"
    旧版 AWS CLI 的 `bedrock-agentcore-control` 没有任何 Registry 相关命令。必须升级到 2.34.29 或更新版本。如果你发现 `create-registry` 命令不存在，先运行：
    
    ```bash
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip"
    cd /tmp && unzip -qo awscliv2.zip && sudo ./aws/install --update
    ```

!!! warning "踩坑 2: update-registry-record 使用 optionalValue 包装模式"
    更新 Record 时，`description` 和 `descriptors` 等字段需要用 `{"optionalValue": ...}` 包装：
    
    ```json
    {
      "description": {"optionalValue": "New description"},
      "descriptors": {"optionalValue": {"mcp": {"optionalValue": {...}}}}
    }
    ```
    
    直接传字符串会报 `ParamValidation` 错误。这是 API 设计用于区分"不更新此字段"和"更新为新值"。

!!! warning "踩坑 3: update-registry-record-status 的 --status-reason 是必填参数"
    文档示例中不够明显，但 CLI 强制要求提供 `--status-reason`。缺少此参数会报错。

!!! info "发现: Registry 名称允许重复"
    同一 Account 和 Region 下，可以创建多个**同名** Registry。它们通过 registryId 区分。这在文档中未明确提及。生产环境建议通过命名规范（如加环境前缀 `prod-`、`dev-`）避免混淆。

!!! info "发现: 语义搜索排序受 tool description 长度影响"
    注册了大量 tool definitions 的 MCP Server Record（如 aws-knowledge 有 6 个 tool，每个都有数千字的 description）在语义搜索中容易"抢占"排名，即使查询与其主题不太相关。**建议**：对意图明确的搜索，配合 metadata filter 使用。

## 费用明细

| 资源 | 费用 |
|------|------|
| Registry 创建/管理 | $0（Preview 阶段） |
| Record 操作 | $0（Preview 阶段） |
| SearchRegistryRecords API | $0（Preview 阶段） |
| **合计** | **< $0.01** |

## 清理资源

```bash
# 1. 删除所有 Records（必须先于 Registry 删除）
for RECORD_ID in BdsnMRNLLsx4 8AKH4aeIxJP3 tqoJLabLFVEp U2LadEeF1Se7 KN7pVRc5LeOa; do
  aws bedrock-agentcore-control delete-registry-record \
    --registry-id <registryId> \
    --record-id $RECORD_ID \
    --region us-west-2
done

# 2. 删除 auto-approval Registry 的 Records
aws bedrock-agentcore-control delete-registry-record \
  --registry-id <autoApproveRegistryId> \
  --record-id <recordId> \
  --region us-west-2

# 3. 删除 Registries
for REG_ID in <registryId> <autoApproveRegistryId> <boundaryRegistryId1> <boundaryRegistryId2>; do
  aws bedrock-agentcore-control delete-registry \
    --registry-id $REG_ID \
    --region us-west-2
done
```

!!! danger "务必清理"
    虽然 Preview 阶段免费，但 GA 后可能产生费用。Lab 完成后请执行清理步骤。

## 结论与建议

### 适用场景推荐

| 场景 | 推荐配置 | 理由 |
|------|---------|------|
| 开发/测试环境 | Auto-approval + IAM auth | 快速迭代，无审批阻塞 |
| 生产目录 | Manual approval + IAM auth | 人工把关资源质量 |
| 跨团队发现（含外部用户） | Manual approval + JWT auth | 支持企业 IdP，无需 IAM |
| IDE 集成 | MCP Endpoint | Kiro/Claude Code 直接查询 |

### 注册方式选择

| 方式 | 适用 | 不适用 |
|------|------|--------|
| 手动注册 | 尚未部署的资源、Custom 类型 | 已部署且频繁更新的 MCP Server |
| URL-based 自动发现 | 已部署的 MCP Server / A2A Agent | 无公网 endpoint 的内部资源 |
| 触发同步更新 | 已注册但元数据有变化 | — |

### 生产注意事项

1. **搜索延迟**：Approve 后到搜索可见有 ~100 秒延迟（最终一致性）。自动化流程中需加重试逻辑。
2. **Auth 不可变**：Registry 的认证方式创建后不可更改。请在创建前确认需求。
3. **名称不唯一**：Registry 名称允许重复，务必制定命名规范。
4. **MCP 协议版本**：MCP Endpoint 仅支持 `2025-11-25`，确保客户端兼容。
5. **搜索优化**：为获得最佳搜索结果，写好 description（用自然语言描述用途和场景），并用 metadata filter 缩小范围。

## 参考链接

- [AWS Agent Registry 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/04/aws-agent-registry-in-agentcore-preview/)
- [AWS Blog: The future of managing agents at scale](https://aws.amazon.com/blogs/machine-learning/the-future-of-managing-agents-at-scale-aws-agent-registry-now-in-preview/)
- [MCP Registry Schema (GitHub)](https://github.com/modelcontextprotocol/static/tree/main/schemas)
