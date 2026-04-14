---
description: "Hands-on lab for Amazon Nova Sonic speech-to-speech model on Bedrock with bidirectional streaming API for real-time voice conversation."
tags:
  - Nova
  - Streaming
  - What's New
---
# Amazon Nova Sonic 实测：Bedrock 双向流式语音对话 Hands-on Lab

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: <$1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-24

## 背景

构建语音对话 AI 应用，传统方案需要串联三个组件：ASR（语音转文字）→ LLM（文本生成）→ TTS（文字转语音）。这种"流水线"架构不仅延迟高、开发复杂，还会丢失语调、韵律等关键语音上下文。

Amazon Nova Sonic 是 AWS 推出的 speech-to-speech 基础模型，将语音理解和生成统一到单一模型中。配合 Bedrock 全新的 **双向流式 API**（`InvokeModelWithBidirectionalStream`），开发者可以用一个 API 调用实现实时语音对话。

本文通过 headless 方式（无需麦克风/扬声器）实测 Nova Sonic 的完整 API 流程，包括音频输入、文本输入、ASR 转写、语音输出、多语言支持等。

## 前置条件

- AWS 账号（需要 Amazon Bedrock 访问权限）
- Python 3.12+（Smithy SDK 要求）
- Git（克隆 Smithy SDK）
- 在 Bedrock Console 中启用 Nova Sonic 模型访问

## 核心概念

### 传统方案 vs Nova Sonic

| 维度 | 传统方案（ASR + LLM + TTS） | Nova Sonic |
|------|---------------------------|------------|
| 架构 | 三个独立模型串行编排 | 单一统一模型 |
| 延迟 | 高（每环节各 100-500ms） | 低（实时双向流） |
| 语境 | 语音→文字时丢失语调/韵律 | 保留完整语音上下文 |
| 开发 | 高（集成 3+ SDK） | 低（单 API + 事件驱动） |

### 关键规格

| 参数 | 值 |
|------|-----|
| Model ID | `amazon.nova-sonic-v1:0` |
| API | `InvokeModelWithBidirectionalStream`（HTTP/2） |
| 输入音频 | PCM, 16kHz, 16-bit, mono |
| 输出音频 | PCM, 24kHz, 16-bit, mono |
| Context Window | 300K tokens |
| 连接限制 | 8 分钟超时, 最多 20 并发/客户 |
| 语言 | English (US/UK), French, Italian, German, Spanish |
| Region | us-east-1, eu-north-1, ap-northeast-1 |

### 事件驱动架构

Nova Sonic 的 API 基于双向流的事件驱动模型：

**输入流（→ 模型）**：

1. `sessionStart` — 初始化推理参数
2. `promptStart` — 配置语音输出（voice ID、采样率）
3. `contentStart/textInput/contentEnd` — 系统提示词
4. `contentStart(AUDIO)/audioInput/contentEnd` — 音频流
5. `contentStart(TEXT)/textInput/contentEnd` — 文本消息（仅 v2）

**输出流（← 模型）**：

1. `textOutput (USER)` — ASR 实时转写
2. `textOutput (ASSISTANT)` — 助手文本响应
3. `audioOutput` — 助手语音响应（base64 PCM）
4. `toolUse` — 工具调用请求

## 动手实践

### Step 1: 安装 Smithy SDK

Nova Sonic 使用 AWS 新一代 Smithy-based Python SDK，不是 boto3。需要先从源码安装核心包：

```bash
# 创建 Python 3.12 虚拟环境
python3.12 -m venv nova-sonic-env
source nova-sonic-env/bin/activate

# 安装 Smithy 核心包（从源码）
git clone --depth 1 https://github.com/smithy-lang/smithy-python.git
cd smithy-python
pip install packages/smithy-core packages/smithy-http packages/smithy-json \
  packages/smithy-aws-core packages/smithy-aws-event-stream packages/aws-sdk-signers

# 安装 Bedrock Runtime SDK
pip install aws_sdk_bedrock_runtime boto3
```

!!! warning "踩坑：SDK 不在 PyPI"
    `aws_sdk_bedrock_runtime` 依赖未发布到 PyPI 的 Smithy 包。必须先手动安装 Smithy 依赖，之后才能 pip install 成功。**已查文档确认**：这是当前 SDK 的已知安装流程。

### Step 2: 准备测试音频

用 Amazon Polly 生成 PCM 格式的测试音频：

