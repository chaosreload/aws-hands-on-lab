# Amazon Bedrock Cross-Region Inference Profile 实测：6 个 Region 延迟对比与路由策略选择指南

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $10（含 EC2 + Bedrock API 调用）
    - **Region**: us-east-1, us-west-2, eu-central-1, ap-northeast-1, ap-southeast-1, ap-southeast-2
    - **最后验证**: 2026-04-12

## 背景

Amazon Bedrock 的 Cross-Region Inference Profile 允许你通过一个 inference profile ID 自动将请求路由到多个 Region。有两种类型：

1. **Geographic Profile**（如 `us.`、`eu.`、`jp.`）：请求只会路由到指定地理区域内的 Region
2. **Global Profile**（`global.`）：请求可路由到全球任意商用 Region，提供最高吞吐量和约 10% 的成本折扣

但这引出一个关键问题：**路由带来的延迟开销到底有多大？** 在不同的调用源 Region，Global 和 Geographic Profile 的延迟差异如何？

本文通过在全球 6 个 Region 部署 EC2 实例，对 Claude Sonnet 4.6 和 Nova 2 Lite 两个模型进行了 1260 次 API 调用的延迟实测，给出数据驱动的选型建议。

## 前置条件

- AWS 账号，开通 Bedrock 模型访问（Claude Sonnet 4.6、Nova 2 Lite）
- AWS CLI v2 已配置
- Python 3 + boto3
- 5 个 Region 的 EC2 启动权限

<details>
<summary>最小 IAM Policy（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

## 核心概念

### Cross-Region Inference Profile 类型

| 类型 | 前缀示例 | 数据驻留 | 吞吐量 | 成本 |
|------|----------|----------|--------|------|
| Geographic | `us.`、`eu.`、`jp.`、`au.` | 限定地理区域 | 高于单 Region | 标准定价 |
| Global | `global.` | 全球商用 Region | 最高 | 约 10% 折扣 |

### 测试模型与 Endpoint 矩阵

我们选择了两个代表性模型：

- **Claude Sonnet 4.6**（`anthropic.claude-sonnet-4-6`）：第三方模型
- **Nova 2 Lite**（`amazon.nova-2-lite-v1:0`）：AWS 自研模型

#### 完整 Inference Profile 列表

以下是这两个模型所有可用的 Cross-Region Inference Profile，包括可调用的 Source Region 和请求路由的 Destination Region：

**Claude Sonnet 4.6**

| Inference Profile ID | Source Region（可发起调用） | Destination Region（请求路由目标） |
|---------------------|--------------------------|-------------------------------|
| `global.anthropic.claude-sonnet-4-6` | 所有商用 Region | 全球所有商用 Region |
| `us.anthropic.claude-sonnet-4-6` | us-east-1, us-east-2, us-west-2 | us-east-1, us-east-2, us-west-2 |
| `eu.anthropic.claude-sonnet-4-6` | eu-north-1, eu-west-1, eu-west-3, eu-south-1, eu-south-2, eu-central-1 | eu-north-1, eu-west-1, eu-west-3, eu-south-1, eu-south-2, eu-central-1 |
| `jp.anthropic.claude-sonnet-4-6` | ap-northeast-1, ap-northeast-3 | ap-northeast-1, ap-northeast-3 |
| `au.anthropic.claude-sonnet-4-6` | ap-southeast-2, ap-southeast-4 | ap-southeast-2, ap-southeast-4 |

**Nova 2 Lite**

| Inference Profile ID | Source Region（可发起调用） | Destination Region（请求路由目标） |
|---------------------|--------------------------|-------------------------------|
| `global.amazon.nova-2-lite-v1:0` | 所有商用 Region | 全球所有商用 Region |
| `us.amazon.nova-2-lite-v1:0` | us-east-1, us-east-2, us-west-2 | us-east-1, us-east-2, us-west-2 |
| `eu.amazon.nova-2-lite-v1:0` | eu-north-1, eu-west-1, eu-west-3, eu-south-1, eu-south-2, eu-central-1 | eu-north-1, eu-west-1, eu-west-3, eu-south-1, eu-south-2, eu-central-1 |
| `jp.amazon.nova-2-lite-v1:0` | ap-northeast-1, ap-northeast-3 | ap-northeast-1, ap-northeast-3 |

