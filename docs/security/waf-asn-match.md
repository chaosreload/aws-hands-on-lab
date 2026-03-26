# AWS WAF ASN Match：基于自治系统编号的精准流量控制实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.50（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-26

## 背景

在 Web 应用防护中，IP 地址是最常用的流量识别维度。但 IP 地址经常变化，管理大量 IP 规则成本很高。**自治系统编号（ASN）** 是分配给大型互联网网络的唯一标识符——ISP、企业、云厂商各有自己的 ASN。基于 ASN 控制流量，可以在网络组织级别精准过滤，无需逐条管理 IP 地址。

2025 年 6 月，AWS WAF 正式支持 **ASN Match Statement**。本文将通过 7 组实测，验证 ASN Match 的核心功能、组合能力和边界行为。

## 前置条件

- AWS 账号（需要 WAF、ELB、Lambda、IAM 权限）
- AWS CLI v2 已配置
- 了解 WAF WebACL 基本概念

## 核心概念

### ASN Match 是什么？

ASN Match 让 AWS WAF 根据请求 IP 地址所属的 ASN 进行匹配。与 IP Set 和 Geo Match 的对比：

| 维度 | ASN Match | IP Set | Geo Match |
|------|-----------|--------|-----------|
| **粒度** | 网络组织级别 | 单 IP/CIDR | 国家/地区 |
| **最大条目** | 100 ASN/规则 | 10,000 IP/集合 | 50 国家/规则 |
| **WCU 消耗** | 1 | 1 | 1 |
| **稳定性** | ASN 很少变化 | IP 经常变化 | 国家不变 |
| **典型场景** | 封锁/允许特定 ISP 或云厂商 | 精确 IP 控制 | 地理合规 |
| **Forwarded IP 支持** | ✅ | ✅ | ✅ |
| **Rate-based 聚合** | ✅ | ✅ | ❌ |

### 关键参数

- **ASN 列表**：1-100 个 ASN，范围 0-4,294,967,295
- **ASN 0**：无法映射 ASN 的 IP 被赋值 ASN 0
- **ForwardedIPConfig**：可选，基于 HTTP header（如 X-Forwarded-For）中的 IP 确定 ASN
- **FallbackBehavior**：MATCH 或 NO_MATCH，处理无效/缺失 IP 的行为

## 动手实践

### Step 1: 准备基础设施

先创建 Lambda 后端和 ALB 作为 WAF 测试目标。

```bash
# 创建 Security Group（⚠️ 仅允许你的 IP 入站，绝不开放 0.0.0.0/0）
MY_IP=$(curl -s https://ipinfo.io/ip)
SG_ID=$(aws ec2 create-security-group \
  --group-name waf-asn-test-alb-sg \
  --description "ALB SG for WAF ASN test" \
  --vpc-id YOUR_VPC_ID \
  --region us-east-1 \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 80 \
  --cidr ${MY_IP}/32 \
  --region us-east-1
```

```bash
# 创建 Lambda 函数（简单 HTTP 响应）
cat > /tmp/handler.py << 'PYEOF'
import json
def handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": "Hello from WAF ASN Test!"})
    }
PYEOF
cd /tmp && zip -j handler.zip handler.py

# 创建 IAM Role
aws iam create-role \
  --role-name waf-asn-test-lambda-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

sleep 10  # 等待 IAM 角色传播

aws lambda create-function \
  --function-name waf-asn-test-handler \
  --runtime python3.12 \
  --handler handler.handler \
  --role arn:aws:iam::ACCOUNT_ID:role/waf-asn-test-lambda-role \
  --zip-file fileb:///tmp/handler.zip \
  --region us-east-1
```

```bash
# 创建 ALB + Target Group + Listener
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name waf-asn-test-alb \
  --subnets SUBNET_1 SUBNET_2 \
  --security-groups $SG_ID \
  --scheme internet-facing \
  --type application \
  --region us-east-1 \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

TG_ARN=$(aws elbv2 create-target-group \
  --name waf-asn-test-tg \
  --target-type lambda \
  --region us-east-1 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# 授权 ALB 调用 Lambda
aws lambda add-permission \
  --function-name waf-asn-test-handler \
  --statement-id alb-invoke \
  --action lambda:InvokeFunction \
  --principal elasticloadbalancing.amazonaws.com \
  --source-arn $TG_ARN \
  --region us-east-1

aws elbv2 register-targets \
  --target-group-arn $TG_ARN \
  --targets Id=arn:aws:lambda:us-east-1:ACCOUNT_ID:function:waf-asn-test-handler \
  --region us-east-1

aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN \
  --region us-east-1
```

等待 ALB 状态变为 `active`（约 2-3 分钟），确认基础连通性：

```bash
ALB_DNS=$(aws elbv2 describe-load-balancers --names waf-asn-test-alb \
  --region us-east-1 --query 'LoadBalancers[0].DNSName' --output text)
curl -s http://$ALB_DNS/
# 应返回: {"message": "Hello from WAF ASN Test!"}
```

