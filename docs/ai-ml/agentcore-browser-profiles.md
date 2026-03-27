# Amazon Bedrock AgentCore Browser Profiles：跨 Session 复用认证状态实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-27

## 背景

在企业级 AI Agent 自动化场景中，Agent 频繁需要访问需要认证的网站——CRM 系统、内部管理平台、SaaS 工具等。每次启动新的浏览器 Session 都要重新登录，不仅浪费时间（从几十秒到几分钟），还增加了 MFA 触发风险和认证 Token 消耗。

2026 年 2 月，Amazon Bedrock AgentCore Browser 新增了 **Browser Profiles** 功能。核心能力：**一次认证，跨 Session 复用**。Profile 持久化浏览器的 Cookies 和 LocalStorage，新 Session 启动时自动恢复认证状态，实现"登录一次，多次使用"。

## 前置条件

- AWS 账号（需要 `bedrock-agentcore` 相关权限）
- AWS CLI v2 已配置
- Python 3.10+（如需使用 Strands Agent SDK）
- 在 [AgentCore Browser 可用的 14 个 Region](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html) 之一操作

### IAM 权限

需要两组权限——**Profile 管理**和 **Profile 使用**：

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "BrowserProfileManagement",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateBrowserProfile",
                "bedrock-agentcore:ListBrowserProfiles",
                "bedrock-agentcore:GetBrowserProfile",
                "bedrock-agentcore:DeleteBrowserProfile"
            ],
            "Resource": "arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:browser-profile/*"
        },
        {
            "Sid": "BrowserProfileUsage",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:SaveBrowserSessionProfile",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:ListBrowserSessions"
            ],
            "Resource": [
                "arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:browser-profile/*",
                "arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:browser/*"
            ]
        }
    ]
}
```

## 核心概念

### 架构：Control Plane + Data Plane

Browser Profiles 使用两个独立的 Service Endpoint：

| 功能 | Service | API 操作 |
|------|---------|---------|
| Profile 管理 | `bedrock-agentcore-control` | CreateBrowserProfile, ListBrowserProfiles, GetBrowserProfile, DeleteBrowserProfile |
| Session + Profile 使用 | `bedrock-agentcore` | StartBrowserSession, SaveBrowserSessionProfile, StopBrowserSession |

### 工作流

```
1. 创建 Profile（一次性）
   └→ Control Plane: CreateBrowserProfile

2. 启动 Session → 执行操作（登录等）→ 保存 Profile
   └→ Data Plane: StartBrowserSession → SaveBrowserSessionProfile

3. 用 Profile 启动新 Session（认证状态自动恢复）
   └→ Data Plane: StartBrowserSession + profileConfiguration
```

### 关键限制

| 限制 | 说明 |
|------|------|
| Save 是覆盖式 | 每次 Save 覆盖之前的 Profile 数据 |
| Session 隔离 | 并发 Session 之间互不可见，各自独立 |
| Profile 加载时机 | 只在 Session 启动时加载，运行中的 Session 不会反映后续 Profile 更新 |
| Cookie 过期 | 浏览器按网站设定的过期时间自动清除 |

## 动手实践

### Step 1: 创建 Browser Profile

```bash
aws bedrock-agentcore-control create-browser-profile \
  --region us-west-2 \
  --name "myAuthProfile" \
  --description "Persistent auth profile for web automation"
```

输出：

```json
{
    "profileId": "myAuthProfile-03aDADqgRP",
    "profileArn": "arn:aws:bedrock-agentcore:us-west-2:595842667825:browser-profile/myAuthProfile-03aDADqgRP",
    "createdAt": "2026-03-27T00:24:20.300588+00:00",
    "status": "READY"
}
```

!!! warning "Profile Name 命名规则"
    Name 必须匹配 `[a-zA-Z][a-zA-Z0-9_]{0,47}`。**不支持连字符 (`-`)**。使用 `myAuthProfile` 而非 `my-auth-profile`。

记录返回的 `profileId`，后续步骤需要。

### Step 2: 启动 Browser Session

```bash
# 设置变量
PROFILE_ID="myAuthProfile-03aDADqgRP"  # 替换为你的 profileId

# 启动 Session（无 Profile）
aws bedrock-agentcore start-browser-session \
  --region us-west-2 \
  --browser-identifier "aws.browser.v1" \
  --name "auth_session" \
  --session-timeout-seconds 3600
