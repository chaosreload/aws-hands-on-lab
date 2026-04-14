# AgentCore Browser OS-Level Actions 实测：突破 CDP 限制的 8 种操作全解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.05
    - **Region**: us-east-1
    - **最后验证**: 2026-04-09

## 背景

用 AI Agent 自动化浏览器操作时，Chrome DevTools Protocol (CDP) 是主流方案——Playwright、BrowserUse 都基于它。但 CDP 有一个硬伤：**它只能控制浏览器内部**。遇到系统打印对话框、JS alert 弹窗、右键上下文菜单这类 OS 级别的 UI 元素，CDP 直接失灵。

Amazon Bedrock AgentCore Browser 新增了 `InvokeBrowser` API，提供 8 种 OS-level 操作（鼠标、键盘、截图），绕过 CDP 直接注入系统事件。这意味着 AI Agent 终于可以处理那些"人能做但 CDP 做不了"的场景。

本文实测全部 8 种 OS-level actions，对比 CDP 的能力边界，并记录一个关键的坐标系行为差异。

## 前置条件

- AWS 账号，需要 `bedrock-agentcore:*` 相关权限
- AWS CLI v2（需包含 `bedrock-agentcore` 服务支持）

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:InvokeBrowser",
        "bedrock-agentcore:ListBrowserSessions"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

### InvokeBrowser API：8 种 OS-Level Actions

`InvokeBrowser` 是一个同步 REST API，每次调用执行一个操作，通过 Tagged Union 结构选择 action 类型：

| Action | 必填参数 | 可选参数 | 说明 |
|--------|---------|---------|------|
| `mouseClick` | x, y | button (LEFT/RIGHT/MIDDLE), clickCount | 在指定坐标点击 |
| `mouseMove` | x, y | — | 移动光标 |
| `mouseDrag` | startX, startY, endX, endY | button | 拖拽 |
| `mouseScroll` | x, y | deltaX, deltaY | 滚动（垂直+水平） |
| `keyType` | text | — | 输入文本字符串 |
| `keyPress` | key | presses | 按键（enter/tab/escape 等） |
| `keyShortcut` | keys[] | — | 组合键（如 ctrl+p） |
| `screenshot` | — | format (PNG) | 全屏截图 |

### CDP Actions vs OS-Level Actions

| 维度 | CDP (WebSocket) | OS-Level (InvokeBrowser) |
|------|----------------|--------------------------|
| 协议 | WebSocket 持久连接 | REST API（同步） |
| 元素定位 | CSS/XPath selector | 屏幕坐标 (x, y) |
| 系统对话框 | ❌ 无法处理 | ✅ 鼠标/键盘可操作 |
| 打印对话框 | ❌ CDP 无法触达 | ✅ keyShortcut + mouseClick |
| 右键菜单 | 有限支持 | ✅ button=RIGHT |
| JS alert/confirm | 阻塞 CDP | ✅ OS 级交互 |
| 截图范围 | 浏览器 viewport | 全屏（含系统 UI） |
| 表单填写 | ✅ 直接 DOM 操作 | 间接（先 click 定位再 keyType） |
| 延迟 | 更低（持久连接） | ~1.5s per action（REST 开销） |

**关键结论**：两者是互补关系，不是替代。日常 DOM 操作用 CDP 更快更精准；遇到系统级 UI 时用 InvokeBrowser 突破。

## 动手实践

### Step 1: 创建 Browser Session

```bash
aws bedrock-agentcore start-browser-session \
  --browser-identifier aws.browser.v1 \
  --name test-os-actions \
  --region us-east-1
```

**实测输出**：
```json
{
    "browserIdentifier": "aws.browser.v1",
    "sessionId": "01KNQX7F2G9Z48596G0GSVJ30R",
    "createdAt": "2026-04-09T01:20:40.474852+00:00",
    "streams": {
        "automationStream": {
            "streamEndpoint": "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/...",
            "streamStatus": "ENABLED"
        },
        "liveViewStream": {
            "streamEndpoint": "https://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/..."
        }
    }
}
```

Session 创建成功，同时获得 CDP WebSocket 端点和 Live View 端点。默认 viewport 1456×819，超时 15 分钟。

记录 `sessionId`，后续所有操作都需要它。

### Step 2: 基础鼠标操作

**点击**（左键，屏幕中心）：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseClick": {"x": 728, "y": 410}}' \
  --region us-east-1
