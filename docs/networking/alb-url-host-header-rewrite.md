# ALB 原生 URL 和 Host Header 重写：告别额外代理层

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.15（含清理）
    - **Region**: us-east-1（所有商业区域可用）
    - **最后验证**: 2026-03-27

## 背景

在微服务架构中，一个常见需求是：**外部用统一 URL 访问，内部按路径重写后分发到不同后端服务**。以前要实现这个，你需要：

- 额外部署 NGINX/Envoy 做反向代理
- 在应用代码中手动处理路径转换
- 或者使用 ALB 的 redirect（但会暴露内部 URL 给客户端）

现在，ALB 原生支持 **URL Rewrite** 和 **Host Header Rewrite**，基于正则表达式匹配，在请求到达后端之前直接完成重写。无需额外组件，无额外费用。

本文通过 10 个实测场景，带你完整体验这个功能的能力和边界。

## 前置条件

- AWS 账号（需要 `elasticloadbalancing:*` 和 `ec2:*` 权限）
- AWS CLI v2 已配置
- 一个 VPC（可使用默认 VPC）
- 至少两个可用区的子网

## 核心概念

### Rewrite vs Redirect

| 特性 | URL Redirect（已有） | URL Rewrite（新功能） |
|------|---------------------|---------------------|
| 浏览器地址栏 | 变化（客户端可见） | **不变（完全透明）** |
| HTTP 状态码 | 301/302 | 无变化（200） |
| 处理位置 | 客户端（浏览器发起二次请求） | **服务端（ALB 直接重写）** |
| 典型场景 | 域名迁移、链接修复 | 微服务路由、API 版本迁移、路径标准化 |
| 后端感知 | 收到新 URL 的独立请求 | **收到重写后的 URL，对客户端透明** |

### Transform 类型

ALB 支持两种 transform，可在同一条 rule 中同时使用：

- **`url-rewrite`**：重写请求 URL 的路径和查询字符串（不能改 protocol/port）
- **`host-header-rewrite`**：重写请求的 Host 头

### 关键限制

| 限制 | 说明 |
|------|------|
| 每条 rule 最多 | 1 个 url-rewrite + 1 个 host-header-rewrite |
| Default rule | ❌ 不支持添加 transform |
| URL rewrite 范围 | 只能改 path + query string |
| 正则不匹配时 | 原始请求透传给 target |
| 额外费用 | 无，按 ALB 标准定价 |

## 动手实践

### Step 1: 准备基础设施

首先创建一个简单的 echo HTTP 服务器作为后端——它会返回收到的 URL 和 Host Header，方便我们验证 rewrite 效果。

**创建 Security Group**（⚠️ 仅允许你的 IP 访问，禁止 0.0.0.0/0）：

```bash
# 创建 ALB 用的 Security Group
ALB_SG=$(aws ec2 create-security-group \
  --group-name alb-rewrite-test-sg \
  --description "ALB rewrite test" \
  --vpc-id <your-vpc-id> \
  --region us-east-1 \
  --query GroupId --output text)

# 仅允许你的 IP 访问（替换为你的公网 IP）
MY_IP=$(curl -s ifconfig.me)
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG \
  --protocol tcp --port 80 \
  --cidr ${MY_IP}/32 \
  --region us-east-1

# 创建 EC2 用的 Security Group（仅允许 ALB 访问）
EC2_SG=$(aws ec2 create-security-group \
  --group-name ec2-rewrite-test-sg \
  --description "EC2 rewrite test - ALB only" \
  --vpc-id <your-vpc-id> \
  --region us-east-1 \
  --query GroupId --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $EC2_SG \
  --protocol tcp --port 8080 \
  --source-group $ALB_SG \
  --region us-east-1
```

**启动 Echo 服务器**：

创建 user-data 脚本 `echo-userdata.sh`：

```bash
#!/bin/bash
cat > /tmp/echo-server.py << PYEOF
#!/usr/bin/env python3
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        response = {
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "headers": dict(self.headers),
            "method": "GET"
        }
        body = json.dumps(response, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, format, *args):
        pass

HTTPServer(("0.0.0.0", 8080), EchoHandler).serve_forever()
PYEOF
nohup python3 /tmp/echo-server.py > /dev/null 2>&1 &
```

