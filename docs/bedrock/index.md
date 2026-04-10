# Amazon Bedrock

Amazon Bedrock 产品功能指南与动手实测。

## 📖 Feature Guides — 功能决策指南

> **定位**：帮你回答"这个功能该不该用、怎么用、有什么坑"。覆盖完整功能全貌，按需查阅。
>
> 和 What's New 的区别：What's New 是增量（每次发布写一篇），Feature Guide 是全貌（一个功能一篇，持续更新）。

*即将推出：Guardrails、Inference Profiles、Knowledge Bases...*

---

## 🆕 What's New 实测

基于 AWS What's New 发布的功能验证文章。每篇聚焦一次发布的增量变化和实测结果。

### Bedrock 核心功能

| 文章 | 关键词 |
|------|--------|
| [Responses API 实战](bedrock-responses-api.md) | OpenAI 兼容、Converse 替代 |
| [Server-Side Custom Tools](bedrock-server-side-custom-tools.md) | Lambda、MCP、服务端工具 |
| [Citations API](bedrock-citations-api-pdf-claude.md) | 溯源、PDF、引用 |
| [Count Tokens API](bedrock-count-tokens-api.md) | Token 预估、成本 |
| [IAM 成本分摊](bedrock-iam-cost-allocation.md) | 按用户追踪费用 |
| [Prompt Caching 1h TTL](../ai-ml/bedrock-prompt-caching-1h-ttl.md) | Claude Code、费用对比 |
| [Model Distillation](../ai-ml/bedrock-model-distillation.md) | 蒸馏、数据准备 |
| [Prompt Optimization](../ai-ml/bedrock-prompt-optimization.md) | 一键优化 Prompt |
| [Intelligent Prompt Routing](../ai-ml/bedrock-intelligent-prompt-routing.md) | 自动路由、省成本 |
| [API Keys](../ai-ml/bedrock-api-keys.md) | 免 IAM 调用 |
| [TTFT + Quota 可观测性](../ai-ml/bedrock-ttft-quota-observability.md) | 延迟监控、配额 |
| [RAG Evaluation](../ai-ml/bedrock-rag-evaluation-ga.md) | LLM-as-a-Judge |
| [Model Evaluation](../ai-ml/bedrock-llm-judge-evaluation.md) | 模型质量评估 |
| [Multi-Agent 协作](../ai-ml/bedrock-multi-agent-collaboration.md) | Supervisor、编排 |
| [Agents CloudWatch 指标](../ai-ml/bedrock-agents-cloudwatch-metrics.md) | 运行时监控 |

### Knowledge Bases

| 文章 | 关键词 |
|------|--------|
| [GraphRAG 实战](../ai-ml/bedrock-knowledge-bases-graphrag.md) | 图增强、跨文档推理 |
| [多模态检索](../ai-ml/multimodal-retrieval-kb.md) | 图片、音频、视频 RAG |

### Data Automation

| 文章 | 关键词 |
|------|--------|
| [Data Automation 实战](../ai-ml/bedrock-data-automation-ga.md) | 文档解析、多模态 |
| [Blueprint 指令优化](../ai-ml/bda-blueprint-instruction-optimization.md) | 样本数据、提取准确率 |

### AgentCore

| 文章 | 关键词 |
|------|--------|
| [AgentCore GA 全景](../ai-ml/bedrock-agentcore-ga-overview.md) | 从零到生产 |
| [Policy（Cedar）](../ai-ml/bedrock-agentcore-policy-ga.md) | 工具调用控制 |
| [Runtime Shell Command](../ai-ml/agentcore-runtime-shell-command.md) | 命令执行 |
| [Session Storage](../ai-ml/agentcore-persistent-filesystems.md) | 持久化文件系统 |
| [S3 Files 集成](../ai-ml/agentcore-s3files-agent-storage.md) | 文件存储方案对比 |
| [Stateful MCP Server](../ai-ml/agentcore-stateful-mcp.md) | Elicitation、Sampling |
| [WebRTC 语音 Agent](../ai-ml/agentcore-webrtc-streaming.md) | KVS TURN、实时语音 |
| [Memory Streaming](../ai-ml/agentcore-memory-streaming-ltm.md) | 长期记忆 |
| [Browser Profiles](../ai-ml/agentcore-browser-profiles.md) | 跨 Session 认证 |
| [AG-UI 协议](../ai-ml/agentcore-runtime-ag-ui.md) | Agent-Frontend 交互 |
| [Direct Code Deploy](../ai-ml/bedrock-agentcore-code-deploy.md) | 快速部署 |
| [Browser Proxy](../ai-ml/agentcore-browser-proxy.md) | 出口 IP、流量路由 |
| [Browser Bot Auth](../ai-ml/agentcore-browser-web-bot-auth.md) | 加密签名 |
| [MCP Server 开发](../ai-ml/bedrock-agentcore-mcp-server.md) | 从安装到部署 |
| [VPC + PrivateLink + CFN](agentcore-vpc-privatelink-cfn-tagging.md) | 企业级部署 |
| [Browser OS-Level Actions](agentcore-browser-os-actions.md) | 突破 CDP 限制 |