### Step 2: 查找你的 ASN

```bash
curl -s https://ipinfo.io | jq '{ip, org}'
# 示例输出: {"ip": "18.140.5.11", "org": "AS16509 Amazon.com, Inc."}
# 记下 ASN 编号，这里是 16509
```

### Step 3: 测试 ASN Match Block 规则

创建 WebACL，默认 Allow，Block 指定 ASN：

```bash
cat > /tmp/waf-asn-block.json << 'JSON_EOF'
{
  "Name": "waf-asn-test",
  "Scope": "REGIONAL",
  "DefaultAction": {"Allow": {}},
  "Rules": [{
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
      "MetricName": "BlockMyASN"
    }
  }],
  "VisibilityConfig": {
    "SampledRequestsEnabled": true,
    "CloudWatchMetricsEnabled": true,
    "MetricName": "WafAsnTest"
  }
}
JSON_EOF

WEBACL_ARN=$(aws wafv2 create-web-acl \
  --cli-input-json file:///tmp/waf-asn-block.json \
  --region us-east-1 \
  --query 'Summary.ARN' --output text)

aws wafv2 associate-web-acl \
  --web-acl-arn $WEBACL_ARN \
  --resource-arn $ALB_ARN \
  --region us-east-1

sleep 10  # 等待 WAF 规则生效
curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" http://$ALB_DNS/
# 预期: HTTP Status: 403 ← ASN 匹配，被 Block
```

### Step 4: 测试 ASN Match Allow（白名单模式）

将规则改为默认 Block，仅 Allow 指定 ASN：

```json
{
  "DefaultAction": {"Block": {}},
  "Rules": [{
    "Name": "allow-my-asn",
    "Priority": 1,
    "Statement": {"AsnMatchStatement": {"AsnList": [16509]}},
    "Action": {"Allow": {}}
  }]
}
```

```bash
# 更新后测试
curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" http://$ALB_DNS/
# 预期: HTTP Status: 200 ← ASN 匹配 Allow 规则
```

### Step 5: 组合 ASN Match + Geo Match（AND 逻辑）

使用 AND Statement 组合两种匹配条件：

```json
{
  "Statement": {
    "AndStatement": {
      "Statements": [
        {"AsnMatchStatement": {"AsnList": [16509]}},
        {"GeoMatchStatement": {"CountryCodes": ["SG"]}}
      ]
    }
  },
  "Action": {"Block": {}}
}
```

实测结果：

| 条件 | ASN 匹配 | Geo 匹配 | AND 结果 | HTTP 状态 |
|------|---------|---------|---------|-----------|
| ASN 16509 + 来自 SG | ✅ | ✅ | 匹配 → Block | **403** |
| ASN 16509 + 来自 US | ✅ | ❌ | 不匹配 → Allow | **200** |

### Step 6: Rate-based 规则 + ASN 聚合

```json
{
  "Statement": {
    "RateBasedStatement": {
      "Limit": 100,
      "EvaluationWindowSec": 60,
      "AggregateKeyType": "CUSTOM_KEYS",
      "CustomKeys": [{"ASN": {}}]
    }
  },
  "Action": {"Block": {}}
}
```

此规则按 ASN 聚合请求并限速。WCU 消耗 = **32**（基础 2 + ASN 聚合键 30）。

### Step 7: ForwardedIPConfig 测试

使用 X-Forwarded-For header 指定源 IP，基于该 IP 的 ASN 匹配：

```json
{
  "Statement": {
    "AsnMatchStatement": {
      "AsnList": [16509],
      "ForwardedIPConfig": {
        "HeaderName": "X-Forwarded-For",
        "FallbackBehavior": "NO_MATCH"
      }
    }
  },
  "Action": {"Block": {}}
}
```

```bash
# 测试：XFF 包含 Amazon IP → Block
curl -H "X-Forwarded-For: 54.239.28.85" http://$ALB_DNS/
# HTTP Status: 403

# 测试：XFF 包含 Google IP → Allow
curl -H "X-Forwarded-For: 8.8.8.8" http://$ALB_DNS/
# HTTP Status: 200

# 测试：无 XFF header → FallbackBehavior=NO_MATCH → Allow
curl http://$ALB_DNS/
# HTTP Status: 200
```

## 测试结果

| # | 测试场景 | 预期 | 实际 | 状态 |
|---|---------|------|------|------|
| 1 | ASN Match Block（ASN 16509） | 403 | 403 | ✅ |
| 2 | ASN Match Allow + Default Block | 200 | 200 | ✅ |
| 3a | AND（ASN 16509 + Geo SG） | 403 | 403 | ✅ |
| 3b | AND（ASN 16509 + Geo US），源 SG | 200 | 200 | ✅ |
| 4 | Rate-based + ASN 聚合 | 创建成功，WCU=32 | 如预期 | ✅ |
| 5 | ASN 0（Block）+ 我们 ASN 16509 | 200 | 200 | ✅ |
| 6a | 100 个 ASN 列表 | 创建成功 | 成功 | ✅ |
| 6b | 101 个 ASN 列表 | ValidationException | 如预期报错 | ✅ |
| 7a | XFF + Amazon IP（ASN 16509） | 403 | 403 | ✅ |
| 7b | XFF + Google IP（ASN 15169） | 200 | 200 | ✅ |
| 7c | 无 XFF + FallbackBehavior=NO_MATCH | 200 | 200 | ✅ |

