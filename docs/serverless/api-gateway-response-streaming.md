# API Gateway REST API 响应流式传输实战：TTFB 降低 86%

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $1-2（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

在 GenAI 应用中，用户发送 prompt 后需要等待完整响应生成才能看到结果——有时长达数十秒。这种"空白等待"严重影响用户体验。

2025 年 11 月，AWS 为 API Gateway REST API 推出了 **Response Streaming** 功能。核心价值三点：

1. **降低 TTFB** — 响应生成的同时即开始传输，用户实时看到增量内容
2. **突破 29 秒超时** — 流式模式下 timeout 可设置到 **15 分钟**
3. **突破 10MB payload 限制** — 可直接流式传输大文件

此前只有 Lambda Function URL 和 HTTP API 支持类似能力。REST API 现在补齐了这一短板，已有 REST API 的用户无需迁移就能获得流式能力，同时保留 WAF、Usage Plans、API Keys 等丰富功能。

## 前置条件

- AWS 账号（需要 Lambda、API Gateway、IAM 权限）
- AWS CLI v2 已配置
- 如果测试 Bedrock 场景，需要开通 Claude 3 Haiku 模型访问

## 核心概念

### 一张表搞定：Buffered vs Stream

| 对比项 | Buffered（默认） | Stream |
|--------|-----------------|--------|
| 传输行为 | 等待完整响应后一次发送 | 边生成边传输 |
| TTFB | 等于响应生成时间 | 远小于响应生成时间 |
| 最大 Timeout | 29 秒 | **15 分钟** |
| 最大 Payload | 10 MB | **无硬性上限**（>10MB 部分限速 2MB/s） |
| 支持的集成类型 | 所有类型 | 仅 `HTTP_PROXY` / `AWS_PROXY` |
| Endpoint Caching | ✅ | ❌ |
| Content Encoding | ✅ | ❌ |
| VTL Response Mapping | ✅ | ❌ |
| Binary Payload | 需配置 binary media types | **自动支持** |

### 关键限制

- **Idle Timeout**：Regional/Private 端点 **5 分钟**，Edge-optimized 端点 **30 秒**
- **仅支持 response streaming**，不支持 request streaming
- Lambda 连接关闭后，Lambda 函数可能继续执行（注意 timeout 设置）

### Lambda URI 路径差异

这是最容易犯错的地方：

```
# 标准（buffered）调用
arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{arn}/invocations

# 流式调用
arn:aws:apigateway:{region}:lambda:path/2021-11-15/functions/{arn}/response-streaming-invocations
```

## 动手实践

### Step 1: 创建 Lambda 执行角色

```bash
# 创建信任策略文件
cat > /tmp/lambda-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

# 创建角色
aws iam create-role \
  --role-name apigw-streaming-lambda-role \
  --assume-role-policy-document file:///tmp/lambda-trust-policy.json \
  --region us-east-1

# 附加基础执行权限
aws iam attach-role-policy \
  --role-name apigw-streaming-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### Step 2: 创建流式 Lambda 函数

```javascript
// streaming-basic.mjs — 使用 awslambda.streamifyResponse
export const handler = awslambda.streamifyResponse(
  async (event, responseStream, context) => {
    const metadata = {
      statusCode: 200,
      headers: { "Content-Type": "text/plain" }
    };

    // HttpResponseStream.from 自动添加 8-null-byte 分隔符
    responseStream = awslambda.HttpResponseStream.from(
      responseStream, metadata
    );

    const chunks = 10;
    const delayMs = parseInt(
      event.queryStringParameters?.delay || "500"
    );

    for (let i = 0; i < chunks; i++) {
      responseStream.write(
        `Chunk ${i + 1}/${chunks} at ${new Date().toISOString()}\n`
      );
      if (i < chunks - 1) {
        await new Promise(r => setTimeout(r, delayMs));
      }
    }

    responseStream.end();
  }
);
```

部署函数：

```bash
zip -j streaming-basic.zip streaming-basic.mjs

aws lambda create-function \
  --function-name apigw-streaming-basic \
  --runtime nodejs20.x \
  --handler streaming-basic.handler \
  --role arn:aws:iam::<ACCOUNT_ID>:role/apigw-streaming-lambda-role \
  --zip-file fileb://streaming-basic.zip \
  --timeout 30 \
  --memory-size 128 \
  --region us-east-1
