# Bedrock KB Retrieval MCP Server 实测：让 AI Agent 通过 MCP 协议查询知识库

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $1-2（OpenSearch Serverless 集合 + KB API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-14

## 背景

在 AI Agent 架构中，让大模型访问企业私有知识一直是 RAG 的核心场景。Amazon Bedrock Knowledge Bases 提供了托管的 RAG 方案，但 Agent 如何优雅地发现和查询这些知识库？

AWS 官方开源了 `awslabs.bedrock-kb-retrieval-mcp-server`，这是一个基于 MCP（Model Context Protocol）的服务器，让任何支持 MCP 的 AI Agent（Claude Desktop、Cursor、VS Code、Kiro 等）都能通过标准协议**自动发现**和**查询**你的 Bedrock Knowledge Bases。

本文从零搭建测试环境，实测 MCP Server 的全部功能，并通过 Reranking On/Off 对比实验，展示如何用一行配置显著提升检索质量。

## 前置条件

- AWS 账号，IAM 用户/角色需要以下权限
- AWS CLI v2 已配置
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) 包管理器

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "KBDiscovery",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListKnowledgeBases",
        "bedrock:GetKnowledgeBase",
        "bedrock:ListTagsForResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "KBDataSources",
      "Effect": "Allow",
      "Action": [
        "bedrock-agent:ListDataSources",
        "bedrock-agent:GetDataSource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "KBQuery",
      "Effect": "Allow",
      "Action": ["bedrock-agent:Retrieve"],
      "Resource": "arn:aws:bedrock:*:*:knowledge-base/*"
    },
    {
      "Sid": "Reranking",
      "Effect": "Allow",
      "Action": ["bedrock:Rerank", "bedrock:InvokeModel"],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

### MCP Server 架构

`awslabs.bedrock-kb-retrieval-mcp-server` 基于 FastMCP 框架，通过 stdio 与 MCP 客户端通信。它暴露两个 Tool：

| Tool | 功能 | 参数 |
|------|------|------|
| `ListKnowledgeBases` | 发现所有带指定 tag 的 KB 及其 data sources | 无参数 |
| `QueryKnowledgeBases` | 自然语言查询指定 KB | query, knowledge_base_id, number_of_results, reranking, reranking_model_name, data_source_ids |

### KB 发现机制

MCP Server **不会**列出账户下所有 KB。它通过 **tag 过滤**来控制哪些 KB 对 Agent 可见：

- 默认 tag key：`mcp-multirag-kb`，value 必须为 `true`
- 可通过 `KB_INCLUSION_TAG_KEY` 环境变量自定义 tag key

### 环境变量配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AWS_PROFILE` | AWS 凭证 profile | - |
| `AWS_REGION` | AWS Region | - |
| `KB_INCLUSION_TAG_KEY` | KB 过滤用的 tag key | `mcp-multirag-kb` |
| `BEDROCK_KB_RERANKING_ENABLED` | 全局 Reranking 开关 | `false` |
| `FASTMCP_LOG_LEVEL` | 日志级别 | `INFO` |

### Reranking 模型 Region 支持

| 模型 | Model ID | us-east-1 | us-west-2 | ap-northeast-1 | eu-central-1 |
|------|----------|-----------|-----------|-----------------|---------------|
| Amazon Rerank 1.0 | amazon.rerank-v1:0 | ❌ | ✅ | ✅ | ✅ |
| Cohere Rerank 3.5 | cohere.rerank-v3-5:0 | ✅ | ✅ | ✅ | ✅ |

!!! warning "注意"
    在 us-east-1 只能使用 Cohere Rerank 3.5，**Amazon Rerank 1.0 不可用**。

## 动手实践

### Step 1: 准备测试数据

创建 S3 bucket 并上传不同主题的测试文档，便于后续验证 data source 过滤和相关性排序：

```bash
# 创建 S3 bucket
aws s3 mb s3://mcp-kb-test-{ACCOUNT_ID} \
  --region us-east-1 \
  --profile your-profile

# 上传 3 个不同主题的文档
aws s3 cp serverless-guide.txt s3://mcp-kb-test-{ACCOUNT_ID}/serverless/ \
  --region us-east-1 --profile your-profile

aws s3 cp bedrock-overview.txt s3://mcp-kb-test-{ACCOUNT_ID}/bedrock/ \
  --region us-east-1 --profile your-profile

aws s3 cp networking-guide.txt s3://mcp-kb-test-{ACCOUNT_ID}/networking/ \
  --region us-east-1 --profile your-profile
```

### Step 2: 创建 Bedrock Knowledge Base

创建 KB 需要：IAM Service Role、OpenSearch Serverless 集合（向量存储）、和 Embedding 模型。

**推荐使用控制台的 Quick Create 方式**，它会自动创建 OpenSearch Serverless 集合和索引：

1. 进入 Bedrock Console → Knowledge bases → Create
2. 选择 "Knowledge base with vector store"
3. Embedding 模型选择 `Amazon Titan Text Embeddings V2`
4. Vector database 选择 "Quick create a new vector store" → Amazon OpenSearch Serverless
5. 添加 3 个 S3 数据源，分别指向 `serverless/`、`bedrock/`、`networking/` 前缀
6. 创建完成后，逐个 Sync 每个数据源

**关键步骤：给 KB 打 tag**

```bash
aws bedrock-agent tag-resource \
  --resource-arn arn:aws:bedrock:us-east-1:{ACCOUNT_ID}:knowledge-base/{KB_ID} \
  --tags mcp-multirag-kb=true \
  --region us-east-1 --profile your-profile
```

!!! tip "为什么需要打 tag？"
    MCP Server 通过 tag 过滤来决定哪些 KB 对 Agent 可见。没有 `mcp-multirag-kb=true` tag 的 KB 不会出现在 `ListKnowledgeBases` 结果中。这是一个安全设计——避免 Agent 意外访问不该访问的 KB。

### Step 3: 安装并测试 MCP Server

```bash
# 安装 uv（如果还没有）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 通过 MCP 协议发送 initialize + ListKnowledgeBases 调用
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ListKnowledgeBases","arguments":{}}}\n' \
  | AWS_PROFILE=your-profile AWS_REGION=us-east-1 \
    KB_INCLUSION_TAG_KEY=mcp-multirag-kb FASTMCP_LOG_LEVEL=ERROR \
    uvx awslabs.bedrock-kb-retrieval-mcp-server@latest
```

**实测输出**（MCP Server v1.27.0）：

```json
{
  "KTVKJDYLDQ": {
    "name": "mcp-test-kb-aws-docs",
    "description": "Test KB for MCP Server lab - AWS documentation topics",
    "data_sources": [
      {"id": "BPIRBENMXS", "name": "serverless-docs"},
      {"id": "EJDFPMMLXW", "name": "networking-docs"},
      {"id": "ZLQXXT1TK9", "name": "bedrock-docs"}
    ]
  }
}
```

成功发现了带 tag 的 KB 及其 3 个数据源。

### Step 4: 基本查询

```bash
# 查询 "What is AWS Lambda and how does it scale?"
printf '...(initialize)...\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"QueryKnowledgeBases","arguments":{"query":"What is AWS Lambda and how does it scale?","knowledge_base_id":"KTVKJDYLDQ","number_of_results":3}}}\n' \
  | AWS_PROFILE=your-profile AWS_REGION=us-east-1 \
    KB_INCLUSION_TAG_KEY=mcp-multirag-kb FASTMCP_LOG_LEVEL=ERROR \
    uvx awslabs.bedrock-kb-retrieval-mcp-server@latest
```

**实测输出**：

| 排名 | 来源文档 | Score | 内容摘要 |
|------|---------|-------|---------|
| 1 | serverless/doc-serverless.txt | 0.6699 | Lambda 自动扩缩、按使用付费 |
| 2 | serverless/doc-serverless.txt | 0.5059 | Lambda 并发控制、容器镜像支持 |
| 3 | networking/doc-networking.txt | 0.3899 | VPC 网络概念（不太相关） |

查询结果按相关性排序，top-1 准确匹配 Lambda 文档，score 从 0.67 降到 0.39。

### Step 5: Data Source 过滤

将同一个查询限制在特定 data source：

```bash
# 仅查询 serverless-docs (BPIRBENMXS)
... "arguments": {
  "query": "What are the best practices?",
  "knowledge_base_id": "KTVKJDYLDQ",
  "number_of_results": 5,
  "data_source_ids": ["BPIRBENMXS"]
}
```

**实测输出**：

| 排名 | 来源文档 | Score |
|------|---------|-------|
| 1 | serverless/doc-serverless.txt | 0.3612 |
| 2 | serverless/doc-serverless.txt | 0.3318 |

仅返回 serverless 数据源的结果，networking 和 bedrock 文档被完全过滤。

### Step 6: Reranking On vs Off 对比实验

这是本文最核心的对比实验。使用同一个查询 "How does Amazon Bedrock work with knowledge bases?"，分别在 Reranking 关闭和开启（Cohere 模型）时执行：

**不使用 Reranking**（默认）：

```bash
... "arguments": {
  "query": "How does Amazon Bedrock work with knowledge bases?",
  "knowledge_base_id": "KTVKJDYLDQ",
  "number_of_results": 5,
  "reranking": false
}
```

**使用 Cohere Reranking**：

```bash
... "arguments": {
  "query": "How does Amazon Bedrock work with knowledge bases?",
  "knowledge_base_id": "KTVKJDYLDQ",
  "number_of_results": 5,
  "reranking": true,
  "reranking_model_name": "COHERE"
}
```

**对比结果**：

| 排名 | 不使用 Reranking | Score | 使用 Cohere Reranking | Score |
|------|-----------------|-------|-----------------------|-------|
| 1 | bedrock/doc-bedrock.txt | 0.6047 | bedrock/doc-bedrock.txt | **0.7613** |
| 2 | serverless/doc-serverless.txt | 0.3722 | networking/doc-networking.txt | 0.0328 |
| 3 | networking/doc-networking.txt | 0.3694 | serverless/doc-serverless.txt | 0.0250 |
| 4 | networking/doc-networking.txt | 0.3619 | networking/doc-networking.txt | 0.0185 |
| 5 | serverless/doc-serverless.txt | 0.3526 | serverless/doc-serverless.txt | 0.0135 |

!!! tip "关键发现"
    Reranking 极大地拉开了相关与不相关结果之间的 score 差距：

    - **不使用 Reranking**：Score 范围 0.35 ~ 0.60，区分度低，Top-1（0.60）和 Top-5（0.35）差距仅 1.7x
    - **使用 Reranking**：Score 范围 0.01 ~ 0.76，Top-1（0.76）和 Top-5（0.01）差距 **58x**

    这意味着：开启 Reranking 后，Agent 可以更自信地使用 Top-1 结果，而不需要担心低质量结果混入。

### Step 7: 边界测试

**7a: Amazon Rerank 模型在 us-east-1**

```bash
... "reranking": true, "reranking_model_name": "AMAZON"
```

```
Error: ValidationException: The provided model identifier is invalid.
```

确认 Amazon Rerank 1.0 不支持 us-east-1。

**7b: 无效的 Knowledge Base ID**

```bash
... "knowledge_base_id": "INVALID_KB_ID"
```

```
Error: ValidationException: 2 validation errors detected:
  Value 'INVALID_KB_ID' at 'knowledgeBaseId' failed to satisfy constraint:
    Member must satisfy regular expression pattern: [0-9a-zA-Z]+
    Member must have length less than or equal to 10
```

KB ID 必须是 10 字符以内的字母数字。

**7c: 空查询字符串**

```bash
... "query": ""
```

```
Error: ValidationException: Text input is required.
```

空字符串会被 Bedrock API 拒绝。

### Step 8: 自定义 Tag Key

```bash
# 给 KB 打自定义 tag
aws bedrock-agent tag-resource \
  --resource-arn arn:aws:bedrock:us-east-1:{ACCOUNT_ID}:knowledge-base/{KB_ID} \
  --tags my-custom-tag=true \
  --region us-east-1 --profile your-profile

# 启动 MCP Server 使用自定义 tag key
KB_INCLUSION_TAG_KEY=my-custom-tag \
  uvx awslabs.bedrock-kb-retrieval-mcp-server@latest
```

使用自定义 tag key 后，只有打了 `my-custom-tag=true` 的 KB 会被发现。这对多团队环境很有用——不同团队可以用不同的 tag key 来隔离各自的 KB。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| 1 | ListKnowledgeBases 正常发现 | ✅ 通过 | 返回 2 个 tagged KB + data sources | |
| 2 | ListKnowledgeBases 无匹配 tag | ✅ 通过 | 返回空 `{}` | 优雅降级 |
| 3 | 基本查询 | ✅ 通过 | Top-1 score 0.67 | 正确匹配文档 |
| 4 | number_of_results | ✅ 通过 | 请求 10 得 5 | 受限于实际 chunk 数 |
| 5 | data_source_ids 过滤 | ✅ 通过 | 仅返回指定 source | 有效隔离 |
| 6 | Reranking On vs Off | ✅ 通过 | 区分度从 1.7x → 58x | **核心对比实验** |
| 7 | AMAZON model us-east-1 | ✅ 预期报错 | ValidationException | 官方文档确认 |
| 8 | 无效 KB ID | ✅ 预期报错 | 格式约束清晰 | |
| 9 | 空查询 | ✅ 预期报错 | Text input required | |
| 10 | 自定义 tag key | ✅ 通过 | 按自定义 tag 过滤 | 多团队隔离 |

## 踩坑记录

!!! warning "踩坑 1: KB Service Role 需要 bedrock:Rerank 权限"
    首次开启 Reranking 时收到 `AccessDeniedException`：

    ```
    User: arn:aws:sts::595842667825:assumed-role/mcp-kb-test-role/BedrockReranking-xxx
    is not authorized to perform: bedrock:Rerank
    ```

    注意不仅是**你的 IAM 用户**需要权限，**KB 的 Service Role** 也需要 `bedrock:Rerank` 和 `bedrock:InvokeModel` 权限。这在 README 中有提到但容易忽略。

    已查文档确认：[Permissions for reranking in Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-prereq.html)

!!! warning "踩坑 2: Amazon Rerank 1.0 不支持 us-east-1"
    直觉上 us-east-1 应该什么都有，但 Amazon Rerank 1.0 目前仅支持 us-west-2、ap-northeast-1、ca-central-1、eu-central-1。在 us-east-1 只能用 Cohere Rerank 3.5。

    MCP Server 中 `reranking_model_name` 参数使用 `"COHERE"` 或 `"AMAZON"`，在 us-east-1 **只能选 `"COHERE"`**。

    已查文档确认：[Supported Regions and models for reranking](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html)

!!! info "踩坑 3: KB 必须打 tag 才能被 MCP Server 发现"
    创建 KB 后如果不打 `mcp-multirag-kb=true` tag，`ListKnowledgeBases` 会返回空结果。这不是 bug，而是设计——通过 tag 机制来控制哪些 KB 对 Agent 可见。

    但如果你在生产中使用，**别忘了在 IaC（CDK/Terraform）模板中加上这个 tag**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch Serverless 集合 | ~$0.24/hr | ~2 hr | ~$0.48 |
| Bedrock Retrieve API | 按查询计 | ~20 次 | < $0.01 |
| Cohere Rerank 3.5 | 按调用计 | ~5 次 | < $0.01 |
| S3 存储 | 极少量 | 3 个小文件 | < $0.01 |
| **合计** | | | **~$0.50** |

## 清理资源

```bash
# 1. 删除 Knowledge Base（会同时删除数据源）
aws bedrock-agent delete-knowledge-base \
  --knowledge-base-id KTVKJDYLDQ \
  --region us-east-1 --profile your-profile

# 2. 删除 OpenSearch Serverless 集合
aws opensearchserverless delete-collection \
  --id 39nsqdqc4h95c3oozt0c \
  --region us-east-1 --profile your-profile

# 3. 删除 OSS 安全策略和访问策略
aws opensearchserverless delete-security-policy \
  --name mcp-kb-test-enc --type encryption \
  --region us-east-1 --profile your-profile

aws opensearchserverless delete-security-policy \
  --name mcp-kb-test-net --type network \
  --region us-east-1 --profile your-profile

aws opensearchserverless delete-access-policy \
  --name mcp-kb-test-access --type data \
  --region us-east-1 --profile your-profile

# 4. 清空并删除 S3 bucket
aws s3 rm s3://mcp-kb-test-2026-595842667825 --recursive \
  --region us-east-1 --profile your-profile
aws s3 rb s3://mcp-kb-test-2026-595842667825 \
  --region us-east-1 --profile your-profile

# 5. 删除 IAM Role
aws iam delete-role-policy \
  --role-name mcp-kb-test-role \
  --policy-name mcp-kb-test-permissions \
  --profile your-profile
aws iam delete-role \
  --role-name mcp-kb-test-role \
  --profile your-profile

# 6. 移除已有 KB 上的测试 tag
aws bedrock-agent untag-resource \
  --resource-arn arn:aws:bedrock:us-east-1:{ACCOUNT_ID}:knowledge-base/RNCL5AH6KK \
  --tag-keys mcp-multirag-kb \
  --region us-east-1 --profile your-profile
```

!!! danger "务必清理"
    OpenSearch Serverless 集合即使空闲也会持续产生费用（~$0.24/hr ≈ $5.76/天）。Lab 完成后请立即删除。

## 结论与建议

### 场景化推荐

| 场景 | 配置建议 |
|------|---------|
| 开发/调试 Agent | `BEDROCK_KB_RERANKING_ENABLED=false`，先验证基本连通性 |
| 生产环境 | `BEDROCK_KB_RERANKING_ENABLED=true`，显著提升检索精度 |
| 多团队隔离 | 使用自定义 `KB_INCLUSION_TAG_KEY`，每个团队用不同 tag |
| us-east-1 部署 | Reranking 只能用 `COHERE`，或考虑部署到 us-west-2 使用 Amazon Rerank |

### MCP Server 集成配置

在你的 MCP 客户端（Kiro、Cursor、VS Code 等）中添加：

```json
{
  "mcpServers": {
    "awslabs.bedrock-kb-retrieval-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.bedrock-kb-retrieval-mcp-server@latest"],
      "env": {
        "AWS_PROFILE": "your-profile",
        "AWS_REGION": "us-east-1",
        "FASTMCP_LOG_LEVEL": "ERROR",
        "KB_INCLUSION_TAG_KEY": "mcp-multirag-kb",
        "BEDROCK_KB_RERANKING_ENABLED": "true"
      }
    }
  }
}
```

### 核心结论

1. **Reranking 是必开项**：Cohere Reranking 将相关结果与无关结果的区分度从 1.7x 提升到 58x，生产环境强烈建议开启
2. **Tag 机制是安全边界**：通过 tag 控制 KB 对 Agent 的可见性，适合多团队/多环境管理
3. **Data Source 过滤实用**：当 KB 包含多个数据源时，Agent 可以精确指定查询范围，提高结果相关性
4. **Region 注意事项**：us-east-1 不支持 Amazon Rerank 1.0，只能用 Cohere Rerank 3.5

## 参考链接

- [GitHub: awslabs/mcp - bedrock-kb-retrieval-mcp-server](https://github.com/awslabs/mcp/tree/main/src/bedrock-kb-retrieval-mcp-server)
- [Amazon Bedrock Knowledge Bases 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [Reranking 支持的 Region 和模型](https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html)
- [Retrieve API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html)
- [MCP 协议规范](https://modelcontextprotocol.io/)
