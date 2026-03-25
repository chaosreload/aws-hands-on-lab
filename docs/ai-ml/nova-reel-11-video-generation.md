# Amazon Nova Reel 1.1 实战：从文字到 2 分钟 AI 视频

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: ~$15（基础测试） / ~$90（全套含 2 分钟视频）
    - **Region**: us-east-1（1.1 仅此 Region）
    - **最后验证**: 2026-03-25

## 背景

2025 年 4 月，AWS 发布 Amazon Nova Reel 1.1，将 AI 视频生成能力从单镜头 6 秒提升至多镜头 2 分钟。这是 Bedrock 平台上首个支持长视频生成的模型。

**1.0 → 1.1 核心升级**：

| 特性 | Nova Reel 1.0 | Nova Reel 1.1 |
|------|--------------|--------------|
| 最大视频时长 | 6 秒 | **2 分钟**（120 秒） |
| 多镜头支持 | ❌ | ✅ 自动/手动模式 |
| 风格一致性 | N/A | ✅ 跨镜头一致 |
| Prompt 长度 | 512 字符 | 自动模式 **4000 字符** |
| Region | us-east-1, eu-west-1, ap-northeast-1 | 仅 us-east-1 |

## 前置条件

- AWS 账号，已开通 Amazon Bedrock 中的 Nova Reel 模型访问
- AWS CLI v2 + Python 3.10+ + boto3
- S3 Bucket（us-east-1，用于存储生成的视频）
- IAM 权限：`bedrock:InvokeModel`、`bedrock:GetAsyncInvoke`、`bedrock:ListAsyncInvokes`、`s3:PutObject`

## 核心概念

Nova Reel 1.1 通过 **异步 API**（`start_async_invoke`）工作，不支持同步调用。提交任务后，视频在后台生成，完成后自动写入指定的 S3 路径。

**三种任务类型**：

1. **TEXT_VIDEO** — 单镜头 6 秒，支持纯文本或文本+参考图（Image-to-Video）
2. **MULTI_SHOT_AUTOMATED** — 自动多镜头，单 prompt 描述整体场景，模型自动分镜（12-120 秒）
3. **MULTI_SHOT_MANUAL** — 手动多镜头，每个 shot 独立 prompt + 可选参考图，精细控制

**输出规格**：1280×720, 24fps, MP4 格式

## 动手实践

### Step 1: 准备环境

```bash
# 创建输出 S3 Bucket
aws s3 mb s3://nova-reel-test-$(aws sts get-caller-identity --query Account --output text) \
    --region us-east-1

# 设置变量
export BUCKET="s3://nova-reel-test-$(aws sts get-caller-identity --query Account --output text)"
export AWS_DEFAULT_REGION=us-east-1
```

### Step 2: 基础 Text-to-Video（6 秒）

创建 `t2v_basic.py`：

```python
import json
import boto3

bedrock_runtime = boto3.client("bedrock-runtime", region_name="us-east-1")

model_input = {
    "taskType": "TEXT_VIDEO",
    "textToVideoParams": {
        "text": "A golden sunset over calm ocean waves, gentle light reflects "
                "on the water surface. Camera slowly pans right revealing a "
                "sandy beach with palm trees."
    },
    "videoGenerationConfig": {
        "durationSeconds": 6,
        "fps": 24,
        "dimension": "1280x720",
        "seed": 42
    }
}

invocation = bedrock_runtime.start_async_invoke(
    modelId="amazon.nova-reel-v1:1",
    modelInput=model_input,
    outputDataConfig={
        "s3OutputDataConfig": {
            "s3Uri": "s3://YOUR_BUCKET/t2v-basic/"  # 注意尾部斜杠！
        }
    }
)

print(f"Job started: {invocation['invocationArn']}")
```

!!! warning "S3 URI 必须以 `/` 结尾"
    如果 S3 URI 不带尾部斜杠，API 会返回 `ValidationException: The provided S3 URI does not point to a bucket or a directory`。这是实测发现的细节，官方文档未明确说明。

### Step 3: 查询任务状态

```python
import json
import boto3

bedrock_runtime = boto3.client("bedrock-runtime", region_name="us-east-1")

invocation = bedrock_runtime.get_async_invoke(
    invocationArn="YOUR_INVOCATION_ARN"
)

status = invocation["status"]  # "InProgress", "Completed", "Failed"
print(f"Status: {status}")

if status == "Completed":
    s3_uri = invocation["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
    print(f"Video: {s3_uri}/output.mp4")
```

### Step 4: Image-to-Video（参考图 → 视频）

使用一张 1280×720 的图片作为视频首帧：