启动 EC2 实例：

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0c421724a94bba6d6 \
  --instance-type t3.micro \
  --security-group-ids $EC2_SG \
  --subnet-id <your-subnet-id> \
  --user-data file://echo-userdata.sh \
  --tag-specifications ResourceType=instance,Tags=[Key=Name] \
  --region us-east-1 \
  --query Instances[0].InstanceId --output text)
```

### Step 2: 创建 ALB 和 Target Group

```bash
# 创建 Target Group
TG_ARN=$(aws elbv2 create-target-group \
  --name alb-rewrite-test-tg \
  --protocol HTTP --port 8080 \
  --vpc-id <your-vpc-id> \
  --target-type instance \
  --health-check-path / \
  --region us-east-1 \
  --query TargetGroups[0].TargetGroupArn --output text)

# 注册 target
aws elbv2 register-targets \
  --target-group-arn $TG_ARN \
  --targets Id=$INSTANCE_ID \
  --region us-east-1

# 创建 ALB（至少两个 AZ）
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name alb-rewrite-test \
  --subnets <subnet-az1> <subnet-az2> \
  --security-groups $ALB_SG \
  --scheme internet-facing \
  --type application \
  --region us-east-1 \
  --query LoadBalancers[0].LoadBalancerArn --output text)

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns $ALB_ARN \
  --region us-east-1 \
  --query LoadBalancers[0].DNSName --output text)

# 创建 Listener
LISTENER_ARN=$(aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN \
  --region us-east-1 \
  --query Listeners[0].ListenerArn --output text)
```

等待 Target 变为 healthy（约 60-90 秒）：

```bash
watch -n 5 "aws elbv2 describe-target-health \
  --target-group-arn $TG_ARN \
  --region us-east-1 \
  --query TargetHealthDescriptions[0].TargetHealth.State \
  --output text"