### WCU 消耗对比

| 规则类型 | WCU |
|---------|-----|
| ASN Match（单独） | 1 |
| Geo Match（单独） | 1 |
| IP Set Match（单独） | 1 |
| Rate-based + ASN 聚合键 | 32（2 + 30） |

## 踩坑记录

!!! warning "WebACL 更新后检查关联状态"
    实测发现 `update-web-acl` 后，WebACL 与 ALB 的关联可能丢失。建议更新规则后执行 `get-web-acl-for-resource` 确认关联仍然存在，必要时重新 `associate-web-acl`。
    **标注**：实测发现，官方未记录。

!!! warning "规则传播延迟"
    WebACL 规则创建或更新后，需要 **5-15 秒** 才能完全生效。自动化测试中应加入等待时间。
    **标注**：实测发现，官方未明确给出具体延迟时间。

!!! tip "ASN 查询工具"
    - `curl https://ipinfo.io` — 查看当前 IP 的 ASN
    - [bgp.he.net](https://bgp.he.net) — 查询任意 ASN 的详细信息
    - [PeeringDB](https://www.peeringdb.com) — ASN 和网络互联数据库

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| ALB | $0.0225/hr | ~0.5 hr | $0.01 |
| WAF WebACL | $5.00/月 | ~0.5 hr | $0.003 |
| WAF 规则 | $1.00/月/规则 | ~0.5 hr | $0.001 |
| Lambda | 免费额度 | 极少调用 | $0.00 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 1. 解除 WAF 关联
aws wafv2 disassociate-web-acl \
  --resource-arn $ALB_ARN \
  --region us-east-1

# 2. 删除 WebACL
LOCK=$(aws wafv2 get-web-acl --name waf-asn-test --scope REGIONAL \
  --id WEBACL_ID --region us-east-1 --query 'LockToken' --output text)
aws wafv2 delete-web-acl --name waf-asn-test --scope REGIONAL \
  --id WEBACL_ID --lock-token $LOCK --region us-east-1

# 3. 删除 ALB 和 Target Group
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN --region us-east-1
sleep 60  # 等待 ALB 完全删除
aws elbv2 delete-target-group --target-group-arn $TG_ARN --region us-east-1

# 4. 删除 Lambda 和 IAM Role
aws lambda delete-function --function-name waf-asn-test-handler --region us-east-1
aws iam delete-role --role-name waf-asn-test-lambda-role

# 5. 检查 ENI 残留后删除 Security Group
aws ec2 describe-network-interfaces \
  --filters Name=group-id,Values=$SG_ID \
  --region us-east-1 --query 'NetworkInterfaces[*].NetworkInterfaceId'
# 确认输出为空后执行
aws ec2 delete-security-group --group-id $SG_ID --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。ALB 按小时计费（$0.0225/hr），WebACL 按月计费（$5/月）。

## 结论与建议

### ASN Match 适用场景

1. **封锁特定云厂商/托管商流量** — 比如阻止来自已知恶意托管服务的请求
2. **仅允许合作伙伴网络访问** — 白名单模式，只放行特定组织的 ASN
3. **合规增强** — 配合 Geo Match，实现"特定国家 + 特定网络"的精准控制
4. **Rate Limiting 优化** — 按 ASN 聚合限速，防止单个网络组织的流量洪峰

### 生产环境建议

- **优先使用 ASN Match 替代大规模 IP Set** — ASN 比 IP 更稳定，管理成本更低
- **组合使用 AND/OR** — ASN + Geo + Rate-based 可构建多层防护
- **注意 ASN 0** — 无法映射 ASN 的 IP 被赋值 0，考虑是否需要显式处理
- **ForwardedIPConfig** — 如果使用 CDN 或反向代理，配置正确的 header 名称
- **WCU 规划** — 单独 ASN Match 仅 1 WCU，但 Rate-based + ASN 聚合需 32 WCU

## 参考链接

- [ASN Match Rule Statement - AWS WAF Developer Guide](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-asn-match.html)
- [AsnMatchStatement API Reference](https://docs.aws.amazon.com/waf/latest/APIReference/API_AsnMatchStatement.html)
- [Rate-based Rule Aggregation Options](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-rate-based-aggregation-options.html)
- [AWS WAF Pricing](https://aws.amazon.com/waf/pricing/)
- [What's New: ASN Match for AWS WAF](https://aws.amazon.com/about-aws/whats-new/2025/06/asn-match-aws-waf/)
