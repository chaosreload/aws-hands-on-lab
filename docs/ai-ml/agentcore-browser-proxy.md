---
tags:
  - AgentCore
  - Browser
  - What's New
---

# Amazon Bedrock AgentCore Browser Proxy 实测：用自有代理实现稳定出口 IP 与精准流量路由

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $5-8（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

使用 Amazon Bedrock AgentCore Browser 执行 Web 自动化任务时，你可能注意到一个问题：**每次 session 的出口 IP 都不同**。这在大多数场景下不成问题，但对于以下需求却是痛点：

- **IP allowlisting**：企业门户或 SaaS 平台要求客户端 IP 在白名单中
- **Session 稳定性**：银行/医疗门户基于源 IP 验证 session，IP 变动导致频繁重新认证
- **合规审计**：安全团队要求所有出站流量经过企业代理，便于记录和审计
- **地理访问**：需要从特定地理位置访问区域性内容

2026 年 2 月，AWS 发布了 **AgentCore Browser Proxy** 功能，允许将浏览器 session 的流量路由到客户自有的代理服务器。本文通过搭建 Squid 代理实测所有核心功能，包括 IP 验证、域名路由、Bypass 规则和边界行为。

## 前置条件

- AWS 账号，具备 `bedrock-agentcore:*` 和 `secretsmanager:GetSecretValue` 权限
- AWS CLI v2 已配置
- 一台可以运行代理服务器的 EC2 实例（或已有的 HTTP/HTTPS 代理）

## 核心概念

### 工作原理

AgentCore Browser 通过 Chromium 的 `--proxy-server` 标志在浏览器级别应用代理配置：

```
StartBrowserSession(proxyConfiguration) → Chromium --proxy-server → 你的代理 → 目标网站
```

**关键特性**：

| 特性 | 说明 |
|------|------|
| 协议 | HTTP / HTTPS（不支持 SOCKS4/SOCKS5） |
| 认证 | HTTP Basic Auth（通过 Secrets Manager）或 IP allowlisting |
| 生命周期 | 创建时设置，**运行时不可修改** |
| 连接验证 | **Fail-open** — 创建时不验证代理连通性 |
| 每 session 代理数 | 最多 5 个 |
| 凭据安全 | API 响应仅返回 secretArn，不返回实际密码 |

### 域名路由

支持三级路由控制（优先级从高到低）：

1. **Bypass domains** — 直连，不经过任何代理
2. **Domain patterns** — 匹配特定域名的流量走指定代理
3. **Default proxy** — 未匹配的流量走默认代理

域名模式语法：

| 模式 | 匹配范围 |
|------|---------|
| `.example.com` | example.com + 所有子域名 |
| `example.com` | 仅精确匹配 |

!!! warning "注意"
    不支持 `*.example.com` 通配符语法，使用前导点 `.example.com`。

## 动手实践

### Step 1: 搭建 Squid 代理服务器

启动一台 EC2 实例，安装并配置 Squid（带 Basic Auth）：

```bash
# 安装 Squid 和密码工具
sudo yum update -y
sudo yum install -y squid httpd-tools

# 创建代理认证用户
sudo htpasswd -bc /etc/squid/proxy_users testuser 'TestP@ss123'

# 配置 Squid
sudo cat > /etc/squid/squid.conf << 'EOF'
auth_param basic program /usr/lib64/squid/basic_ncsa_auth /etc/squid/proxy_users
auth_param basic children 5
auth_param basic realm Proxy Authentication Required
auth_param basic credentialsttl 2 hours

acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all

http_port 3128
access_log /var/log/squid/access.log squid
EOF

sudo systemctl enable squid && sudo systemctl start squid
```

!!! danger "安全提醒"
    确保 Security Group **仅允许必要的源 IP** 访问 3128 端口，绝不使用 0.0.0.0/0。结合 Squid Basic Auth 做双重防护。

### Step 2: 在 Secrets Manager 中存储代理凭据

```bash
aws secretsmanager create-secret \
  --name "browser-proxy-creds" \
  --secret-string '{"username":"testuser","password":"TestP@ss123"}' \
  --region us-east-1
```

凭据格式要求：

| 字段 | 允许字符 |
|------|---------|
| `username` | 字母数字 + `@ . _ + = -` |
| `password` | 字母数字 + `@ . _ + = - ! # $ % & *` |

