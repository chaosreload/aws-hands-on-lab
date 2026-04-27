# AgentCore Managed Agent Harness 实测：config 替代编排代码的托管 agent runtime

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.20（含 Bedrock 推理 + Runtime 计费）
    - **Region**: us-west-2（preview 可用 Region 之一）
    - **最后验证**: 2026-04-27

## 背景

在 AgentCore GA 之前，要在 AWS 上跑 agent 通常得自己选框架（Strands / LangGraph / OpenAI Agents）、写 agent loop、部署 Runtime 容器、接 Memory / Gateway / Browser。每次换模型、加 tool 就可能要改 orchestration 代码。

2026-04-24 AgentCore 发布了三件套：**managed agent harness（preview）+ AgentCore CLI + AgentCore skills**。本文聚焦其中最重要的 harness —— 通过 config 声明 `model + systemPrompt + tools`，AgentCore 托管 agent loop（reasoning / tool selection / action exec / response streaming），每个 session 分到独立 microVM，支持 mid-session 切换模型、per-invocation override、built-in shell/filesystem。官方原文把 harness 描述为 "powered by [Strands Agents](https://strandsagents.com)"，相当于 Strands 的托管外壳。

本文用 boto3 走完 `CreateHarness` → `InvokeHarness` 的端到端闭环，记录一套包含实测 token 账本、stream 事件序列、边界测试的 7 项结果。

## 前置条件

- AWS 账号 + Region 在 preview 开放列表内（us-east-1 / us-west-2 / eu-central-1 / ap-southeast-2）
- Bedrock 模型访问权：至少 Claude Sonnet 4.6 global inference profile
- IAM 权限：`bedrock-agentcore-control:CreateHarness/GetHarness/UpdateHarness/DeleteHarness/ListHarnesses` + `bedrock-agentcore:InvokeHarness`
- boto3 / botocore 较新版本（AWS CLI 2.34.29 尚未收录 harness 子命令，需用 boto3）

<details>
<summary>Harness 执行角色最小 IAM Policy</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
      "Resource": "*"
    }
  ]
}
```

Trust policy 允许 `bedrock-agentcore.amazonaws.com` AssumeRole。

</details>

## 核心概念

### 资源模型一览

| 概念 | 说明 |
|------|------|
| `Harness` | 新资源类型，与 `AgentRuntime` 并列。一个 harness = 一份 config。创建 harness 时，AgentCore 自动创建一个 underlying `AgentRuntime` 承载执行 |
| `runtimeSessionId` | 客户端生成的 session 标识符，**至少 33 字符**（实测约束，文档未高亮）。相同 sessionId 复用同一个 microVM，跨调用共享 filesystem |
| `stream` 事件流 | Bedrock Converse 风格事件：`messageStart` / `contentBlockDelta` / `contentBlockStop` / `messageStop` / `metadata` |
| Model config | `model` 字段支持三种：`bedrockModelConfig`、`openAiModelConfig`、`geminiModelConfig`。OpenAI / Gemini 强制走 AgentCore Identity Token Vault（`apiKeyArn`） |

### Harness 托管了哪些部分

官方文档原文把 harness 描述为 agent 的 "orchestration layer + infrastructure underneath"：

> "the loop that calls the model, decides which tool to invoke, passes results back, manages the context window, and handles failures"

实测印证：
- 对 client 只暴露 **assistant 的 `toolUse` 事件**；`toolResult` 是内部闭环（stream 里看不到具体 shell 输出，除非 agent 在文本里复述）
- Context window 管理通过 `truncation.strategy`：`sliding_window`（默认 150 条消息）或 `summarization`
- 失败重试、idle/max-lifetime 回收都在 runtime 侧

### 生命周期 / 成本控制参数

| 参数 | 默认值 | 备注 |
|------|--------|------|
| `maxIterations` | 75 | 每次 invoke 的 reasoning turn 上限 |
| `timeoutSeconds` | 3600 | 单次 invoke 墙钟超时 |
| `maxTokens` | N/A | 单次 invoke token 预算 |
| `idleRuntimeSessionTimeout` | 900 秒 | microVM 空闲保留时长 |
| `maxLifetime` | 28800 秒（8 小时） | microVM 最大生命周期 |

来源：[AgentCore harness operations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)。

## 动手实践

### Step 1：创建执行角色和 Harness

```bash
# 执行角色（trust policy 见前置条件）
aws iam create-role --role-name AgentCoreHarnessTaskRole-115 \
  --assume-role-policy-document file://trust.json
