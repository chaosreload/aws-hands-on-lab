# Bedrock Prompt Caching 1 小时 TTL 实测：Claude Code 场景下费用对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟（不含边界测试等待时间）
    - **预估费用**: < $0.20
    - **Region**: us-west-2
    - **最后验证**: 2026-03-27

## 背景

如果你用过 Claude Code 或其他 AI 编程助手，一定体验过这种模式：每次对话都要把系统 prompt、工具定义、代码上下文一起发过去，而这些前缀在多轮对话中几乎不变。这意味着你在为同样的内容**反复付费**。

Amazon Bedrock 的 Prompt Caching 可以缓存这些重复前缀，后续请求直接读取缓存，成本降低 90%。但之前只有 5 分钟 TTL — 如果你思考一个问题超过 5 分钟，缓存就过期了，又要重新写入。

**2026 年 1 月**，AWS 发布了 **1 小时 TTL** 选项，支持 Claude Sonnet 4.5、Opus 4.5 和 Haiku 4.5。本文实测对比三种策略的费用差异：无缓存、5 分钟缓存、1 小时缓存。

## 前置条件

- AWS 账号，已开通 Bedrock Claude Sonnet 4.5 模型访问权限
- AWS CLI v2 已配置
- Python 3.8+，安装 `boto3` 和 `requests`
- 了解 Bedrock Converse API 基本用法

## 核心概念

### Prompt Caching 工作原理

在你的 prompt 中标记 **cache checkpoint**，Bedrock 会缓存从开头到 checkpoint 之间的内容。后续请求如果前缀完全匹配，直接从缓存读取。

### 5 分钟 vs 1 小时 TTL

| 对比项 | 5 分钟 TTL（默认） | 1 小时 TTL（新） |
|--------|-------------------|-----------------|
| Cache Write 价格 | $3.75/M tokens (Sonnet 4.5) | $6.00/M tokens (**贵 60%**) |
| Cache Read 价格 | $0.30/M tokens | $0.30/M tokens（**相同**） |
| 缓存持续时间 | 5 分钟（每次 hit 重置） | 1 小时（每次 hit 重置） |
| 适用场景 | 高频连续对话（< 5min 间隔） | Agentic workflow、低频长对话 |
| 支持模型 | 所有 Claude 模型 | Sonnet 4.5, Opus 4.5, Haiku 4.5 |

### 关键定价（Claude Sonnet 4.5, us-west-2）

| Token 类型 | 价格 (per 1M tokens) |
|-----------|---------------------|
| 普通 Input | $3.00 |
| 5m Cache Write | $3.75 |
| 1h Cache Write | $6.00 |
| Cache Read | $0.30 |
| Output | $15.00 |

**核心洞察**：Cache Read 比普通 Input 便宜 **90%**。只要缓存能命中，就是巨大的节省。

## 动手实践

### 场景设计：模拟 Claude Code 编程对话

我们构造一个典型的 Claude Code prompt 结构：

- **System prompt**（~600 tokens）：角色定义、行为规则、安全约束
- **代码上下文**（~1,500 tokens）：当前项目的 Python 文件
- **工具定义**（~2,000 tokens）：15 个工具（Bash、ReadFile、WriteFile、Edit、Grep 等）
- **总 static prefix**：~4,100 tokens

然后进行 5 轮编程对话，对比三种策略的费用。

### Step 1: 准备 API 调用

由于 Claude Sonnet 4.5 需要使用 **inference profile**（不支持直接用 model ID 调 on-demand），我们先确认可用的 profile：

