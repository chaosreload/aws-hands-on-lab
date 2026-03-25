---
description: "Amazon Nova 2 Sonic 实测：跨模态输入、Polyglot 多语言语音、v1 对比的 Hands-on Lab"
---
# Amazon Nova 2 Sonic 实测：跨模态对话 + Polyglot 语音 + v1 对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: <$0.50（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

Amazon Nova 2 Sonic 是 AWS 第二代 speech-to-speech 模型，在 [Nova Sonic (v1)](nova-sonic-speech-to-speech.md) 基础上做了重大升级。核心变化不是更快——而是更灵活：

- **跨模态输入**：在同一对话中混合文本和语音，不再只能"听"
- **Polyglot 语音**：同一个 voice（如 tiffany）能说 7 种语言，告别"每种语言换一个声音"
- **1M token 上下文**：从 300K 扩展到 1M，支持持续交互
- **异步 Tool Calling**：工具调用时模型继续对话，不中断

本文实测验证这些新功能，并与 v1 做直接对比。

## 前置条件

- AWS 账号（需要 Amazon Bedrock 访问权限）
- Python 3.12+（Smithy SDK 要求）
- 在 Bedrock Console 中启用 Nova 2 Sonic 模型访问

## 核心概念

### Nova Sonic v1 → Nova 2 Sonic 升级一览

| 维度 | Nova Sonic v1 | Nova 2 Sonic |
|------|--------------|--------------|
| Model ID | `amazon.nova-sonic-v1:0` | `amazon.nova-2-sonic-v1:0` |
| 语言 | EN, FR, IT, DE, ES (5) | +PT, HI (7) |
| Context Window | 300K tokens | 1M tokens |
| 跨模态输入 | ❌ 仅音频 | ✅ 文本+音频混合 |
| Polyglot 语音 | ❌ | ✅ 同一 voice 多语言 |
| 异步 Tool Calling | ❌ 同步阻塞 | ✅ 后台执行不中断 |
| Turn-taking 控制 | 固定 | high/medium/low 可调 |
| 英语口音 | US, UK | US, UK, India, Australia |
| 电话集成 | ❌ | ✅ Connect, Twilio, Vonage |
| Region | us-east-1, eu-north-1, ap-northeast-1 | us-east-1, us-west-2, ap-northeast-1 |

### API 架构

Nova 2 Sonic 沿用 v1 的双向流式 API（`InvokeModelWithBidirectionalStream`），事件协议完全兼容。迁移只需改 model ID。

```
客户端 ─── 事件流 ──→ Nova 2 Sonic
       ←── 事件流 ───
```

关键事件：`sessionStart` → `promptStart` → `contentStart` → `audioInput`/`textInput` → `contentEnd` → 模型响应

## 动手实践

### Step 1: 安装 Smithy SDK

Nova Sonic 系列使用 AWS 新一代 Smithy-based Python SDK，不是 boto3：

```bash
# 创建 Python 3.12 虚拟环境
python3.12 -m venv nova2-sonic-env
source nova2-sonic-env/bin/activate

# 安装 Smithy 核心包（从源码）
git clone --depth 1 https://github.com/smithy-lang/smithy-python.git
cd smithy-python
pip install packages/smithy-core packages/smithy-http packages/smithy-json \
  packages/smithy-aws-core packages/smithy-aws-event-stream packages/aws-sdk-signers

# 安装 Bedrock Runtime SDK + 依赖
pip install aws_sdk_bedrock_runtime boto3
```

!!! warning "踩坑：SDK 安装"
    `aws_sdk_bedrock_runtime` 依赖未完全发布到 PyPI 的 Smithy 包。**必须先手动从源码安装 Smithy 依赖**，否则 pip install 会找不到包。**已查文档确认**：这是当前 SDK 的已知安装流程。

### Step 2: 准备测试音频

用 Amazon Polly 生成 PCM 格式的多语言测试音频：

```bash
# 英语
aws polly synthesize-speech \
  --output-format pcm --sample-rate 16000 \
  --voice-id Matthew --engine neural \
  --text "What are the three largest planets in our solar system?" \
  --region us-east-1 test_en.pcm

# 葡萄牙语（v2 新增语言）
aws polly synthesize-speech \
  --output-format pcm --sample-rate 16000 \
  --voice-id Camila --engine neural \
  --text "Qual e a capital do Brasil?" \
  --region us-east-1 test_pt.pcm

# 西班牙语
aws polly synthesize-speech \
  --output-format pcm --sample-rate 16000 \
  --voice-id Lupe --engine neural \
  --text "Cuantos continentes hay en el mundo?" \
  --region us-east-1 test_es.pcm
```

