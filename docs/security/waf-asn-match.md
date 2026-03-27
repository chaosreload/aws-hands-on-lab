# AWS WAF ASN Match 实战：基于自治系统编号的精准流量控制

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $0.50（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

在 Web 安全防护中，封锁恶意流量的传统方式是维护 IP 黑名单。但 IP 地址频繁变化，一个大型 ISP 或云服务商可能拥有数以万计的 IP 段。AWS WAF 新增的 **ASN Match Statement** 让你可以直接基于 ASN（自治系统编号）来匹配和过滤流量 —— 一条规则就能覆盖整个网络组织的所有 IP，无需维护庞大的 IP 列表。

**典型使用场景**：

- 封锁特定 ISP / 托管商 / 云服务商的流量
- 仅允许来自可信合作伙伴网络的访问
- 结合 Rate-based 规则，按 ASN 维度做流量限速

## 前置条件

- AWS 账号（需要 WAF、ELBv2、Lambda、IAM 权限）
- AWS CLI v2 已配置
- 一个可用的 ALB（本文会创建测试用 ALB）

## 核心概念

### ASN 是什么？

ASN（Autonomous System Number）是分配给大型网络的唯一标识符。每个 ISP、大型企业、云服务商都有自己的 ASN。例如：

- **AS16509** — Amazon.com, Inc.
- **AS15169** — Google LLC
- **AS13335** — Cloudflare, Inc.

### ASN Match vs IP Set vs Geo Match

| 维度 | ASN Match | IP Set | Geo Match |
|------|-----------|--------|-----------|
| **粒度** | 网络组织级别 | 单 IP/CIDR | 国家/地区 |
| **最大条目** | 100 ASN/规则 | 10,000 IP/IP Set | 50 国家/规则 |
| **WCU** | 1 | 1 | 1 |
| **维护成本** | 低（ASN 稳定） | 高（IP 频繁变化） | 低 |
| **适用场景** | 封锁特定 ISP/云商 | 精确 IP 控制 | 合规/地理封锁 |
| **Rate-based 聚合** | ✅ | ✅ | ❌ |

### 关键限制

- 每条规则最多 **100 个 ASN**
- ASN 有效范围：**0 ~ 4,294,967,295**
- **ASN 0** 代表无法映射 ASN 的 IP（特殊值）
- WCU 消耗：ASN Match 规则 = 1 WCU；Rate-based + ASN 聚合 = 32 WCU

## 动手实践

### Step 1: 准备测试环境

创建 Lambda 后端 + ALB 作为测试目标。

```bash
# 设置环境变量
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export VPC_ID=your-vpc-id  # 使用默认 VPC 即可
```

创建 Lambda 函数：

```bash
# 创建 Lambda 代码
cat > /tmp/waf-asn-lambda.py << 'EOF'
import json
def handler(event, context):
    return {
        "statusCode": 200,
        "statusDescription": "200 OK",
        "isBase64Encoded": False,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "message": "Hello from WAF ASN Test!",
            "sourceIp": event.get("headers", {}).get("x-forwarded-for", "unknown")
        })
    }
EOF

cd /tmp && zip -j waf-asn-lambda.zip waf-asn-lambda.py

# 创建 IAM 角色
aws iam create-role --role-name waf-asn-test-lambda-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

sleep 10  # 等待角色传播

# 创建 Lambda 函数
ROLE_ARN=$(aws iam get-role --role-name waf-asn-test-lambda-role \
  --query 'Role.Arn' --output text)
aws lambda create-function \
  --function-name waf-asn-test-handler \
  --runtime python3.12 \
  --handler waf-asn-lambda.handler \
  --role $ROLE_ARN \
  --zip-file fileb:///tmp/waf-asn-lambda.zip \
  --timeout 10 --memory-size 128
```

创建 Security Group 和 ALB：

