# Amazon Bedrock Intelligent Prompt Routing 实测：自动路由省成本的正确打开方式

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（纯 API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

你有一批 prompt 要处理，有的简单（"2+2=？"），有的复杂（"设计一个多区域容灾架构"）。全用大模型太贵，全用小模型质量不够。手动分流太蠢。

**Amazon Bedrock Intelligent Prompt Routing**（2025-04-22 GA）解决的就是这个问题：一个端点，自动判断 prompt 复杂度，简单的发小模型，复杂的发大模型。不用你写任何路由逻辑。

本文实测三个模型家族的 Default Router + Custom Router，用数据告诉你：路由准不准？能省多少钱？有哪些坑？

## 前置条件

- AWS 账号（需要 Bedrock 访问权限）
- AWS CLI v2 已配置
- 所用模型已在 Bedrock Model Access 中开启

## 核心概念

### 两种 Router

| 类型 | 配置 | 适用场景 |
|------|------|---------|
| **Default Router** | 预配置，开箱即用 | 快速体验，不想调参 |
| **Custom Router** | 自选模型对 + 设置质量阈值 | 生产环境，需要精细控制 |

### 关键参数：responseQualityDifference

这是控制路由行为的唯一旋钮：

- **值越小（如 0）**→ 对质量差异容忍度低 → 更可能路由到大模型
- **值越大（如 50）**→ 对质量差异容忍度高 → 更多路由到小模型（省钱）
- **取值约束**：必须是 **5 的倍数整数**（0, 5, 10, 15, 20...50）

### 支持的模型家族（GA）

| 家族 | 小模型 | 大模型 |
|------|--------|--------|
| Amazon Nova | Nova Lite ($0.06/$0.24 per 1M) | Nova Pro ($0.80/$3.20 per 1M) |
| Meta Llama | Llama 3.1 8B ($0.22/$0.22) | Llama 3.1 70B ($0.72/$0.72) |
| Anthropic Claude | Claude 3 Haiku | Claude 3.5 Sonnet |

> ⚠️ Anthropic Default Router 目前使用 Claude 3 Haiku + Sonnet 3.5 v1，这些模型已被标记为 **Legacy**，新账号可能无法使用。需创建 Custom Router 选择更新的模型。

## 动手实践

### Step 1: 查看可用的 Default Router

```bash
aws bedrock list-prompt-routers \
    --region us-east-1 \
    --query 'promptRouterSummaries[].{Name:promptRouterName,ARN:promptRouterArn,Models:models[].modelArn,Fallback:fallbackModel.modelArn,QD:routingCriteria.responseQualityDifference}' \
    --output table
```

你会看到 3 个预配置的 router（Nova、Anthropic、Meta）。

### Step 2: 用 Default Router 发送请求

直接把 Router ARN 当作 `modelId` 传入 Converse API：

```bash
# 简单 prompt
aws bedrock-runtime converse \
    --model-id "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:default-prompt-router/amazon.nova:1" \
    --messages '[{"role":"user","content":[{"text":"What is 2+2?"}]}]' \
    --region us-east-1
```

关键看响应中的 `trace.promptRouter.invokedModelId`——这就是 router 实际选择的模型。

### Step 3: 创建 Custom Router

```bash
aws bedrock create-prompt-router \
    --prompt-router-name my-nova-router \
    --models '[
        {"modelArn":"arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"},
        {"modelArn":"arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"}
    ]' \
    --fallback-model '{"modelArn":"arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"}' \
    --routing-criteria '{"responseQualityDifference": 10}' \
    --region us-east-1
```

> ⚠️ `fallback-model` 指定的模型 **必须** 同时出现在 `models` 列表中，否则报 `ValidationException`。

### Step 4: 对比测试不同 Quality Difference

用同一批 prompt 分别测试 qd=0、qd=20、qd=50 的路由行为差异。

### Step 5: 清理 Custom Router

```bash
aws bedrock delete-prompt-router \
    --prompt-router-arn "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:prompt-router/<ROUTER_ID>" \
    --region us-east-1
```

## 测试结果

### 路由准确性：Meta Router 表现最佳

我们分别向每个 router 发送 5 个简单 prompt 和 5 个复杂 prompt：

| Router | 简单→小模型 | 复杂→大模型 | 路由区分度 |
|--------|------------|------------|-----------|
| **Meta Llama** | 4/5 (80%) | **5/5 (100%)** | ⭐⭐⭐ 最佳 |
| **Amazon Nova** | 3/5 (60%) | 1/5 (20%) | ⭐ 倾向小模型 |
| Anthropic Claude | ❌ Legacy 无法调用 | ❌ | N/A |

**Meta router 区分度最高**：复杂 prompt 100% 路由到 70B，简单 prompt 80% 路由到 8B。

**Nova router 偏向 Lite**：即使是复杂分析类 prompt 也大部分路由到 Nova Lite，这可能是 Nova 家族两个模型能力差距本身较小的体现。

### responseQualityDifference 对路由行为的影响

