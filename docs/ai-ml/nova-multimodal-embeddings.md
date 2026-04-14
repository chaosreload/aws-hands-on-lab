---
tags:
  - Nova
  - Embedding
  - What's New
---

# Amazon Nova Multimodal Embeddings 实测：首个统一多模态 Embedding 模型

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: < $1（纯 API 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

在构建 RAG 或语义搜索系统时，不同类型的内容（文本、图片、文档、视频、音频）通常需要各自独立的 Embedding 模型。这带来了多模型维护的复杂性、数据孤岛和更高的成本。

Amazon Nova Multimodal Embeddings 是首个通过单一模型统一支持 text、image、document、video 和 audio 的 Embedding 模型，将不同模态映射到统一的语义空间，实现跨模态检索。

## 前置条件

- AWS 账号，开通 Amazon Bedrock 中的 Nova Multimodal Embeddings 模型访问权限
- AWS CLI v2 已配置
- Python 3.x + boto3（用于测试脚本）
- `pip install Pillow`（可选，用于生成测试图片）

## 核心概念

### 模型参数一览

| 参数 | 说明 |
|------|------|
| **Model ID** | `amazon.nova-2-multimodal-embeddings-v1:0` |
| **输入模态** | text, image, document, video, audio |
| **输出维度** | 256 / 384 / 1024 / 3072 |
| **最大上下文** | 8K tokens（text）；30 秒（video/audio） |
| **Region** | US East (N. Virginia) |
| **API** | 同步（InvokeModel）+ 异步（StartAsyncInvoke） |
| **维度训练方式** | Matryoshka Representation Learning (MRL) |

### embeddingPurpose 用途

模型支持通过 `embeddingPurpose` 参数优化 Embedding 的用途方向：

| Purpose | 用途 |
|---------|------|
| `GENERIC_INDEX` | 通用索引（存入向量数据库时用） |
| `DOCUMENT_RETRIEVAL` | 文档检索查询端 |
| `IMAGE_RETRIEVAL` | 图片检索查询端 |
| `VIDEO_RETRIEVAL` | 视频检索查询端 |
| `AUDIO_RETRIEVAL` | 音频检索查询端 |
| `CLUSTERING` | 聚类任务 |
| `CLASSIFICATION` | 分类任务 |

!!! warning "INDEX 和 RETRIEVAL 不能混用"
    实测发现，同一文本在 `GENERIC_INDEX` 和 `DOCUMENT_RETRIEVAL` 下生成的 Embedding 余弦相似度仅 0.58。务必在索引端和查询端使用匹配的 purpose。

## 动手实践

### Step 1: 生成 Text Embedding

最基础的用法——将文本转换为 Embedding 向量：

```bash
# 创建请求 payload
cat > /tmp/text-embed.json << 'EOF'
{
  "taskType": "SINGLE_EMBEDDING",
  "singleEmbeddingParams": {
    "embeddingPurpose": "GENERIC_INDEX",
    "embeddingDimension": 1024,
    "text": {
      "truncationMode": "END",
      "value": "Amazon Nova is a multimodal foundation model for building AI applications"
    }
  }
}
EOF

# 调用 Bedrock
aws bedrock-runtime invoke-model \
  --model-id amazon.nova-2-multimodal-embeddings-v1:0 \
  --body fileb:///tmp/text-embed.json \
  --content-type application/json \
  --region us-east-1 \
  /tmp/text-embed-output.json

# 查看结果
python3 -c "
import json
with open('/tmp/text-embed-output.json') as f:
    data = json.load(f)
emb = data['embeddings'][0]['embedding']
print(f'维度: {len(emb)}')
print(f'前 5 个值: {emb[:5]}')
"
```

### Step 2: 生成 Image Embedding

将图片转换为 Embedding，与文本在同一语义空间中：

```python
import json, base64, boto3
from PIL import Image
from io import BytesIO

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"

# 准备图片（这里用 PIL 生成测试图片，实际使用中替换为你的图片文件）
img = Image.new("RGB", (100, 100), color=(255, 0, 0))
buf = BytesIO()
img.save(buf, format="JPEG")
image_bytes = base64.b64encode(buf.getvalue()).decode("utf-8")

# 生成 Image Embedding
body = {
    "taskType": "SINGLE_EMBEDDING",
    "singleEmbeddingParams": {
        "embeddingPurpose": "GENERIC_INDEX",
        "embeddingDimension": 1024,
        "image": {
            "format": "jpeg",
            "source": {"bytes": image_bytes}
        }
    }
}

response = bedrock.invoke_model(
    modelId=MODEL_ID,
    body=json.dumps(body),
    contentType="application/json"
)

data = json.loads(response["body"].read())
embedding = data["embeddings"][0]["embedding"]
print(f"Image embedding 维度: {len(embedding)}")
```

### Step 3: 跨模态语义检索

真正的亮点——用文本查询找到相关的图片，或反过来：