!!! warning "Geographic Profile 有 Source Region 限制"
    Geographic inference profile **只能从其指定的 Source Region 调用**，不能跨区域使用。例如 `jp.anthropic.claude-sonnet-4-6` 只能从 ap-northeast-1 或 ap-northeast-3 调用，从 ap-southeast-1 调用会返回 `ValidationException: The provided model identifier is invalid`。Global profile 没有此限制，可从任意商用 Region 调用。详见 [官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)。

#### 本次测试的 Profile 组合

| EC2 Region | Claude Sonnet 4.6 | Nova 2 Lite | 备注 |
|------------|-------------------|-------------|------|
| us-east-1 | global, us | global, us | |
| us-west-2 | global, us | global, us | |
| eu-central-1 | global, eu | global, eu | |
| ap-northeast-1 | global, jp | global, jp | |
| ap-southeast-1 | global | global | 无可用 Geographic Profile |
| ap-southeast-2 | global, au | global | Nova 2 Lite 无 au. profile |

每个 Region 测试 Global Profile 和该 Region 可用的 Geographic Profile（如有），对比「全球路由」和「就近路由」的延迟差异。ap-southeast-1 不在任何 Geographic Profile 的 Source Region 列表中，因此只能使用 Global Profile。

### 关键定价信息

!!! info "定价规则"
    - Cross-Region Inference **无额外路由费用**
    - 价格按 **调用源 Region** 计算，不是推理实际执行的 Region
    - Global Profile 享受约 **10% 折扣**（与同 Region 的 Geographic Profile 对比）

## 动手实践

### Step 1: 准备测试环境

在 5 个 Region 部署 t3.micro EC2（Amazon Linux 2023），第 6 个 Region（ap-southeast-1）使用已有的 dev-server。

```bash
# 获取 AMI ID（每个 Region 都要执行）
aws ssm get-parameter --region us-east-1 \
  --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" \
  --query "Parameter.Value" --output text

# 创建安全组（仅出站 HTTPS，无入站规则）
aws ec2 create-security-group --region us-east-1 \
  --group-name bedrock-benchmark-sg \
  --description "Benchmark - egress only"

# 启动实例
aws ec2 run-instances --region us-east-1 \
  --image-id $AMI_ID \
  --instance-type t3.micro \
  --security-group-ids $SG_ID \
  --iam-instance-profile Name=bedrock-benchmark-profile \
  --associate-public-ip-address
```

!!! warning "安全提醒"
    安全组 **只需出站 HTTPS**，不需要任何入站规则。通过 SSM Session Manager 管理实例。

### Step 2: 编写测试脚本

使用 Python + boto3 的 `converse_stream` API 测量 TTFB（Time to First Byte）和总延迟：

```python
import boto3, time

def run_single_test(region, model_id):
    client = boto3.Session(region_name=region).client("bedrock-runtime")
    messages = [{"role": "user", "content": [{"text": 
        "Explain what cloud computing is in exactly 3 sentences."}]}]
    
    start = time.perf_counter()
    ttfb = None
    
    response = client.converse_stream(
        modelId=model_id,
        messages=messages,
        inferenceConfig={"maxTokens": 200, "temperature": 0.1},
    )
    
    for event in response["stream"]:
        if ttfb is None:
            ttfb = (time.perf_counter() - start) * 1000
    
    total = (time.perf_counter() - start) * 1000
    return ttfb, total
```

测试设计要点：

- 固定 prompt："Explain what cloud computing is in exactly 3 sentences."
- 每个 endpoint 跑 **30 次**
- **交替执行**（ABCABC...）而非批量执行（AAA BBB...），避免时段偏差
- 调用间隔 500ms，避免触发限流

### Step 3: 执行测试并收集结果

通过 SSM RunCommand 在各 EC2 上执行测试脚本，结果上传至 S3：

```bash
aws ssm send-command --region us-east-1 \
  --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["cd /tmp && BENCH_REGION=us-east-1 python3 benchmark.py"]}'
```

共收集 **1260 次** API 调用数据（6 Region × 2-4 endpoint × 30 次）。

## 测试结果

### 总览：各 Region × Profile 的延迟分布

**Claude Sonnet 4.6（TTFB / 总延迟，P50，单位 ms）**

| EC2 Region | Global Profile | Geographic Profile | TTFB 差异 | 总延迟差异 |
|------------|---------------:|------------------:|----------:|----------:|
| us-east-1 | 822 / 2509 | us: 860 / 2538 | -38ms | -28ms |
| us-west-2 | 881 / 2554 | us: 904 / 2603 | -23ms | -49ms |
| eu-central-1 | 947 / 2788 | eu: 766 / 2473 | +181ms | +315ms |
| ap-northeast-1 | 988 / 2710 | jp: 663 / 2215 | +325ms | +496ms |
| ap-southeast-1 | 1020 / 2806 | —（无可用 Geographic Profile） | — | — |
| ap-southeast-2 | 1114 / 2792 | au: 788 / 2405 | +326ms | +387ms |