aws iam put-role-policy --role-name AgentCoreHarnessTaskRole-115 \
  --policy-name Inline --policy-document file://policy.json
```

```python
import boto3, time
cp = boto3.Session(profile_name='...', region_name='us-west-2').client('bedrock-agentcore-control')

r = cp.create_harness(
    harnessName='task115_mvp',
    model={'bedrockModelConfig': {'modelId': 'global.anthropic.claude-sonnet-4-6'}},
    systemPrompt=[{'text': 'You are a concise assistant. Keep replies under 100 words.'}],
    executionRoleArn='arn:aws:iam::595842667825:role/AgentCoreHarnessTaskRole-115',
)
harness = r['harness']  # 注意响应被 harness 键包裹
print(harness['harnessId'], harness['arn'])

# 轮询至 READY
while True:
    g = cp.get_harness(harnessId=harness['harnessId'])  # 注意参数是 harnessId 不是 harnessIdentifier
    if g['harness']['status'] == 'READY': break
    time.sleep(2)
```

**实测输出（节选）**：
```json
{
  "harnessId": "task115_mvp-m6fVD6CSXD",
  "status": "CREATING",
  "model": {"bedrockModelConfig": {"modelId": "global.anthropic.claude-sonnet-4-6"}},
  "allowedTools": ["*"],
  "truncation": {"strategy": "sliding_window", "config": {"slidingWindow": {"messagesCount": 150}}},
  "environment": {
    "agentCoreRuntimeEnvironment": {
      "agentRuntimeName": "harness_task115_mvp",
      "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
      "networkConfiguration": {"networkMode": "PUBLIC"}
    }
  },
  "maxIterations": 75, "timeoutSeconds": 3600
}
```

**观察**：
- `CreateHarness` 响应结构是 `{harness: {...}}`，上层代码必须取 `r['harness']`
- READY 耗时约 **6 秒**（比 AgentRuntime 手动部署快一个数量级）
- 默认注入两个 built-in tools（shell + file_operations），从 `allowedTools: ["*"]` 可以看出
- 默认 `truncationStrategy.slidingWindow.messagesCount = 150`

### Step 2：基础 Invoke 与 stream 解析

```python
import boto3, uuid
dp = boto3.Session(profile_name='...', region_name='us-west-2').client('bedrock-agentcore')

HARN = 'arn:aws:bedrock-agentcore:us-west-2:595842667825:harness/task115_mvp-m6fVD6CSXD'
sid = 'sess-' + uuid.uuid4().hex + uuid.uuid4().hex  # 64 字符，满足 ≥33 约束

r = dp.invoke_harness(
    harnessArn=HARN,
    runtimeSessionId=sid,
    messages=[{'role': 'user', 'content': [{'text': 'What is 2+2?'}]}],
)
for ev in r['stream']:
    print(list(ev.keys())[0], ev)