```

输出包含 `sessionId` 和两个 Stream Endpoint：

```json
{
    "sessionId": "01KMPAWQJ7ZSS1F89WS146F5SG",
    "streams": {
        "automationStream": {
            "streamEndpoint": "wss://bedrock-agentcore.us-west-2.amazonaws.com/browser-streams/aws.browser.v1/sessions/<SESSION_ID>/automation"
        },
        "liveViewStream": {
            "streamEndpoint": "https://bedrock-agentcore.us-west-2.amazonaws.com/browser-streams/aws.browser.v1/sessions/<SESSION_ID>/live-view"
        }
    }
}
```

### Step 3: 执行浏览器操作（登录网站）

通过 Automation WebSocket 或 Strands Agent SDK 操作浏览器，完成登录等操作。

**方式一：使用 Strands Agent SDK（推荐）**

```python
import os
os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'

from strands import Agent
from strands_tools.browser import AgentCoreBrowser

browser = AgentCoreBrowser(region="us-west-2")
agent = Agent(
    tools=[browser.browser],
    system_prompt="You are a browser automation assistant."
)

# Agent 自动管理 Session 生命周期
response = agent("Navigate to https://example.com/login and log in with the provided credentials.")
```

**方式二：使用 Boto3 SDK**

```python
import boto3

region = "us-west-2"
dp_client = boto3.client('bedrock-agentcore', region_name=region)

# 启动 Session
response = dp_client.start_browser_session(
    browserIdentifier="aws.browser.v1",
    name="my_session",
    sessionTimeoutSeconds=3600
)
session_id = response['sessionId']
# 通过 WebSocket automation endpoint 操作浏览器...
```

### Step 4: 保存 Session 到 Profile

```bash
SESSION_ID="<your-session-id>"  # 从 Step 2 获取
PROFILE_ID="<your-profile-id>"  # 从 Step 1 获取

aws bedrock-agentcore save-browser-session-profile \
  --region us-west-2 \
  --profile-id "$PROFILE_ID" \
  --browser-identifier "aws.browser.v1" \
  --session-id "$SESSION_ID"
```

!!! warning "Save 时机很关键"
    **必须在 Session 活跃时保存**。Session 停止后再 Save 会报 `ValidationException: Browser session is not active`。如果使用 Strands Agent SDK，注意 Agent 的 browser tool 会在任务完成后自动 Stop Session，需要在此之前手动调用 Save。

### Step 5: 用 Profile 启动新 Session

```bash
aws bedrock-agentcore start-browser-session \
  --region us-west-2 \
  --browser-identifier "aws.browser.v1" \
  --name "restored_session" \
  --session-timeout-seconds 3600 \
  --profile-configuration '{"profileIdentifier":"'"$PROFILE_ID"'"}'
```

这个 Session 将自动恢复之前保存的 Cookies 和 LocalStorage，无需重新登录。

### Step 6: 管理 Profile

```bash
# 查看所有 Profiles
aws bedrock-agentcore-control list-browser-profiles --region us-west-2

# 获取 Profile 详情（包含 lastSavedAt 等元数据）
aws bedrock-agentcore-control get-browser-profile \
  --region us-west-2 \
  --profile-id "$PROFILE_ID"

# 删除 Profile
aws bedrock-agentcore-control delete-browser-profile \
  --region us-west-2 \
  --profile-id "$PROFILE_ID"
