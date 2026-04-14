---
tags:
  - Bedrock
  - Agent
  - What's New
---

# Amazon Bedrock 多 Agent 协作实战：Supervisor + Collaborator 编排模式深度测试

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $5-10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

当单个 AI Agent 面对复杂的多步骤任务时，往往力不从心——它需要同时扮演研究者、分析师、规划师多个角色。Amazon Bedrock 的 **Multi-Agent Collaboration** 功能提供了一种解决方案：通过 **Supervisor + Collaborator** 层级模型，让多个专业 Agent 协同工作。

2025 年 3 月，AWS 宣布该功能正式 GA，新增 Inline Agents、Payload Referencing、CFN/CDK 支持以及 Agent 监控与可观测性。

本文通过搭建一个「旅行规划多 Agent 团队」，实测 Supervisor 路由准确性、relay-conversation-history 对多轮对话的影响，以及单 Agent vs 多 Agent 的性能差异。

## 前置条件

- AWS 账号（需要 `bedrock:*`、`iam:*` 权限）
- AWS CLI v2 已配置
- Python 3 + boto3（用于调用 Agent）

## 核心概念

### 架构模型

```
用户请求 → Supervisor Agent → 制定计划 → 分发给 Collaborator Agents
                                              ↓
                                    Collaborator A (目的地专家)
                                    Collaborator B (预算规划师)
                                              ↓
                              Supervisor 汇总 → 返回给用户
```

### 关键特性对比

| 特性 | 单 Agent | 多 Agent (Supervisor) |
|------|----------|----------------------|
| 任务分解 | 全部自己做 | 自动拆分分发 |
| 专业化 | 通才 | 每个 Agent 专精一个领域 |
| 对话历史 | 自动保持 | 通过 relay-conversation-history 控制 |
| 延迟 | 低（1 次 LLM 调用）| 高（多次 LLM 调用）|
| 可扩展性 | 指令越长效果越差 | 新增 Collaborator 即可扩展 |

### 关键限制

- Supervisor 必须先 save 才能关联 Collaborator
- Collaborator 需要通过 Agent Alias 关联（不是 Agent ID）
- 新模型必须使用 **Inference Profile**（如 `us.anthropic.claude-sonnet-4-20250514-v1:0`），直接用 Model ID 会报错

## 动手实践

### Step 1: 创建 IAM Role

```bash
# 创建信任策略
cat > /tmp/bedrock-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "<YOUR_ACCOUNT_ID>"}
    }
  }]
}
EOF

# 创建 Role
aws iam create-role \
  --role-name BedrockAgentRole-MultiAgent \
  --assume-role-policy-document file:///tmp/bedrock-trust-policy.json \
  --region us-east-1

# 附加权限策略
cat > /tmp/bedrock-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                 "bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["bedrock:GetAgent", "bedrock:InvokeAgent", "bedrock:GetAgentAlias"],
      "Resource": [
        "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:agent/*",
        "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:agent-alias/*"
      ]
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name BedrockAgentRole-MultiAgent \
  --policy-name BedrockInvokePolicy \
  --policy-document file:///tmp/bedrock-policy.json
```

!!! warning "权限注意"
    Agent Role 必须包含 `bedrock:GetAgentAlias` 权限，否则 `associate-agent-collaborator` 会报 "insufficient permissions"。这是一个常见踩坑点。

### Step 2: 创建 Collaborator Agents

```bash
ROLE_ARN="arn:aws:iam::<YOUR_ACCOUNT_ID>:role/BedrockAgentRole-MultiAgent"

# 创建目的地专家
aws bedrock-agent create-agent \
  --agent-name "destination-expert" \
  --foundation-model "us.anthropic.claude-haiku-4-5-20251001-v1:0" \
  --instruction "You are a destination expert specializing in travel recommendations. \
Provide detailed information about top attractions, best time to visit, \
local culture, food recommendations, and practical travel tips." \
  --agent-resource-role-arn "$ROLE_ARN" \
  --region us-east-1

# 创建预算规划师
aws bedrock-agent create-agent \
  --agent-name "budget-planner" \
  --foundation-model "us.anthropic.claude-haiku-4-5-20251001-v1:0" \
  --instruction "You are a travel budget planner. Provide detailed cost breakdowns \
including flights, accommodation, food, activities, and transport. \
Give price ranges for budget/mid-range/luxury levels in USD." \
  --agent-resource-role-arn "$ROLE_ARN" \
  --region us-east-1
```

### Step 3: Prepare 并创建 Alias