不允许：冒号 `:`、换行、空格、引号。

### Step 3: 验证 — 无代理 Session 的出口 IP

先建立基线，看不使用代理时的出口 IP：

```bash
aws bedrock-agentcore start-browser-session \
  --browser-identifier "aws.browser.v1" \
  --name "no-proxy-baseline" \
  --region us-east-1
```

通过 Playwright 连接后访问 `https://api.ipify.org`：

```
Browser IP (无代理): 54.158.19.128
```

### Step 4: 使用认证代理创建 Session

```bash
aws bedrock-agentcore start-browser-session \
  --browser-identifier "aws.browser.v1" \
  --name "proxy-auth-test" \
  --region us-east-1 \
  --proxy-configuration '{
    "proxies": [{
      "externalProxy": {
        "server": "<你的代理IP>",
        "port": 3128,
        "credentials": {
          "basicAuth": {
            "secretArn": "arn:aws:secretsmanager:us-east-1:<account-id>:secret:<secret-name>"
          }
        }
      }
    }]
  }'
```

验证出口 IP：

```
Browser IP (代理): 3.227.245.118  ← 代理服务器的公网 IP ✅
```

### Step 5: 域名路由测试

配置只有特定域名走代理：

```bash
aws bedrock-agentcore start-browser-session \
  --browser-identifier "aws.browser.v1" \
  --name "domain-routing-test" \
  --region us-east-1 \
  --proxy-configuration '{
    "proxies": [{
      "externalProxy": {
        "server": "<你的代理IP>",
        "port": 3128,
        "credentials": {
          "basicAuth": {
            "secretArn": "<secret-arn>"
          }
        },
        "domainPatterns": [".ipify.org", ".ifconfig.me"]
      }
    }],
    "bypass": {
      "domainPatterns": [".amazonaws.com"]
    }
  }'
```

### Step 6: Bypass 域名测试

配置默认走代理，但特定域名直连：

```bash
aws bedrock-agentcore start-browser-session \
  --browser-identifier "aws.browser.v1" \
  --name "bypass-test" \
  --region us-east-1 \
  --proxy-configuration '{
    "proxies": [{
      "externalProxy": {
        "server": "<你的代理IP>",
        "port": 3128,
        "credentials": {
          "basicAuth": {
            "secretArn": "<secret-arn>"
          }
        }
      }
    }],
    "bypass": {
      "domainPatterns": [".ipify.org"]
    }
  }'
```

## 测试结果

### 出口 IP 对比

| 测试场景 | 访问目标 | 出口 IP | 是否经过代理 |
|---------|---------|---------|------------|
| 无代理 baseline | api.ipify.org | 54.158.19.128 | ❌ |
| 认证代理 | api.ipify.org | 3.227.245.118 | ✅ |
| 域名路由 — 匹配域名 | api.ipify.org | 3.227.245.118 | ✅ |
| 域名路由 — 未匹配域名 | httpbin.org | 3.226.107.152 | ❌ |
| Bypass — 被 bypass 域名 | api.ipify.org | 44.213.40.103 | ❌ |
| Bypass — 非 bypass 域名 | ifconfig.me | 3.227.245.118 | ✅ |

**关键发现**：不使用代理时，AgentCore Browser 的出口 IP **每次 session 都不同**。测试中观察到 5 个不同的 IP（54.158.x、54.161.x、3.83.x、3.226.x、44.213.x），这直接验证了代理功能的业务价值。

### 边界行为测试

| 测试场景 | Session 创建 | 运行时行为 |
|---------|------------|-----------|
| 无效代理地址 (192.0.2.1:9999) | ✅ 成功 | `ERR_TUNNEL_CONNECTION_FAILED` |
| 错误凭据 | ✅ 成功 | `ERR_INVALID_AUTH_CREDENTIALS` |

这证实了 **fail-open** 行为：创建 session 时不验证代理连通性，错误在实际访问时才暴露。

### 凭据安全验证

通过 `get-browser-session` API 查看返回信息：

