# Strands Agents TypeScript SDK 实测：与 Python SDK 的全面对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $0.50（Bedrock API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

2025 年 5 月，AWS 开源了 Strands Agents SDK —— 一个用模型驱动方式构建 AI Agent 的 Python 框架。2025 年 12 月，TypeScript 版本以 preview 形式发布，让全栈 JavaScript/TypeScript 开发者也能用同一套框架构建 Agent。

本文将实际测试 TypeScript SDK 的核心功能（Agent 创建、Tool 定义、流式响应、Structured Output、Multi-Agent 编排），并与 Python SDK 做详细对比，帮你判断哪个版本更适合你的场景。

## 前置条件

- AWS 账号（需要 Bedrock model access 权限）
- Node.js 20+（TypeScript 测试）
- Python 3.10+（Python 对比测试）
- AWS CLI v2 已配置，且已开启 Claude Sonnet 4 模型访问
- 了解基本的 Agent/LLM 概念

## 核心概念

Strands Agents 的核心理念是 **model-driven agent**：把复杂的 Agent 行为交给 LLM 自己决定，开发者只需定义 tools 和 system prompt，框架处理交互循环。

### TypeScript vs Python SDK 速览

| 维度 | Python SDK (v1.33.0, GA) | TypeScript SDK (v0.7.0, Preview) |
|------|--------------------------|----------------------------------|
| 工具定义 | `@tool` 装饰器 + docstring | `tool()` 函数 + Zod schema |
| 类型安全 | 运行时（依赖 docstring） | **编译时推断**（Zod → TypeScript） |
| Model Providers | 9+（Bedrock, Anthropic, Gemini, Ollama, Llama, OpenAI…） | 2（Bedrock, OpenAI） |
| Structured Output | 支持 | **Zod schema + 自动验证重试** |
| Multi-Agent | 支持 | Graph + Swarm 模式 |
| 运行环境 | Python runtime | Node.js + **浏览器** + AWS Lambda |
| 内置 Tools 包 | `strands-agents-tools`（丰富） | Notebook / File Editor / HTTP |
| 状态 | GA | **Preview** |

## 动手实践

### Step 1: 创建 TypeScript 项目

```bash
mkdir strands-ts-test && cd strands-ts-test
npm init -y
npm install @strands-agents/sdk zod
npm install -D typescript tsx @types/node
npx tsc --init --target es2022 --module nodenext --moduleResolution nodenext
```

### Step 2: 基础 Agent 调用

创建 `test-basic.ts`：

```typescript
import { Agent, BedrockModel } from '@strands-agents/sdk';

const model = new BedrockModel({
  region: 'us-east-1',
  // ⚠️ 必须使用 inference profile ID，不能直接用 model ID
  modelId: 'us.anthropic.claude-sonnet-4-20250514-v1:0',
  maxTokens: 1024,
});

const agent = new Agent({
  model,
  systemPrompt: 'You are a helpful assistant. Keep answers brief.',
});

const result = await agent.invoke('What is the capital of France?');
console.log('Stop reason:', result.stopReason);
// 注意：响应文本默认会 stream 到 stdout
// 最终消息通过 result.lastMessage 获取（不是 result.message）
```

运行：

```bash
AWS_PROFILE=your-profile npx tsx test-basic.ts
```

输出：
```
The capital of France is Paris.
Stop reason: endTurn
```

### Step 3: 用 Zod 定义类型安全 Tools

这是 TypeScript SDK 最大的亮点 —— 工具的输入参数通过 Zod schema 定义，编译时就能获得完整的类型推断：

```typescript
import { Agent, BedrockModel, tool } from '@strands-agents/sdk';
import { z } from 'zod';

const weatherTool = tool({
  name: 'get_weather',
  description: 'Get the current weather for a specific location.',
  inputSchema: z.object({
    location: z.string().describe('The city name'),
  }),
  callback: (input) => {
    // input.location 自动推断为 string ✅
    return `The weather in ${input.location} is 22°C and sunny.`;
  },
});

const agent = new Agent({
  model,
  tools: [weatherTool],
});

await agent.invoke('What is the weather in Tokyo?');
```