```bash
# 获取你的公网 IP
MY_IP=$(curl -s https://checkip.amazonaws.com)

# 创建 Security Group（仅允许你的 IP 访问，不要用 0.0.0.0/0）
SG_ID=$(aws ec2 create-security-group \
  --group-name waf-asn-test-sg \
  --description "WAF ASN test ALB SG" \
  --vpc-id $VPC_ID \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID --protocol tcp --port 80 --cidr ${MY_IP}/32

# 创建 ALB（选择 2 个 AZ 的子网）
SUBNETS="subnet-aaaa subnet-bbbb"  # 替换为你的公有子网
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name waf-asn-test-alb \
  --subnets $SUBNETS \
  --security-groups $SG_ID \
  --scheme internet-facing --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# 创建 Target Group + 注册 Lambda
TG_ARN=$(aws elbv2 create-target-group \
  --name waf-asn-test-tg --target-type lambda \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

LAMBDA_ARN=$(aws lambda get-function --function-name waf-asn-test-handler \
  --query 'Configuration.FunctionArn' --output text)

aws lambda add-permission --function-name waf-asn-test-handler \
  --statement-id alb-invoke --action lambda:InvokeFunction \
  --principal elasticloadbalancing.amazonaws.com

aws elbv2 register-targets --target-group-arn $TG_ARN \
  --targets Id=$LAMBDA_ARN

# 创建 Listener
aws elbv2 create-listener --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN

# 等待 ALB 就绪
aws elbv2 wait load-balancer-available --load-balancer-arns $ALB_ARN

# 获取 ALB DNS 名称
ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns $ALB_ARN \
  --query 'LoadBalancers[0].DNSName' --output text)
echo "ALB ready: http://$ALB_DNS"
```

验证 ALB 正常工作：

```bash
curl http://$ALB_DNS/
# 预期输出: {"message": "Hello from WAF ASN Test!", "sourceIp": "x.x.x.x"}
```

### Step 2: 查找你的 ASN

```bash
curl -s https://ipinfo.io | grep org
# 输出示例: "org": "AS16509 Amazon.com, Inc."
# 你的 ASN 就是 16509
```

### Step 3: 创建 ASN Match Block 规则

创建 WebACL，封锁你的 ASN（用于验证功能）：

```bash
cat > /tmp/waf-asn-block.json << 'EOF'
{
  "Name": "waf-asn-test-block",
  "Scope": "REGIONAL",
  "DefaultAction": {"Allow": {}},
  "Rules": [
    {
      "Name": "block-my-asn",
      "Priority": 1,
      "Statement": {
        "AsnMatchStatement": {
          "AsnList": [16509]
        }
      },
      "Action": {"Block": {}},
      "VisibilityConfig": {
        "SampledRequestsEnabled": true,
        "CloudWatchMetricsEnabled": true,
        "MetricName": "block-my-asn"
      }
    }
  ],
  "VisibilityConfig": {
    "SampledRequestsEnabled": true,
    "CloudWatchMetricsEnabled": true,
    "MetricName": "waf-asn-test-block"
  }
}
EOF

# 创建 WebACL
ACL_ARN=$(aws wafv2 create-web-acl \
  --cli-input-json file:///tmp/waf-asn-block.json \
  --query 'Summary.ARN' --output text)

# 关联到 ALB（新建 WebACL 需要等待 30-120 秒）
sleep 30
aws wafv2 associate-web-acl --web-acl-arn $ACL_ARN --resource-arn $ALB_ARN
```

验证 Block 效果：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://$ALB_DNS/
# 预期: 403 — 你的 ASN 被封锁
```

### Step 4: ASN + Geo Match 组合规则

ASN Match 支持嵌套，可以和 Geo Match 组合使用 AND/OR 逻辑：

```bash
cat > /tmp/waf-asn-and-geo.json << 'EOF'
{
  "Name": "waf-asn-test-and-geo",
  "Scope": "REGIONAL",
  "DefaultAction": {"Allow": {}},
  "Rules": [
    {
      "Name": "block-asn-and-country",
      "Priority": 1,
      "Statement": {
        "AndStatement": {
          "Statements": [
            {
              "AsnMatchStatement": {
                "AsnList": [16509]
              }
            },
            {
              "GeoMatchStatement": {
                "CountryCodes": ["SG"]
              }
            }
          ]
        }
      },
      "Action": {"Block": {}},
      "VisibilityConfig": {
        "SampledRequestsEnabled": true,
        "CloudWatchMetricsEnabled": true,
        "MetricName": "block-asn-and-country"
      }
    }
  ],
  "VisibilityConfig": {
    "SampledRequestsEnabled": true,
    "CloudWatchMetricsEnabled": true,
    "MetricName": "waf-asn-test-and-geo"
  }
}
EOF