```

```json
{"result": {"mouseClick": {"status": "SUCCESS"}}}
```

**移动光标**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseMove": {"x": 100, "y": 100}}' \
  --region us-east-1
```

**拖拽**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseDrag": {"startX": 100, "startY": 100, "endX": 300, "endY": 300}}' \
  --region us-east-1
```

**滚动**（垂直 + 水平）：

```bash
# 垂直滚动
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseScroll": {"x": 728, "y": 410, "deltaY": 300}}' \
  --region us-east-1

# 水平滚动
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseScroll": {"x": 728, "y": 410, "deltaX": 200, "deltaY": 0}}' \
  --region us-east-1
```

四种鼠标操作全部 SUCCESS。支持双击（`clickCount: 2`）、中键（`button: MIDDLE`）。

### Step 3: 键盘操作

**输入文本**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyType": {"text": "Hello AgentCore Browser"}}' \
  --region us-east-1
```

**按键**（支持重复）：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyPress": {"key": "tab", "presses": 3}}' \
  --region us-east-1
```

**组合键**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyShortcut": {"keys": ["ctrl", "a"]}}' \
  --region us-east-1
```

三种键盘操作全部 SUCCESS。

### Step 4: 核心对比 — 右键菜单与打印对话框

这是 OS-level actions 的核心价值：处理 CDP 搞不定的系统级 UI。

**右键上下文菜单**（CDP 无法可靠触发）：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseClick": {"x": 400, "y": 400, "button": "RIGHT"}}' \
  --region us-east-1
```

```json
{"result": {"mouseClick": {"status": "SUCCESS"}}}
```

随后截图确认菜单已弹出：

![右键菜单截图 — 可以看到浏览器原生上下文菜单被成功触发](images/agentcore-os-actions-right-click.png)

**打印对话框**（CDP 触发 `window.print()` 后会被阻塞）：

```bash
# 触发打印对话框
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyShortcut": {"keys": ["ctrl", "p"]}}' \
  --region us-east-1

# 截图确认对话框已出现
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"screenshot": {"format": "PNG"}}' \
  --region us-east-1

# 关闭对话框
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyPress": {"key": "escape"}}' \
  --region us-east-1
```

截图可以看到 Chromium 打印对话框被成功触发：

![打印对话框截图 — Ctrl+P 触发的系统打印对话框](images/agentcore-os-actions-print-dialog.png)

三步全部 SUCCESS。这个工作流——**触发系统对话框 → 截图确认 → 操作关闭**——正是 OS-level actions 的典型用法。

### Step 5: 全屏截图

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"screenshot": {"format": "PNG"}}' \
  --region us-east-1
```

返回 `result.screenshot.data` 字段包含 base64 编码的 PNG 图片。目前仅支持 PNG 格式。

![全屏截图示例 — OS-level screenshot 捕获的完整浏览器画面](images/agentcore-os-actions-initial.png)

!!! tip "截图用途"
    截图是 vision-based AI agent 的核心能力——agent 可以截图后用多模态模型（如 Claude、Nova）分析页面内容，决定下一步操作坐标。这比 CDP 获取 DOM 树更接近人类操作方式。

### Step 6: 边界测试

**负坐标**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseClick": {"x": -10, "y": -10}}' \
  --region us-east-1
```

```
ValidationException: Coordinates (x=-10, y=-10) must be strictly within 
viewport bounds (1 to 1454, 1 to 817)
```

**超大坐标**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"mouseClick": {"x": 99999, "y": 99999}}' \
  --region us-east-1
```

```
ValidationException: Coordinates (x=99999, y=99999) must be strictly within 
viewport bounds (1 to 1454, 1 to 817)
```

**空文本输入**：

```bash
aws bedrock-agentcore invoke-browser \
  --browser-identifier aws.browser.v1 \
  --session-id 01KNQX7F2G9Z48596G0GSVJ30R \
  --action '{"keyType": {"text": ""}}' \
  --region us-east-1
```

```json
{"result": {"keyType": {"status": "SUCCESS"}}}
```

空字符串不报错，静默返回成功（no-op）。

## 测试结果