对比 Python 版本的 `@tool` 装饰器：

```python
from strands import Agent, tool

@tool
def get_weather(location: str) -> str:
    """Get the current weather for a specific location."""
    return f"The weather in {location} is 22°C and sunny."

agent = Agent(tools=[get_weather])
agent("What is the weather in Tokyo?")
```

**对比观察**：Python 用 docstring 描述工具，更简洁（3 行）；TypeScript 用 Zod schema，更严谨（~10 行），但换来了编译时类型检查。

### Step 4: Structured Output（Zod 验证 LLM 输出）

TypeScript SDK 的杀手级功能 —— 用 Zod schema 约束 LLM 的输出结构：

```typescript
import { z } from 'zod';

const PersonSchema = z.object({
  name: z.string().describe('Name of the person'),
  age: z.number().describe('Age of the person'),
  occupation: z.string().describe('Occupation of the person'),
  skills: z.array(z.string()).describe('List of skills'),
});

const agent = new Agent({
  model,
  structuredOutputSchema: PersonSchema,
});

const result = await agent.invoke(
  'John Smith is a 35-year-old cloud architect specializing in AWS, Kubernetes, and Terraform.'
);

// result.structuredOutput 完全类型安全
console.log(result.structuredOutput.name);   // "John Smith" (string)
console.log(result.structuredOutput.age);    // 35 (number)
console.log(result.structuredOutput.skills); // ["AWS", "Kubernetes", "Terraform"] (string[])
```

实测输出：
```json
{
  "name": "John Smith",
  "age": 35,
  "occupation": "cloud architect",
  "skills": ["AWS", "Kubernetes", "Terraform"]
}
```

**幕后机制**：SDK 内部注入了一个 `strands_structured_output` tool，让 LLM 通过 tool use 返回结构化数据，然后用 Zod 验证。验证失败会自动重试。

### Step 5: 流式响应

```typescript
for await (const event of agent.stream('List 3 AWS services.')) {
  console.log('[Event]', event.type);
}
```

实测事件类型（共 9 种）：
```
beforeInvocationEvent (1)  — Agent 开始
beforeModelCallEvent (1)   — 调用模型前
modelStreamUpdateEvent (14) — 流式 token
contentBlockEvent (1)       — 内容块
modelMessageEvent (1)       — 模型消息
afterModelCallEvent (1)     — 模型调用后
messageAddedEvent (2)       — 消息加入
afterInvocationEvent (1)    — Agent 结束
agentResultEvent (1)        — 最终结果
```

这些生命周期事件为自定义 hooks 提供了丰富的接入点。

### Step 6: Multi-Agent Graph 编排

TypeScript SDK 内置 Graph 模式，支持确定性的多 Agent 编排：

```typescript
import { Agent, BedrockModel, Graph } from '@strands-agents/sdk';

const researcher = new Agent({
  model,
  id: 'researcher',
  systemPrompt: 'List 3 key facts about the topic. Be concise.',
});

const writer = new Agent({
  model,
  id: 'writer',
  systemPrompt: 'Rewrite the research into a polished 2-sentence summary.',
});

const graph = new Graph({
  nodes: [researcher, writer],
  edges: [['researcher', 'writer']],
});

const result = await graph.invoke('Amazon Bedrock');
// result.type === 'multiAgentResult'
// result.results[0].nodeId === 'researcher', duration: 3625ms
// result.results[1].nodeId === 'writer'
```

## 测试结果

### 延迟对比（同一模型、同一问题）

| 测试场景 | TypeScript SDK | Python SDK | 差异 |
|----------|---------------|------------|------|
| 基础问答 | 1,510 ms | 1,786 ms | TS 快 15% |
| Tool Use | 3,523 ms | 6,064 ms | **TS 快 42%** |
| Streaming | 2,630 ms | — | — |
| Structured Output | 4,139 ms | — | — |
| Multi-Agent Graph | 5,849 ms | — | — |

> 注：延迟主要取决于 Bedrock API 响应时间，SDK 本身开销很小。单次测试数据仅供参考。

### 功能支持对比

