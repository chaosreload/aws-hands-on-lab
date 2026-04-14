---
tags:
  - AgentCore
  - MCP
  - What's New
---

# Amazon Bedrock AgentCore MCP Server 实战：从安装到部署的完整开发体验

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $2-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

2025 年 10 月，AWS 发布了 Amazon Bedrock AgentCore 的开源 MCP Server。这个 MCP Server 旨在让开发者通过 Agentic IDE（如 Kiro、Claude Code、Cursor、Amazon Q CLI）以自然语言方式快速开发、转换和部署 AgentCore 兼容的 AI Agent。

**核心问题**：开发和部署一个 AI Agent 到 AgentCore Runtime 需要了解 SDK 用法、IAM 配置、部署流程、可观测性设置等多个环节。MCP Server 的目标是通过给 IDE 的 AI 助手提供 AgentCore 上下文，大幅降低这个学习曲线。

本文将完整验证：**从零开始，使用 AgentCore CLI + MCP Server 创建、本地测试、云端部署一个 AI Agent 的全过程**，并深入分析 MCP Server 的实际能力边界。

## 前置条件

- AWS 账号（需要 Bedrock AgentCore、IAM、S3 相关权限）
- AWS CLI v2 已配置
- Python ≥ 3.10
- `uv` 包管理器（用于 `uvx` 安装 MCP Server）

## 核心概念

### AgentCore MCP Server 是什么

