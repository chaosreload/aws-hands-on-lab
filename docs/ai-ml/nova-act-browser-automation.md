# Amazon Nova Act 实测：用自然语言驱动浏览器自动化的 AI Agent 服务

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-23

## 背景

浏览器自动化一直是个痛点。传统方案（Selenium、Playwright 脚本）需要维护大量 CSS selector 和 XPath，网页一改版就全部失效。

2025 年 12 月 2 日，AWS 正式 GA 了 **Amazon Nova Act** —— 一个让你用自然语言描述任务，AI 来操作浏览器完成的服务。不写 selector，不管 DOM 结构，只告诉它"点击 Learn more 链接"就行。

本文通过 CLI + Python SDK 全链路实测，对比 v1.0 GA 和 v1.1 Preview 两个模型版本，并探索边界行为。

## 前置条件

- AWS 账号（需要 `nova-act` 相关 IAM 权限）
- AWS CLI v2 已配置
- Python 3.10+
- `pip install nova-act`（会自动安装 Playwright 等依赖）

## 核心概念

Nova Act 采用 **客户端-服务器** 架构：

| 层级 | 说明 |
|------|------|
| **Workflow Definition** | 定义你的自动化任务（模板） |
| **Workflow Run** | 一次执行实例，绑定特定模型版本 |
| **Session** | 一个浏览器实例，支持串行多个 act |
| **Act** | 一次自然语言任务调用 |
| **Step** | 模型"看页面 → 决策 → 执行动作"的一个循环 |

**关键架构**：模型在 AWS 云端运行（观察页面截图、生成动作指令），浏览器在你的本地/客户端执行。SDK 封装了这个循环。

**模型版本**：

| Model ID | 说明 |
|----------|------|
| `nova-act-latest` | 自动跟踪最新 GA 版本（当前 v1.0） |
| `nova-act-preview` | 自动跟踪最新 Preview 版本（当前 v1.1） |
| `nova-act-v1.0` | Pin 到 v1.0 GA，至少支持 1 年 |

!!! note "注意"
    `nova-act-latest` 永远不会自动升级到 preview 模型。Preview 模型不支持 pin 到特定版本。

## 动手实践

### Step 1: 用 CLI 管理 Workflow 生命周期

```bash
# 创建 Workflow Definition
aws nova-act create-workflow-definition \
  --name my-data-extract-workflow \
  --description 'Extract data from web pages' \
  --region us-east-1

# 查看已创建的 Workflow Definition
aws nova-act get-workflow-definition \
  --workflow-definition-name my-data-extract-workflow \
  --region us-east-1
```

输出示例：

```json
{
    "name": "my-data-extract-workflow",
    "arn": "arn:aws:nova-act:us-east-1:123456789012:workflow-definition/my-data-extract-workflow",
    "createdAt": "2026-03-23T23:15:00.970000+00:00",
    "status": "ACTIVE"
}
```

### Step 2: 用 Python SDK 执行浏览器自动化

创建文件 `nova_act_demo.py`：

```python
import os
from nova_act import NovaAct
from nova_act.types.workflow import Workflow

os.environ['NOVA_ACT_HEADLESS'] = '1'  # 无头模式

with Workflow(
    model_id="nova-act-latest",
    workflow_definition_name="my-data-extract-workflow",
) as wf:
    print(f"Workflow Run ID: {wf.workflow_run_id}")
    
    with NovaAct(
        starting_page="https://aws.amazon.com/ai/generative-ai/nova/",
        headless=True,
        workflow=wf,
    ) as nova:
        # Act 1: 提取页面信息
        result = nova.act(
            "Read the main heading and the first 3 feature sections. "
            "Return a brief summary of each.",
            max_steps=10,
        )
        print(f"Steps: {result.metadata.num_steps_executed}")
        print(f"Time: {result.metadata.time_worked}")
        
        # Act 2: 页面导航
        result2 = nova.act(
            "Click on the 'Learn more' link if available.",
            max_steps=10,
        )
        print(f"Steps: {result2.metadata.num_steps_executed}")
```

执行：

```bash
python3 nova_act_demo.py
```

你会看到模型的思考链和执行过程：

```
start session xxx on https://aws.amazon.com/ai/generative-ai/nova/

> act("Read the main heading and the first 3 feature sections...")
> think("I am on the AWS page for Amazon Nova. I can see the main heading...")
> agentScroll("down", "<box>0,0,813,1600</box>")
> think("I can see Nova Act, Nova Forge, and Nova Models sections...")
> return("Main Heading: Amazon Nova - ...")
⏱️ Approx. Time Worked: 21.3s
```

### Step 3: 对比 v1.0 和 v1.1 Preview

只需修改 `model_id`：

```python
# v1.1 Preview
with Workflow(
    model_id="nova-act-preview",  # 改这一行
    workflow_definition_name="my-data-extract-workflow",
) as wf:
    # ... 其余代码相同
```

### Step 4: 查看执行历史

```bash
# 列出所有 Workflow Runs
aws nova-act list-workflow-runs \
  --workflow-definition-name my-data-extract-workflow \
  --region us-east-1

# 查看特定 Run 详情
aws nova-act get-workflow-run \
  --workflow-definition-name my-data-extract-workflow \
  --workflow-run-id <run-id> \
  --region us-east-1
```

