---
tags:
  - Bedrock
  - RAG
  - What's New
---

# Amazon Bedrock Citations API 实战：让 AI 回答可追溯到源文档

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.10（纯 API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2025-06-30

## 背景

在 RAG（检索增强生成）和文档问答场景中，一个老问题始终存在：**AI 说的话，到底有没有依据？**

用户问："这个条款是什么意思？" AI 回答了，但回答出自哪里？引用了文档的哪一句？如果没有引用溯源，用户只能选择相信或不相信——这在法律研究、学术写作、事实核查等场景中是不可接受的。

2025 年 6 月 30 日，Amazon Bedrock 为 Anthropic Claude 模型推出了 **Citations API** 和 **PDF 文档支持**。Citations API 让模型能够在回答中标注引用来源，精确到**字符位置**（文本）或**页码**（PDF），把"AI 说的"变成"AI 引用了文档第 X 段说的"。

## 前置条件

- AWS 账号，具有 `bedrock:InvokeModel` 权限
- AWS CLI v2 已配置
- Python 3.8+ 及 `boto3` 库（SDK 版本需支持 Converse API 的 citations 字段）
- 已启用 Claude Sonnet 4 或 Opus 4 模型访问（通过 Bedrock Model access）

## 核心概念

### Citations API 工作原理

```
请求                                    响应
┌─────────────────────┐                 ┌─────────────────────────────┐
│ Document(s)         │                 │ citationsContent block      │
│  ├─ name            │                 │  ├─ content: 生成的文本      │
│  ├─ format: txt/pdf │    Converse     │  ├─ citations:              │
│  ├─ source: text/   │───  API  ──────>│  │  ├─ title: 文档名        │
│  │  bytes/s3        │                 │  │  ├─ sourceContent: 原文   │
│  └─ citations:      │                 │  │  └─ location:            │
│     enabled: true   │                 │  │     ├─ documentChar (txt) │
│                     │                 │  │     └─ documentPage (pdf) │
│ Question (text)     │                 │                             │
└─────────────────────┘                 │ text block (未引用部分)      │
                                        └─────────────────────────────┘
```

### 关键特性一览

| 特性 | 说明 |
|------|------|
| 支持格式 | Citations 启用时仅支持 **txt** 和 **pdf**（其他格式如 csv、docx 需关闭 citations） |
| 引用定位 | 文本文档：字符位置（0-indexed）；PDF：页码范围 |
| 多文档支持 | ✅ 通过 `documentIndex` 区分不同文档的引用 |
| 支持模型 | Claude Opus 4、Sonnet 4、Sonnet 3.7、Sonnet 3.5v2 |
| API 接口 | Converse API（推荐） 和 InvokeModel API 均支持 |
| 成本影响 | cited_text 不计入 output tokens |

## 动手实践

### Step 1: 纯文本文档 + Citations

最基础的用法：传入一个文本文档，启用 citations，观察引用结构。

```python
import boto3
import json

client = boto3.Session(
    profile_name="your-profile",   # 替换为你的 profile
    region_name="us-east-1"
).client("bedrock-runtime")

# 准备一段测试文档
doc_text = """Amazon Bedrock 于 2023 年 9 月正式发布，是一项全托管的基础模型服务。
Bedrock 支持来自 Anthropic、Meta、Mistral 和 Amazon 等多家提供商的模型。
该服务提供文本生成、Embeddings 和图像生成等 API。
Bedrock 还提供 Guardrails、Knowledge Bases 和 Agents 等生产级功能。
2024 年，Amazon 推出 Nova 系列作为 Bedrock 上的第一方基础模型。
Bedrock 按需付费，无最低消费承诺。
Converse API 为所有支持的模型提供统一的调用接口。"""

response = client.converse(
    modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",  # 使用推理配置文件 ID
    messages=[{
        "role": "user",
        "content": [
            {
                "document": {
                    "name": "bedrock-overview",
                    "format": "txt",
                    "source": {"text": doc_text},
                    "citations": {"enabled": True}  # 启用引用
                }
            },
            {"text": "Bedrock 是什么时候发布的？支持哪些模型提供商？"}
        ]
    }],
    inferenceConfig={"maxTokens": 1024}
)

# 解析响应
for block in response["output"]["message"]["content"]:
    if "citationsContent" in block:
        cite = block["citationsContent"]
        print(f"📝 生成文本: {cite['content'][0]['text']}")
        for c in cite["citations"]:
            loc = c["location"]["documentChar"]
            print(f"   📌 引用自 [{c['title']}] 字符 {loc['start']}-{loc['end']}")
            print(f"   📄 原文: {c['sourceContent'][0]['text'].strip()}")
        print()
    elif "text" in block:
        if block["text"].strip():
            print(f"💬 {block['text'].strip()}\n")
```