```bash
aws bedrock list-inference-profiles \
  --region us-west-2 \
  --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `sonnet-4-5`)].inferenceProfileId' \
  --output text
# 输出: us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

### Step 2: Converse API 中使用 Cache Point

**无缓存**：直接发送 system + messages + tools，不设 cache point。

**5 分钟缓存**：在 system 内容末尾添加 cache checkpoint：

```json
{
  "system": [
    {"text": "系统 prompt 内容..."},
    {"text": "代码上下文..."},
    {"cachePoint": {"type": "default", "ttl": "5m"}}
  ],
  "messages": [...]
}
```

**1 小时缓存**：只需把 `ttl` 改为 `"1h"`：

```json
{
  "system": [
    {"text": "系统 prompt 内容..."},
    {"text": "代码上下文..."},
    {"cachePoint": {"type": "default", "ttl": "1h"}}
  ],
  "messages": [...]
}
```

### Step 3: 执行 5 轮多轮对话

每组测试发送 5 轮编程对话（间隔 2 秒），模拟快速迭代编码：

1. "分析 CostAnalyzer 类，建议添加季节性模式检测"
2. "实现季节性调整，处理历史数据不足的情况"
3. "为季节性检测添加测试用例"
4. "修复跨年边界的月份分组 bug"
5. "添加计算结果缓存机制"

## 测试结果

### 主测试：5 轮快速对话费用对比

#### Group A: 无 Prompt Cache

| Turn | Input Tokens | Cache Read | Cache Write | Cost |
|------|-------------|------------|-------------|------|
| 1 | 4,473 | 0 | 0 | $0.0182 |
| 2 | 4,541 | 0 | 0 | $0.0182 |
| 3 | 4,586 | 0 | 0 | $0.0171 |
| 4 | 4,631 | 0 | 0 | $0.0180 |
| 5 | 4,672 | 0 | 0 | $0.0181 |
| **合计** | **22,903** | **0** | **0** | **$0.0897** |

#### Group B: 5 分钟 TTL Cache

| Turn | Input Tokens | Cache Read | Cache Write | Cost |
|------|-------------|------------|-------------|------|
| 1 | 377 | 0 | 4,096 | $0.0167 |
| 2 | 445 | 4,096 | 0 | $0.0058 |
| 3 | 490 | 4,096 | 0 | $0.0044 |
| 4 | 535 | 4,096 | 0 | $0.0054 |
| 5 | 576 | 4,096 | 0 | $0.0053 |
| **合计** | **2,423** | **16,384** | **4,096** | **$0.0376** |

#### Group C: 1 小时 TTL Cache

| Turn | Input Tokens | Cache Read | Cache Write | Cost |
|------|-------------|------------|-------------|------|
| 1 | 377 | 4,096 | 0 | $0.0060 |
| 2 | 445 | 4,096 | 0 | $0.0060 |
| 3 | 490 | 4,096 | 0 | $0.0045 |
| 4 | 535 | 4,096 | 0 | $0.0089 |
| 5 | 576 | 4,096 | 0 | $0.0056 |
| **合计** | **2,423** | **20,480** | **0** | **$0.0310** |

!!! note "Group C 为何没有 Cache Write？"
    Group C 的第 1 轮直接命中了 Group B 写入的缓存 — Bedrock 的 prompt cache 是跨请求共享的，相同前缀的缓存无论用哪种 TTL 写入，后续请求都能读到。这证明了缓存的共享机制。

#### 费用对比总结

| 策略 | 5 轮总费用 | vs 无缓存节省 | 节省百分比 |
|------|----------|-------------|-----------|
| ❌ 无 Cache | $0.0897 | — | — |
| ⏱️ 5min Cache | $0.0376 | $0.0521 | **58.1%** |
| 🕐 1h Cache | $0.0310 | $0.0587 | **65.4%** |

### 边界测试：6 分钟间隔后的真正差异

快速连续对话中，5min 和 1h cache 表现相近。**真正的差异在间隔超过 5 分钟时**：

| 场景 | 6min 后第 2 轮费用 | 缓存状态 |
|------|-------------------|---------|
| 5min Cache | $0.0123 | ❌ **MISS** → 重新 Write |
| 1h Cache | $0.0026 | ✅ **HIT** → 直接读取 |
| **差异** | **$0.0098** | **1h 节省 79%** |

!!! success "关键发现"
    当对话间隔超过 5 分钟时（比如你在思考、测试代码、或去倒杯咖啡），5 分钟缓存会过期。此时 1 小时 TTL 的优势就体现出来了 — 每次避免一次 cache re-write，在 ~2K 缓存 token 的场景下就能省 $0.01。

    在实际 Claude Code 使用中，system prompt + tools 可能有 **10-20K tokens**，节省效果会更加显著。

### 费用模型分析：何时选择 1h TTL？

1h cache write 比 5m 贵 60%（$6.00 vs $3.75/M），所以不是无脑选 1h 就好。关键在于：**1h write 的额外成本能否被避免的 re-write 抵消？**

假设 static prefix 为 **N** tokens：

- 5m write 成本：N × $3.75/M
- 1h write 成本：N × $6.00/M
- 额外成本 Δ：N × $2.25/M
- 每次避免 re-write 节省：N × ($3.75 - $0.30)/M = N × $3.45/M

**Break-even**：只需 **1 次** cache hit 在 5-60min 间隔内发生，1h TTL 就比 5m TTL 划算（因为 $3.45 > $2.25）。

| Static Prefix | 1h Write 额外成本 | 避免 1 次 Re-write 节省 | 结论 |
|--------------|-------------------|----------------------|------|
| 5K tokens | $0.01125 | $0.01725 | ✅ 1 次就回本 |
| 10K tokens | $0.02250 | $0.03450 | ✅ 1 次就回本 |
| 20K tokens | $0.04500 | $0.06900 | ✅ 1 次就回本 |

## 踩坑记录

!!! warning "Model ID vs Inference Profile"
    Claude Sonnet 4.5 不能直接用 model ID（`anthropic.claude-sonnet-4-5-20250929-v1:0`）调用 on-demand inference，必须使用 inference profile ID（`us.anthropic.claude-sonnet-4-5-20250929-v1:0`）。否则会报 `ValidationException: Invocation with on-demand throughput isn't supported`。
    
    **已查文档确认**：这是 Bedrock 对较新模型的设计，需要通过 inference profile 路由。