**Nova 2 Lite（TTFB / 总延迟，P50，单位 ms）**

| EC2 Region | Global Profile | Geographic Profile | TTFB 差异 | 总延迟差异 |
|------------|---------------:|------------------:|----------:|----------:|
| us-east-1 | 390 / 1184 | us: 405 / 1202 | -15ms | -18ms |
| us-west-2 | 344 / 1167 | us: 333 / 1154 | +12ms | +13ms |
| eu-central-1 | 764 / 1551 | eu: 379 / 1214 | +384ms | +336ms |
| ap-northeast-1 | 673 / 1494 | jp: 336 / 1042 | +336ms | +451ms |
| ap-southeast-1 | 592 / 1351 | —（无可用 Geographic Profile） | — | — |
| ap-southeast-2 | 708 / 1557 | —（无可用 Geographic Profile） | — | — |

### 关键发现

#### 发现 1: 在美国 Region，Global Profile 和 US Profile 几乎无差异

从 us-east-1 和 us-west-2 发起调用时：

- Claude Sonnet 4.6：Global 和 US Profile 的 P50 总延迟差异在 **±50ms 以内**
- Nova 2 Lite：差异在 **±20ms 以内**

这说明从美国 Region 调用时，Global Profile 很可能就近路由到了美国的 Region，路由开销可忽略。

#### 发现 2: 在非美国 Region，Global Profile 比 Geographic Profile 慢 12-43%

| Region | 模型 | Global vs Regional 总延迟差异 |
|--------|------|------|
| eu-central-1 | Claude | +12.7% |
| eu-central-1 | Nova 2 Lite | +27.7% |
| ap-northeast-1 | Claude | +22.4% |
| ap-northeast-1 | Nova 2 Lite | +43.3% |
| ap-southeast-2 | Claude | +16.1% |

**这是最重要的发现**：在欧洲和亚太 Region，Global Profile 的请求很可能被路由到了美国的 Region，导致了显著的跨洋延迟。

#### 发现 3: TTFB 差异比总延迟差异更显著

以 ap-northeast-1 为例：

- Nova 2 Lite Global TTFB: 673ms vs JP TTFB: 336ms → **TTFB 翻倍**
- Nova 2 Lite Global 总延迟: 1494ms vs JP 总延迟: 1042ms → 总延迟 +43%

TTFB 的差异比例更大，因为 TTFB 主要反映网络 RTT + 路由开销，而总延迟还包含了模型生成时间（与网络无关）。对于流式输出场景，TTFB 直接影响用户感知的"响应速度"。

#### 发现 4: ap-southeast-1 的 P99 延迟异常高

ap-southeast-1（新加坡）只有 Global Profile 可用，且出现了极高的 P99 延迟：

- Claude: P50=2806ms，P99=44508ms
- Nova 2 Lite: P50=1351ms，P99=182188ms

这可能是因为 Global Profile 从新加坡路由到远端 Region 时，偶尔遇到高延迟路由或冷启动。

### 尾部延迟对比（P95/P99）

| EC2 Region | Model | Profile | P50 | P95 | P99 |
|------------|-------|---------|-----|-----|-----|
| us-east-1 | Claude | global | 2509 | 3491 | 9653 |
| us-east-1 | Claude | us | 2538 | 2873 | 3698 |
| ap-northeast-1 | Claude | global | 2710 | 3516 | 3539 |
| ap-northeast-1 | Claude | jp | 2215 | 2376 | 2412 |
| eu-central-1 | Nova 2 Lite | global | 1551 | 1840 | 1853 |
| eu-central-1 | Nova 2 Lite | eu | 1214 | 1420 | 1434 |

Geographic Profile 的尾部延迟明显更稳定——P50 和 P99 之间的差距更小。

## 踩坑记录

!!! warning "踩坑 1: SSM 默认角色 vs EC2 Instance Profile"
    通过 SSM RunCommand 执行的命令使用 SSM 默认管理角色（`EpoxyAWSSystemsManagerDefaultEC2InstanceManagementRole`），而不是 EC2 Instance Profile。如果你的测试脚本需要访问 S3 或其他服务，需要在 SSM 命令中显式获取 Instance Metadata 凭证。