**输出示例**：

```
📝 生成文本: Amazon Bedrock 于 2023 年 9 月正式发布，是一项全托管的基础模型服务。
   📌 引用自 [bedrock-overview] 字符 0-42
   📄 原文: Amazon Bedrock 于 2023 年 9 月正式发布，是一项全托管的基础模型服务。

📝 生成文本: Bedrock 支持来自 Anthropic、Meta、Mistral 和 Amazon 等多家提供商的模型。
   📌 引用自 [bedrock-overview] 字符 43-85
   📄 原文: Bedrock 支持来自 Anthropic、Meta、Mistral 和 Amazon 等多家提供商的模型。
```

每个有引用的声明都作为独立的 `citationsContent` block 返回，包含精确的字符位置和原文片段。

### Step 2: PDF 文档 + Citations

PDF 文档的引用定位方式不同——返回**页码**而非字符位置。

```python
# 读取 PDF 文件
with open("report.pdf", "rb") as f:
    pdf_bytes = f.read()

response = client.converse(
    modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
    messages=[{
        "role": "user",
        "content": [
            {
                "document": {
                    "name": "cloud-report-2024",
                    "format": "pdf",
                    "source": {"bytes": pdf_bytes},
                    "citations": {"enabled": True}
                }
            },
            {"text": "文档中提到了哪些 AI 服务？"}
        ]
    }],
    inferenceConfig={"maxTokens": 1024}
)

# PDF 引用返回页码
for block in response["output"]["message"]["content"]:
    if "citationsContent" in block:
        cite = block["citationsContent"]
        print(f"📝 {cite['content'][0]['text']}")
        for c in cite["citations"]:
            page = c["location"]["documentPage"]
            print(f"   📌 引用自第 {page['start']}-{page['end']} 页")
```

**实测数据**：同样内容，PDF 格式比纯文本的 input tokens 高约 5 倍（4191 vs 816），因为 PDF 需要额外的解析处理。

### Step 3: 多文档交叉引用

传入多个文档时，Citations API 通过 `documentIndex` 精确区分每个引用的来源。

```python
response = client.converse(
    modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
    messages=[{
        "role": "user",
        "content": [
            {
                "document": {
                    "name": "s3-overview",
                    "format": "txt",
                    "source": {"text": "Amazon S3 是对象存储服务，提供 99.999999999% 持久性。支持生命周期策略自动管理数据。"},
                    "citations": {"enabled": True}
                }
            },
            {
                "document": {
                    "name": "dynamodb-overview",
                    "format": "txt",
                    "source": {"text": "Amazon DynamoDB 是全托管 NoSQL 数据库，提供个位数毫秒级延迟。支持文档和键值数据模型。"},
                    "citations": {"enabled": True}
                }
            },
            {"text": "对比 S3 和 DynamoDB 的核心特点。"}
        ]
    }],
    inferenceConfig={"maxTokens": 1024}
)

# 引用会通过 documentIndex 区分来源
for block in response["output"]["message"]["content"]:
    if "citationsContent" in block:
        for c in block["citationsContent"]["citations"]:
            doc_idx = c["location"]["documentChar"]["documentIndex"]
            print(f"引用自文档 #{doc_idx} ({c['title']})")
```

### Step 4: InvokeModel API 方式（Anthropic 原生格式）

如果使用 InvokeModel 而非 Converse，引用格式略有不同：

```python
import json

body = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 1024,
    "messages": [{
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": "草是绿色的。天空是蓝色的。水是湿的。"
                },
                "title": "自然常识",
                "citations": {"enabled": True}
            },
            {"type": "text", "text": "草是什么颜色的？"}
        ]
    }]
}

response = client.invoke_model(
    modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
    body=json.dumps(body)
)
result = json.loads(response["body"].read())

# InvokeModel 的引用嵌入在 text block 中
for block in result["content"]:
    if "citations" in block:
        for c in block["citations"]:
            print(f"类型: {c['type']}")           # char_location
            print(f"引用原文: {c['cited_text']}")
            print(f"位置: {c['start_char_index']}-{c['end_char_index']}")
```

## 测试结果

### 各场景对比数据