```python
import json
import boto3
import base64

bedrock_runtime = boto3.client("bedrock-runtime", region_name="us-east-1")

# 加载参考图（必须是 1280x720）
with open("reference.png", "rb") as f:
    image_base64 = base64.b64encode(f.read()).decode("utf-8")

model_input = {
    "taskType": "TEXT_VIDEO",
    "textToVideoParams": {
        "text": "Camera slowly dolly forward, cherry blossom petals "
                "gently falling, koi fish swimming in the pond below",
        "images": [{
            "format": "png",
            "source": {"bytes": image_base64}
        }]
    },
    "videoGenerationConfig": {
        "durationSeconds": 6,
        "fps": 24,
        "dimension": "1280x720",
        "seed": 42
    }
}

invocation = bedrock_runtime.start_async_invoke(
    modelId="amazon.nova-reel-v1:1",
    modelInput=model_input,
    outputDataConfig={
        "s3OutputDataConfig": {
            "s3Uri": "s3://YOUR_BUCKET/i2v-test/"
        }
    }
)

print(f"I2V Job: {invocation['invocationArn']}")
```

### Step 5: 多镜头自动模式（18 秒）

一个 prompt，模型自动分成 3 个连贯镜头：

```python
model_input = {
    "taskType": "MULTI_SHOT_AUTOMATED",
    "multiShotAutomatedParams": {
        "text": "A cinematic documentary about the ocean. "
                "Aerial drone footage of deep blue ocean waves crashing "
                "on rocky cliffs. Underwater scene with colorful coral reef "
                "and tropical fish. A whale breaches the surface in slow "
                "motion with water droplets catching sunlight."
    },
    "videoGenerationConfig": {
        "durationSeconds": 18,  # 必须是 6 的倍数，范围 12-120
        "fps": 24,
        "dimension": "1280x720",
        "seed": 1234
    }
}

invocation = bedrock_runtime.start_async_invoke(
    modelId="amazon.nova-reel-v1:1",
    modelInput=model_input,
    outputDataConfig={
        "s3OutputDataConfig": {
            "s3Uri": "s3://YOUR_BUCKET/multishot-auto/"
        }
    }
)
```

完成后 S3 中会有：

```
multishot-auto/<invocation-id>/
├── manifest.json
├── output.mp4          # 拼接完整视频
├── shot_0001.mp4       # 第 1 个 6 秒镜头
├── shot_0002.mp4       # 第 2 个 6 秒镜头
├── shot_0003.mp4       # 第 3 个 6 秒镜头
└── video-generation-status.json
```

### Step 6: 多镜头手动模式（精细控制）

每个镜头独立 prompt，可选参考图：

```python
model_input = {
    "taskType": "MULTI_SHOT_MANUAL",
    "multiShotManualParams": {
        "shots": [
            {
                "text": "Aerial view of a futuristic city with glass "
                        "skyscrapers at dawn, golden light hitting buildings"
            },
            {
                "text": "Street level view of autonomous vehicles moving "
                        "smoothly through the city, pedestrians on sidewalks"
            },
            {
                "text": "Close-up of a robotic hand gently touching a flower "
                        "in a rooftop garden, city skyline in background"
            }
        ]
    },
    "videoGenerationConfig": {
        "fps": 24,
        "dimension": "1280x720",
        "seed": 1234
    }
}

invocation = bedrock_runtime.start_async_invoke(
    modelId="amazon.nova-reel-v1:1",
    modelInput=model_input,
    outputDataConfig={
        "s3OutputDataConfig": {
            "s3Uri": "s3://YOUR_BUCKET/multishot-manual/"
        }
    }
)
```

!!! tip "手动模式 vs 自动模式"
    - **自动模式**：一个长 prompt（最多 4000 字符），模型决定分镜，适合快速生成
    - **手动模式**：每个 shot 独立 prompt（最多 512 字符），可附参考图，适合精确控制叙事

### Step 7: 挑战——2 分钟长视频

```python
model_input = {
    "taskType": "MULTI_SHOT_AUTOMATED",
    "multiShotAutomatedParams": {
        "text": "An epic nature documentary. Begin with a sunrise over "
                "snow-capped mountains, then transition to a vast green "
                "valley with a winding river. Show herds of wild horses "
                "galloping across golden plains. Cut to a dense tropical "
                "rainforest with exotic birds and waterfalls. End with a "
                "breathtaking aurora borealis over a frozen arctic landscape."
    },
    "videoGenerationConfig": {
        "durationSeconds": 120,  # 最大值
        "fps": 24,
        "dimension": "1280x720",
        "seed": 777
    }
}
```

## 测试结果

### 生成性能