!!! warning "踩坑 2: AL2023 默认不自带 pip 和 boto3"
    Amazon Linux 2023 的 t3.micro 实例默认没有 `pip3` 命令，需要先 `dnf install -y python3-pip` 再 `pip3 install boto3`。

!!! info "观察: x-amzn-bedrock-inference-region Header"
    ConverseStream 的 ResponseMetadata HTTPHeaders 中未返回 `x-amzn-bedrock-inference-region`。要获取实际推理 Region，需查看 CloudTrail 的 `additionalEventData.inferenceRegion` 字段。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.micro × 5 | $0.0104-0.0152/hr | ~1hr | < $0.10 |
| Bedrock API（Claude Sonnet 4.6） | ~$3/$15 per M tokens | ~500 calls | < $3.00 |
| Bedrock API（Nova 2 Lite） | ~$0.04/$0.16 per M tokens | ~500 calls | < $0.50 |
| S3 存储 | — | < 1MB | < $0.01 |
| **合计** | | | **< $5.00** |

## 清理资源

```bash
# 1. 终止所有测试 EC2
for REGION in us-east-1 us-west-2 eu-central-1 ap-northeast-1 ap-southeast-2; do
  INSTANCE_IDS=$(aws ec2 describe-instances --region $REGION \
    --filters "Name=tag:Name,Values=bedrock-benchmark-*" \
              "Name=instance-state-name,Values=running" \
    --query "Reservations[].Instances[].InstanceId" --output text)
  [ -n "$INSTANCE_IDS" ] && aws ec2 terminate-instances \
    --region $REGION --instance-ids $INSTANCE_IDS
done

# 2. 等待终止完成后删除安全组
sleep 60
for REGION in us-east-1 us-west-2 eu-central-1 ap-northeast-1 ap-southeast-2; do
  SG_ID=$(aws ec2 describe-security-groups --region $REGION \
    --filters "Name=group-name,Values=bedrock-benchmark-sg" \
    --query "SecurityGroups[0].GroupId" --output text)
  [ "$SG_ID" != "None" ] && aws ec2 delete-security-group \
    --region $REGION --group-id $SG_ID
done

# 3. 删除 IAM 资源
aws iam remove-role-from-instance-profile \
  --instance-profile-name bedrock-benchmark-profile \
  --role-name bedrock-benchmark-role
aws iam delete-instance-profile \
  --instance-profile-name bedrock-benchmark-profile
aws iam detach-role-policy --role-name bedrock-benchmark-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role-policy --role-name bedrock-benchmark-role \
  --policy-name bedrock-access
aws iam delete-role --role-name bedrock-benchmark-role

# 4. 删除 S3 bucket
aws s3 rb s3://bedrock-benchmark-results-595842667825 --force
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。EC2 按小时计费，忘记清理会持续产生费用。

## 结论与建议

### 场景化推荐

| 场景 | 推荐 Profile | 理由 |
|------|-------------|------|
| 调用源在美国 | **Global** ✅ | 延迟与 US Profile 几乎相同，还有 ~10% 折扣 |
| 调用源在欧洲/亚太，延迟敏感 | **Geographic**（eu/jp/au） | 比 Global 快 12-43%，TTFB 可节省 180-384ms |
| 调用源在欧洲/亚太，成本优先 | **Global** | 接受 200-500ms 额外延迟，换取 ~10% 折扣 |
| 调用源不在任何 Geographic Profile 的 Source Region 中 | **Global**（唯一选择） | 如 ap-southeast-1，不在 jp./au. 等 profile 的 Source Region 列表中 |
| 流式输出、对首 token 敏感 | **Geographic** | TTFB 差异比总延迟差异更大 |
| 高并发、需要最大吞吐 | **Global** | 全球 Region 池更大，容量更充裕 |

### 定量决策参考

当你在 **非美国 Region** 调用 Bedrock 时：

- 使用 Geographic Profile 比 Global 的 **TTFB 快 ~300ms**
- 总延迟快 **~300-500ms**（视模型而定）
- Global Profile 的 **尾部延迟更不稳定**（P50 到 P99 波动更大）

但 Global Profile 提供 **~10% 成本折扣** + **更高吞吐上限**。

**一句话总结**：在美国用 Global，在海外用 Geographic —— 除非你更在乎成本而非延迟。

## 参考链接

- [Cross-Region Inference 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
- [Supported Regions and Models for Inference Profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Global Cross-Region Inference](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html)