```

创建对照组——标准（buffered）Lambda：

```javascript
// buffered-basic.mjs — 标准 handler，返回完整响应
export const handler = async (event, context) => {
  const chunks = 10;
  const delayMs = parseInt(
    event.queryStringParameters?.delay || "500"
  );

  let body = "";
  for (let i = 0; i < chunks; i++) {
    body += `Chunk ${i + 1}/${chunks} at ${new Date().toISOString()}\n`;
    if (i < chunks - 1) {
      await new Promise(r => setTimeout(r, delayMs));
    }
  }

  return {
    statusCode: 200,
    headers: { "Content-Type": "text/plain" },
    body: body
  };
};
```

### Step 3: 创建 REST API 并配置 Streaming

```bash
# 创建 REST API
aws apigateway create-rest-api \
  --name "streaming-test" \
  --endpoint-configuration types=REGIONAL \
  --region us-east-1

# 获取 root resource ID
aws apigateway get-resources \
  --rest-api-id <API_ID> \
  --region us-east-1

# 创建 /stream 资源
aws apigateway create-resource \
  --rest-api-id <API_ID> \
  --parent-id <ROOT_ID> \
  --path-part stream \
  --region us-east-1

# 创建 GET 方法
aws apigateway put-method \
  --rest-api-id <API_ID> \
  --resource-id <STREAM_RESOURCE_ID> \
  --http-method GET \
  --authorization-type NONE \
  --region us-east-1

# 关键配置：设置 STREAM 模式和流式 Lambda URI
aws apigateway put-integration \
  --rest-api-id <API_ID> \
  --resource-id <STREAM_RESOURCE_ID> \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:us-east-1:lambda:path/2021-11-15/functions/<LAMBDA_ARN>/response-streaming-invocations" \
  --response-transfer-mode STREAM \
  --timeout-in-millis 30000 \
  --region us-east-1
```

注意两个关键参数：

- `--response-transfer-mode STREAM` — 启用流式响应
- URI 中使用 `/response-streaming-invocations` 而非 `/invocations`

对照组使用标准配置：

```bash
# /buffered 资源使用标准 invocations
aws apigateway put-integration \
  --rest-api-id <API_ID> \
  --resource-id <BUFFERED_RESOURCE_ID> \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/<LAMBDA_ARN>/invocations" \
  --timeout-in-millis 29000 \
  --region us-east-1