aws wafv2 create-web-acl --cli-input-json file:///tmp/waf-asn-and-geo.json
```

### Step 5: ForwardedIPConfig — 从 X-Forwarded-For 提取 ASN

当流量经过 CDN 或代理时，真实客户端 IP 在 `X-Forwarded-For` header 中：

```bash
cat > /tmp/waf-asn-xff.json << 'EOF'
{
  "Name": "waf-asn-test-xff",
  "Scope": "REGIONAL",
  "DefaultAction": {"Allow": {}},
  "Rules": [
    {
      "Name": "block-asn-via-xff",
      "Priority": 1,
      "Statement": {
        "AsnMatchStatement": {
          "AsnList": [16509],
          "ForwardedIPConfig": {
            "HeaderName": "X-Forwarded-For",
            "FallbackBehavior": "NO_MATCH"
          }
        }
      },
      "Action": {"Block": {}},
      "VisibilityConfig": {
        "SampledRequestsEnabled": true,
        "CloudWatchMetricsEnabled": true,
        "MetricName": "block-asn-via-xff"
      }
    }
  ],
  "VisibilityConfig": {
    "SampledRequestsEnabled": true,
    "CloudWatchMetricsEnabled": true,
    "MetricName": "waf-asn-test-xff"
  }
}
EOF
```

验证 ForwardedIPConfig 行为：

```bash
# 带 Amazon IP 的 XFF → 403（ASN 16509 匹配）
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Forwarded-For: 54.239.28.85" http://$ALB_DNS/

# 带 Google IP 的 XFF → 200（ASN 15169 不匹配）
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Forwarded-For: 8.8.8.8" http://$ALB_DNS/

# 无 XFF header → 200（FallbackBehavior=NO_MATCH）
curl -s -o /dev/null -w "%{http_code}\n" http://$ALB_DNS/
```

### Step 6: Rate-based + ASN 聚合

以 ASN 作为聚合维度做频率限制：

```bash
cat > /tmp/waf-asn-rate.json << 'EOF'
{
  "Name": "waf-asn-test-rate",
  "Scope": "REGIONAL",
  "DefaultAction": {"Allow": {}},
  "Rules": [
    {
      "Name": "rate-limit-by-asn",
      "Priority": 1,
      "Statement": {
        "RateBasedStatement": {
          "Limit": 100,
          "EvaluationWindowSec": 60,
          "AggregateKeyType": "CUSTOM_KEYS",
          "CustomKeys": [
            {
              "ASN": {}
            }
          ]
        }
      },
      "Action": {"Block": {}},
      "VisibilityConfig": {
        "SampledRequestsEnabled": true,
        "CloudWatchMetricsEnabled": true,
        "MetricName": "rate-limit-by-asn"
      }
    }
  ],
  "VisibilityConfig": {
    "SampledRequestsEnabled": true,
    "CloudWatchMetricsEnabled": true,
    "MetricName": "waf-asn-test-rate"
  }
}
EOF
```

验证 WCU 消耗：

```bash
aws wafv2 check-capacity --scope REGIONAL \
  --rules '[{"Name":"rate-by-asn","Priority":1,"Statement":{"RateBasedStatement":{"Limit":100,"EvaluationWindowSec":60,"AggregateKeyType":"CUSTOM_KEYS","CustomKeys":[{"ASN":{}}]}},"Action":{"Block":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"rate-by-asn"}}]'