| 测试场景 | 延迟 | Input Tokens | Output Tokens | 内容块数 | 引用精确度 |
|----------|------|-------------|---------------|----------|-----------|
| T1: 纯文本 Citations ON | 2.47s | 816 | 124 | 5 (3 cite + 2 text) | ✅ 字符精确 |
| T5: 纯文本 Citations OFF | 2.57s | 217 | 108 | 1 (text only) | N/A |
| T2: PDF 2页文档 | 5.49s | 4,191 | 343 | 15 | ✅ 页码准确 |
| T3: 多文档(2个 txt) | 4.18s | 834 | 396 | 17 | ✅ documentIndex 区分 |
| T6: 大文档(15KB, 100条) | 4.00s | 5,354 | 148 | 5 | ✅ 字符验证匹配 |
| T7: InvokeModel API | 1.86s | 632 | 51 | 5 | ✅ char_location |

### 关键发现

1. **Input tokens 差异显著**：启用 citations 后 input tokens 约为未启用时的 4 倍（816 vs 217），因为文档以特殊格式处理
2. **Output tokens 影响较小**：仅多约 15%
3. **PDF 解析成本高**：同样信息量，PDF 的 input tokens 是纯文本的 5 倍
4. **大文档无精度损失**：15KB 文档的字符位置引用经过逐字节验证，完全匹配

## 踩坑记录

!!! warning "踩坑 1: Citations 仅支持 txt 和 pdf"
    虽然 DocumentBlock 的 format 字段支持 csv、doc、docx、html 等 9 种格式，但**启用 citations 时只支持 txt 和 pdf**。尝试对 CSV 启用 citations 会直接报 `ValidationException: Unsupported document format. Only txt and pdf formats are supported when citations are enabled`。

    **已查文档确认**：这是 API 层面的限制。

!!! warning "踩坑 2: text source 是 Citations 专属"
    `DocumentSource` 有四种来源：`bytes`、`text`、`s3Location`、`content`。但 **`text` source 只有在启用 citations 时才可用**。不启用 citations 时使用 `text` source 会报 `ValidationException`，必须改用 `bytes` 或 `s3Location`。

    **实测发现，官方文档未明确记录此限制。**

!!! warning "踩坑 3: 必须使用 Inference Profile"
    直接使用 model ID（如 `anthropic.claude-sonnet-4-20250514-v1:0`）会报 `ValidationException: Invocation with on-demand throughput isn't supported`。需要使用推理配置文件 ID（如 `us.anthropic.claude-sonnet-4-20250514-v1:0`）。

!!! warning "踩坑 4: Legacy 模型限制"
    虽然公告表示支持 Sonnet 3.5v2 和 3.7，但这些模型已标记为 **Legacy**。如果 15 天未使用，会被锁定，报 `ResourceNotFoundException: Access denied. This Model is marked by provider as Legacy`。建议直接使用 Claude Sonnet 4 或更新版本。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Claude Sonnet 4 Input | $3/M tokens | ~12K tokens | $0.036 |
| Claude Sonnet 4 Output | $15/M tokens | ~1.2K tokens | $0.018 |
| **合计** | | | **< $0.10** |

## 清理资源

本 Lab 仅涉及 API 调用，**无需清理任何基础设施资源**。不会产生持续费用。

## 结论与建议

### 适用场景

- **RAG 应用**：为检索增强生成提供引用溯源，提升用户信任度
- **法律/合规文档**：需要精确引用条款来源的场景
- **学术研究**：论文分析、文献综述中的引用追踪
- **客服知识库**：回答时引用官方文档的具体段落

### 生产环境建议

1. **优先使用 Converse API**：结构更清晰，返回独立的 `citationsContent` block，易于前端渲染
2. **文本优先于 PDF**：如果源数据可以文本化，优先用 txt 格式，可节省约 5 倍 input tokens
3. **字符位置验证**：生产环境中建议对引用的 `start`/`end` 做二次校验，确保与原文匹配
4. **使用 ACTIVE 模型**：避免使用 Legacy 模型（3.5v2、3.7），直接使用 Sonnet 4 或更新版本
5. **注意格式限制**：如果数据源包含 CSV/DOCX 等格式，需先转换为 txt 再启用 citations

## 参考链接

- [AWS What's New: Citations API and PDF support for Claude models](https://aws.amazon.com/about-aws/whats-new/2025/06/citations-api-pdf-claude-models-amazon-bedrock/)
- [DocumentBlock API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html)
- [CitationsConfig API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CitationsConfig.html)
- [Converse API Reference](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html)
- [Anthropic Citations Documentation](https://docs.anthropic.com/en/docs/build-with-claude/citations)