!!! warning "boto3 TTL 字段支持"
    截至 boto3 1.40.61，`CachePointBlock` shape 只包含 `type` 字段，不支持 `ttl`。调用时会触发参数验证错误：`Unknown parameter in cachePoint: "ttl"`。
    
    **解决方案**：使用 raw HTTP 请求 + SigV4 签名直接调用 Converse API endpoint（`/model/{modelId}/converse`），绕过 boto3 参数验证。
    
    **实测发现，官方未记录**：预计后续 boto3 版本会添加 TTL 支持。

!!! warning "Cache 跨请求共享"
    不同 TTL 的请求共享同一前缀缓存。如果用 5m TTL 写入了缓存，后续用 1h TTL 的请求可以直接读取该缓存。
    
    **实测发现**：这意味着在混合使用场景中，缓存行为可能与预期不同。

## 费用明细

| 测试项 | 请求数 | 费用 |
|--------|-------|------|
| T1: Group A 无缓存 (5轮) | 5 | $0.0897 |
| T2: Group B 5min缓存 (5轮) | 5 | $0.0376 |
| T3: Group C 1h缓存 (5轮) | 5 | $0.0310 |
| T4a: 1h 冷启动 (2轮) | 2 | $0.0144 |
| T4b: 5min 超时测试 (2轮) | 2 | $0.0219 |
| T5: 1h 存活测试 (2轮) | 2 | $0.0153 |
| **合计** | **21** | **< $0.21** |

## 清理资源

本 Lab 仅使用 Bedrock API 调用，**无需创建或清理任何 AWS 资源**。

## 结论与建议

### 三种策略选择指南

| 场景 | 推荐策略 | 理由 |
|------|---------|------|
| 高频连续对话（< 5min 间隔） | 5min TTL | Write 更便宜，hit 率高 |
| Agentic workflow（工具执行 > 5min） | **1h TTL** | 避免中间步骤导致 cache 过期 |
| 交互式编码（间歇性暂停） | **1h TTL** | 思考、测试、调试间隔常 > 5min |
| Batch processing | **1h TTL** | 跨批次保持缓存 |
| 预算极敏感、连续使用 | 5min TTL | Write 成本更低 |

### Claude Code 场景建议

对于典型的 Claude Code 使用模式：

1. **System prompt + Tools 使用 1h TTL** — 这部分最大（10-20K tokens）且完全不变，用 1h 缓存可以避免思考间隙导致的 re-write
2. **对话历史可以用 5m TTL** — 变化频繁，5min 通常够用
3. **混合使用时 1h 必须在前** — Bedrock 要求长 TTL 的 cache entry 出现在短 TTL 之前

### 总结

Prompt Caching 1h TTL 是 Bedrock 为 **真实 AI 编程工作流** 量身定制的优化。在 Claude Code 场景中，它可以：

- 快速对话中节省 **58-65%** 的输入费用
- 在对话间隔 > 5min 时额外节省 **79%**（vs 5min cache miss）
- 只需 **1 次** 5-60min 间隔的 cache hit 就能回本 1h write 的额外成本

## 参考链接

- [Amazon Bedrock Prompt Caching 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [What's New: 1-hour duration prompt caching](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-bedrock-one-hour-duration-prompt-caching/)
- [Anthropic Prompt Caching 文档](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