```bash
# Prepare Collaborators
aws bedrock-agent prepare-agent --agent-id <DEST_AGENT_ID> --region us-east-1
aws bedrock-agent prepare-agent --agent-id <BUDGET_AGENT_ID> --region us-east-1

# 等待 Prepared 状态（约 10-15 秒）
sleep 15

# 创建 Alias
aws bedrock-agent create-agent-alias \
  --agent-id <DEST_AGENT_ID> --agent-alias-name "live" --region us-east-1

aws bedrock-agent create-agent-alias \
  --agent-id <BUDGET_AGENT_ID> --agent-alias-name "live" --region us-east-1
```

### Step 4: 创建 Supervisor 并关联 Collaborators

```bash
# 创建 Supervisor（注意 --agent-collaboration SUPERVISOR）
aws bedrock-agent create-agent \
  --agent-name "travel-supervisor" \
  --foundation-model "us.anthropic.claude-sonnet-4-20250514-v1:0" \
  --agent-collaboration SUPERVISOR \
  --instruction "You are a travel planning supervisor. Coordinate with specialist agents: \
1) Route destination questions to Destination Expert \
2) Route budget questions to Budget Planner \
3) Synthesize responses into a cohesive travel plan." \
  --agent-resource-role-arn "$ROLE_ARN" \
  --region us-east-1

# 关联 Collaborators（⚠️ 注意：Supervisor 不能在没有 Collaborator 的情况下 Prepare）
aws bedrock-agent associate-agent-collaborator \
  --agent-id <SUPERVISOR_ID> \
  --agent-version DRAFT \
  --agent-descriptor aliasArn=<DEST_ALIAS_ARN> \
  --collaborator-name "DestinationExpert" \
  --collaboration-instruction "Route destination, attraction, and culture questions to this agent." \
  --relay-conversation-history TO_COLLABORATOR \
  --region us-east-1

aws bedrock-agent associate-agent-collaborator \
  --agent-id <SUPERVISOR_ID> \
  --agent-version DRAFT \
  --agent-descriptor aliasArn=<BUDGET_ALIAS_ARN> \
  --collaborator-name "BudgetPlanner" \
  --collaboration-instruction "Route budget, cost, and pricing questions to this agent." \
  --relay-conversation-history TO_COLLABORATOR \
  --region us-east-1

# 现在可以 Prepare Supervisor
aws bedrock-agent prepare-agent --agent-id <SUPERVISOR_ID> --region us-east-1
sleep 15

# 创建 Supervisor Alias
aws bedrock-agent create-agent-alias \
  --agent-id <SUPERVISOR_ID> --agent-alias-name "live" --region us-east-1
```

### Step 5: 调用测试

```python
import boto3
import time

session = boto3.Session(region_name='us-east-1')
client = session.client('bedrock-agent-runtime',
    config=boto3.session.Config(read_timeout=300))

def invoke_agent(agent_id, alias_id, prompt, session_id=None):
    if not session_id:
        session_id = f"test-{int(time.time())}"

    response = client.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=prompt,
        enableTrace=True  # 查看路由信息
    )

    result = ""
    for event in response['completion']:
        if 'chunk' in event:
            result += event['chunk']['bytes'].decode('utf-8')
    return result

# 测试 Multi-Agent
print(invoke_agent("<SUPERVISOR_ID>", "<SUPERVISOR_ALIAS>",
    "What are the top 5 things to do in Bangkok and how much would each cost?"))
```

## 测试结果

### 单 Agent vs 多 Agent 性能对比

| 测试场景 | 单 Agent | 多 Agent | 延迟倍率 | 内容量倍率 |
|----------|----------|----------|----------|-----------|
| Tokyo 5 天计划 | 23.4s / 3,016 字符 | 45.2s / 1,108 字符 | 1.9x | 0.4x* |
| Bangkok Top 5 | 6.5s / 1,211 字符 | 56.7s / 2,846 字符 | 8.7x | 2.4x |

*多 Agent 在 Tokyo 计划场景选择先问 follow-up 问题，而非直接给答案。

### 路由准确性测试

| 问题类型 | 路由结果 | 正确性 |
|----------|----------|--------|
| 纯目的地（"Kyoto temples"）| → DestinationExpert | ✅ |
| 纯预算（"Bali cost breakdown"）| → BudgetPlanner + DestinationExpert | ✅ 智能交叉引用 |
| 混合（"Tokyo plan + budget"）| → 两个 Collaborator | ✅ |

### relay-conversation-history 对比

| 场景 | relay ON | relay OFF |
|------|----------|-----------|
| Turn 1: "Best Japan city?" | 推荐 Tokyo (28.7s) | 问 follow-up (17.1s) |
| Turn 2: "3-day cost?" | **Tokyo 具体**预算 ✅ | **日本通用**预算 ❌ |