```bash
aws polly synthesize-speech \
  --output-format pcm \
  --sample-rate 16000 \
  --voice-id Matthew \
  --text 'What is Amazon Bedrock?' \
  test-audio.pcm \
  --region us-east-1
```

### Step 3: 建立双向流连接

```python
import asyncio, json, uuid, time, base64
from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart
)
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

# 初始化客户端
config = Config(
    endpoint_uri="https://bedrock-runtime.us-east-1.amazonaws.com",
    region="us-east-1",
    aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
)
client = BedrockRuntimeClient(config=config)

# 建立双向流
stream = await client.invoke_model_with_bidirectional_stream(
    InvokeModelWithBidirectionalStreamOperationInput(
        model_id="amazon.nova-sonic-v1:0"
    )
)
```

### Step 4: 发送事件初始化会话

```python
async def send(evt):
    chunk = InvokeModelWithBidirectionalStreamInputChunk(
        value=BidirectionalInputPayloadPart(bytes_=json.dumps(evt).encode())
    )
    await stream.input_stream.send(chunk)

prompt_name = str(uuid.uuid4())

# 1. 会话开始
await send({"event": {"sessionStart": {
    "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7}
}}})

# 2. Prompt 开始（配置输出格式和 Voice）
await send({"event": {"promptStart": {
    "promptName": prompt_name,
    "textOutputConfiguration": {"mediaType": "text/plain"},
    "audioOutputConfiguration": {
        "mediaType": "audio/lpcm",
        "sampleRateHertz": 24000,      # 输出采样率
        "sampleSizeBits": 16,
        "channelCount": 1,
        "voiceId": "tiffany",           # 可选：matthew, tiffany 等
        "encoding": "base64",
        "audioType": "SPEECH"
    }
}}})

# 3. 系统提示词
content_name = str(uuid.uuid4())
await send({"event": {"contentStart": {
    "promptName": prompt_name, "contentName": content_name,
    "type": "TEXT", "interactive": False, "role": "SYSTEM",
    "textInputConfiguration": {"mediaType": "text/plain"}
}}})
await send({"event": {"textInput": {
    "promptName": prompt_name, "contentName": content_name,
    "content": "You are a friendly assistant. Keep responses short."
}}})
await send({"event": {"contentEnd": {
    "promptName": prompt_name, "contentName": content_name
}}})

# 4. 开始音频输入流
audio_content_name = str(uuid.uuid4())
await send({"event": {"contentStart": {
    "promptName": prompt_name,
    "contentName": audio_content_name,
    "type": "AUDIO", "interactive": True, "role": "USER",
    "audioInputConfiguration": {
        "mediaType": "audio/lpcm",
        "sampleRateHertz": 16000,       # 输入采样率
        "sampleSizeBits": 16,
        "channelCount": 1,
        "audioType": "SPEECH",
        "encoding": "base64"
    }
}}})
```

### Step 5: 流式发送音频并接收响应

```python
# 读取 Polly 生成的 PCM 音频
with open("test-audio.pcm", "rb") as f:
    audio_data = f.read()

# 分块发送（每块 100ms = 3200 字节 @ 16kHz 16-bit）
for i in range(0, len(audio_data), 3200):
    chunk = audio_data[i:i+3200]
    b64 = base64.b64encode(chunk).decode()
    await send({"event": {"audioInput": {
        "promptName": prompt_name,
        "contentName": audio_content_name,
        "content": b64
    }}})
    await asyncio.sleep(0.05)  # 模拟实时速率

# 发送尾部静音（触发模型识别语音结束）
silent = base64.b64encode(b'\x00' * 3200).decode()
for _ in range(30):
    await send({"event": {"audioInput": {
        "promptName": prompt_name,
        "contentName": audio_content_name,
        "content": silent
    }}})
    await asyncio.sleep(0.05)
```

接收响应的异步任务：

```python
async def receive_responses():
    while True:
        output = await stream.await_output()
        result = await output[1].receive()
        if result.value and result.value.bytes_:
            data = json.loads(result.value.bytes_.decode())
            event = data.get("event", {})

            if "textOutput" in event:
                text = event["textOutput"]["content"]
                role = "..."  # 从 contentStart 事件获取
                if role == "USER":
                    print(f"ASR: {text}")      # 实时转写
                elif role == "ASSISTANT":
                    print(f"Reply: {text}")    # 助手回复

            elif "audioOutput" in event:
                audio = base64.b64decode(event["audioOutput"]["content"])
                # 播放或保存 24kHz PCM 音频
```