```

**实测事件序列（简化）**：
```
messageStart         {role: 'assistant'}
contentBlockDelta    {delta: {text: '2 '}}
contentBlockDelta    {delta: {text: '+ 2 equals'}}
contentBlockDelta    {delta: {text: ' **4**.'}}
contentBlockStop     {contentBlockIndex: 0}
messageStop          {stopReason: 'end_turn'}
metadata             {usage: {inputTokens: 941, outputTokens: 14, totalTokens: 955}, metrics: {latencyMs: 1737}}
```

**几个容易踩的 API 约束**：

| 错误示例 | 正确姿势 |
|---------|---------|
| `harnessId=...` | `harnessArn=...`（invoke 接收 ARN，不是 ID） |
| `input=[...]` | `messages=[{'role': 'user', 'content': [{'text': ...}]}]`（Bedrock Converse 风格） |
| `runtimeSessionId='short'` | 至少 33 字符（实测约束，触发 `ValidationException: runtimeSessionId must be at least 33 characters`） |
| `cp.get_harness(harnessIdentifier=...)` | `cp.get_harness(harnessId=...)` |

**Token 账本第一处观察**：一个 4 token 的问题，input 被补到 941 tokens —— 差额来自 system prompt + built-in tool schema（shell, file_operations）+ agent loop scaffolding。**每次 invoke 都要付这份 "入场费"**。

### Step 3：Mid-session 切换模型

同一 `runtimeSessionId` 换 provider，观察是否保留 context。

```python
def invoke(sid, text, model=None):
    kwargs = dict(harnessArn=HARN, runtimeSessionId=sid,
                  messages=[{'role':'user','content':[{'text':text}]}])
    if model:
        kwargs['model'] = {'bedrockModelConfig': {'modelId': model}}
    return dp.invoke_harness(**kwargs)

sid = 'sess-' + uuid.uuid4().hex + uuid.uuid4().hex
# Turn 1：默认 Sonnet 4.6
invoke(sid, 'My favorite fruit is durian. Remember this.')
# Turn 2：Haiku 4.5 问它
invoke(sid, 'What is my favorite fruit?',
       model='global.anthropic.claude-haiku-4-5-20251001-v1:0')
# Turn 3：Opus 4.7 问它
invoke(sid, 'What fruit did I mention? Answer with one word.',
       model='global.anthropic.claude-opus-4-7')