**关键发现**：`relay-conversation-history: TO_COLLABORATOR` 将之前的对话上下文传递给 Collaborator，使得 Collaborator 能理解完整上下文。关闭后，Collaborator 只能看到 Supervisor 转发的当前请求。

## 踩坑记录

!!! warning "踩坑 1：Supervisor 不能无 Collaborator Prepare"
    `--agent-collaboration SUPERVISOR` 的 Agent 在没有关联任何 Collaborator 的情况下调用 `prepare-agent` 会报错：*"This agent cannot be prepared. The AgentCollaboration attribute is set to SUPERVISOR but no agent collaborators are added."*

    **正确顺序**：先创建并 Prepare Collaborators → 创建 Alias → 关联到 Supervisor → 再 Prepare Supervisor。已查文档确认。

!!! warning "踩坑 2：Agent Role 需要 GetAgentAlias 权限"
    `associate-agent-collaborator` 调用时，Supervisor 的 IAM Role 需要对 Collaborator 的 Alias ARN 有 `bedrock:GetAgentAlias` 权限，否则报 *"You do not have sufficient permissions to collaborate with this agent alias"*。实测发现，官方文档未专门说明此权限需求。

!!! warning "踩坑 3：新模型必须用 Inference Profile"
    直接使用 Model ID（如 `anthropic.claude-sonnet-4-20250514-v1:0`）会报错：*"Invocation of model ID ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile."*

    **解决**：使用 Inference Profile ID（如 `us.anthropic.claude-sonnet-4-20250514-v1:0`）。可通过 `aws bedrock list-inference-profiles` 查看可用列表。已查文档确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Claude Sonnet 4 (Supervisor + Baseline) | $3/M input, $15/M output | ~50K tokens | ~$1.00 |
| Claude Haiku 4.5 (Collaborators) | $0.80/M input, $4/M output | ~30K tokens | ~$0.20 |
| Bedrock Agent（无额外费用）| $0 | - | $0 |
| IAM Role | $0 | - | $0 |
| **合计** | | | **~$1.20** |

## 清理资源

```bash
# 1. 删除所有 Agent Alias
aws bedrock-agent delete-agent-alias --agent-id <SUPERVISOR_ID> --agent-alias-id <ALIAS_ID> --region us-east-1
aws bedrock-agent delete-agent-alias --agent-id <DEST_ID> --agent-alias-id <ALIAS_ID> --region us-east-1
aws bedrock-agent delete-agent-alias --agent-id <BUDGET_ID> --agent-alias-id <ALIAS_ID> --region us-east-1

# 2. 删除所有 Agent
aws bedrock-agent delete-agent --agent-id <SUPERVISOR_ID> --region us-east-1
aws bedrock-agent delete-agent --agent-id <DEST_ID> --region us-east-1
aws bedrock-agent delete-agent --agent-id <BUDGET_ID> --region us-east-1

# 3. 删除 IAM Role
aws iam delete-role-policy --role-name BedrockAgentRole-MultiAgent --policy-name BedrockInvokePolicy
aws iam delete-role --role-name BedrockAgentRole-MultiAgent
```

!!! danger "务必清理"
    Bedrock Agent 本身不产生持续费用，但保留测试资源可能导致混淆。建议测试完立即清理。

## 结论与建议

### 多 Agent 适合的场景

1. **领域复杂度高**：当任务需要多个专业领域知识（如旅行规划 = 目的地 + 预算 + 交通）
2. **需要专业化**：每个 Agent 可以有不同的 Knowledge Base 和 Action Group
3. **可扩展性要求**：新增能力只需添加 Collaborator，不用修改 Supervisor

### 不建议使用的场景

1. **延迟敏感**：Multi-Agent 延迟约为 Single Agent 的 **2-9 倍**
2. **简单任务**：如果单个 Agent + 好的 Prompt 就能解决，不需要多 Agent
3. **成本敏感**：多次 LLM 调用意味着更多 token 消耗

### 生产环境建议

- **始终开启 `relay-conversation-history`**：对多轮对话质量有显著影响
- **Collaborator 用小模型**：如 Claude Haiku 或 Nova Lite，降低成本
- **Supervisor 用强模型**：路由决策质量依赖模型推理能力
- **Instruction 要明确分工**：避免 Collaborator 职责重叠
- **开启 `enableTrace`**：调试路由逻辑的必备工具

## 参考链接

- [Amazon Bedrock Multi-Agent Collaboration 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-multi-agent-collaboration.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-multi-agent-collaboration/)
- [Amazon Bedrock Agents 产品页](https://aws.amazon.com/bedrock/agents/)