```

别忘了添加 Lambda 权限和部署 API：

```bash
# 授权 API Gateway 调用 Lambda
aws lambda add-permission \
  --function-name apigw-streaming-basic \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:us-east-1:<ACCOUNT_ID>:<API_ID>/*" \
  --region us-east-1

# 部署 API
aws apigateway create-deployment \
  --rest-api-id <API_ID> \
  --stage-name test \
  --region us-east-1
```

### Step 4: 测试对比

```bash
BASE_URL="https://<API_ID>.execute-api.us-east-1.amazonaws.com/test"

# 测试 Streaming TTFB
curl -s -o /dev/null -w "TTFB:%{time_starttransfer} Total:%{time_total}\n" \
  "$BASE_URL/stream?delay=500"

# 测试 Buffered TTFB
curl -s -o /dev/null -w "TTFB:%{time_starttransfer} Total:%{time_total}\n" \
  "$BASE_URL/buffered?delay=500"
```

## 测试结果

### 对比实验 1：TTFB（10 次平均值）

Lambda 生成 10 个 chunk，每个间隔 500ms，总计约 5 秒执行时间。

| 模式 | 平均 TTFB | 平均 Total | TTFB 改善 |
|------|-----------|-----------|----------|
| **Streaming** | **0.73s** | 5.25s | — |
| **Buffered** | **5.22s** | 5.22s | — |
| **差异** | **-4.49s** | +0.03s | **↓ 86%** |

关键发现：Streaming 模式下，第一个字节在 0.73 秒内到达客户端；Buffered 模式下需要等待全部 5 秒。总传输时间几乎相同，但用户体验截然不同。

### 对比实验 2：突破 29 秒超时限制

| 场景 | Streaming | Buffered |
|------|-----------|----------|
| 45 秒 Lambda 执行 | ✅ 成功（TTFB 1.0s） | ❌ 超时（最大 29s） |
| timeout 可设置范围 | 50ms - 900,000ms | 50ms - 29,000ms |

### 对比实验 3：大 Payload 传输

| Payload | TTFB | 总时间 | 平均速率 |
|---------|------|--------|---------|
| 5 MB | 0.73s | 2.41s | 2.07 MB/s |
| 10 MB | 0.70s | 2.74s | 3.65 MB/s |
| 15 MB ⭐ | 0.69s | 4.84s | 3.09 MB/s |
| 20 MB ⭐ | 0.70s | 7.16s | 2.79 MB/s |

⭐ 超过旧版 10MB 限制的 payload 均成功传输。超过 10MB 的部分可观察到限速效果（2MB/s），与官方文档一致。

### 边界测试：Idle Timeout

| Idle 时间 | Regional 端点结果 |
|-----------|------------------|
| 4 分钟 | ✅ 正常 — 第二个 chunk 成功到达 |
| 6 分钟 | ❌ 连接在 ~310s 后被切断 |

Regional 端点的 5 分钟 idle timeout 已实测确认。连接被**静默切断**——没有错误消息，因为 HTTP 200 状态码已经发送。

### GenAI 实战：Bedrock 流式推理

通过 Lambda 调用 Bedrock Claude 3 Haiku 流式推理，经 API Gateway 流式传输到客户端：

| 指标 | 数值（6 次平均） |
|------|----------------|
| TTFB | **0.47s** |
| 总时间 | 3.09s |

用户在 0.47 秒内就能看到第一个 token 开始输出——这对 AI 应用的交互体验至关重要。

## 踩坑记录

!!! warning "注意事项"

    1. **Lambda URI 路径必须区分** — Streaming 用 `/response-streaming-invocations`，Buffered 用 `/invocations`。用错了会得到 500 错误。（已查文档确认）

    2. **流式 Lambda 必须用 `streamifyResponse` 包裹 handler** — 不能用标准 handler 格式（返回 JSON 对象）。反过来，Buffered 端点不能用 `streamifyResponse` 的函数。

    3. **Idle timeout 是静默断开** — 超过 idle timeout 后连接直接关闭，客户端收不到任何错误信息。生产环境务必实现心跳机制。（已查文档确认：Regional/Private 5 分钟，Edge-optimized 30 秒）

    4. **API Gateway 部署传播有延迟** — 新增 resource 后 `create-deployment` 可能需要几秒才能生效。如果返回 403 "Missing Authentication Token"，等待后重试或手动 `update-stage` 指定 deploymentId。（实测发现，官方未记录）

    5. **Lambda 可能继续执行** — 客户端或 API Gateway 断开连接后，Lambda 函数可能继续运行直到超时。生产环境中需要适当设置 Lambda timeout。（已查文档确认）

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda 调用 | $0.20/100 万次 | ~100 次 | ~$0.00 |
| Lambda 计算 | $0.0000166667/GB·s | ~500 GB·s | ~$0.01 |
| API Gateway 请求 | $3.50/100 万次 | ~100 次 | ~$0.00 |
| API Gateway Streaming | $0.01/GB | ~0.5 GB | ~$0.01 |
| Bedrock Claude 3 Haiku | ~$0.00025/1K input + $0.00125/1K output | ~10 次 | ~$0.05 |
| **合计** | | | **~$0.07** |

## 清理资源

```bash
# 1. 删除 REST API（包含所有资源、方法、Stage）
aws apigateway delete-rest-api \
  --rest-api-id <API_ID> \
  --region us-east-1

# 2. 删除所有 Lambda 函数
for fn in apigw-streaming-basic apigw-streaming-basic-buffered \
  apigw-streaming-long-run apigw-streaming-bedrock \
  apigw-streaming-large-payload apigw-streaming-idle-test; do
  aws lambda delete-function --function-name $fn --region us-east-1
done

# 3. 删除 IAM 角色（先删内联策略和托管策略）
aws iam delete-role-policy \
  --role-name apigw-streaming-lambda-role \
  --policy-name bedrock-invoke

aws iam detach-role-policy \
  --role-name apigw-streaming-lambda-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam delete-role --role-name apigw-streaming-lambda-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然费用极低，但长期闲置的 API Gateway 仍会产生不必要的安全暴露面。

## 结论与建议

### 适用场景

- ✅ **GenAI/LLM 应用** — 流式输出 token，大幅提升交互体验
- ✅ **大文件传输** — 不再需要 S3 预签名 URL 作为中间方案
- ✅ **长时间运行任务** — 实时进度更新，最长支持 15 分钟
- ✅ **SSE（Server-Sent Events）** — 增量推送场景

### 不适用场景

- ❌ 需要 Endpoint Caching 的 API
- ❌ 需要 VTL Response Mapping 的复杂转换
- ❌ Edge-optimized endpoint 的长 idle 场景（30 秒限制太短）

### 生产环境建议

1. **实现心跳机制** — 避免 idle timeout 断连，每 2-3 分钟发送一次心跳数据
2. **设置合理的 Lambda timeout** — 比 API Gateway timeout 略短，确保函数不会在断连后持续运行
3. **监控 TTFB 指标** — 启用 CloudWatch 指标监控流式 API 的性能
4. **客户端做好断连重试** — idle timeout 断开是静默的，客户端需要检测并重试

## 参考链接

- [官方文档 — Response Transfer Mode](https://docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode.html)
- [AWS Blog — Building responsive APIs with API Gateway response streaming](https://aws.amazon.com/blogs/compute/building-responsive-apis-with-amazon-api-gateway-response-streaming/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/11/api-gateway-response-streaming-rest-apis/)
- [PutIntegration API Reference](https://docs.aws.amazon.com/apigateway/latest/api/API_PutIntegration.html)