AgentCore MCP Server 是一个遵循 [Model Context Protocol](https://modelcontextprotocol.io/) 标准的开源服务器，运行在开发者本地，为 IDE 的 AI 助手提供 AgentCore 相关的上下文和操作能力。

**关键发现**：虽然 What's New 公告主要强调了文档搜索能力（`search_agentcore_docs` 和 `fetch_agentcore_doc`），但 **v1.26.0 的实际能力远超公告描述**：

| 工具类别 | 工具数量 | 能力 |
|---------|---------|------|
| 文档搜索 | 2 | 搜索和获取 AgentCore 文档 |
| Browser | 25 | 云端浏览器自动化（navigate, click, type, snapshot 等） |
| Code Interpreter | 9 | 沙箱代码执行、文件上传下载 |
| Runtime/Memory/Gateway | 多个 | 完整的 AgentCore 服务操作 |

它已经从一个"文档辅助工具"进化为**完整的 AgentCore 开发平台 MCP 接口**。

### AgentCore CLI：核心执行工具

AgentCore CLI（`bedrock-agentcore-starter-toolkit`）是实际执行操作的命令行工具：

| 命令 | 作用 |
|------|------|
| `agentcore create` | 创建 Agent 项目（支持 Strands、LangGraph、CrewAI、OpenAI Agents SDK、Google ADK、AutoGen） |
| `agentcore dev` | 启动本地开发服务器（含热重载） |
| `agentcore deploy` | 部署到 AgentCore Runtime（Direct Code Deploy 或容器部署） |
| `agentcore invoke` | 调用 Agent（本地 `--dev` 或远程） |
| `agentcore status` | 查看部署状态 |
| `agentcore destroy` | 销毁资源 |

### AgentCore Runtime 关键特性

- **Session 隔离**：每个用户 session 运行在独立 microVM 中
- **消费计费**：按实际 CPU/内存使用计费，I/O 等待期间不收 CPU 费用
- **最长 8 小时**执行时间
- **15 分钟**空闲超时后自动终止 session

## 动手实践

### Step 1: 安装 AgentCore CLI 和 MCP Server

```bash
# 安装 AgentCore CLI 和 SDK
pip install bedrock-agentcore-starter-toolkit bedrock-agentcore

# 验证 CLI 可用
agentcore --help

# 验证 MCP Server 可用（测试启动后 Ctrl+C 退出）
uvx awslabs.amazon-bedrock-agentcore-mcp-server@latest
```

MCP Server 启动后会输出类似日志：

```
INFO | Browser tools registered (25 tools)
INFO | Code interpreter tools registered (9 tools)
```

### Step 2: 创建 Agent 项目

```bash
# 创建项目目录
mkdir agentcore-test && cd agentcore-test

# 创建 Agent 项目（Strands 框架 + Bedrock 模型）
agentcore create \
  --project-name mytestagent \
  --template basic \
  --agent-framework Strands \
  --model-provider Bedrock \
  --non-interactive \
  --no-venv
```

!!! warning "项目名限制"
    项目名只能使用字母和数字（不支持 `-` 或 `_`），最长 36 个字符。这一限制在官方文档中未明确说明，实测发现。

生成的项目结构：

```
mytestagent/
├── .bedrock_agentcore.yaml    # AgentCore 配置
├── pyproject.toml              # Python 项目配置
├── src/
│   ├── main.py                 # Agent 主入口
│   ├── model/load.py           # 模型加载
│   └── mcp_client/client.py    # MCP 客户端
└── test/
```

关键代码 `src/main.py` 自动包含：

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke(payload, context):
    # Agent 逻辑
    ...

if __name__ == "__main__":
    app.run()
```

### Step 3: 本地开发测试

```bash
cd mytestagent

# 启动本地开发服务器（Terminal 1）
agentcore dev
```

dev server 会自动创建虚拟环境并安装 146 个依赖，启动在 `http://localhost:8080`。

```bash
# 测试调用（Terminal 2）
agentcore invoke --dev 'What is 2+3? Use the add_numbers tool.'
```

预期输出：

```
✓ Response from dev server:
The sum of 2 + 3 is **5**.
```

### Step 4: 部署到 AgentCore Runtime

```bash
# 部署到云端（无需 Docker）
agentcore deploy
```

部署过程自动完成：

1. **创建 IAM 执行角色**（自动命名如 `AmazonBedrockAgentCoreSDKRuntime-us-east-1-xxxxx`）
2. **打包源代码**（Direct Code Deploy，约 51.55 MB zip 包）
3. **上传到 S3**（自动创建 bucket）
4. **部署到 AgentCore Runtime**
5. **配置 CloudWatch + X-Ray 可观测性**

整个过程约 2 分钟，零配置。

```
╭─────────────── Deployment Success ───────────────╮
│ Agent ARN:                                        │
│ arn:aws:bedrock-agentcore:us-east-1:XXXX:runtime/ │
│ mytestagent_Agent-XXXXX                           │
│ Deployment Type: Direct Code Deploy               │
╰───────────────────────────────────────────────────╯
```

### Step 5: 远程调用测试

```bash
# 调用已部署的 Agent
agentcore invoke '{"prompt": "What is 2+3? Use the add_numbers tool."}'
```

### Step 6: 配置 MCP Server（可选）

如果你使用 Claude Code 或 Q CLI，将以下配置添加到对应的 `mcp.json`：

```json
{
  "mcpServers": {
    "bedrock-agentcore-mcp-server": {
      "command": "uvx",
      "args": ["awslabs.amazon-bedrock-agentcore-mcp-server@latest"],
      "env": {
        "FASTMCP_LOG_LEVEL": "ERROR"
      }
    }
  }
}
```

配置位置：

| IDE | 配置文件路径 |
|-----|------------|
| Claude Code | `~/.claude/mcp.json` |
| Amazon Q CLI | `~/.aws/amazonq/mcp.json` |
| Kiro | `.kiro/settings/mcp.json` |
| Cursor | `.cursor/mcp.json` |

## 测试结果

### 远程调用延迟（5 次采样，warm invocation）

| 调用 # | Prompt | 端到端延迟 |
|--------|--------|-----------|
| 1 | "What is 10+5?" | 5.778s |
| 2 | "What is 20+10?" | 5.517s |
| 3 | "What is 30+15?" | 5.236s |
| 4 | "What is 40+20?" | 5.452s |
| 5 | "What is 50+25?" | 5.945s |
| **统计** | | **平均 5.586s, p50=5.517s** |

延迟包含：CLI 启动 → HTTP 请求 → Bedrock ConverseStream → tool 调用 → 响应流。所有调用在同一 session 内执行（warm），Agent 正确调用了 `add_numbers` 工具。

### 边界测试：空 prompt

```bash
agentcore invoke '{"prompt": ""}'
```

返回清晰的错误：

```json
{
  "error": "ValidationException",
  "message": "The text field in the ContentBlock object at messages.0.content.0 is blank."
}
```

错误来自 Bedrock ConverseStream API 而非 AgentCore Runtime，说明 Runtime 会将 prompt 直接透传给模型 API。

### MCP Server 工具能力总览

| 类别 | 工具数 | 示例工具 |
|------|--------|---------|
| 文档搜索 | 2 | search_agentcore_docs, fetch_agentcore_doc |
| Browser 自动化 | 25 | start_browser_session, browser_navigate, browser_click, browser_type, browser_snapshot |
| Code Interpreter | 9 | start_code_interpreter_session, execute_code, execute_command, install_packages |
| 其他 | 多个 | Runtime, Memory, Gateway, Identity 操作 |
| **总计** | **36+** | |

## 踩坑记录

!!! warning "项目名只能用字母数字"
    `agentcore create --project-name my-agent` 会报错：`Invalid value: Project must only contain alphanumeric characters (no '-' or '_') up to 36 chars.` 
    
    **状态**: 实测发现，官方文档未记录。

!!! warning "MCP Server 能力远超公告描述"
    What's New 公告和博客主要介绍 MCP Server 的文档搜索能力，但 v1.26.0 已包含 Browser 自动化（25 个工具）、Code Interpreter（9 个工具）等完整功能集。实际使用时注意评估这些额外能力。

!!! warning "dev server 自动创建虚拟环境"
    即使 `agentcore create` 时使用 `--no-venv`，`agentcore dev` 仍会自动创建 `.venv` 并安装依赖（使用 CPython 3.13.12）。这是预期行为——dev server 需要独立的运行环境。

## 费用明细

| 资源 | 计费方式 | 预估费用 |
|------|---------|---------|
| AgentCore Runtime (CPU) | 按实际 CPU 秒计费 | ~$0.50 |
| AgentCore Runtime (Memory) | 按峰值内存秒计费 | ~$0.20 |
| Bedrock Converse (Claude Sonnet 4.5) | 按 token 计费 | ~$1-2 |
| S3 存储 (deployment zip) | S3 Standard | < $0.01 |
| CloudWatch Logs | 标准日志计费 | < $0.10 |
| **合计** | | **~$2-3** |

## 清理资源

```bash
# 1. 销毁 AgentCore Runtime Agent
cd mytestagent
agentcore destroy

# 2. 验证资源清理
agentcore status
# 预期输出：Agent not found 或类似

# 3. 手动检查残留资源
# IAM Role
aws iam list-roles --query "Roles[?contains(RoleName, 'AgentCoreSDKRuntime')]" \
  --region us-east-1 --profile weichaol-testenv2-awswhatsnewtest

# S3 bucket
aws s3 ls --profile weichaol-testenv2-awswhatsnewtest | grep agentcore

# CloudWatch Log Groups
aws logs describe-log-groups \
  --log-group-name-prefix "/aws/bedrock-agentcore" \
  --region us-east-1 --profile weichaol-testenv2-awswhatsnewtest
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。AgentCore Runtime 按使用计费，session 空闲 15 分钟后自动终止，但 IAM Role 和 S3 bucket 会持续存在。

## 结论与建议

### AgentCore MCP Server 的价值

1. **降低入门门槛**：通过 MCP 协议为 IDE AI 助手提供 AgentCore 上下文，新手可以用自然语言完成复杂的 agent 开发流程
2. **能力远超预期**：不仅是文档搜索，而是包含 Browser、Code Interpreter、Runtime 操作的完整开发平台接口
3. **分层架构设计**：5 层 MCP 架构（IDE → AWS 文档 → 框架文档 → SDK 文档 → Steering files）提供递进的上下文深度

### AgentCore CLI 的亮点

1. **零配置部署**：`agentcore deploy` 自动处理 IAM、S3、CloudWatch、X-Ray，开发者无需手动配置基础设施
2. **Direct Code Deploy**：无需 Docker，直接 zip 打包上传，适合快速迭代
3. **多框架支持**：Strands、LangGraph、CrewAI、OpenAI Agents SDK、Google ADK、AutoGen

### 适用场景

| 场景 | 推荐度 | 原因 |
|------|--------|------|
| 快速原型开发 | ⭐⭐⭐⭐⭐ | CLI + MCP Server 大幅降低上手成本 |
| 生产环境部署 | ⭐⭐⭐⭐ | Runtime 提供 session 隔离、可观测性、消费计费 |
| 多 Agent 协作 | ⭐⭐⭐⭐ | 支持 MCP 和 A2A 协议 |
| 需要精细控制的场景 | ⭐⭐⭐ | 建议结合容器部署方式获得更多控制 |

### 生产环境建议

- 使用 `--template production` 创建项目，包含 IaC（CDK/Terraform）配置
- 配置 Inbound Auth（OAuth 2.0）保护 Agent 端点
- 启用 AgentCore Memory 实现跨 session 上下文保持
- 通过 AgentCore Gateway 集成外部工具和 API

## 参考链接

- [What's New: Open Source MCP Server for Amazon Bedrock AgentCore](https://aws.amazon.com/about-aws/whats-new/2025/10/open-source-mcp-server-amazon-bedrock-agentcore/)
- [AWS Blog: Accelerate development with the AgentCore MCP Server](https://aws.amazon.com/blogs/machine-learning/accelerate-development-with-the-amazon-bedrock-agentcore-mcpserver/)
- [AgentCore MCP Server GitHub](https://github.com/awslabs/mcp/tree/main/src/amazon-bedrock-agentcore-mcp-server)
- [AgentCore Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)
- [AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