```json
{
  "proxyConfiguration": {
    "proxies": [{
      "externalProxy": {
        "server": "3.227.245.118",
        "port": 3128,
        "credentials": {
          "basicAuth": {
            "secretArn": "arn:aws:secretsmanager:us-east-1:595842667825:secret:browser-proxy-test-creds-MAAtwD"
          }
        }
      }
    }]
  }
}
```

API 仅返回 `secretArn`，不返回实际密码 ✅。

## 踩坑记录

!!! warning "Security Group 配置"
    AgentCore Browser session 的出站 IP **不可预测**，每次 session 可能使用不同的 AWS IP 段。配置代理 Security Group 时需要覆盖较宽的 AWS IP 范围。**生产环境建议**：
    
    1. 使用 VPC 部署 AgentCore（通过 VPC 配置），代理部署在同一 VPC 的私有子网 — 最安全
    2. 或结合代理自身的认证机制（Basic Auth）做双重防护
    
    已查文档确认：官方建议需要网络层控制时使用 VPC 部署。

!!! warning "Squid 配置注意事项"
    Squid 的默认配置（`http_access deny all`）会拒绝所有请求。配置 Basic Auth 时确保 `http_access allow authenticated` 在 `deny all` 之前。
    
    Squid 日志中会出现大量 `CONNECT 0.0.0.0:443` 请求（TCP_TUNNEL/503），这是 Chromium 内部的更新检查等请求，**不是配置错误**。

!!! info "代理配置不可变"
    代理配置在 session 创建时固定，运行时**无法修改**。如需更改代理设置，必须创建新 session。已查文档确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.micro（代理服务器） | $0.0104/hr | ~2 hr | $0.02 |
| Secrets Manager Secret | $0.40/月 | 1 个 | $0.01 |
| AgentCore Browser sessions | 按 session 计费 | ~7 个 | ~$3-5 |
| **合计** | | | **~$5** |

## 清理资源

```bash
# 1. 删除 Secrets Manager secret
aws secretsmanager delete-secret \
  --secret-id browser-proxy-creds \
  --force-delete-without-recovery \
  --region us-east-1

# 2. 终止 EC2 实例
aws ec2 terminate-instances \
  --instance-ids <instance-id> \
  --region us-east-1

# 3. 等待实例终止后删除 Security Group
aws ec2 delete-security-group \
  --group-id <sg-id> \
  --region us-east-1

# 4. 删除 Key Pair
aws ec2 delete-key-pair \
  --key-name proxy-test-key \
  --region us-east-1

# 5. 确认无残留 browser sessions
aws bedrock-agentcore list-browser-sessions \
  --browser-identifier "aws.browser.v1" \
  --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。特别注意 EC2 实例和 Secrets Manager secret 的持续计费。

## 结论与建议

### 适用场景

| 场景 | 推荐方案 |
|------|---------|
| IP allowlisting / 稳定出口 IP | ✅ 使用代理（推荐 VPC 内部署） |
| 企业合规审计 | ✅ 通过企业代理记录流量 |
| 地理内容访问 | ✅ 使用特定地区的代理 |
| 多租户隔离 | ✅ domainPatterns + 多代理 |
| 一般 Web 自动化 | ❌ 不需要代理，直连即可 |

### 生产环境建议

1. **使用 VPC 部署**：将 AgentCore Browser 和代理部署在同一 VPC，用 Security Group 精准控制，避免暴露代理到公网
2. **监控代理健康**：由于 fail-open 行为，代理故障不会阻止 session 创建，需要在应用层做健康检查
3. **凭据轮换**：利用 Secrets Manager 的轮换功能定期更新代理密码
4. **性能优化**：将 AWS 端点加入 bypass domains 以减少延迟

### 与 Browser Profiles 的关系

| 功能 | Browser Profiles | Browser Proxy |
|------|-----------------|---------------|
| 解决的问题 | 浏览器状态/Cookie 持久化 | 流量路由/出口 IP 控制 |
| 配置位置 | `profileConfiguration` | `proxyConfiguration` |
| 可同时使用 | ✅ 是 | ✅ 是 |

两者互补：Profile 管理"浏览器记住了什么"，Proxy 管理"流量从哪里出去"。

## 参考链接

- [Browser Proxies 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-proxies.html)
- [AgentCore Browser 快速入门](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-quickstart.html)
- [VPC 配置](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)
- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/02/bedrock-agentcore-browser-proxy/)