```python
import math

def cosine_sim(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b)

def get_text_embedding(text, dim=1024):
    body = {
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": "GENERIC_INDEX",
            "embeddingDimension": dim,
            "text": {"truncationMode": "END", "value": text}
        }
    }
    resp = bedrock.invoke_model(
        modelId=MODEL_ID, body=json.dumps(body), contentType="application/json"
    )
    return json.loads(resp["body"].read())["embeddings"][0]["embedding"]

# 用文本查询匹配图片
text_queries = [
    "a red colored square",
    "a blue colored square",
    "a cat playing with a ball",
]

for query in text_queries:
    text_emb = get_text_embedding(query)
    sim = cosine_sim(image_embedding, text_emb)
    print(f"  '{query}' vs red image: {sim:.4f}")
```

## 测试结果

### 输出维度对比

使用相同的文本对，测试不同维度下的语义区分能力：

| 维度 | 相关文本相似度 | 无关文本相似度 | 区分度(Δ) | 延迟 |
|------|--------------|--------------|-----------|------|
| 256 | 0.7604 | 0.4587 | **0.3017** | ~880ms |
| 384 | 0.7733 | 0.4738 | **0.2995** | ~466ms |
| 1024 | 0.7885 | 0.4751 | **0.3134** | ~413ms |
| 3072 | 0.8112 | 0.4820 | **0.3292** | ~631ms |

**发现**: 256 维已有不错的区分能力（Δ=0.30），3072 维最精确（Δ=0.33）但差异不大。对于大多数场景，1024 维是性价比最高的选择。

### 跨模态检索效果

| 文本查询 | vs 红色图 | vs 蓝色图 |
|---------|----------|----------|
| "red square" | **0.1858** | 0.1017 |
| "blue square" | 0.0734 | **0.1443** |
| "cat playing" | -0.0376 | 0.0156 |

**发现**: 模型确实能区分图文语义关系——红色图与"red"描述更相似，蓝色图与"blue"描述更相似。完全无关的文本（cat）则呈现负/近零相似度。

### embeddingPurpose 影响

同一文本在 `GENERIC_INDEX` vs `DOCUMENT_RETRIEVAL` 下的余弦相似度仅 **0.5824**，说明不同 purpose 确实生成了差异化的 Embedding 空间。

## 踩坑记录

!!! warning "1. 维度选项不是任意的"
    只有 **256 / 384 / 1024 / 3072** 四种有效维度。传入 512、768 等值会返回 `ValidationException`。这是 Matryoshka Representation Learning 训练决定的。（实测发现，官方 Blog 未列出具体值）

!!! warning "2. INDEX 和 RETRIEVAL 的 Embedding 空间不同"
    `GENERIC_INDEX` 和 `DOCUMENT_RETRIEVAL` 产生的向量差异显著（相似度仅 0.58）。索引时用 `GENERIC_INDEX`，检索时用对应的 `*_RETRIEVAL`，不能混用。（已查 Blog 确认设计如此）

!!! warning "3. 长文本限制约 50K-75K 字符"
    虽然标称 8K tokens，实际测试中 50K 字符的文本可以接受，75K 字符被拒绝。`truncationMode=END/START` 会自动截断超长文本，`NONE` 模式下超限会报 `Input Tokens Exceeded` 错误。

!!! warning "4. 空文本会被拒绝"
    传入空字符串会收到 `ValidationException`。调用前需要做非空校验。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| Bedrock invoke-model | ~30 次 API 调用 | < $0.10 |
| **合计** | | **< $0.10** |

Nova Multimodal Embeddings 按输入 token 数计费，测试用量极小。

## 清理资源

本 Lab 仅使用 Bedrock API 调用，**无需清理任何 AWS 资源**。

```bash
# 清理本地临时文件
rm -f /tmp/text-embed.json /tmp/text-embed-output.json /tmp/nova-embed-*.json
```

## 结论与建议

### 适合场景

- **多模态 RAG 系统**：无需维护多个 Embedding 模型，一个模型统一处理所有内容类型
- **跨模态搜索**：用文字搜图、用图搜视频等场景
- **内容理解与分类**：利用 CLUSTERING/CLASSIFICATION purpose 优化下游任务

### 维度选择建议

| 场景 | 推荐维度 | 理由 |
|------|---------|------|
| 原型验证 / 低成本 | 256 | 存储最小，区分度已达 0.30 |
| 生产通用 | 1024 | 精度与存储的最佳平衡点 |
| 高精度要求 | 3072 | 最高区分度，适合金融/医疗文档 |

### 生产环境注意

1. **索引/检索 Purpose 必须配对使用**——索引端用 `GENERIC_INDEX`，查询端用对应的 `*_RETRIEVAL`
2. **文本长度预处理**——建议在应用层控制输入长度，设置 `truncationMode=END` 作为安全网
3. **Region 限制**——目前仅 us-east-1，跨区域延迟需考虑
4. **Video/Audio > 25MB**——必须使用异步 API（StartAsyncInvoke）

## 参考链接

- [AWS Blog: Amazon Nova Multimodal Embeddings](https://aws.amazon.com/blogs/aws/amazon-nova-multimodal-embeddings-now-available-in-amazon-bedrock/)
- [AWS What's New](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-nova-multimodal-embeddings/)
- [Amazon Nova 用户指南](https://docs.aws.amazon.com/nova/latest/nova2-userguide/what-is-nova-2.html)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