## 测试结果

### 核心指标

| 测试项 | 模型 | 输入 | 延迟 | 结果 |
|--------|------|------|------|------|
| 连接建立 | v1 | — | 5ms | ✅ |
| 连接建立 | v2 | — | 6ms | ✅ |
| 音频→语音+文字 | v1 | Polly PCM 42KB | 4,239ms | ✅ ASR + 188KB 音频输出 |
| 文本→语音+文字 | v2 | 文本 | 2,233ms | ✅ 文本 + 88KB 音频输出 |
| 西班牙语 | v2 | 西班牙语文本 | 2,161ms | ✅ 正确西班牙语回复 |

### ASR 精度

| 输入 | ASR 输出 | 评价 |
|------|---------|------|
| "What is Amazon Bedrock?" (Polly 合成) | "what is amazon bed rock?" | 基本准确，"Bedrock"被拆分为 "bed rock" |

### V1 vs V2 对比

| 特性 | Nova Sonic v1 | Nova 2 Sonic |
|------|--------------|-------------|
| Model ID | `amazon.nova-sonic-v1:0` | `amazon.nova-2-sonic-v1:0` |
| 音频输入 | ✅ | ✅ |
| 文本输入 | ❌ | ✅ |
| 输出 | 语音 + 文字 | 语音 + 文字 |
| 文本响应速度 | — | ~2.2s |
| 音频响应速度 | ~4.2s | ~2.2s |

!!! tip "V2 新增文本输入"
    Nova 2 Sonic 支持 text+audio 混合输入，可以在语音对话中穿插文本消息，适合客服系统中的知识检索场景。

## 踩坑记录

!!! warning "SDK 安装"
    `aws_sdk_bedrock_runtime` 不在标准 PyPI 上，需要先手动安装 Smithy 依赖。且**要求 Python 3.12+**，3.10 会报版本不兼容错误。**实测发现，官方未记录**完整安装步骤。

!!! warning "静音流保持连接"
    必须持续发送 silent audio chunks 保持双向流连接活跃。如果停止发送音频超过几秒，连接可能自动关闭。**已查文档确认**：8 分钟连接超时。

!!! warning "V1 不支持纯文本输入"
    Nova Sonic v1 仅接受 AUDIO 类型的交互式输入。发送 TEXT 类型的交互式输入不会产生响应。**实测发现**：需要使用 v2（`amazon.nova-2-sonic-v1:0`）才能使用文本输入。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Bedrock Nova Sonic v1 | $0.0006/s 输入 + $0.0012/s 输出 | ~60s | ~$0.10 |
| Bedrock Nova 2 Sonic | 按 token 计费 | 4 次文本请求 | ~$0.05 |
| Polly (TTS) | $4/1M chars | 23 chars | ~$0.00 |
| **合计** | | | **~$0.15** |

## 清理资源

本 Lab 是纯 API 调用，**无需清理 AWS 资源**。

```bash
# 清理本地文件（可选）
rm -rf nova-sonic-env smithy-python test-audio.pcm
```

!!! tip "无基础设施费用"
    Nova Sonic 是 Bedrock On-Demand 调用，按使用量计费，无常驻资源。停止调用即停止计费。

## 结论与建议

**适用场景**：

- 📞 客服呼叫中心自动化 — 替代传统 IVR
- 🤖 语音助手 — 实时对话式 AI
- 🎓 教育/语言学习 — 自适应语速和语调
- 🏢 企业 AI 助手 — 通过 tool use 集成业务系统

**生产建议**：

1. **优先使用 Nova 2 Sonic** — 支持文本+音频混合输入，更灵活
2. **处理 8 分钟超时** — 使用 session continuation 模式（保存历史上下文，重建连接）
3. **关注 SDK 更新** — 当前 Smithy SDK 安装流程繁琐，预计后续会简化
4. **计划并发** — 每客户 20 并发连接限制，高并发场景需提前规划

## 参考链接

- [AWS What's New: Amazon Nova Sonic](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-nova-sonic-speech-to-speech-conversations-bedrock/)
- [AWS Blog: Introducing Amazon Nova Sonic](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-sonic-human-like-voice-conversations-for-generative-ai-applications/)
- [Amazon Nova User Guide](https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html)
- [Nova Sonic Code Samples](https://github.com/aws-samples/amazon-nova-samples/tree/main/speech-to-speech)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
