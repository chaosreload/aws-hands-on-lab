---
tags:
  - AgentCore
  - Browser
  - Security
  - What's New
---

# Amazon Bedrock AgentCore Browser Web Bot Auth 实测：用加密签名让 AI Agent "证明自己是好 Bot"

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-27

## 背景

AI Agent 需要上网执行任务——填表单、查数据、抓取信息。但当 Agent 打开一个网站时，迎面而来的往往是 CAPTCHA、速率限制、甚至直接封 IP。问题的根源在于：**网站的 WAF（Web Application Firewall）无法区分"好 Bot"和"坏 Bot"**。

传统解决方案各有短板：

| 方案 | 问题 |
|------|------|
| 程序化解 CAPTCHA | 脆弱、昂贵、绕过了域名所有者的意图 |
| IP 白名单 | 云环境 IP 频繁变化，不可扩展 |
| User-Agent 字符串 | 可被任意伪造，不提供可验证身份 |

2025 年 10 月，Amazon Bedrock AgentCore Browser 发布了 **Web Bot Auth（Preview）** 支持。这是基于 [IETF 草案协议](https://datatracker.ietf.org/doc/html/draft-meunier-web-bot-auth-architecture) 的解决方案：**让 AI Agent 用加密签名证明自己的身份**，而不是试图绕过安全措施。

!!! tip "Browser 系列文章"
    本文是 AgentCore Browser 系列的第三篇，三篇文章解决不同层面的问题：

    | 文章 | 解决的问题 | 核心能力 |
    |------|-----------|---------|
    | [Browser Profiles](agentcore-browser-profiles.md) | 重复登录浪费时间 | 一次认证，跨 Session 复用 |
    | [Browser Proxy](agentcore-browser-proxy.md) | 出口 IP 不固定 | 自有代理，稳定 IP + 流量路由 |
    | **Web Bot Auth（本文）** | **CAPTCHA 阻断 Agent 工作流** | **加密签名证明身份，减少 CAPTCHA** |

## 前置条件

- AWS 账号（需要 `bedrock-agentcore:*` 和 `iam:CreateRole` 权限）
- AWS CLI v2 已配置
- Python 3.10+（如需使用 Strands Agent SDK）

## 核心概念

### Web Bot Auth 工作原理

Web Bot Auth 的核心思想很简单：**给 AI Agent 一个可验证的加密身份**。

```
1. 创建 Browser Tool 时启用签名
   └── AgentCore 自动生成密钥对（Ed25519）
   └── 公钥注册到 WAF 供应商目录

2. Agent 发起 HTTP 请求
   └── AgentCore 自动用私钥签名每个请求
   └── 添加 3 个 Header：
       - Signature: 加密签名值
       - Signature-Agent: 公钥目录 URL
       - Signature-Input: 签名元数据

3. 网站 WAF 接收请求
   └── 读取 Signature-Agent → 获取公钥
   └── 验证 Signature → 确认来自 AgentCore
   └── 应用域名策略（允许/限制/阻止）
```

### 域名所有者的三级控制

Web Bot Auth **不是绕过安全措施**，而是给域名所有者更多选择：

- **全部阻止**：拒绝所有自动化流量，即使有签名
- **允许已验证 Bot**：接受有有效签名的请求（许多站点的默认策略）
- **允许特定已验证 Bot**：只允许特定组织的 Agent 访问特定路径

### 支持的 WAF 供应商

| WAF 供应商 | 说明 |
|-----------|------|
| Cloudflare | 保护数百万网站，许多默认允许已验证 Bot |
| HUMAN Security | 企业级 Bot 防护 |
| Akamai Technologies | CDN + 安全服务 |
| DataDome | Bot 检测与防护 |

## 动手实践

### Step 1: 创建 IAM Execution Role

Web Bot Auth 需要一个 IAM 执行角色。这个角色**只需要 trust policy**，不需要任何额外的 IAM 权限策略。

创建 trust policy 文件：

```bash
cat > /tmp/browser-trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "BedrockAgentCoreBuiltInTools",
        "Effect": "Allow",
        "Principal": {
            "Service": "bedrock-agentcore.amazonaws.com"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
            "StringEquals": {
                "aws:SourceAccount": "<YOUR_ACCOUNT_ID>"
            },
            "ArnLike": {
                "aws:SourceArn": "arn:aws:bedrock-agentcore:<REGION>:<YOUR_ACCOUNT_ID>:*"
            }
        }
    }]
}
EOF
```

创建角色：

```bash
aws iam create-role \
  --role-name agentcore-browser-web-bot-auth-role \
  --assume-role-policy-document file:///tmp/browser-trust-policy.json \
  --description "Execution role for AgentCore Browser Web Bot Auth"
```

### Step 2: 创建启用签名的 Browser Tool

关键参数是 `--browser-signing '{"enabled":true}'`：

```bash
aws bedrock-agentcore-control create-browser \
  --name webBotAuthSigned \
  --description "Browser with Web Bot Auth signing enabled" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --execution-role-arn "arn:aws:iam::<YOUR_ACCOUNT_ID>:role/agentcore-browser-web-bot-auth-role" \
  --browser-signing '{"enabled":true}' \
  --region us-west-2 \
  --no-cli-pager
```

预期输出：

```json
{
    "browserId": "webBotAuthSigned-ZXGNk2fca3",
    "status": "READY"
}
```

!!! warning "Browser 名称规则"
    名称只支持 `[a-zA-Z][a-zA-Z0-9_]{0,47}` 模式。**不能用连字符（-）**，只能用字母、数字和下划线。

### Step 3: 创建对照组（不启用签名）

```bash
aws bedrock-agentcore-control create-browser \
  --name webBotAuthUnsigned \
  --description "Browser without Web Bot Auth for comparison" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --region us-west-2 \
  --no-cli-pager
```

### Step 4: 对比两个 Browser 配置

```bash
# 查看签名版
aws bedrock-agentcore-control get-browser \
  --browser-id <SIGNED_BROWSER_ID> \
  --region us-west-2 --no-cli-pager

# 查看无签名版
aws bedrock-agentcore-control get-browser \
  --browser-id <UNSIGNED_BROWSER_ID> \
  --region us-west-2 --no-cli-pager
```

对比结果：

| 字段 | 签名版 | 无签名版 |
|------|--------|---------|
| `browserSigning` | `{"enabled": true}` | **字段不存在** |
| `executionRoleArn` | IAM Role ARN | **字段不存在** |
| `networkMode` | PUBLIC | PUBLIC |
| `status` | READY | READY |

### Step 5: 用 Strands SDK 对比浏览体验

创建 Python 脚本，对比两个 Browser 访问 httpbin.org 时的 HTTP Header 差异：

```python
import os
os.environ["AWS_PROFILE"] = "<YOUR_PROFILE>"
os.environ["AWS_DEFAULT_REGION"] = "us-west-2"

from strands_tools.browser import AgentCoreBrowser
from strands_tools.browser.models import (
    BrowserInput, InitSessionAction, NavigateAction,
    GetHtmlAction, CloseAction
)

def inspect_headers(browser_id, label):
    """启动 Browser Session，访问 httpbin.org 查看请求 Headers"""
    browser = AgentCoreBrowser(region="us-west-2", identifier=browser_id)
    session_name = f"test-{label}-headers-01"

    # 初始化 Session
    browser.browser(BrowserInput(action=InitSessionAction(
        type="init_session",
        description=f"Header inspection - {label}",
        session_name=session_name
    )))

    # 导航到 httpbin.org/headers（会显示服务端收到的所有 HTTP Headers）
    browser.browser(BrowserInput(action=NavigateAction(
        type="navigate",
        session_name=session_name,
        url="https://httpbin.org/headers"
    )))

    import time; time.sleep(3)

    # 获取页面 HTML
    result = browser.browser(BrowserInput(action=GetHtmlAction(
        type="get_html",
        session_name=session_name,
        selector="pre"
    )))
    print(f"\n{'='*50}")
    print(f"{label.upper()} Browser Headers:")
    print(f"{'='*50}")
    print(result)

    # 关闭 Session
    browser.browser(BrowserInput(action=CloseAction(
        type="close", session_name=session_name
    )))

# 对比签名 vs 无签名
inspect_headers("<SIGNED_BROWSER_ID>", "signed")
inspect_headers("<UNSIGNED_BROWSER_ID>", "unsigned")
```

## 测试结果

### Header 对比：签名的"指纹"

这是最核心的发现——通过 httpbin.org 可以直接看到签名版和无签名版的请求差异：

**签名版 Browser 的请求 Headers：**

```json
{
  "Accept": "text/html,...",
  "Host": "httpbin.org",
  "Sec-Ch-Ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\"",
  "Signature": "sig1=:pKe7yoIeAtPFGvfkNqYAIW/VQ39WXHw3Tg...==:",
  "Signature-Agent": "\"https://bxtrz00tv0lm1.keydirectory.signer.us-west-2.on.aws\"",
  "Signature-Input": "sig1=(\"@authority\" \"signature-agent\");created=1774591476;alg=\"ed25519\";keyid=\"tmNl1z...\";tag=\"web-bot-auth\";expires=1774595076;nonce=\"M...\""
}
```

**无签名版 Browser 的请求 Headers：**

```json
{
  "Accept": "text/html,...",
  "Host": "httpbin.org",
  "Sec-Ch-Ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\"",
  "Upgrade-Insecure-Requests": "1",
  "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36..."
}
```

### 签名 Header 解读

| Header | 作用 | 实测值 |
|--------|------|--------|
| `Signature` | Ed25519 加密签名值 | Base64 编码的签名数据 |
| `Signature-Agent` | 公钥目录 URL | `https://{id}.keydirectory.signer.{region}.on.aws` |
| `Signature-Input` | 签名元数据 | 包含算法、密钥 ID、有效期等 |

从 `Signature-Input` 中可以提取关键信息：

| 参数 | 含义 | 实测值 |
|------|------|--------|
| `alg` | 签名算法 | `ed25519` |
| `keyid` | 密钥标识符 | 唯一密钥 ID |
| `tag` | 协议标识 | `web-bot-auth` |
| `created` | 签名创建时间 | Unix 时间戳 |
| `expires` | 签名过期时间 | created + 3600（约 1 小时） |
| 签名覆盖组件 | 被签名的请求部分 | `@authority`（域名）+ `signature-agent` |

### 对照实验总结

| 维度 | 签名版 | 无签名版 |
|------|--------|---------|
| 额外 HTTP Headers | Signature + Signature-Agent + Signature-Input | 无 |
| 身份可验证 | ✅ WAF 可通过公钥目录验证 | ❌ 无法验证 |
| 对无 WAF 站点影响 | 无影响，正常访问 | 正常访问 |
| 创建速度 | < 1 秒 | < 1 秒 |
| 需要 IAM Role | ✅ 必须 | ❌ 不需要 |

## 踩坑记录

!!! warning "踩坑 1：Browser 名称不支持连字符"
    **现象**：使用 `web-bot-auth-signed` 创建 Browser 时报错 `ValidationException`。

    **原因**：Browser 名称必须匹配 `[a-zA-Z][a-zA-Z0-9_]{0,47}`，只支持字母、数字和下划线，不支持连字符。

    **解决**：使用驼峰命名或下划线：`webBotAuthSigned` 或 `web_bot_auth_signed`。

    *已查文档确认：API 文档中有 pattern 说明。*

!!! warning "踩坑 2：签名配置创建后不可修改"
    **现象**：没有 `update-browser` API 可以在创建后开启/关闭签名。

    **影响**：如果要切换签名状态，需要删除并重新创建 Browser Tool。

    *实测发现，官方未明确记录。*

!!! warning "踩坑 3：不传 Execution Role 时启用签名会直接报错"
    **现象**：`create-browser` 只传 `--browser-signing` 但不传 `--execution-role-arn` 时，报错 `ValidationException: ExecutionRoleArn is required when signing configuration is provided`。

    **要点**：签名功能强制依赖 execution role，即使 role 不需要任何 IAM policy。

    *已查文档确认。*

!!! warning "踩坑 4：Strands SDK Session 名称有严格限制"
    **现象**：Session 名称必须至少 10 个字符，且只支持 `^[a-z0-9-]+$`（小写字母、数字和连字符）。

    **解决**：使用如 `test-signed-session-01` 格式。

    *实测发现，Strands SDK 的 Pydantic 模型强制校验。*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| IAM Role | 免费 | 1 | $0 |
| Browser Tool (创建) | 免费 | 2 | $0 |
| Browser Session | ~$0.01/session | 约 6 sessions | ~$0.06 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除签名版 Browser Tool
aws bedrock-agentcore-control delete-browser \
  --browser-id <SIGNED_BROWSER_ID> \
  --region us-west-2 --no-cli-pager

# 2. 删除无签名版 Browser Tool
aws bedrock-agentcore-control delete-browser \
  --browser-id <UNSIGNED_BROWSER_ID> \
  --region us-west-2 --no-cli-pager

# 3. 删除 IAM Role
aws iam delete-role \
  --role-name agentcore-browser-web-bot-auth-role
```

!!! danger "务必清理"
    Browser Tool 本身不产生持续费用，但活跃的 Session 会按使用量计费。确保所有 Session 已终止。

## 结论与建议

### Web Bot Auth 适合什么场景？

| 场景 | 适合度 | 说明 |
|------|--------|------|
| Agent 访问 Cloudflare/Akamai 保护的网站 | ⭐⭐⭐ | 直接受益，CAPTCHA 减少 |
| 企业 Agent 自动化工作流 | ⭐⭐⭐ | 减少人工介入 CAPTCHA |
| 爬虫/数据采集 | ⭐⭐ | 域名所有者仍可阻止 |
| 无 WAF 的内部系统 | ⭐ | 不影响，但也无意义 |

### 与传统方案的对比

| 方案 | 可验证 | 可扩展 | 尊重域名策略 | 额外成本 |
|------|--------|--------|------------|---------|
| CAPTCHA 求解 | ❌ | ❌ | ❌ 绕过 | 高 |
| IP 白名单 | ❌ | ❌ | ✅ | 中 |
| User-Agent 伪装 | ❌ | ✅ | ❌ 欺骗 | 低 |
| **Web Bot Auth** | **✅** | **✅** | **✅** | **无额外** |

### 生产环境建议

1. **默认启用签名**：没有额外成本，只需一个空 IAM Role，签名不影响无 WAF 站点
2. **组合使用**：签名（身份）+ Profiles（认证复用）+ Proxy（出口 IP），三层组合覆盖大部分场景
3. **注意 Preview 状态**：底层 IETF 协议仍在草案阶段，API 参数可能变更
4. **域名控制权**：即使开启签名，域名所有者仍可限制或阻止访问。不要假设签名 = 无限制访问

### Browser 系列总结

三篇文章覆盖了 AgentCore Browser 的三个关键增强：

```
                    ┌─────────────────────┐
                    │   AI Agent 浏览网站  │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼───────┐ ┌─────▼──────┐ ┌───────▼───────┐
     │  Web Bot Auth  │ │  Profiles  │ │    Proxy      │
     │ "我是好 Bot"   │ │ "我已登录" │ │ "我的 IP 稳定" │
     │  加密签名身份  │ │ Cookie复用 │ │  自有代理出口  │
     └────────────────┘ └────────────┘ └───────────────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   减少 CAPTCHA 摩擦  │
                    │   持久化认证状态     │
                    │   稳定可审计的出口   │
                    └─────────────────────┘
```

## 参考链接

- [Web Bot Auth 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-web-bot-auth.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-browser-web-bot-auth-preview/)
- [AWS 博客：Reduce CAPTCHAs with Web Bot Auth](https://aws.amazon.com/blogs/machine-learning/reduce-captchas-for-ai-agents-browsing-the-web-with-web-bot-auth-preview-in-amazon-bedrock-agentcore-browser/)
- [IETF Draft: HTTP Message Signatures for automated traffic](https://datatracker.ietf.org/doc/html/draft-meunier-web-bot-auth-architecture)
- [AgentCore Browser 文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-tool.html)
- [AgentCore 定价](https://aws.amazon.com/bedrock/agentcore/pricing/)