| 测试 | 任务类型 | 视频时长 | 生成时间 | 文件大小 | 每秒视频的生成耗时 |
|------|----------|----------|----------|----------|-------------------|
| T2V 基础 | TEXT_VIDEO | 6s | 79s | 5.50 MB | 13.2s |
| Image-to-Video | TEXT_VIDEO+image | 6s | 70s | 5.59 MB | 11.7s |
| 多镜头自动 (18s) | MULTI_SHOT_AUTOMATED | 18s | 152s | 16.38 MB | 8.4s |
| 多镜头手动 (18s) | MULTI_SHOT_MANUAL | 18s | 146s | 9.41 MB | 8.1s |
| 长视频 (60s) | MULTI_SHOT_AUTOMATED | 60s | 351s | 37.7 MB | 5.9s |
| 长视频 (120s) | MULTI_SHOT_AUTOMATED | 120s | 509s | 44.1 MB | 4.2s |
| 摄像机运动 | TEXT_VIDEO | 6s | 76s | 6.02 MB | 12.7s |

### 关键发现

1. **实际生成速度比文档标称更快**：文档说 6 秒视频需要约 90 秒、2 分钟视频需要 14-17 分钟，实测分别为 ~76 秒和 ~8.5 分钟
2. **多镜头有规模效应**：视频越长，每秒视频的生成耗时越短（6 秒: 13s/s → 120 秒: 4.2s/s）
3. **I2V 比纯 T2V 更快**：提供参考图后生成时间从 79 秒降至 70 秒
4. **Seed 确实控制生成**：相同 prompt + 不同 seed → 完全不同的视频内容和文件大小
5. **多镜头同时输出分镜和拼接版**：自动生成 `shot_XXXX.mp4` + 拼接的 `output.mp4`

### Seed 对比实验

同一 prompt "A red sports car driving along a coastal highway"：

| Seed | 文件大小 | 生成时间 |
|------|----------|----------|
| 42 | 4.36 MB | 76s |
| 999 | 5.21 MB | 77s |

不同 seed 产生完全不同的视频，文件大小差异达 19%。

## 踩坑记录

!!! warning "踩坑 1: S3 URI 必须以 `/` 结尾"
    **问题**：`s3://bucket/prefix` 会返回 ValidationException。
    **解决**：使用 `s3://bucket/prefix/`。**实测发现，官方文档未明确记录。**

!!! warning "踩坑 2: API 限流较严格"
    **问题**：快速连续提交多个 job 会触发 ThrottlingException 或 ServiceUnavailableException。
    **解决**：请求间增加 10-15 秒间隔，并实现指数退避重试。**已查文档确认，这是正常的服务容量限制。**

!!! warning "踩坑 3: 长视频可能失败"
    **问题**：120 秒视频首次尝试失败（shot 11 遇到 INTERNAL_SERVER_EXCEPTION，导致级联失败），重试后成功。
    **解决**：长视频建议实现重试机制。失败的 shot 状态会记录在 `video-generation-status.json` 中。**已查文档确认，INTERNAL_SERVER_EXCEPTION 是已知的失败类型之一。**

## 费用说明

Nova Reel 按生成的视频秒数计费。具体单价请查看 [Amazon Bedrock 定价页](https://aws.amazon.com/bedrock/pricing/)（选择 Amazon → Pricing for Creative Content Generation models）。

本次测试共生成约 258 秒视频 + 1 张 Nova Canvas 参考图。

## 清理资源

```bash
# 删除测试 S3 Bucket 及所有内容
aws s3 rb s3://nova-reel-test-YOUR_ACCOUNT_ID --force --region us-east-1

# 无需清理其他资源（Bedrock 异步 job 自动归档）
```

!!! danger "务必清理"
    S3 中的视频文件会持续产生存储费用。Lab 完成后请清理 Bucket。

## 结论与建议

**Nova Reel 1.1 适合什么场景**：

- ✅ 营销短视频、社交媒体内容（6-18 秒）
- ✅ 产品演示、概念验证视频
- ✅ 基于品牌图片的动态内容（I2V 模式）
- ⚠️ 2 分钟长视频可用但稳定性待观察
- ❌ 不适合需要精确人物/文字渲染的场景

**生产环境建议**：

1. **必须实现重试机制** — 长视频和高并发场景下 API 可能返回 503 或限流
2. **使用手动模式控制叙事** — 自动模式方便但对分镜控制有限
3. **Seed 用于可复现性** — 同一 seed + 同一 prompt 保证结果一致
4. **注意 Region 限制** — 1.1 仅 us-east-1，跨区域需考虑延迟

## 参考链接

- [Amazon Nova Reel 用户指南](https://docs.aws.amazon.com/nova/latest/userguide/video-generation.html)
- [AWS What's New: Amazon Nova Reel 1.1](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-nova-reel-1-1/)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [Amazon Nova Creative Models](https://aws.amazon.com/ai/generative-ai/nova/creative/)