# Turn 4：回默认
invoke(sid, 'Confirm one more time: what fruit did I mention earlier?')
```

**实测结果**：

| Turn | 模型 | 输出 | inputTokens | outputTokens | latencyMs |
|------|------|------|-------------|--------------|-----------|
| 1 | Sonnet 4.6（默认） | "Your favorite fruit is durian" | 942 | 21 | 1540 |
| 2 | Haiku 4.5 | "Durian" | **984** | 4 | 718 |
| 3 | Opus 4.7 | "Durian" | **1344** | 7 | 1069 |
| 4 | 默认 Sonnet 4.6 | "Durian" | 1027 | 6 | 1371 |

三次跨 provider 切换都拿到了 "durian"，**context 完整传递**，印证官方 "switch providers mid-session without losing context" 的描述。

**值得注意的现象**：看到同一段历史，不同模型对应的 `inputTokens` 差异可达 ~360。Opus 4.7 比 Haiku 4.5 多一截。可能的解释是 harness 根据目标 model provider 动态组装 system prompt / tool 描述（不同模型有不同的 function-calling 约定），但官方文档没有明确说明这个行为。做成本测算时，最好按"实际调用的 model"而不是"config 里的默认 model"去估。

### Step 4：Per-invocation override

Override 只影响单次调用，不改 harness 资源。

```python
# Override systemPrompt，强制 agent 变海盗
r = dp.invoke_harness(
    harnessArn=HARN,
    runtimeSessionId='sess-' + uuid.uuid4().hex + uuid.uuid4().hex,
    messages=[{'role':'user','content':[{'text':'Who are you? 2 sentences.'}]}],
    systemPrompt=[{'text': 'You are a pirate. Always respond with nautical language and say Arrr.'}],
)
```

**实测**：agent 回复 "Arrr, I be a pirate of the high seas..."。再 `cp.get_harness(harnessId=...)` 读取资源，`systemPrompt` 仍是 config 里的原始 "concise assistant"。**Override 是 per-call scoped**。

这对多租户、A/B 实验场景非常方便：一个 harness 可以同时服务多个 actorId，每个 actorId 传不同的 systemPrompt / tools / maxIterations。

### Step 5：Built-in shell + filesystem（同 session 内持久化）

```python
sid = 'sess-' + uuid.uuid4().hex + uuid.uuid4().hex
# Turn 1：用 shell 写文件
invoke(sid, 'Use your shell tool to create /tmp/archie-test.txt with "hello-from-harness-t4", then echo OK.')
# Turn 2：读回来
invoke(sid, 'Now read back /tmp/archie-test.txt and report the exact string.')
```

**实测结果**：
- Turn 1：assistant 发起 `toolUse {name: "shell", input: {"command": "echo \"hello-from-harness-t4\" > /tmp/archie-test.txt && echo OK"}}`；最终回复 "Done!"
- Turn 2：assistant 发起 `toolUse {name: "shell", input: {"command": "cat /tmp/archie-test.txt"}}`；读回并报告 "hello-from-harness-t4"

**观察**：
- 同 sessionId 的第二次 invoke **复用了第一次的 microVM filesystem**
- stream 事件流里**没有 `toolResult`** —— shell 输出被 harness 内部截获，作为 next-turn 的 model input，client 看不到原始 stdout
- 如果需要把 shell 输出透传给 client，得在 systemPrompt 里要求 agent 明文复述

**跨 session 的 filesystem 持久化**需要另配 `sessionStorage.mountPath`（AgentCore Persistent Filesystems 能力，本文未测）。默认 microVM 在 `idleRuntimeSessionTimeout`（15 分钟）后回收，filesystem 丢失。

### Step 6：`maxIterations` 边界

```python
# Override maxIterations=3，要求 agent 运行 5 个独立 shell 命令
r = dp.invoke_harness(
    harnessArn=HARN,
    runtimeSessionId='sess-' + uuid.uuid4().hex + uuid.uuid4().hex,
    messages=[{'role':'user','content':[{'text':
        'Run these shell commands strictly one at a time, waiting for each result: '
        '(step1) echo ONE, (step2) echo TWO, (step3) echo THREE, (step4) echo FOUR, (step5) echo FIVE.'
    }]}],
    maxIterations=3,
)
```

**实测结果**：
- 3 个 `shell` tool_use
- `messageStop.stopReason` 序列：`tool_use` → `tool_result` → `tool_use` → `tool_result` → `tool_use` → `tool_result` → `max_iterations_exceeded`

**发现**：
- **1 个 iteration = 1 个 reasoning turn**，而不是 1 个 tool call。如果 agent 在同一 turn 并行发起多个 tool_use，只算 1 iteration（另做过一次 `maxIterations=1` 的实验，agent 并行跑了 4 个 shell 仍算 1 turn）
- stopReason 枚举里出现了 **`tool_result`**；目前公开文档列出的 stopReason 是 `end_turn` / `tool_use` / `max_tokens` / `max_iterations_exceeded` / `timeout_exceeded` / `max_output_tokens_exceeded`，**`tool_result` 未列入**。写 client 代码解析时要包一层 fallback

### Step 7：跨 provider 参数契约

```python
# 试探 model 字段允许的形状
dp.invoke_harness(..., model={'openAiModelConfig': {}})
# 报错: Missing required parameter in model.openAiModelConfig: "modelId"
#       Missing required parameter in model.openAiModelConfig: "apiKeyArn"