```

## 测试结果

### 功能验证

| 测试场景 | 结果 | 说明 |
|---------|------|------|
| Profile CRUD（Create/List/Get/Delete） | ✅ 通过 | 创建即 READY，无异步等待 |
| 无 Profile Session → httpbin cookies | ✅ 空 | `{"cookies": {}}` |
| 有 Profile Session → httpbin cookies | ✅ 恢复 | Profile 配置正确传入 |
| 并行 Session 引用同一 Profile | ✅ 通过 | 两个 Session 都正常 READY |
| 删除正在使用的 Profile | ✅ 通过 | Profile 被删，Session 继续运行 |
| Save 已停止的 Session | ✅ 报错 | `ValidationException: session is not active` |

### Profile 元数据追踪

Save 操作后，Profile 自动记录：

```json
{
    "lastSavedAt": "2026-03-27T00:36:44.907675+00:00",
    "lastSavedBrowserSessionId": "01KMPBHF5EJC66ABAT9Q0EAF30",
    "lastSavedBrowserId": "aws.browser.v1"
}
```

## 踩坑记录

!!! warning "踩坑 1：Profile Name 不支持连字符"
    使用 `profile-test-1` 会报 `ValidationException: must satisfy regular expression pattern [a-zA-Z][a-zA-Z0-9_]{0,47}`。
    **已查文档确认**：官方文档未明确说明此正则限制，属于 API 层面的校验规则。

!!! warning "踩坑 2：CLI 参数名与文档不一致"
    官方文档示例使用 `--profile-identifier`，但 AWS CLI 实际接受的参数名是 `--profile-id`。使用文档中的参数名会报 `ParamValidation` 错误。
    **实测发现，官方未记录**。

!!! warning "踩坑 3：Strands Agent 自动关闭 Session"
    使用 Strands Agent 的 `AgentCoreBrowser` tool 时，Agent 在完成 tool call 后会自动 Stop Session。如果需要在 Agent 操作后 Save Profile，必须**在自定义 tool 内部**或通过**手动 Session 管理**来确保 Save 在 Stop 之前执行。

!!! warning "踩坑 4：WebSocket CDP 需要 SigV4 认证"
    直接用 Playwright 连接 `automationStream` 的 WSS URL 会收到 403 Forbidden。需要通过 SDK（如 `strands-agents-tools`）或自行实现 SigV4 WebSocket 认证。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Browser Session (CPU) | $0.0895/vCPU-hr | ~10 sessions × 3min | ~$0.05 |
| Browser Session (Memory) | $0.00945/GB-hr | ~10 sessions × 3min | ~$0.01 |
| Browser Profile | 免费 | 3 profiles | $0 |
| **合计** | | | **< $0.10** |

> Browser 定价采用 pay-per-use 模式，只收活跃处理时间（I/O 等待免费）。Profile 本身不收费，费用产生在 Session 的 compute 资源使用上。

## 清理资源

```bash
# 1. 停止所有活跃 Session
aws bedrock-agentcore list-browser-sessions \
  --region us-west-2 \
  --browser-identifier "aws.browser.v1" \
  --query 'sessionSummaries[?status==`READY`].sessionId' \
  --output text | tr '\t' '\n' | while read sid; do
    aws bedrock-agentcore stop-browser-session \
      --region us-west-2 \
      --browser-identifier "aws.browser.v1" \
      --session-id "$sid"
    echo "Stopped: $sid"
done

# 2. 删除 Browser Profiles
aws bedrock-agentcore-control list-browser-profiles \
  --region us-west-2 \
  --query 'profileSummaries[].profileId' \
  --output text | tr '\t' '\n' | while read pid; do
    aws bedrock-agentcore-control delete-browser-profile \
      --region us-west-2 \
      --profile-id "$pid"
    echo "Deleted: $pid"
done
```

!!! danger "务必清理"
    Browser Session 在超时前会持续计费。Lab 完成后请停止所有 Session 并删除测试 Profile。

## 结论与建议

### 适用场景

- **企业自动化**：Agent 需要频繁访问需认证的内部系统（CRM、ERP、Ticket 系统）
- **批量任务处理**：每天数百/数千次浏览器任务，避免每次重新登录
- **多步骤工作流**：跨多个 Session 的复杂流程，需要保持上下文
- **并行处理**：多个 Agent 同时使用同一 Profile（只读模式）

### 生产建议

1. **Session 管理**：显式管理 Session 生命周期，在 Stop 前 Save Profile
2. **Profile 命名**：使用字母开头 + 字母数字下划线，如 `prodAuth_crm`
3. **Cookie 过期**：监控 Profile 中 Cookie 的有效期，设计自动刷新机制
4. **安全**：Profile 含敏感认证数据，严格控制 IAM 权限
5. **并行读取**：多个 Session 可同时使用同一 Profile，但写入（Save）会互相覆盖

### 与直接 Session 方式对比

| 维度 | 无 Profile | 有 Profile |
|------|-----------|-----------|
| 启动时间 | 需完整登录（分钟级） | 直接恢复（秒级） |
| MFA 触发 | 每次登录 | 仅首次 |
| 并行效率 | 每个 Session 独立登录 | 共享认证状态 |
| 复杂度 | 低 | 中（需管理 Profile 生命周期） |

## 参考链接

- [Browser Profiles 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-profiles.html)
- [AgentCore Browser 快速入门](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart.html)
- [AgentCore Browser Fundamentals](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-resource-session-management.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-bedrock-agentcore-browser-profiles/)
- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