| 功能 | Python | TypeScript | 备注 |
|------|:------:|:----------:|------|
| 基础 Agent | ✅ | ✅ | 体验一致 |
| Tool Use | ✅ | ✅ | TS 用 Zod，PY 用 decorator |
| Streaming | ✅ | ✅ | TS 事件类型更丰富 |
| Structured Output | ✅ | ✅ | TS 的 Zod 验证更强 |
| Multi-Agent | ✅ | ✅ | TS 有 Graph + Swarm |
| MCP 集成 | ✅ | ✅ | 都原生支持 |
| 浏览器运行 | ❌ | ✅ | TS 独有 |
| 热加载 Tools | ✅ | ❌ | PY 独有 |
| Model Providers | 9+ | 2 | PY 明显更多 |

## 踩坑记录

!!! warning "踩坑 1: 必须使用 Inference Profile ID"
    直接使用 `anthropic.claude-sonnet-4-20250514-v1:0` 会报 `ValidationException: Invocation of model ID ... with on-demand throughput isn't supported`。
    **解决**：使用 inference profile ID 如 `us.anthropic.claude-sonnet-4-20250514-v1:0`。
    注意：SDK README 示例中的 modelId 尚未更新为 inference profile 格式。
    **状态**：实测发现，README 示例待更新。

!!! warning "踩坑 2: result.message 不存在"
    TypeScript SDK 的结果对象属性是 `result.lastMessage`（不是 `result.message`），`result.stopReason`（不是 `result.stop_reason`）。
    Python SDK 用 snake_case（`result.stop_reason`），TypeScript 用 camelCase。

!!! warning "踩坑 3: 默认 stream 到 stdout"
    `agent.invoke()` 会自动将 LLM 响应流式打印到 console。如果要静默调用，需要配置相应的 hooks/options。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Bedrock Claude Sonnet 4 (input) | $3/1M tokens | ~10K tokens | $0.03 |
| Bedrock Claude Sonnet 4 (output) | $15/1M tokens | ~5K tokens | $0.08 |
| **合计** | | | **< $0.50** |

## 清理资源

本 Lab 不创建任何持久化 AWS 资源（仅 Bedrock API 调用），清理只需删除本地项目目录：

```bash
rm -rf strands-ts-test strands-py-test
```

!!! success "无需担心遗留费用"
    Strands Agents SDK 本身完全免费开源，费用仅来自 Bedrock API 调用（按用量计费，无预留）。

## 结论与建议

### TypeScript SDK 的优势

1. **类型安全**：Zod schema 让工具定义和 Structured Output 在编译时就有完整类型推断，减少运行时 bug
2. **浏览器兼容**：可以在前端直接运行 Agent，适合构建交互式 AI 应用
3. **全栈 TypeScript**：配合 CDK 部署，实现从前端到基础设施的统一语言栈
4. **Multi-Agent 模式成熟**：Graph + Swarm 两种编排模式开箱即用

### Python SDK 的优势

1. **生态更丰富**：9+ model providers，更多内置 tools
2. **更简洁**：`@tool` 装饰器 3 行搞定，DX 更流畅
3. **GA 状态**：生产就绪，API 更稳定
4. **热加载**：`load_tools_from_directory` 适合快速迭代

### 选型建议

| 场景 | 推荐 |
|------|------|
| 全栈 TS/JS 项目 | **TypeScript SDK** |
| 需要浏览器端 Agent | **TypeScript SDK** |
| 需要多种 model provider | **Python SDK** |
| 生产环境部署 | **Python SDK**（GA） |
| 快速原型/实验 | Python SDK（更简洁） |
| 类型安全要求高 | TypeScript SDK |

TypeScript SDK 目前处于 preview 阶段（v0.7.0），功能已相当完善，但 model provider 支持和内置 tools 还在追赶 Python 版本。如果你的技术栈是 TypeScript，现在就可以开始体验；如果追求稳定性和生态丰富度，Python SDK 是更安全的选择。

## 参考链接

- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/typescript-strands-agents-preview/)
- [TypeScript SDK GitHub](https://github.com/strands-agents/sdk-typescript)
- [Python SDK GitHub](https://github.com/strands-agents/sdk-python)
- [Strands Agents 官网](https://strandsagents.com/)