# 输出: {"Capacity": 32}  — 2(base) + 30(ASN key) = 32 WCU
```

## 测试结果

### 功能验证汇总

| # | 测试场景 | 预期 | 实际 | 状态 |
|---|---------|------|------|------|
| 1 | ASN Match Block（封锁 ASN 16509） | 403 | 403 | ✅ |
| 2 | ASN Match Allow + 默认 Block | 200 | 200 | ✅ |
| 3 | AND(ASN + Geo) 组合规则 | 403 | 403 | ✅ |
| 4 | Rate-based + ASN 聚合（WCU 验证） | 32 WCU | 32 WCU | ✅ |
| 5 | ASN 0 不匹配已知 ASN | 200 | 200 | ✅ |
| 6 | 100 ASN 上限（100 通过 / 101 拒绝） | 100✅ 101❌ | 100✅ 101❌ | ✅ |
| 7 | ForwardedIPConfig（XFF header） | 按 XFF 中 IP 的 ASN 匹配 | 符合预期 | ✅ |

### ForwardedIPConfig 详细结果

| 场景 | XFF Header | 预期 | 实际 |
|------|-----------|------|------|
| Amazon IP | `54.239.28.85` (ASN 16509) | 403 | 403 |
| Google IP | `8.8.8.8` (ASN 15169) | 200 | 200 |
| 无 Header | —（FallbackBehavior=NO_MATCH） | 200 | 200 |

### WCU 消耗对比

| 规则类型 | WCU |
|---------|-----|
| 单纯 ASN Match | 1 |
| Rate-based + ASN 聚合 | 32 (2 + 30) |
| AND(ASN + Geo) | 2 (1 + 1) |

## 踩坑记录

!!! warning "WebACL 关联延迟"
    新创建的 WebACL 通过 `associate-web-acl` 关联到 ALB 时，可能需要等待 **30-120 秒**。在此期间会收到 `WAFUnavailableEntityException` 错误，需要重试。建议在脚本中加入 `sleep 30` 和重试逻辑。（实测发现，官方文档仅提示 "Retry your request"，未说明具体等待时间。）

!!! warning "Rate-based 规则非实时生效"
    Rate-based 规则有 **1-5 分钟的评估延迟**，不是请求超限后立即生效。适合防护持续性攻击，不适合精确的瞬时限流。（已查文档确认：WAF 在评估窗口内统计请求。）

!!! warning "GetRateBasedStatementManagedKeys 限制"
    使用 CUSTOM_KEYS（如 ASN 聚合）的 Rate-based 规则，无法通过 `GetRateBasedStatementManagedKeys` API 查看已限流的键。此 API 仅支持 `AggregateKeyType` 为 `IP` 或 `FORWARDED_IP` 的规则。（已查文档确认。）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| WebACL | $5/月 | ~1 小时 | ~$0.007 |
| 规则 | $1/月/规则 | ~1 小时 | ~$0.001 |
| ALB | $0.0225/hr | ~1 小时 | $0.023 |
| Lambda | — | 免费额度内 | $0 |
| WAF 请求 | $0.60/百万 | <1000 | ~$0 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 1. 解除 WebACL 关联
aws wafv2 disassociate-web-acl --resource-arn $ALB_ARN

# 2. 删除所有 WebACL（需要获取每个的 Id 和 LockToken）
# 列出所有 WebACL
aws wafv2 list-web-acls --scope REGIONAL
# 对每个 WebACL 执行：
aws wafv2 delete-web-acl --name NAME --scope REGIONAL --id ID --lock-token LOCK_TOKEN

# 3. 删除 ALB
aws elbv2 delete-listener --listener-arn $LISTENER_ARN
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN
aws elbv2 delete-target-group --target-group-arn $TG_ARN

# 4. 删除 Lambda
aws lambda delete-function --function-name waf-asn-test-handler

# 5. 删除 IAM 角色
aws iam delete-role --role-name waf-asn-test-lambda-role

# 6. 等待 ALB 完全删除后，删除 Security Group
sleep 60
aws ec2 delete-security-group --group-id $SG_ID
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ALB 按小时计费（$0.0225/hr），WebACL 按月计费（$5/月）。

## 结论与建议

### 适用场景

1. **封锁特定 ISP/托管商** — 当某个 ISP 持续产生恶意流量时，一条 ASN 规则覆盖其所有 IP
2. **白名单模式** — 仅允许可信合作伙伴网络访问，配合默认 Block
3. **分层防御** — AND(ASN + Geo) 组合使用，精准定位流量来源
4. **按网络维度限速** — Rate-based + ASN 聚合，按运营商级别限速

### 与 IP Set 的选择建议

- IP 段明确、需要精确控制 → **IP Set**
- 封锁整个 ISP/组织、减少维护成本 → **ASN Match**
- 两者可以同时使用，ASN 做粗粒度、IP Set 做细粒度

### 生产环境建议

1. 先用 **Count** 模式观察 ASN 匹配情况，确认无误后切换为 Block
2. 大型 ASN（如 AWS 自身的 AS16509）覆盖面广，Block 前务必确认影响范围
3. 利用 **ASN 0** 处理无法映射的 IP（如私有 IP），避免安全漏洞
4. ForwardedIPConfig 的 **FallbackBehavior** 建议设为 NO_MATCH（安全侧），避免因缺失 header 误封

## 参考链接

- [ASN match rule statement - AWS WAF](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-asn-match.html)
- [Rate-based rule statements - AWS WAF](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-rate-based.html)
- [AWS What's New: ASN Match in AWS WAF](https://aws.amazon.com/about-aws/whats-new/2025/06/asn-match-aws-waf/)
- [AWS WAF Pricing](https://aws.amazon.com/waf/pricing/)