用 7 个不同复杂度的 prompt 测试 Nova Custom Router：

| Quality Diff | Lite 路由比例 | Pro 路由比例 | 效果 |
|-------------|-------------|-------------|------|
| 0 | 71% | 29% | 默认平衡 |
| 5 | 71% | 29% | 几乎无变化 |
| 10 | 71% | 29% | 几乎无变化 |
| **20** | **86%** | **14%** | 开始偏向小模型 |
| **50** | **100%** | **0%** | 完全使用小模型 |

**关键发现**：qd 在 0-10 范围内路由行为完全一致，差异从 **qd=20 开始明显**，qd=50 时等于放弃大模型。

### 成本节省：实测 46.7%

10 个混合 prompt（6 个路由到 Lite，4 个到 Pro）：

| 方案 | 总成本 | 说明 |
|------|--------|------|
| Router（Default） | $0.004067 | 自动路由 |
| 全用 Nova Pro | $0.007625 | 无路由基线 |
| **节省** | **46.7%** | 与官方 35% 基本吻合 |

Nova Lite 单价是 Pro 的 **1/13**（input）和 **1/13**（output），所以每一个路由到 Lite 的请求都能省 ~92% 的费用。

### 延迟开销

| 场景 | 平均延迟 |
|------|---------|
| Nova Lite 直接调用 | 2961ms |
| Nova Pro 直接调用 | 4102ms |
| Nova Router → Lite | 3290ms |
| **Router 开销** | **~329ms** |

Router 额外增加了 ~329ms（官方声称 85ms P90），但因为路由到更快的 Lite 模型，整体延迟反而比直接调用 Pro **快了 812ms**。

## 踩坑记录

!!! warning "坑 1：responseQualityDifference 必须是 5 的倍数"
    文档说"percentage"，但 **没说必须是 5 的倍数整数**。传 0.25、0.5 这样的浮点数会报 `ValidationException: Response quality difference must be a value multiple of 5`。实测发现，官方未记录。

!!! warning "坑 2：Anthropic Default Router 不可用"
    默认的 Anthropic router 使用 Claude 3 Haiku + Sonnet 3.5 v1，这两个模型在 2026 年已被标记为 **Legacy**。调用会返回 `ResourceNotFoundException: Access denied. This Model is marked by provider as Legacy`。
    
    **解决方案**：创建 Custom Router 选择 Haiku 3.5 + Sonnet 3.5 v2。

!!! warning "坑 3：fallback-model 必须在 models 列表中"
    创建 Custom Router 时，`fallback-model` 指定的模型 ARN **必须**同时出现在 `models` 列表中。文档 CLI 示例中它们看起来是独立参数，容易误解。实测发现，官方未记录。

!!! warning "坑 4：中文 prompt 路由判断不准"
    简单中文 prompt 路由到 Pro，复杂中文 prompt 反而路由到 Lite——完全反了。这与官方声明"仅优化英文 prompt"一致。**非英文场景不建议使用**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| API 调用（~100 次） | 按 token | ~22K tokens | < $0.10 |
| Router 创建 | 免费 | 5 个 Custom Router | $0 |
| **合计** | | | **< $0.10** |

Intelligent Prompt Routing 本身 **不收取额外费用**，仅按实际调用的模型 token 定价收费。

## 清理资源

```bash
# 列出所有 Custom Router
aws bedrock list-prompt-routers --region us-east-1 \
    --query 'promptRouterSummaries[?type==`custom`].promptRouterArn' --output text

# 逐个删除
aws bedrock delete-prompt-router \
    --prompt-router-arn "<ROUTER_ARN>" \
    --region us-east-1
```

!!! danger "务必清理"
    虽然 Custom Router 本身不产生费用，但建议测试完毕后删除，保持账号整洁。

## 结论与建议

### 适合场景
- **混合复杂度的批量请求**：客服问答、内容审核等场景，prompt 复杂度差异大
- **成本敏感型应用**：预算有限但需要大模型能力兜底
- **快速原型**：不想自己写路由逻辑的 PoC

### 不适合场景
- **全是复杂 prompt**：路由意义不大，直接用大模型
- **非英文为主**：路由准确度下降
- **对延迟极度敏感**：router 额外增加 ~329ms 开销

### 生产建议
1. **从 Default Router 开始**，观察 `trace.promptRouter.invokedModelId` 了解路由分布
2. **Meta Llama 家族路由区分度最高**，推荐优先试用
3. **Custom Router 的 qd 建议从 10 开始调**，0-10 无明显差异，20+ 才有效果
4. **监控路由比例**，通过 CloudWatch 跟踪各模型的调用占比
5. **非英文场景慎用**，路由判断可能完全错误

## 参考链接

- [官方文档：Intelligent Prompt Routing](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html)
- [AWS Blog：Cost and Latency Benefits](https://aws.amazon.com/blogs/machine-learning/use-amazon-bedrock-intelligent-prompt-routing-for-cost-and-latency-benefits/)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-bedrock-intelligent-prompt-routing-generally-available/)
- [Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