```

验证基础设施：

```bash
curl -s http://$ALB_DNS/
# 应返回 JSON，显示 path="/" 和完整 headers
```

### Step 3: 配置 URL Rewrite

**场景：API 版本迁移** — 将 `/api/v1/*` 透明重写为 `/v2/*`。

创建 `url-rewrite-rule.json`：

```json
{
  "ListenerArn": "<your-listener-arn>",
  "Priority": 10,
  "Conditions": [
    {
      "Field": "path-pattern",
      "PathPatternConfig": {
        "Values": ["/api/v1/*"]
      }
    }
  ],
  "Actions": [
    {
      "Type": "forward",
      "TargetGroupArn": "<your-tg-arn>"
    }
  ],
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [
          {
            "Regex": "^/api/v1/(.*)$",
            "Replace": "/v2/$1"
          }
        ]
      }
    }
  ]
}
```

```bash
aws elbv2 create-rule \
  --cli-input-json file://url-rewrite-rule.json \
  --region us-east-1
```

**验证**（等待 5-10 秒让规则传播）：

```bash
# 客户端请求 /api/v1/users
curl -s http://$ALB_DNS/api/v1/users | jq .path
# 输出: "/v2/users" ← 后端收到重写后的路径！
```

### Step 4: 配置 Host Header Rewrite

**场景：外部域名映射到内部服务域名**。

```json
{
  "ListenerArn": "<your-listener-arn>",
  "Priority": 20,
  "Conditions": [
    {
      "Field": "path-pattern",
      "PathPatternConfig": { "Values": ["/host-test/*"] }
    }
  ],
  "Actions": [
    {
      "Type": "forward",
      "TargetGroupArn": "<your-tg-arn>"
    }
  ],
  "Transforms": [
    {
      "Type": "host-header-rewrite",
      "HostHeaderRewriteConfig": {
        "Rewrites": [
          {
            "Regex": "^(.*)$",
            "Replace": "internal.svc.local"
          }
        ]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/host-test/page1 | jq .host
# 输出: "internal.svc.local" ← Host header 被重写！
```

### Step 5: 两种 Transform 同时使用

一条 rule 可以同时配置 URL rewrite 和 Host header rewrite：

```json
{
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [{ "Regex": "^/combo/(.*)$", "Replace": "/internal/$1" }]
      }
    },
    {
      "Type": "host-header-rewrite",
      "HostHeaderRewriteConfig": {
        "Rewrites": [{ "Regex": "^(.*)$", "Replace": "backend.internal" }]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/combo/service-a/data | jq {path, cat > /home/ubuntu/chaosreload/study/repo/chaosreload/aws-hands-on-lab/docs/networking/alb-url-host-header-rewrite.md << 'ARTICLE_EOF'
# ALB 原生 URL 和 Host Header 重写：告别额外代理层

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.15（含清理）
    - **Region**: us-east-1（所有商业区域可用）
    - **最后验证**: 2026-03-27

## 背景

在微服务架构中，一个常见需求是：**外部用统一 URL 访问，内部按路径重写后分发到不同后端服务**。以前要实现这个，你需要：

- 额外部署 NGINX/Envoy 做反向代理
- 在应用代码中手动处理路径转换
- 或者使用 ALB 的 redirect（但会暴露内部 URL 给客户端）

现在，ALB 原生支持 **URL Rewrite** 和 **Host Header Rewrite**，基于正则表达式匹配，在请求到达后端之前直接完成重写。无需额外组件，无额外费用。

本文通过 10 个实测场景，带你完整体验这个功能的能力和边界。

## 前置条件

- AWS 账号（需要 `elasticloadbalancing:*` 和 `ec2:*` 权限）
- AWS CLI v2 已配置
- 一个 VPC（可使用默认 VPC）
- 至少两个可用区的子网

## 核心概念

### Rewrite vs Redirect

| 特性 | URL Redirect（已有） | URL Rewrite（新功能） |
|------|---------------------|---------------------|
| 浏览器地址栏 | 变化（客户端可见） | **不变（完全透明）** |
| HTTP 状态码 | 301/302 | 无变化（200） |
| 处理位置 | 客户端（浏览器发起二次请求） | **服务端（ALB 直接重写）** |
| 典型场景 | 域名迁移、链接修复 | 微服务路由、API 版本迁移、路径标准化 |
| 后端感知 | 收到新 URL 的独立请求 | **收到重写后的 URL，对客户端透明** |

### Transform 类型

ALB 支持两种 transform，可在同一条 rule 中同时使用：

- **`url-rewrite`**：重写请求 URL 的路径和查询字符串（不能改 protocol/port）
- **`host-header-rewrite`**：重写请求的 Host 头

### 关键限制

| 限制 | 说明 |
|------|------|
| 每条 rule 最多 | 1 个 url-rewrite + 1 个 host-header-rewrite |
| Default rule | ❌ 不支持添加 transform |
| URL rewrite 范围 | 只能改 path + query string |
| 正则不匹配时 | 原始请求透传给 target |
| 额外费用 | 无，按 ALB 标准定价 |

## 动手实践

### Step 1: 准备基础设施

首先创建一个简单的 echo HTTP 服务器作为后端——它会返回收到的 URL 和 Host Header，方便我们验证 rewrite 效果。

**创建 Security Group**（⚠️ 仅允许你的 IP 访问，禁止 0.0.0.0/0）：

```bash
# 创建 ALB 用的 Security Group
ALB_SG=$(aws ec2 create-security-group \
  --group-name alb-rewrite-test-sg \
  --description "ALB rewrite test" \
  --vpc-id <your-vpc-id> \
  --region us-east-1 \
  --query GroupId --output text)

# 仅允许你的 IP 访问（替换为你的公网 IP）
MY_IP=$(curl -s ifconfig.me)
aws ec2 authorize-security-group-ingress \
  --group-id $ALB_SG \
  --protocol tcp --port 80 \
  --cidr ${MY_IP}/32 \
  --region us-east-1

# 创建 EC2 用的 Security Group（仅允许 ALB 访问）
EC2_SG=$(aws ec2 create-security-group \
  --group-name ec2-rewrite-test-sg \
  --description "EC2 rewrite test - ALB only" \
  --vpc-id <your-vpc-id> \
  --region us-east-1 \
  --query GroupId --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $EC2_SG \
  --protocol tcp --port 8080 \
  --source-group $ALB_SG \
  --region us-east-1
```

**启动 Echo 服务器**：

创建 user-data 脚本 `echo-userdata.sh`：

```bash
#!/bin/bash
cat > /tmp/echo-server.py << PYEOF
#!/usr/bin/env python3
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        response = {
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "headers": dict(self.headers),
            "method": "GET"
        }
        body = json.dumps(response, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, format, *args):
        pass

HTTPServer(("0.0.0.0", 8080), EchoHandler).serve_forever()
PYEOF
nohup python3 /tmp/echo-server.py > /dev/null 2>&1 &
```

启动 EC2 实例：

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0c421724a94bba6d6 \
  --instance-type t3.micro \
  --security-group-ids $EC2_SG \
  --subnet-id <your-subnet-id> \
  --user-data file://echo-userdata.sh \
  --tag-specifications ResourceType=instance,Tags=[Value=alb-rewrite-echo-server] \
  --region us-east-1 \
  --query Instances[0].InstanceId --output text)
```

### Step 2: 创建 ALB 和 Target Group

```bash
# 创建 Target Group
TG_ARN=$(aws elbv2 create-target-group \
  --name alb-rewrite-test-tg \
  --protocol HTTP --port 8080 \
  --vpc-id <your-vpc-id> \
  --target-type instance \
  --health-check-path / \
  --region us-east-1 \
  --query TargetGroups[0].TargetGroupArn --output text)

# 注册 target
aws elbv2 register-targets \
  --target-group-arn $TG_ARN \
  --targets Id=$INSTANCE_ID \
  --region us-east-1

# 创建 ALB（至少两个 AZ）
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name alb-rewrite-test \
  --subnets <subnet-az1> <subnet-az2> \
  --security-groups $ALB_SG \
  --scheme internet-facing \
  --type application \
  --region us-east-1 \
  --query LoadBalancers[0].LoadBalancerArn --output text)

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns $ALB_ARN \
  --region us-east-1 \
  --query LoadBalancers[0].DNSName --output text)

# 创建 Listener
LISTENER_ARN=$(aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN \
  --region us-east-1 \
  --query Listeners[0].ListenerArn --output text)
```

等待 Target 变为 healthy（约 60-90 秒）：

```bash
watch -n 5 "aws elbv2 describe-target-health \
  --target-group-arn $TG_ARN \
  --region us-east-1 \
  --query TargetHealthDescriptions[0].TargetHealth.State \
  --output text"
```

验证基础设施：

```bash
curl -s http://$ALB_DNS/
# 应返回 JSON，显示 path="/" 和完整 headers
```

### Step 3: 配置 URL Rewrite

**场景：API 版本迁移** — 将 `/api/v1/*` 透明重写为 `/v2/*`。

创建 `url-rewrite-rule.json`：

```json
{
  "ListenerArn": "<your-listener-arn>",
  "Priority": 10,
  "Conditions": [
    {
      "Field": "path-pattern",
      "PathPatternConfig": {
        "Values": ["/api/v1/*"]
      }
    }
  ],
  "Actions": [
    {
      "Type": "forward",
      "TargetGroupArn": "<your-tg-arn>"
    }
  ],
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [
          {
            "Regex": "^/api/v1/(.*)$",
            "Replace": "/v2/$1"
          }
        ]
      }
    }
  ]
}
```

```bash
aws elbv2 create-rule \
  --cli-input-json file://url-rewrite-rule.json \
  --region us-east-1
```

**验证**（等待 5-10 秒让规则传播）：

```bash
# 客户端请求 /api/v1/users
curl -s http://$ALB_DNS/api/v1/users | jq .path
# 输出: "/v2/users" ← 后端收到重写后的路径！
```

### Step 4: 配置 Host Header Rewrite

**场景：外部域名映射到内部服务域名**。

```json
{
  "ListenerArn": "<your-listener-arn>",
  "Priority": 20,
  "Conditions": [
    {
      "Field": "path-pattern",
      "PathPatternConfig": { "Values": ["/host-test/*"] }
    }
  ],
  "Actions": [
    {
      "Type": "forward",
      "TargetGroupArn": "<your-tg-arn>"
    }
  ],
  "Transforms": [
    {
      "Type": "host-header-rewrite",
      "HostHeaderRewriteConfig": {
        "Rewrites": [
          {
            "Regex": "^(.*)$",
            "Replace": "internal.svc.local"
          }
        ]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/host-test/page1 | jq .host
# 输出: "internal.svc.local" ← Host header 被重写！
```

### Step 5: 两种 Transform 同时使用

一条 rule 可以同时配置 URL rewrite 和 Host header rewrite：

```json
{
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [{ "Regex": "^/combo/(.*)$", "Replace": "/internal/$1" }]
      }
    },
    {
      "Type": "host-header-rewrite",
      "HostHeaderRewriteConfig": {
        "Rewrites": [{ "Regex": "^(.*)$", "Replace": "backend.internal" }]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/combo/service-a/data | jq {path, host}
# 输出:
# {
#   "path": "/internal/service-a/data",
#   "host": "backend.internal"
# }
```

### Step 6: 使用 Condition Regex（新功能）

除了 transform 支持正则，**条件（Condition）本身也新增了正则匹配**。

```json
{
  "Conditions": [
    {
      "Field": "path-pattern",
      "PathPatternConfig": {
        "RegexValues": ["^/lang/(en|fr|de)/(.*)$"]
      }
    }
  ],
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [{
          "Regex": "^/lang/(en|fr|de)/(.*)$",
          "Replace": "/$2?locale=$1"
        }]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/lang/en/about | jq .path
# "/about?locale=en"

curl -s http://$ALB_DNS/lang/fr/contact | jq .path
# "/contact?locale=fr"

curl -s http://$ALB_DNS/lang/es/about | jq .path
# "/lang/es/about" ← 不在 regex 范围内，走 default rule
```

### Step 7: 多捕获组实战

**场景：将 RESTful 路径转为查询参数**。

```json
{
  "Transforms": [
    {
      "Type": "url-rewrite",
      "UrlRewriteConfig": {
        "Rewrites": [{
          "Regex": "^/products/([^/]+)/reviews/([0-9]+)$",
          "Replace": "/api/reviews?item=$1&review_id=$2"
        }]
      }
    }
  ]
}
```

```bash
curl -s http://$ALB_DNS/products/laptop-x1/reviews/42 | jq .path
# "/api/reviews?item=laptop-x1&review_id=42"
```

## 测试结果

### 完整测试矩阵

| # | 测试场景 | 输入 | 期望输出 | 实际输出 | 结果 |
|---|---------|------|---------|---------|------|
| 1 | URL path rewrite | `/api/v1/users` | `/v2/users` | `/v2/users` | ✅ |
| 2 | Host header rewrite | host=ALB DNS | host=`internal.svc.local` | `internal.svc.local` | ✅ |
| 3 | 双 transform 同时 | `/combo/svc-a/data` | path+host 都重写 | 都正确重写 | ✅ |
| 4 | Rewrite vs Redirect | 同路径 | rewrite=200; redirect=301 | 如预期 | ✅ |
| 5 | Regex 不匹配 | `/nomatch-test/page1` | 原始请求透传 | 未变化 | ✅ |
| 6 | 不存在的捕获组 $9 | `/fail-test/hello` | 文档说 HTTP 500 | **200, $9→空字符串** | ⚠️ |
| 7 | Default rule 加 transform | modify default rule | 报错 | OperationNotPermitted | ✅ |
| 8 | 多捕获组 $1,$2 | `/products/x/reviews/42` | `/api/reviews?item=x&review_id=42` | 正确 | ✅ |
| 9 | Condition RegexValues | `/lang/en/about` | `/about?locale=en` | 正确 | ✅ |
| 10 | Query string 处理 | `/api/v1/users?page=2` | `/v2/users?page=2` | 正确 | ✅ |

### Rewrite vs Redirect 行为对比

```
# Redirect: 客户端看到 301，浏览器地址栏变化
$ curl -I http://$ALB_DNS/redirect-test/page1
HTTP/1.1 301 Moved Permanently
Location: http://ALB-DNS:80/redirected/redirect-test/page1

# Rewrite: 客户端看到 200，完全透明
$ curl -s http://$ALB_DNS/api/v1/users
HTTP 200 → path=/v2/users（客户端无感知）
```

## 踩坑记录

!!! warning "踩坑 1：正则匹配范围包含 Query String"
    **现象**：regex `^/products/([^/]+)/reviews/([0-9]+)$` 对 `/products/x/reviews/42` 生效，但对 `/products/x/reviews/42?format=json` **不生效**。

    **原因**：URL rewrite 的正则匹配范围是**完整请求 URI（path + query string）**，而不仅仅是 path。`$` 锚点在有 query string 时会匹配失败。

    **解决方案**：如果需要兼容带/不带 query string，将正则改为 `^/products/([^/]+)/reviews/([0-9]+)(\?.*)?$`，或去掉 `$` 锚点。

    **状态**：⚠️ 实测发现，官方文档未明确记录此行为。

!!! warning "踩坑 2：不存在的捕获组不会报错"
    **现象**：Replace 中引用 `$9`，但正则只有 1 个捕获组。预期按文档应返回 HTTP 500。

    **实际**：返回 200，`$9` 被替换为空字符串。

    **影响**：不会导致服务中断，但可能产生意想不到的重写结果。检查 replace 模板中的捕获组引用是否正确。

    **状态**：⚠️ 实测行为与文档描述有差异。文档说"transform fails → HTTP 500"，但此情况不被视为 failure。

!!! warning "踩坑 3：规则传播需要几秒"
    **现象**：create-rule 后立即 curl 测试，rewrite 未生效。

    **原因**：新规则需要 5-10 秒传播到所有 ALB 节点。

    **建议**：自动化测试脚本中，在创建规则后 sleep 10 秒再验证。

!!! tip "技巧：Default Rule 的限制"
    Default rule 不支持 transform（API 直接拒绝），也不支持 modify-rule。如果需要对"所有请求"做重写，创建一条 catch-all 规则（如 `path-pattern: /*`）并设置最低优先级。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ALB | $0.0225/hr | ~2 hr | $0.045 |
| EC2 t3.micro | $0.0104/hr | ~2 hr | $0.021 |
| Transform 功能 | $0 | - | $0 |
| **合计** | | | **< $0.10** |

## 清理资源

```bash
# 1. 删除所有 listener rules（保留 default rule）
for RULE_ARN in $(aws elbv2 describe-rules \
  --listener-arn $LISTENER_ARN \
  --region us-east-1 \
  --query Rules[?!IsDefault].RuleArn --output text); do
  aws elbv2 delete-rule --rule-arn $RULE_ARN --region us-east-1
  echo "Deleted rule: $RULE_ARN"
done

# 2. 删除 Listener
aws elbv2 delete-listener --listener-arn $LISTENER_ARN --region us-east-1

# 3. 删除 ALB
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN --region us-east-1

# 4. 等待 ALB 完全删除
echo "Waiting for ALB deletion..."
aws elbv2 wait load-balancers-deleted --load-balancer-arns $ALB_ARN --region us-east-1

# 5. 删除 Target Group
aws elbv2 delete-target-group --target-group-arn $TG_ARN --region us-east-1

# 6. 终止 EC2 实例
aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region us-east-1
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID --region us-east-1

# 7. 删除 Security Groups（等 ENI 释放）
echo "Waiting 60s for ENIs to release..."
sleep 60
aws ec2 delete-security-group --group-id $EC2_SG --region us-east-1
aws ec2 delete-security-group --group-id $ALB_SG --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ALB 按小时计费（$0.0225/hr）。

## 结论与建议

### 适用场景

| 场景 | 推荐方案 |
|------|---------|
| API 版本迁移（v1→v2） | ✅ URL rewrite — 前端零改动 |
| 微服务路径前缀剥离 | ✅ URL rewrite — 替代 NGINX Ingress |
| 外部域名→内部服务映射 | ✅ Host header rewrite |
| 永久域名迁移（SEO） | ❌ 仍用 redirect（需要 301） |
| 协议升级（HTTP→HTTPS） | ❌ 用 redirect（rewrite 不能改 protocol） |

### 生产环境建议

1. **Regex 要考虑 query string**：如果你的用户请求可能带 query string，避免在 regex 末尾使用 `$` 锚点
2. **测试 replace 模板**：确保捕获组引用（$1, $2...）与 regex 中的分组数量匹配
3. **监控 5xx**：虽然未能在测试中触发 transform 的 HTTP 500，但仍建议通过 ALB 的 HTTPCode_ELB_5XX_Count 指标监控
4. **规则传播延迟**：生产环境部署后，建议等待 10+ 秒再进行流量切换验证
5. **EKS 用户**：考虑用此功能替代 NGINX Ingress Controller 的 path rewrite annotation

## 参考链接

- [ALB Transforms 文档](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-transforms.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/10/application-load-balancer-url-header-rewrite/)
- [AWS Blog: Introducing URL and host header rewrite](https://aws.amazon.com/blogs/networking-and-content-delivery/introducing-url-and-host-header-rewrite-with-aws-application-load-balancers)
- [ALB create-rule CLI 参考](https://docs.aws.amazon.com/cli/latest/reference/elbv2/create-rule.html)