| # | 测试场景 | 结果 | 关键数据 | 备注 |
|---|---------|------|---------|------|
| T1 | OS-level 全屏截图 | ✅ 通过 | base64 PNG | |
| T2 | 鼠标操作（click/move/drag/scroll） | ✅ 通过 | 全部 SUCCESS | 含双击、中键、水平滚动 |
| T3 | 键盘操作（type/press/shortcut） | ✅ 通过 | 全部 SUCCESS | 含重复按键 presses=3 |
| T4 | 右键菜单 | ✅ 通过 | button=RIGHT | CDP 无法可靠处理 |
| T5 | 打印对话框 ctrl+p | ✅ 通过 | 触发+截图+关闭 | CDP 被阻塞的典型场景 |
| T7 | 负坐标 / 超大坐标 | ✅ 预期报错 | ValidationException | 坐标受限于 viewport bounds |
| T8 | 空文本 keyType | ⚠️ 静默成功 | no-op | 官方未记录 |
| T9 | 滚动（垂直+水平） | ✅ 通过 | deltaX/deltaY | |
| T10 | API 延迟 | ✅ 完成 | 见下表 | |

### 性能数据

| 操作 | 平均延迟 | 范围 |
|------|---------|------|
| screenshot | 1,897ms | 1,877 – 1,921ms |
| mouseClick | 1,475ms | 1,440 – 1,494ms |
| keyType | 1,479ms | 1,461 – 1,494ms |

> 延迟包含完整 REST API 往返（签名 → 发送 → OS 事件注入 → 响应）。CDP WebSocket 操作理论上更快，因为省去了 HTTP 连接建立开销。

## 踩坑记录

!!! warning "踩坑 1: 坐标系是 viewport bounds，不是 OS 屏幕坐标"
    What's New 公告声称支持 "OS-level coordinates extending beyond the browser viewport"，但实测发现坐标**严格受限于 viewport 尺寸**。
    
    Viewport 1456×819 的 session，有效坐标范围是 `(1, 1)` 到 `(1454, 817)`——viewport 尺寸减去边距。
    
    ```
    ValidationException: Coordinates (x=-10, y=-10) must be strictly within 
    viewport bounds (1 to 1454, 1 to 817)
    ```
    
    **影响**：如果你的 agent 需要在浏览器窗口外操作（比如系统任务栏），当前版本不支持。"OS-level" 指的是操作机制（绕过 CDP 注入操作系统事件），而非坐标范围。实测发现，官方未明确记录此限制。

!!! info "踩坑 2: 空文本 keyType 静默成功"
    `keyType` 传入空字符串不会报错，返回 `SUCCESS`。如果你的 agent 程序中有空值传入的可能性，需要在应用层做校验。实测发现，官方未记录。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Browser Session (2vCPU, 4GB) | CPU $0.0895/vCPU/hr + Mem $0.00945/GB/hr | ~3 min | < $0.02 |
| **合计** | | | **< $0.02** |

Browser 按秒计费，I/O 等待期间免 CPU 费用。短时间测试几乎不产生费用。

## 清理资源

```bash
# 停止 Browser Session
aws bedrock-agentcore stop-browser-session \
  --browser-identifier aws.browser.v1 \
  --session-id <YOUR_SESSION_ID> \
  --region us-east-1
```

!!! tip "自动清理"
    使用 AWS 管理 Browser (`aws.browser.v1`) 无需创建/删除 Browser 资源。Session 会在超时后自动终止（默认 15 分钟），但建议显式停止以避免不必要的费用。

## 结论与建议

### 场景选型指南

| 场景 | 推荐方案 | 理由 |
|------|---------|------|
| 表单填写、页面导航、数据提取 | CDP (WebSocket) | DOM 直接操作，更快更精准 |
| 系统对话框处理（打印/下载/alert） | OS-Level (InvokeBrowser) | CDP 无法触达系统 UI |
| 右键菜单交互 | OS-Level | CDP 支持有限 |
| Vision-based AI Agent | OS-Level screenshot + 多模态 LLM | 截图→分析→点击 的循环 |
| 复杂工作流（混合场景） | CDP + OS-Level 组合 | 用 CDP 导航定位，遇到系统 UI 切换到 OS-Level |

### 生产注意事项

1. **坐标定位策略**：OS-level 操作依赖屏幕坐标，建议 screenshot → LLM 分析坐标 → 操作 的循环模式
2. **延迟预期**：单次 OS-level 操作 ~1.5s，复杂工作流需要多次调用，总耗时会累积
3. **每次只能执行一个 action**：如果需要"先点击再输入"，必须分两次 API 调用
4. **坐标范围限制**：当前坐标受限于 viewport bounds，无法操作浏览器窗口外的区域

## 参考链接

- [AgentCore Browser 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-tool.html)
- [AWS What's New: AgentCore Browser OS-level Actions](https://aws.amazon.com/about-aws/whats-new/2026/04/agentcore-browser-os-actions/)
- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