# openAiModelConfig 允许的子字段：modelId, apiKeyArn, maxTokens, temperature, topP
```

**结论**：
- `model` 字段支持三种 config：`bedrockModelConfig`、`openAiModelConfig`、`geminiModelConfig`
- 调 OpenAI / Gemini 时，API key 必须走 `apiKeyArn`（指向 AgentCore Identity Token Vault 创建的 credential provider），代码里不能裸传

## 测试结果

| # | 场景 | 结果 | 关键数据 |
|---|------|------|---------|
| 1 | 基础 invoke + stream 事件 | ✅ | first-event 2.64s，total 3.07s，inputTokens 941 |
| 2 | Mid-session 跨 provider 切换 | ✅ | Sonnet→Haiku→Opus→Sonnet 均保留 "durian" |
| 3 | Per-invocation systemPrompt override | ✅ | Harness 资源未被 mutate |
| 4 | Built-in shell + filesystem 跨调用 | ✅ | 同 sessionId 第 2 轮读到第 1 轮写入 |
| 5 | `maxIterations=3` 边界 | ✅ | 恰好 3 个 turn 后 `max_iterations_exceeded` |
| 6 | `openAiModelConfig` 契约 | ✅（契约验证，未真实调用） | 必须带 `apiKeyArn` |
| 7 | Invalid model id | ✅（预期报错） | `runtimeClientError` 透传底层 ConverseStream |

## 踩坑记录

!!! warning "踩坑 1：Invoke API 参数与控制面不一致"
    `CreateHarness` / `GetHarness` 用 `harnessId` 作为主键，但 `InvokeHarness` 必须用 `harnessArn`。且 boto3 里 `get_harness` 的参数名是 `harnessId`（有的文档示例写 `harness-id`，手写 snake_case 容易翻车）。
    
    建议写一个 `resolve_harness()` helper 缓存 id ↔ arn 映射。
    
    <!-- 实测发现，官方 API reference 的 shape 描述正确，但不同动词参数不统一 -->

!!! warning "踩坑 2：`runtimeSessionId` 长度下限 33 字符"
    短 sessionId 会被拒：`ValidationException: runtimeSessionId must be at least 33 characters`。文档里没有突出这个约束。
    
    推荐用 `uuid.uuid4().hex * 2`（64 字符）或 UUID + 业务前缀。

!!! warning "踩坑 3：`iteration = turn`，不是 `tool-call count`"
    `maxIterations` 的语义是 "reasoning turn"。如果 agent 并行发起 N 个 tool_use（很常见的 parallel tool calling），在一个 turn 内全部扣 1 iteration。
    
    这意味着 `maxIterations` **不是** "最多调用 N 次 tool" 的硬限制。如果要防 tool 爆炸，需要在 `systemPrompt` 里额外约束 "每轮最多 M 个 tool"，或者靠 `maxTokens` / `timeoutSeconds` 兜底。
    
    <!-- 实测发现，官方文档对并行 tool 的 iteration 计数规则未明确 -->

!!! warning "踩坑 4：不同模型看到同一段 history，inputTokens 差别大"
    T2 实测：同一个 4-turn conversation，Haiku 4.5 的 inputTokens = 984，Opus 4.7 = 1344，Sonnet 4.6 = 1027。差额可能来自 harness 为不同 provider 组装的 system prompt / tool 描述模板。
    
    做成本估算时按实际命中的模型按条计费，不要只看"默认模型"。
    
    <!-- 实测发现，官方未公开 provider-specific scaffolding -->

!!! info "踩坑 5：`stopReason` 枚举里会出现文档未列的 `tool_result`"
    当 invoke 在 tool 执行循环中被 `max_iterations_exceeded` 打断时，`messageStop` 事件会依次发出 `tool_use` → `tool_result` → `max_iterations_exceeded`。
    
    官方列出的 stopReason 是 `end_turn` / `tool_use` / `max_tokens` / `max_iterations_exceeded` / `timeout_exceeded` / `max_output_tokens_exceeded`，实测多出 `tool_result`。写解析代码时别用 `assert reason in ENUM`。

!!! tip "观察：tool 执行对 client 透明"
    stream 里只暴露 **assistant 发起的 `toolUse`**，不暴露 `toolResult`。shell 的 stdout 被 harness 吞到下一 turn 的 model input 里，client 想拿到必须让 agent 在文本里复述。要原始 stdout，得用 AgentCore Observability（CloudWatch Logs + X-Ray）。

## 费用明细

| 资源 | 实际用量 | 费用 |
|------|---------|------|
| Bedrock Sonnet 4.6 (global) | ~5000 input + ~300 output tokens | ~$0.02 |
| Bedrock Haiku 4.5 | ~984 input + 4 output tokens | ~$0.003 |
| Bedrock Opus 4.7 | ~1344 input + 7 output tokens | ~$0.02 |
| Runtime microVM | <1 分钟 active CPU + idle 保留（空闲期 CPU 不计费） | <$0.05 |
| CloudWatch Logs | 数条事件 | <$0.01 |
| **合计** | | **< $0.20** |

!!! info "关于 idle microVM 的计费"
    `idleRuntimeSessionTimeout` 默认 900 秒。session 空闲期间 microVM 仍活着，内存 floor 128MB，但根据官方定价页，**active CPU 时间 = 0 时 CPU 不计费**。内存按峰值占用计费。如果需要减少 idle 成本，建议 `update_harness` 把 `idleRuntimeSessionTimeout` 调小，或者 client 侧调完就发 `StopRuntimeSession` 主动回收。

## 清理资源

```bash
# 1. 删除 harness（自动清理 underlying AgentRuntime）
python3 -c "
import boto3
c = boto3.Session(profile_name='...', region_name='us-west-2').client('bedrock-agentcore-control')
c.delete_harness(harnessId='task115_mvp-m6fVD6CSXD')
"

