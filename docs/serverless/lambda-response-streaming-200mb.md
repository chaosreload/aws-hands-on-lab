---
tags:
  - Serverless
---

# AWS Lambda Response Streaming 200 MB 实测：从入门到边界探索

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

AWS Lambda Response Streaming 自推出以来，默认最大响应负载一直是 20 MB。2025 年 7 月，AWS 将这一限制提升至 **200 MB**（10 倍增长），这意味着你可以直接在 Lambda 中处理大型数据集、图片密集型 PDF、甚至音频文件，而无需借助 S3 做中转。

这项改进对以下场景尤其有价值：

- **实时 AI 对话**：大模型生成长篇响应时，流式传输显著降低用户感知延迟
- **数据处理管道**：处理大型 JSON/CSV 数据集后直接流式返回结果
- **文件生成**：动态生成报告、PDF 等大文件并实时推送给客户端

本文将通过实际测试验证 200 MB 限制的真实表现，包括带宽分层模型、边界行为和性能对比。

## 前置条件

- AWS 账号（需要 Lambda、IAM 权限）
- AWS CLI v2 已配置
- Python 3 + boto3（用于 SDK 调用测试）

## 核心概念

### Streaming vs Buffered 对比

| 维度 | Buffered 模式 | Streaming 模式 |
|------|--------------|---------------|
| 最大响应 | 6 MB | **200 MB** |
| TTFB | 等待全部生成完 | 数据就绪即推送 |
| 内存需求 | 需容纳完整响应 | 流式写入，无需全部加载 |
| 调用 API | `Invoke` | `InvokeWithResponseStream` |
| Function URL 模式 | `BUFFERED` | `RESPONSE_STREAM` |

### 带宽分层模型

Lambda Response Streaming 的带宽并非均匀分配：

- **前 6 MB**：无限速（uncapped），实测可达数十 MBps
- **6 MB 以后**：带宽上限 **2 MBps**

这意味着小于 6 MB 的响应几乎瞬时完成，而 200 MB 的大负载理论上需要约 97 秒传输。

### 运行时支持

- **Node.js 托管运行时**：原生支持 `awslambda.streamifyResponse()` 装饰器
- **其他语言（Python 等）**：需要自定义运行时或 [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)

## 动手实践

### Step 1: 创建 IAM 角色

```bash
aws iam create-role \
  --role-name lambda-stream-test-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region us-east-1

aws iam attach-role-policy \
  --role-name lambda-stream-test-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
  --region us-east-1
```

### Step 2: 创建 Streaming Lambda 函数

创建 `index.mjs`：

```javascript
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';

export const handler = awslambda.streamifyResponse(
  async (event, responseStream, _context) => {
    const sizeMB = parseInt(
      event.queryStringParameters?.size || event.size || '1', 10
    );
    const totalBytes = sizeMB * 1024 * 1024;
    const chunkSize = 64 * 1024; // 64KB chunks
    let written = 0;

    const startTime = Date.now();

    // 分块写入数据
    const chunk = Buffer.alloc(chunkSize, 'A');
    while (written < totalBytes) {
      const remaining = totalBytes - written;
      const toWrite = remaining < chunkSize
        ? Buffer.alloc(remaining, 'A')
        : chunk;
      responseStream.write(toWrite);
      written += toWrite.length;
    }

    responseStream.end();
  }
);
```

打包并部署：

```bash
zip -j function.zip index.mjs

aws lambda create-function \
  --function-name stream-test-200mb \
  --runtime nodejs22.x \
  --handler index.handler \
  --role arn:aws:iam::<ACCOUNT_ID>:role/lambda-stream-test-role \
  --zip-file fileb://function.zip \
  --timeout 300 \
  --memory-size 1024 \
  --region us-east-1
```

### Step 3: 创建 Function URL（可选）

```bash
aws lambda create-function-url-config \
  --function-name stream-test-200mb \
  --auth-type AWS_IAM \
  --invoke-mode RESPONSE_STREAM \
  --region us-east-1
```