## 测试结果

### 模型版本对比（同一任务）

| 指标 | v1.0 (GA) | v1.1 (Preview) | 差异 |
|------|-----------|----------------|------|
| 特性提取 — 步数 | 3 steps | 3 steps | 相同 |
| 特性提取 — 耗时 | 21.8s | 19.2s | **v1.1 快 12%** |
| 链接点击 — 步数 | 2 steps | 2 steps | 相同 |
| 链接点击 — 耗时 | 13.4s | 12.9s | **v1.1 快 4%** |
| 平均 Step 服务端耗时 | 3.1 - 6.1s | 3.1 - 4.6s | v1.1 更稳定 |
| 输出质量 | 准确 | 准确，稍简洁 | 相当 |
| **总执行时间** | **42.6s** | **39.5s** | **v1.1 快 7%** |

### 简单任务性能

| 任务 | 步数 | 耗时 | 说明 |
|------|------|------|------|
| 读取 Wikipedia 标题 | 1 step | 4.4s | 极简任务基准 |
| 读取 example.com 标题 | 1 step | 4.0s | 最小耗时约 4s |

### 边界行为

| 场景 | 行为 | 说明 |
|------|------|------|
| 不存在的按钮 | 搜索/尝试变通 → max_steps 超时 | 不会直接报错，会尝试恢复 |
| 404 页面 | 导航到首页 → 继续尝试 | 体现 AI Agent "韧性" |
| SSL 证书错误 | 抛出 InvalidCertificate 异常 | 可通过 `ignore_https_errors=True` 解决 |

## 踩坑记录

!!! warning "1. Preview 模型不能直接指定版本号"
    `nova-act-v1.1_2026-02-09` 会报 `ValidationException`，必须使用 `nova-act-preview` 别名。已查文档确认：官方明确 "Preview models are not version-pinnable"。

!!! warning "2. act() 和 act_get() 的区别"
    `act()` 返回 `ActResult`（只有 metadata），**没有 response 字段**。如果需要模型返回结构化数据，使用 `act_get()` 返回 `ActGetResult`。实测发现，官方文档未详细说明。

!!! warning "3. Headless 服务器需要 Chromium"
    SDK 默认尝试启动 Chrome，在服务器环境自动 fallback 到 Chromium。建议在 CI/CD 中设置 `NOVA_ACT_HEADLESS=1` 并确保 Playwright Chromium 已安装。

!!! warning "4. max_steps 是你的安全网"
    模型遇到无法完成的任务时不会主动报错，而是尝试各种变通方法直到耗尽步数。**务必设置合理的 `max_steps`**（默认 30，建议根据任务复杂度设 5-15）。

!!! warning "5. CLI vs SDK 的定位不同"
    CLI 适合管理 Workflow 生命周期（创建/查看/删除 Definition 和 Run），**不适合直接执行浏览器自动化**。`invoke-act-step` 需要客户端实现完整的浏览器执行循环。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| Nova Act Steps | ~40 steps × ~$0.003/step | ~$0.12 |
| EC2/VPC | 无 | $0 |
| **合计** | | **~$0.12** |

Nova Act 按 step 计费，每个 step 是模型"观察 + 决策"的一次循环。简单任务（读标题）仅 1 step，复杂任务（数据提取 + 导航）约 3-5 steps。

## 清理资源

```bash
# 删除 Workflow Definition（会级联清理关联的 Runs）
aws nova-act delete-workflow-definition \
  --workflow-definition-name my-data-extract-workflow \
  --region us-east-1
```

!!! danger "务必清理"
    虽然 Nova Act 的运行时费用极低，但保留不用的 Workflow Definition 会占用账户配额。

## 结论与建议

**适合场景**：

- 🟢 **Web 数据提取** — 不用维护 selector，适应网页变化
- 🟢 **表单自动填充** — 自然语言描述即可
- 🟢 **端到端 QA 测试** — 用自然语言写测试用例
- 🟡 **生产级批量任务** — 需要合理设置 max_steps 和错误处理

**v1.0 vs v1.1**：v1.1 Preview 在速度上有约 7-12% 的提升，输出质量相当。生产环境建议使用 `nova-act-latest`（稳定 GA），实验环境可以用 `nova-act-preview` 获得更快的响应。

**最佳实践**：

1. 用 `@workflow` 装饰器管理生命周期
2. 设置合理的 `max_steps`（建议 5-15）
3. 对不可控的网页使用 `ignore_https_errors=True`
4. 需要结构化返回时用 `act_get()` 而非 `act()`
5. 在 CI/CD 中设置 `NOVA_ACT_HEADLESS=1`

## 参考链接

- [Amazon Nova Act 官方文档](https://docs.aws.amazon.com/nova-act/latest/userguide/what-is-nova-act.html)
- [Nova Act 模型版本选择](https://docs.aws.amazon.com/nova-act/latest/userguide/model-version-selection.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/build-automate-production-ui-workflows-nova-act/)
- [Nova Act Python SDK (PyPI)](https://pypi.org/project/nova-act/)
- [Amazon Nova Act 定价](https://aws.amazon.com/nova/pricing/)