### Step 3: 建立双向流连接

```python
import asyncio, json, uuid, time, base64, os, wave
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
)
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

REGION = "us-east-1"
MODEL_V2 = "amazon.nova-2-sonic-v1:0"
MODEL_V1 = "amazon.nova-sonic-v1:0"
SILENCE = base64.b64encode(b'\x00' * 3200).decode()  # 100ms 静音

def make_client():
    config = Config(
        endpoint_uri=f"https://bedrock-runtime.{REGION}.amazonaws.com",
        region=REGION,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
    )
    return BedrockRuntimeClient(config=config)

async def send_event(stream, event_dict):
    chunk = InvokeModelWithBidirectionalStreamInputChunk(
        value=BidirectionalInputPayloadPart(
            bytes_=json.dumps(event_dict).encode("utf-8")
        )
    )
    await stream.input_stream.send(chunk)
```

### Step 4: 测试跨模态输入（v2 独有）

这是 Nova 2 Sonic 最重要的新功能之一——在语音对话中直接发送文本：

```python
async def test_crossmodal(model_id, text, voice_id="matthew"):
    """跨模态：在活跃音频流中插入文本输入"""
    client = make_client()
    stream = await client.invoke_model_with_bidirectional_stream(
        InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
    )
    prompt_name = str(uuid.uuid4())
    audio_cn = str(uuid.uuid4())
    text_cn = str(uuid.uuid4())
    sys_cn = str(uuid.uuid4())

    # 1. 会话初始化
    await send_event(stream, {"event": {"sessionStart": {
        "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7}
    }}})
    await send_event(stream, {"event": {"promptStart": {
        "promptName": prompt_name,
        "textOutputConfiguration": {"mediaType": "text/plain"},
        "audioOutputConfiguration": {
            "mediaType": "audio/lpcm", "sampleRateHertz": 24000,
            "sampleSizeBits": 16, "channelCount": 1,
            "voiceId": voice_id, "encoding": "base64", "audioType": "SPEECH"
        }
    }}})

    # 2. 系统提示词
    await send_event(stream, {"event": {"contentStart": {
        "promptName": prompt_name, "contentName": sys_cn,
        "type": "TEXT", "interactive": False, "role": "SYSTEM",
        "textInputConfiguration": {"mediaType": "text/plain"}
    }}})
    await send_event(stream, {"event": {"textInput": {
        "promptName": prompt_name, "contentName": sys_cn,
        "content": "You are a helpful assistant. Be concise."
    }}})
    await send_event(stream, {"event": {"contentEnd": {
        "promptName": prompt_name, "contentName": sys_cn
    }}})

    # 3. 开启音频流（必须！跨模态需要活跃的音频流）
    await send_event(stream, {"event": {"contentStart": {
        "promptName": prompt_name, "contentName": audio_cn,
        "type": "AUDIO", "interactive": True, "role": "USER",
        "audioInputConfiguration": {
            "mediaType": "audio/lpcm", "sampleRateHertz": 16000,
            "sampleSizeBits": 16, "channelCount": 1,
            "audioType": "SPEECH", "encoding": "base64"
        }
    }}})

    # 发送少量静音帧建立音频流
    for _ in range(5):
        await send_event(stream, {"event": {"audioInput": {
            "promptName": prompt_name, "contentName": audio_cn,
            "content": SILENCE
        }}})
        await asyncio.sleep(0.05)

    # 4. 发送文本输入（跨模态核心）
    await send_event(stream, {"event": {"contentStart": {
        "promptName": prompt_name, "contentName": text_cn,
        "type": "TEXT", "interactive": True, "role": "USER",
        "textInputConfiguration": {"mediaType": "text/plain"}
    }}})
    await send_event(stream, {"event": {"textInput": {
        "promptName": prompt_name, "contentName": text_cn,
        "content": text
    }}})
    await send_event(stream, {"event": {"contentEnd": {
        "promptName": prompt_name, "contentName": text_cn
    }}})

    # 5. 持续发送静音，等待响应
    # ... (同时异步接收响应事件)
```