!!! note "关于 Function URL 认证"
    建议使用 `AWS_IAM` 认证类型。如果使用 `NONE`，部分账户可能因为组织策略阻止公开访问。
    实际生产中，推荐通过 SDK `InvokeWithResponseStream` API 调用。

### Step 4: 通过 SDK 调用测试

创建 `invoke_stream.py`：

```python
import boto3
import json
import sys
import time

client = boto3.client("lambda", region_name="us-east-1")

size_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 1
func_name = "stream-test-200mb"

print(f"Invoking {func_name} with size={size_mb} MB...")
start = time.time()

response = client.invoke_with_response_stream(
    FunctionName=func_name,
    Payload=json.dumps({"size": str(size_mb)}).encode(),
)

ttfb = None
total_bytes = 0
chunk_count = 0

for event in response["EventStream"]:
    if "PayloadChunk" in event:
        if ttfb is None:
            ttfb = (time.time() - start) * 1000
        total_bytes += len(event["PayloadChunk"]["Payload"])
        chunk_count += 1
    if "InvokeComplete" in event:
        ic = event["InvokeComplete"]
        if "ErrorCode" in ic:
            print(f"Error: {ic['ErrorCode']}")

total_ms = (time.time() - start) * 1000
print(f"TTFB: {ttfb:.0f}ms | Total: {total_ms:.0f}ms | "
      f"Bytes: {total_bytes:,} | Chunks: {chunk_count}")
```

运行测试：

```bash
# 1 MB 测试
python3 invoke_stream.py 1

# 50 MB 测试
python3 invoke_stream.py 50

# 199 MB 测试（安全上限）
python3 invoke_stream.py 199
```

## 测试结果

### 不同负载大小的性能表现

| 负载大小 | TTFB (ms) | 总时间 | 吞吐量 (MBps) | 块数 | 状态 |
|----------|-----------|--------|--------------|------|------|
| 1 MB | 947 | 2.0s | 0.49 | 51 | ✅ |
| 10 MB | 717 | 2.9s | 3.41 | 372 | ✅ |
| 50 MB | 918 | 5.3s | 9.37 | 1,839 | ✅ |
| 199 MB | 695 | 68.3s | 2.91 | 7,744 | ✅ |
| 200 MB+ | 988 | 68.9s | 2.91 | 7,830 | ❌ ResponseSizeTooLarge |

> 测试环境：从同区域 EC2 (us-east-1) 通过 SDK `InvokeWithResponseStream` 调用

### 关键发现

**1. TTFB 与负载大小无关**

无论响应负载是 1 MB 还是 199 MB，首字节时间（TTFB）稳定在 **700-1000ms** 范围。这证实了 Streaming 模式的核心价值——客户端无需等待完整响应生成。

**2. 带宽分层清晰可见**

- 50 MB 负载的平均吞吐量达到 9.37 MBps（前 6 MB burst 拉高了均值）
- 199 MB 负载的平均吞吐量降至 2.91 MBps（接近 2 MBps 限制，受大量 >6MB 数据拖累）
- 实测中 2 MBps 限制并非严格硬限，同区域调用可略超此值

**3. 200 MB 是精确硬限制**

超过 209,715,200 bytes (200 × 1024 × 1024) 后，Lambda 立即截断响应并返回 `Function.ResponseSizeTooLarge` 错误。已传输的数据仍然有效。

### Buffered vs Streaming 对比（3 MB 负载）

| 模式 | 响应时间 | 特点 |
|------|---------|------|
| Buffered | 740ms（一次性返回） | 简单、延迟低（小负载） |
| Streaming | 677ms TTFB / 2,347ms 总计 | 更早开始接收，总时间略长 |

> 对于小于 6 MB 的负载，Buffered 模式更简单高效。Streaming 的优势在大负载场景才真正体现。

## 踩坑记录

