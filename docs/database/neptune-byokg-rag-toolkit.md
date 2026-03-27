# Amazon Neptune BYOKG-RAG Toolkit 实战：用已有知识图谱构建 GraphRAG 问答系统

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: ~$15（Neptune Analytics 128 m-NCU × ~20min + Bedrock 调用）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

GraphRAG 是当前 GenAI 领域的热门方向——通过知识图谱增强大模型的推理能力，减少幻觉，支持多跳推理。AWS 此前已通过 [Bedrock Knowledge Bases GraphRAG](../ai-ml/bedrock-knowledge-bases-graphrag.md) 提供了全托管的 GraphRAG 方案。

2025 年 8 月，Neptune 团队发布了 **BYOKG-RAG（Bring Your Own Knowledge Graph - RAG）** 功能，这是一条完全不同的路径：不自动构建知识图谱，而是让你**直接使用已有的知识图谱**，通过开源 [GraphRAG Toolkit](https://github.com/awslabs/graphrag-toolkit) 接入 LLM，实现知识图谱问答（KGQA）。

**核心差异**：

| 维度 | Bedrock KB GraphRAG | Neptune BYOKG-RAG |
|------|--------------------|--------------------|
| KG 来源 | 自动从文档构建 | 使用已有 KG（BYOKG） |
| 检索策略 | 图增强向量检索 | 4 种策略组合（Agentic/Scoring/Path/Query） |
| 控制粒度 | 低（全托管黑盒） | 高（开源 toolkit，完全可配置） |
| 目标用户 | 有文档，想快速 RAG | 已有 KG，想接 LLM |
| 依赖 | Bedrock KB 全托管 | 开源 Python toolkit + Neptune |

**本文实测内容**：

1. 用 Local Graph 快速验证 BYOKG-RAG 的 4 种检索策略
2. 在 Neptune Analytics 上加载 EDGAR 股票持仓数据并查询
3. 对比有/无 CypherKGLinker 的检索效果差异
4. 不同 iterations 参数对结果的影响

## 前置条件

- AWS 账号，有 Neptune Analytics 和 Bedrock 权限
- AWS CLI v2 已配置 Profile
- Python 3.10+
- 安装 graphrag-toolkit byokg-rag：

```bash
pip install 'https://github.com/awslabs/graphrag-toolkit/archive/refs/tags/v3.17.1.zip#subdirectory=byokg-rag'
```

!!! warning "PyTorch 依赖注意"
    byokg-rag 依赖 PyTorch（通过 sentence-transformers）。如果在没有 GPU 的环境中安装，建议先安装 CPU 版本以节省空间：
    ```bash
    pip install torch --index-url https://download.pytorch.org/whl/cpu
    ```
    再安装 byokg-rag 以避免下载 3GB+ 的 CUDA 依赖。

## 核心概念

### 四种检索策略

BYOKG-RAG 的核心创新在于**多策略检索**——不依赖单一方法，而是组合四种互补的检索策略：

```
用户问题 → ByoKGQueryEngine
              ├── KGLinker → LLM 提取实体 + metapath
              │     ├── EntityLinker → 模糊匹配图节点
              │     ├── AgenticRetriever → LLM 引导图探索
              │     └── PathRetriever → metapath 多跳路径
              ├── CypherKGLinker (可选) → NL→Cypher 精确查询
              └── LLM Generator → 基于上下文生成答案
```

| 策略 | 原理 | 适合场景 |
|------|------|---------|
| **Agentic Retrieval** | LLM 动态决定探索哪些图路径 | 复杂多步推理 |
| **Scoring-based** | 语义相似度打分检索三元组 | 快速语义匹配 |
| **Path-based** | 沿 metapath 模式遍历多跳路径 | 实体间关系链 |
| **Query-based** | 自然语言→Cypher 直接查询 | 精确结构化查询 |

### 学术背景

BYOKG-RAG 基于 [EMNLP 2025 论文](https://arxiv.org/abs/2507.04127)，在三个知识图谱基准上超越了基线方法：

| 方法 | Wiki-KG | Temp-KG | Med-KG |
|------|---------|---------|--------|
| Agent Baseline | 77.8% | 57.3% | 59.2% |
| **BYOKG-RAG** | **80.1%** | **65.5%** | **65.0%** |

## 动手实践

### Step 1: Local Graph 快速验证

先用本地图验证基本流程，**不消耗 AWS 资源**。

#### 1.1 加载示例知识图谱

```python
from graphrag_toolkit.byokg_rag.graphstore import LocalKGStore

graph_store = LocalKGStore()
graph_store.read_from_csv('data/freebase_tiny_kg.csv')  # Freebase 子集

nodes = graph_store.nodes()
triplets = graph_store.get_triplets()
schema = graph_store.get_schema()
print(f"图规模: {len(nodes)} 节点, {len(triplets)} 边")
# 输出: 图规模: 1691 节点, 21911 边
```

#### 1.2 初始化组件

```python
from graphrag_toolkit.byokg_rag.llm import BedrockGenerator
from graphrag_toolkit.byokg_rag.graph_connectors import KGLinker
from graphrag_toolkit.byokg_rag.indexing import FuzzyStringIndex
from graphrag_toolkit.byokg_rag.graph_retrievers import (
    EntityLinker, AgenticRetriever, PathRetriever,
    GTraversal, TripletGVerbalizer, PathVerbalizer
)
from graphrag_toolkit.byokg_rag.byokg_query_engine import ByoKGQueryEngine

# LLM（注意：配置默认的 Claude 3.7 Sonnet 可能已被标记为 Legacy，建议用更新版本）
llm = BedrockGenerator(
    model_name='us.anthropic.claude-sonnet-4-20250514-v1:0',
    region_name='us-east-1'
)

# 实体链接器（模糊匹配）
string_index = FuzzyStringIndex()
string_index.add(graph_store.nodes())
entity_linker = EntityLinker(retriever=string_index.as_entity_matcher())

# 检索器
graph_traversal = GTraversal(graph_store)
triplet_retriever = AgenticRetriever(
    llm_generator=llm,
    graph_traversal=graph_traversal,
    graph_verbalizer=TripletGVerbalizer()
)
path_retriever = PathRetriever(
    graph_traversal=graph_traversal,
    path_verbalizer=PathVerbalizer()
)

# KG Linker
kg_linker = KGLinker(graph_store=graph_store, llm_generator=llm)
```

#### 1.3 拆解每个组件的输出

先看各组件是如何协作的：

```python
question = "What genre of film is associated with the place where Wynton Marsalis was born?"
# 正确答案: "Backstage Musical"

# Step 1: KGLinker — LLM 分析问题，提取实体和 metapath
response = kg_linker.generate_response(
    question=question, schema=schema,
    graph_context="Not provided.")
artifacts = kg_linker.parse_response(response)
```

**KGLinker 输出**（4.91 秒）：

- 提取实体：`['Wynton Marsalis', 'New Orleans', 'Louisiana']`
- 推理路径：`people.person.place_of_birth → film.film_location → film.film.genre`
- 初步答案猜测：`['Jazz', 'Blues', 'Drama', 'Crime', 'Horror', 'Comedy', 'Thriller']`

```python
# Step 2: EntityLinker — 将 LLM 提取的实体匹配到图节点
linked_entities = entity_linker.link(artifacts["entity-extraction"], return_dict=False)
# 结果: ['Wynton Marsalis', 'New Orleans', 'Louisiana', 'Louis Armstrong', ...]
```

**EntityLinker**（0.03 秒）：模糊匹配将 "New Orleans" 也关联到了 "Louis Armstrong New Orleans International Airport" 等相关节点。

```python
# Step 3: AgenticRetriever — LLM 引导图探索
triplet_context = triplet_retriever.retrieve(query=question, source_nodes=linked_entities)
```

**AgenticRetriever 输出**（6.91 秒，5 条三元组）：

```
- Wynton Marsalis → people.person.place_of_birth → New Orleans
- New Orleans → film.film.genre → Backstage Musical  ← 关键证据！
- Louisiana → film.film_location.featured_in_films → Damn Citizen | ...
```

```python
# Step 4: PathRetriever — 多跳路径检索
metapaths = [[c.strip() for c in p.split("->")] for p in artifacts["path-extraction"]]
path_context = path_retriever.retrieve(linked_entities, metapaths, linked_answers)
# 输出: 21 条路径
```

#### 1.4 运行完整 Pipeline

```python
engine = ByoKGQueryEngine(
    graph_store=graph_store,
    kg_linker=kg_linker,
    triplet_retriever=triplet_retriever,
    path_retriever=path_retriever,
    entity_linker=entity_linker,
    llm_generator=llm  # 必须传入，否则使用默认模型（可能已 Legacy）
)

ctx = engine.query(question)
answer, response = engine.generate_response(question, "\n".join(ctx))
print(answer)  # ['Backstage Musical'] ✅
```

### Step 2: Neptune Analytics 云端验证

#### 2.1 创建 Neptune Analytics Graph

```bash
aws neptune-graph create-graph \
  --graph-name byokg-test \
  --provisioned-memory 128 \
  --public-connectivity \
  --replica-count 0 \
  --vector-search-configuration '{"dimension": 384}' \
  --no-deletion-protection \
  --region us-east-1
```

等待状态变为 `AVAILABLE`（约 3-5 分钟）：

```bash
aws neptune-graph get-graph \
  --graph-identifier <graph-id> \
  --region us-east-1 \
  --query '{status: status}'
```

#### 2.2 加载 EDGAR 数据

EDGAR 是 SEC 的公开股票持仓数据，包含投资机构 → 持仓 → 季度报告的关系。

```python
from graphrag_toolkit.byokg_rag.graphstore import NeptuneAnalyticsGraphStore

graph_store = NeptuneAnalyticsGraphStore(
    graph_identifier='<graph-id>',
    region='us-east-1'
)

# 设置节点文本表示
graph_store.assign_text_repr_prop_for_nodes({
    "Holder": "name",
    "Holding": "name"
})

# 从 S3 公开数据集加载
graph_store.read_from_csv(
    s3_path=f"s3://aws-neptune-customer-samples-us-east-1/sample-datasets/gremlin/edgar/"
)
# 加载时间: 13.47s → 43,072 节点, 11,335,002 边
```

#### 2.3 查询股票持仓

```python
# 基本管线（KGLinker only）
engine_basic = ByoKGQueryEngine(
    graph_store=graph_store,
    kg_linker=kg_linker,
    triplet_retriever=triplet_retriever,
    path_retriever=path_retriever,
    entity_linker=entity_linker,
    llm_generator=llm
)

ctx = engine_basic.query("What stocks does Berkshire Hathaway hold?", iterations=1)
answer, _ = engine_basic.generate_response(q, "\n".join(ctx))
# 答案: ['Apple Inc.', 'Bank of America', 'Chevron Corporation', ...]（10 只股票）
```

#### 2.4 加入 CypherKGLinker

```python
from graphrag_toolkit.byokg_rag.graph_connectors import CypherKGLinker
from graphrag_toolkit.byokg_rag.graph_retrievers import GraphQueryRetriever

cypher_linker = CypherKGLinker(llm_generator=llm, graph_store=graph_store)
graph_query_executor = GraphQueryRetriever(graph_store=graph_store)

engine_cypher = ByoKGQueryEngine(
    graph_store=graph_store,
    kg_linker=kg_linker,
    cypher_kg_linker=cypher_linker,
    triplet_retriever=triplet_retriever,
    path_retriever=path_retriever,
    entity_linker=entity_linker,
    llm_generator=llm,
    graph_query_executor=graph_query_executor
)

ctx3 = engine_cypher.query("What stocks does Berkshire Hathaway hold?", iterations=1)
answer3, _ = engine_cypher.generate_response(q, "\n".join(ctx3))
# 答案: 30 只股票（3x more than basic!）
```

## 测试结果

### Local Graph（Freebase 1,691 节点）

| 测试 | 时间 | 结果 |
|------|------|------|
| 标准问答 | 28.17s | ✅ 正确 — "Backstage Musical" |
| 无关问题 | 37.65s | ✅ 正确识别无法回答 |
| 复杂多跳 | 62.73s | ✅ 正确 — "United States of America" |

**组件耗时分布**：

| 组件 | 耗时 | 说明 |
|------|------|------|
| KGLinker | 4.91s | LLM 实体/路径提取 |
| EntityLinker | 0.03s | 模糊匹配 |
| AgenticRetriever | 6.91s | LLM 引导图探索 |
| PathRetriever | 0.02s | metapath 遍历 |

**瓶颈在 LLM 调用**：KGLinker + AgenticRetriever 占了大部分时间。

### Neptune Analytics（EDGAR 43K 节点 / 11M 边）

| 配置 | 时间 | 上下文数 | 答案数 |
|------|------|---------|--------|
| KGLinker only (iter=1) | 42.4s | 1 | 10 |
| **+ CypherKGLinker** (iter=1) | 57.0s | **9 (+8)** | **30 (+20)** |
| KGLinker only (iter=2) | 65.3s | 1 | 21 |

**关键发现**：CypherKGLinker 是核心增量——增加 15 秒换来 3 倍答案量。

### Iterations 对比

| iterations | Local Graph 时间 | 上下文数 | Neptune 时间 | 上下文数 |
|-----------|-----------------|---------|-------------|---------|
| 1 | 16.65s | 17 | 42.6s | 1 |
| 2 | 37.96s | 59 | 65.3s | 1 |
| 3 | 31.42s | 23 | — | — |

**观察**：iterations 影响因图结构和问题而异。Local Graph 上 iter=2 获得最多上下文（59 项），但 iter=3 反而减少到 23 项（可能因去重/剪枝）。

## 踩坑记录

!!! warning "踩坑 1: Claude 3.7 Sonnet 被标记为 Legacy"
    Toolkit 配置默认模型为 `anthropic.claude-3-7-sonnet-20250219-v1:0`，但该版本可能已被标记为 Legacy，调用时报 `ResourceNotFoundException`。**已查文档确认**：Bedrock 对 15 天未使用的 Legacy 模型会限制访问。解决：使用 `us.anthropic.claude-sonnet-4-20250514-v1:0` 或更新版本。

!!! warning "踩坑 2: 实体名称大小写匹配"
    KG 中实体名为全大写 `BERKSHIRE HATHAWAY INC`，LLM 生成的 Cypher 查询用 `Berkshire Hathaway`，精确匹配失败。**实测发现，官方未记录**：CypherKGLinker 生成的查询不做大小写归一化，需要图数据本身有一致的命名规范，或依赖 FuzzyStringIndex 做模糊匹配。

!!! warning "踩坑 3: PyTorch GPU 依赖"
    byokg-rag 通过 sentence-transformers → PyTorch 引入 CUDA 依赖（3GB+），在无 GPU 环境中浪费空间。先安装 CPU-only PyTorch 再装 byokg-rag 可节省空间。**实测发现，官方未记录**。

!!! warning "踩坑 4: edges() 方法不适合大图"
    Neptune Analytics 的 `edges()` 方法会将所有边加载到内存。EDGAR 数据集有 11M 边，全量加载会导致进程长时间无响应。**实测发现**：对大图应避免调用 `edges()`，改用 Cypher 查询获取统计信息。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Neptune Analytics (128 m-NCU) | ~$0.10/m-NCU/hr | ~0.3 hr | ~$3.84 |
| Bedrock Claude Sonnet 4 | ~$3/M input, $15/M output | ~20 次调用 | ~$2-5 |
| S3 (EDGAR 数据读取) | 免费（公开数据集） | — | $0 |
| **合计** | | | **~$6-9** |

!!! tip "省钱技巧"
    1. 先用 Local Graph 验证逻辑（$0），再上 Neptune Analytics
    2. Neptune Analytics 支持暂停（10% 费用）——测试间隙暂停
    3. 测试完立即删除 Graph：`aws neptune-graph delete-graph --graph-identifier <id> --skip-snapshot`

## 清理资源

```bash
# 1. 删除 Neptune Analytics Graph
aws neptune-graph delete-graph \
  --graph-identifier <graph-id> \
  --skip-snapshot \
  --region us-east-1

# 2. 确认删除完成
aws neptune-graph list-graphs --region us-east-1
```

!!! danger "务必清理"
    Neptune Analytics 按 m-NCU·小时计费，128 m-NCU 每小时约 $12.80。不用时务必删除或暂停。

## 结论与建议

### 适用场景

| 场景 | 推荐方案 |
|------|---------|
| 有文档，想快速建 KG 做 RAG | Bedrock KB GraphRAG |
| **已有 KG，想接 LLM 做 KGQA** | **Neptune BYOKG-RAG** ✅ |
| 需要精确控制检索策略 | Neptune BYOKG-RAG |
| 追求全托管，不想管基础设施 | Bedrock KB GraphRAG |

### 生产建议

1. **必加 CypherKGLinker**：实测从 10 个答案提升到 30 个，是最重要的增量组件
2. **iterations 从 1 开始**：增加 iterations 不总是线性提升质量，建议按实际场景调优
3. **实体命名规范**：确保 KG 中实体名称统一（大小写、缩写），这直接影响 Entity Linking 质量
4. **大图避免 edges()**：用 Cypher 聚合查询替代全量加载
5. **LLM 选择**：配置文件默认 Claude 3.7 Sonnet 可能已 Legacy，建议显式指定活跃模型

### 与 Bedrock KB GraphRAG 的互补

两种方案不是替代关系，而是互补：

- **Bedrock KB GraphRAG**：适合从非结构化文档出发，自动构建 KG
- **Neptune BYOKG-RAG**：适合已有领域知识图谱（如金融关系图、医疗知识库、供应链图），直接接入 LLM

实际项目中可以混合使用——用 Bedrock KB 从文档构建初始 KG，再用 BYOKG-RAG 的多策略检索增强问答能力。

## 参考链接

- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/08/amazon-neptune-supports-byokg-rag-toolkit/)
- [GraphRAG Toolkit GitHub](https://github.com/awslabs/graphrag-toolkit)
- [BYOKG-RAG 文档](https://github.com/awslabs/graphrag-toolkit/tree/main/byokg-rag)
- [BYOKG-RAG 论文 (arXiv)](https://arxiv.org/abs/2507.04127)
- [Neptune Analytics 文档](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/what-is-neptune-analytics.html)
- [Neptune Analytics 定价](https://aws.amazon.com/neptune/pricing/)