!!! warning "踩坑：跨模态需要活跃音频流"
    纯文本输入（不开音频流）会导致 55 秒超时断连。跨模态的正确做法是：先 `contentStart(AUDIO)` + 发静音帧，再发 `contentStart(TEXT)` + `textInput`。**实测发现，官方文档未明确说明此要求。**

### Step 5: 测试音频输入（v1 vs v2 对比）

```python
async def test_audio(model_id, audio_path, voice_id="matthew"):
    """音频输入测试，带静音帧保持连接"""
    with open(audio_path, "rb") as f:
        audio_data = f.read()

    # ... (建立连接和会话，同 Step 3-4)

    # 分块发送音频（每块 100ms = 3200 字节 @ 16kHz 16-bit）
    for i in range(0, len(audio_data), 3200):
        chunk = audio_data[i:i+3200]
        b64 = base64.b64encode(chunk).decode()
        await send_event(stream, {"event": {"audioInput": {
            "promptName": prompt_name,
            "contentName": audio_cn,
            "content": b64
        }}})
        await asyncio.sleep(0.05)

    # 发送静音帧触发 end-of-speech 检测
    for _ in range(50):
        await send_event(stream, {"event": {"audioInput": {
            "promptName": prompt_name,
            "contentName": audio_cn,
            "content": SILENCE
        }}})
        await asyncio.sleep(0.05)
```

!!! tip "静音帧的作用"
    发送完实际音频后，持续发送 **静音帧** 是 headless 测试的关键。这模拟了"用户说完话后的沉默"，让模型检测到语音结束并开始响应。

### Step 6: 测试 Polyglot 语音

使用同一个 `tiffany` 语音分别说英语和西班牙语：

```python
# 英语
await test_crossmodal(MODEL_V2,
    "Hello! Tell me about Mars in one sentence.",
    voice_id="tiffany")

# 同一个 tiffany 语音，切换到西班牙语
await test_crossmodal(MODEL_V2,
    "Ahora, dime sobre Venus en una frase en espanol.",
    voice_id="tiffany")
```

## 测试结果

### 核心指标

| 测试 | 模型 | TTFB 文本 | TTFB 音频 | 音频输出时长 | ASR/响应 |
|------|------|----------|----------|------------|---------|
| 跨模态文本(EN) | v2 | **1.665s** | 1.796s | 8.4s | ✅ 正确回答 |
| 音频输入(EN) | v2 | 3.166s | 3.286s | 15.4s | ✅ ASR+语音回复 |
| 音频输入(EN) | v1 | 3.000s | 3.188s | 11.0s | ✅ ASR+语音回复 |
| 葡萄牙语音频 | v2 | **2.809s** | 2.941s | 21.4s | ✅ 葡萄牙语回复 |
| Polyglot EN | v2 | **1.794s** | 1.927s | 8.1s | ✅ tiffany 英语 |
| Polyglot ES | v2 | **1.574s** | 1.710s | 7.9s | ✅ tiffany 西班牙语 |
| 音频输入(ES) | v2 | 2.887s | 3.027s | 18.4s | ✅ 西班牙语回复 |
| 音频输入(ES) | v1 | 3.053s | 3.252s | 16.6s | ✅ 西班牙语回复 |

### 关键发现

#### 1. 跨模态文本输入比音频快约 50%

文本输入的 TTFB 约 1.6s，音频输入约 3.1s。这很好理解：文本不需要 ASR 处理，省去了语音识别环节。

**适用场景**：客服系统中，知识库检索结果以文本形式注入对话流，让模型用语音回复。

#### 2. 音频延迟 v1 ≈ v2

| 指标 | v1 (EN) | v2 (EN) | 差异 |
|------|---------|---------|------|
| TTFB 文本 | 3.000s | 3.166s | +5.5% |
| TTFB 音频 | 3.188s | 3.286s | +3.1% |

v2 并没有在音频处理速度上有显著提升，升级重点在功能而非延迟。

#### 3. Polyglot 语音效果出色

同一个 `tiffany` voice 在英语和西班牙语之间无缝切换：

- 英语回复："Mars is often called the Red Planet..."
- 西班牙语回复："Venus es el segundo planeta del sistema solar..."

声音特征一致，各语言发音自然。这对多语言客服场景是重大改进。

#### 4. 葡萄牙语支持验证