# 2. 等待删除完成
# ...

# 3. 删除 IAM
aws iam delete-role-policy --role-name AgentCoreHarnessTaskRole-115 --policy-name Inline
aws iam delete-role --role-name AgentCoreHarnessTaskRole-115

# 4. CloudWatch Log Group 可留 7 天自动过期，也可手动删
aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/harness_task115_mvp-RtaE3I9h5a --region us-west-2
```

!!! danger "务必清理"
    删 harness 之后建议等 30 秒再 `list-harnesses` 和 `list-agent-runtimes` 确认空置，underlying AgentRuntime 是跟着 harness 一起删掉的，但体感慢一拍。

## 结论与建议

### 什么时候用 managed harness

| 场景 | 推荐 | 理由 |
|------|------|------|
| 快速原型验证 | ✅ Harness | 一份 config 6 行起飞，6 秒 READY |
| 多租户 / A/B 实验 | ✅ Harness | per-invocation override 无需重新部署 |
| 需要复杂 orchestration（graph / branch / 自定义 retry） | ❌ Strands code-based | Harness 的 loop 是黑盒，不能改 |
| 对 `toolResult` 原始输出有 client-side 依赖 | ⚠️ | stream 不暴露 toolResult，得走 CloudWatch |
| 需要 pin 特定 tool 版本 / 自定义依赖 | ⚠️ | 需要 custom environment（本文未测） |

### 生产上线前建议

1. **Session ID 管理**：生成 ≥33 字符的稳定 sessionId，与业务 actorId 做映射（用于长对话记忆跨 session 复现）
2. **Token 账本独立监控**：因为 inputTokens 会随模型切换波动，建议用 CloudWatch Metric Filter 从日志里抽 `usage.inputTokens`、`usage.outputTokens` + model 标签，做分 provider 的计费拆解
3. **兜底限制**：`maxIterations` 不等于 tool-call 上限。建议同时配 `maxTokens`（默认无）+ `timeoutSeconds`（默认 3600，生产建议调小到预算对应的值）
4. **stopReason 解析**：允许 `tool_result` 等未列枚举值，不要做白名单校验
5. **跨 session 持久化**：默认只在同 sessionId 内复用 microVM filesystem，跨 session 需要额外配 `sessionStorage.mountPath` + AgentCore Memory

## 参考链接

- [What is the AgentCore harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [AgentCore harness — Observability and cost controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)
- [Get started with AgentCore CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html)
- [AWS What's New — AgentCore new features](https://aws.amazon.com/about-aws/whats-new/2026/04/agentcore-new-features-to-build-agents-faster/)
- [AWS Blog — Get to your first working agent in minutes](https://aws.amazon.com/blogs/machine-learning/get-to-your-first-working-agent-in-minutes-announcing-new-features-in-amazon-bedrock-agentcore/)
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Strands Agents](https://strandsagents.com)