!!! warning "Function URL 公开访问可能被阻止"
    设置 `auth-type NONE` 并添加了 `Principal: *` 的资源策略后，仍返回 403 AccessDeniedException。
    这可能是账户级别策略或 SCP 限制了公开 Lambda Function URL 访问。
    **建议**：生产环境使用 `AWS_IAM` 认证 + SDK 调用。

!!! warning "Buffered 模式的 6 MB 限制包含 base64 编码"
    通过 `Invoke` API 返回二进制数据时需要 base64 编码，编码后体积增加约 33%。
    因此 buffered 模式实际可返回的原始二进制数据约 **4.5 MB**（6 MB / 1.33）。
    **已查文档确认**：gettingstarted-limits.html 明确标注 "6 MB each for request and response (synchronous)"。

!!! warning "Response Streaming 函数在控制台始终显示 buffered"
    Lambda 控制台的测试功能不支持 streaming 输出，始终返回 buffered 结果。需要通过 Function URL 或 SDK 测试。
    **已查文档确认**：configuration-response-streaming.html 明确标注此行为。

!!! tip "超限行为：截断而非崩溃"
    超过 200 MB 限制时，Lambda 不会让函数 crash，而是截断响应并返回 `Function.ResponseSizeTooLarge` 错误码。
    已传输的数据对客户端仍然可用。
    **实测发现，官方文档未明确记录此错误码的具体行为**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Lambda 执行 | $0.0000166667/GB-s | ~200 GB-s | ~$3.34 |
| Lambda 请求 | $0.20/百万次 | 12 次 | ~$0.00 |
| 数据传输（同区域） | $0.00 | ~500 MB | $0.00 |
| **合计** | | | **< $5** |

## 清理资源

```bash
# 1. 删除 Function URL
aws lambda delete-function-url-config \
  --function-name stream-test-200mb \
  --region us-east-1

aws lambda delete-function-url-config \
  --function-name buffered-test-200mb \
  --region us-east-1

# 2. 删除 Lambda 函数
aws lambda delete-function \
  --function-name stream-test-200mb \
  --region us-east-1

aws lambda delete-function \
  --function-name buffered-test-200mb \
  --region us-east-1

# 3. 删除 CloudWatch 日志组
aws logs delete-log-group \
  --log-group-name /aws/lambda/stream-test-200mb \
  --region us-east-1

aws logs delete-log-group \
  --log-group-name /aws/lambda/buffered-test-200mb \
  --region us-east-1

# 4. 分离并删除 IAM 角色
aws iam detach-role-policy \
  --role-name lambda-stream-test-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam delete-role \
  --role-name lambda-stream-test-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。特别是带有公开 Function URL 的 Lambda 函数。

## 结论与建议

### 适用场景

- ✅ **AI/LLM 流式输出**：大模型生成长篇内容时，用户可以实时看到输出
- ✅ **大文件生成**：动态生成报告、PDF、CSV 等超过 6 MB 的文件
- ✅ **实时数据处理**：处理大型数据集并流式返回结果

### 何时继续使用 Buffered

- 响应 < 6 MB 且对总延迟敏感（buffered 模式在小负载下更快）
- 需要简单的 request/response 模型
- 下游服务不支持流式消费

### 生产建议

1. **提前评估响应大小**：如果可能超过 6 MB，直接用 streaming 模式
2. **注意超时设置**：200 MB 在 2 MBps 限制下需要 ~97s 传输，确保 timeout 足够
3. **客户端断连不停止计费**：函数会继续执行到完成，合理设置 timeout
4. **监控带宽**：前 6 MB 快速传输，后续降速，设计 UX 时需考虑
5. **VPC 注意**：Function URL 在 VPC 内不支持 streaming，需用 SDK + VPC Endpoint

## 参考链接

- [Lambda Response Streaming 官方文档](https://docs.aws.amazon.com/lambda/latest/dg/configuration-response-streaming.html)
- [编写 Streaming 函数](https://docs.aws.amazon.com/lambda/latest/dg/config-rs-write-functions.html)
- [Lambda 限制与配额](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-lambda-response-streaming-200-mb-payloads/)