输入（Polly 合成）："Qual é a capital do Brasil?"
ASR 转写："qual é a capital do brasil?"（✅ 完全准确）
回复："A capital do Brasil é Brasília. Ela foi inaugurada em 21 de abril de 1960..."

### ASR 精度对比

| 输入语言 | 输入文本 | v2 ASR | v1 ASR |
|---------|---------|--------|--------|
| EN | "What are the three largest planets..." | "what are the three largest planets in our solar system?" ✅ | "what are the three largest planets in our solar system?" ✅ |
| PT | "Qual é a capital do Brasil?" | "qual é a capital do brasil?" ✅ | N/A（v1 不支持） |
| ES | "¿Cuántos continentes hay en el mundo?" | "¿cuántos continentes hay en el mundo?" ✅ | "cuántos continentes hay en el mundo?" ✅ |

## 踩坑记录

!!! warning "跨模态必须有活跃音频流"
    纯文本输入（没有并行音频流）会在 55 秒后超时断连。正确做法：先开 AUDIO contentStart + 发静音帧，再发 TEXT contentStart。**实测发现，官方文档未明确说明。**

!!! warning "55 秒无活动超时"
    虽然文档标注连接限制为 8 分钟，但如果**停止发送音频帧超过 55 秒**，连接会断开。Headless 测试中，需要在音频结束后持续发送静音帧直到收到完整响应。**实测发现。**

!!! warning "SDK 安装流程未变"
    aws_sdk_bedrock_runtime 升级到 0.4.0，但**仍需手动安装 Smithy 依赖**（从 GitHub 源码）。**已查文档确认**：当前 SDK 安装流程。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Bedrock Nova 2 Sonic | ~$0.0006/s 输入 + $0.0012/s 输出 | 6 次会话 | ~$0.15 |
| Bedrock Nova Sonic v1 | ~$0.0006/s 输入 + $0.0012/s 输出 | 2 次会话 | ~$0.08 |
| Polly Neural | $4/1M chars | ~100 chars | ~$0.00 |
| **合计** | | | **~$0.23** |

## 清理资源

本 Lab 是纯 API 调用，**无需清理 AWS 资源**。

```bash
# 清理本地文件（可选）
cd ~
rm -rf nova2-sonic-env smithy-python test_en.pcm test_pt.pcm test_es.pcm
```

!!! tip "无基础设施费用"
    Nova 2 Sonic 是 Bedrock On-Demand 调用，按使用量计费。停止调用即停止计费。

## 结论与建议

### v1 → v2 该升级吗？

**毫无疑问，是的。** 升级只需改 model ID，API 完全兼容。即使不用新功能，也能获得更好的 ASR 精度和语言支持。

### 新功能的最佳用法

| 功能 | 推荐场景 |
|------|---------|
| 跨模态文本输入 | 客服系统注入知识库结果、IVR 菜单导航、个性化欢迎语 |
| Polyglot 语音 | 多语言客服（用户切换语言不换声音）、全球化产品 |
| 异步 Tool Calling | 多步骤任务（查天气+查日程同时进行） |
| Turn-taking 控制 | 教育应用（low=给学生更多思考时间）、快速问答（high=最快响应） |

### 生产建议

1. **优先用跨模态文本注入上下文** — TTFB 比音频快 ~50%，适合 RAG 场景
2. **处理 55 秒超时** — 无活动时发送静音帧保活，或用 session continuation 模式
3. **Polyglot 减少 voice 管理** — 一个 tiffany 替代多个语言专属 voice
4. **关注 8 分钟连接限制** — 长对话需要 session continuation（保存历史，重建连接）

## 参考链接

- [AWS What's New: Amazon Nova 2 Sonic](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-nova-2-sonic-real-time-conversational-ai/)
- [AWS Blog: Introducing Amazon Nova 2 Sonic](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-2-sonic-next-generation-speech-to-speech-model-for-conversational-ai/)
- [Amazon Nova 2 User Guide](https://docs.aws.amazon.com/nova/latest/nova2-userguide/using-conversational-speech.html)
- [Nova 2 Sonic Code Samples](https://github.com/aws-samples/amazon-nova-samples/tree/main/speech-to-speech/amazon-nova-2-sonic)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Nova Sonic v1 Hands-on Lab](nova-sonic-speech-to-speech.md)（前置文章）
