---
tags:
  - Analytics
---

# Amazon OpenSearch Ingestion 批量 AI 推理实战：用 SageMaker 批量生成向量嵌入

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟（含等待资源创建）
    - **预估费用**: $3-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

在构建语义搜索、RAG 应用时，一个常见的需求是：**将大量文本数据转换为向量嵌入并写入 OpenSearch**。

以前，OpenSearch 的 AI connectors 只支持实时推理（real-time inference），适用于低延迟的流式场景。但当你需要处理百万甚至数十亿条数据时，逐条实时调用嵌入模型效率极低，成本也不划算。

2025 年 10 月，AWS 发布了 **OpenSearch Ingestion 批量 AI 推理**功能，通过 `ml_inference` 处理器的 `batch_predict` 模式，实现大规模数据的异步批量嵌入生成。官方文档声称：10 亿请求使用 100 个 `ml.m4.xlarge` 实例，14 小时即可完成向量化。

本文将从零开始，动手验证这一功能的端到端流程。

## 前置条件

- AWS 账号（需要 OpenSearch、SageMaker、S3、IAM 权限）
- AWS CLI v2 已配置
- Python 3 + awscurl（用于 SigV4 签名请求）
- 对 OpenSearch Service 和 ML Commons 有基本了解

## 核心概念

### 批量推理 vs 实时推理

| 对比项 | 实时推理 (predict) | 批量推理 (batch_predict) |
|--------|-------------------|------------------------|
| 调用方式 | 同步，逐条处理 | 异步，批量提交 |
| 延迟 | 低（毫秒级） | 高（分钟到小时级） |
| 适用场景 | 流式数据、查询时嵌入 | 历史数据迁移、大规模索引 |
| 成本效率 | 较低（需持续运行端点） | 较高（按需启动计算资源） |
| 扩展性 | 受端点实例数限制 | 可动态扩展到数百实例 |

### 三管道架构

OpenSearch Ingestion 的批量推理采用三管道架构：

1. **Pipeline 1 — 数据准备**（可选）：从外部数据源读取 → 转换为 JSONL 格式 → 写入 S3
2. **Pipeline 2 — 批量推理触发**：S3 事件检测 → `ml_inference` 处理器调用 `batch_predict` → SageMaker/Bedrock 异步处理 → 结果写回 S3
3. **Pipeline 3 — 批量写入**：从 S3 读取推理结果 → 数据转换 → 写入 OpenSearch 索引

**关键设计**：S3 Scan 使用 metadata-only 模式，仅收集文件元信息而不读取内容，通过 S3 文件 URL 与 ML Commons 协调批量处理，最大化吞吐效率。

## 动手实践

本文使用 **SageMaker Batch Transform + DJL text embedding 模型 (all-MiniLM-L6-v2)** 路径来验证批量推理的端到端流程。

### Step 1: 创建 S3 Bucket

```bash
aws s3 mb s3://osi-batch-ai-inference-test-${ACCOUNT_ID} \
  --region us-east-1
```

### Step 2: 创建 IAM 角色

需要两个角色：

**SageMaker 执行角色**（用于 Transform Job 执行和 OpenSearch ML Commons 调用）：

```bash
# 创建信任策略（允许 SageMaker 和 OpenSearch Service assume）
cat > /tmp/sagemaker-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": ["sagemaker.amazonaws.com", "opensearchservice.amazonaws.com"]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name osi-batch-sagemaker-role \
  --assume-role-policy-document file:///tmp/sagemaker-trust-policy.json
```

附加权限策略（S3 读写 + ECR 拉取镜像 + SageMaker Transform + CloudWatch Logs）：

```bash
cat > /tmp/sagemaker-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::osi-batch-ai-inference-test-YOUR_ACCOUNT_ID",
        "arn:aws:s3:::osi-batch-ai-inference-test-YOUR_ACCOUNT_ID/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
                 "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob",
                 "sagemaker:StopTransformJob"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:YOUR_ACCOUNT_ID:log-group:*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name osi-batch-sagemaker-role \
  --policy-name sagemaker-batch-policy \
  --policy-document file:///tmp/sagemaker-policy.json
```

**OSI 管道角色**（用于管道读 S3、写 OpenSearch、调 SageMaker）：

```bash
cat > /tmp/osi-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "osis-pipelines.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {"StringEquals": {"aws:SourceAccount": "YOUR_ACCOUNT_ID"}}
    }
  ]
}
EOF

aws iam create-role \
  --role-name osi-batch-pipeline-role \
  --assume-role-policy-document file:///tmp/osi-trust-policy.json
```

附加权限（S3 + OpenSearch + SageMaker + PassRole）：

```bash
cat > /tmp/osi-pipeline-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket",
                 "s3:GetBucketLocation", "s3:DeleteObject", "s3:GetBucketNotification"],
      "Resource": ["arn:aws:s3:::osi-batch-ai-inference-test-YOUR_ACCOUNT_ID",
                   "arn:aws:s3:::osi-batch-ai-inference-test-YOUR_ACCOUNT_ID/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut",
                 "es:ESHttpDelete", "es:DescribeDomain", "es:DescribeDomains"],
      "Resource": "arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/osi-batch-test*"
    },
    {
      "Effect": "Allow",
      "Action": ["sagemaker:CreateTransformJob", "sagemaker:DescribeTransformJob",
                 "sagemaker:StopTransformJob"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::YOUR_ACCOUNT_ID:role/osi-batch-sagemaker-role"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name osi-batch-pipeline-role \
  --policy-name osi-pipeline-policy \
  --policy-document file:///tmp/osi-pipeline-policy.json
```

### Step 3: 创建 OpenSearch 域（2.17+）

```bash
aws opensearch create-domain \
  --domain-name osi-batch-test \
  --engine-version OpenSearch_2.17 \
  --cluster-config InstanceType=t3.small.search,InstanceCount=1 \
  --ebs-options EBSEnabled=true,VolumeType=gp3,VolumeSize=10 \
  --node-to-node-encryption-options Enabled=true \
  --encryption-at-rest-options Enabled=true \
  --domain-endpoint-options EnforceHTTPS=true \
  --advanced-security-options 'Enabled=true,InternalUserDatabaseEnabled=true,MasterUserOptions={MasterUserName=admin,MasterUserPassword=YourPassword123!}' \
  --access-policies '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":"es:*","Resource":"arn:aws:es:us-east-1:YOUR_ACCOUNT_ID:domain/osi-batch-test/*"}]}' \
  --region us-east-1
```

!!! warning "等待域创建"
    域创建需要 15-20 分钟。可用以下命令检查状态：
    ```bash
    aws opensearch describe-domain --domain-name osi-batch-test \
      --query 'DomainStatus.{Processing:Processing, Endpoint:Endpoint}' \
      --region us-east-1
    ```

### Step 4: 创建 SageMaker 模型

使用 DJL (Deep Java Library) 的 all-MiniLM-L6-v2 文本嵌入模型：

```bash
cat > /tmp/sagemaker-model.json << 'EOF'
{
  "ModelName": "djl-text-embedding-minilm-batch",
  "PrimaryContainer": {
    "Image": "763104351884.dkr.ecr.us-east-1.amazonaws.com/djl-inference:0.29.0-cpu-full",
    "Environment": {
      "SERVING_LOAD_MODELS": "djl://ai.djl.huggingface.pytorch/sentence-transformers/all-MiniLM-L6-v2"
    }
  },
  "ExecutionRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/osi-batch-sagemaker-role"
}
EOF

aws sagemaker create-model \
  --cli-input-json file:///tmp/sagemaker-model.json \
  --region us-east-1
```

### Step 5: 配置 OpenSearch ML Commons

域就绪后，需要配置 ML Commons 和角色映射。

```bash
OS_ENDPOINT="https://<your-domain-endpoint>"

# 启用远程模型支持
curl -u 'admin:YourPassword123!' -X PUT \
  "$OS_ENDPOINT/_cluster/settings" \
  -H 'Content-Type: application/json' \
  -d '{"persistent":{"plugins.ml_commons.only_run_on_ml_node":false,"plugins.ml_commons.model_access_control_enabled":true}}' \
  --insecure

# 映射 IAM 角色到 ml_full_access
curl -u 'admin:YourPassword123!' -X PUT \
  "$OS_ENDPOINT/_plugins/_security/api/rolesmapping/ml_full_access" \
  -H 'Content-Type: application/json' \
  -d '{"backend_roles":["arn:aws:iam::YOUR_ACCOUNT_ID:role/osi-batch-pipeline-role"],"users":["admin","arn:aws:iam::YOUR_ACCOUNT_ID:user/YOUR_IAM_USER"]}' \
  --insecure

# 映射 all_access（管道需要写索引）
curl -u 'admin:YourPassword123!' -X PUT \
  "$OS_ENDPOINT/_plugins/_security/api/rolesmapping/all_access" \
  -H 'Content-Type: application/json' \
  -d '{"backend_roles":["arn:aws:iam::YOUR_ACCOUNT_ID:role/osi-batch-pipeline-role"],"users":["admin","arn:aws:iam::YOUR_ACCOUNT_ID:user/YOUR_IAM_USER"]}' \
  --insecure
```

### Step 6: 创建 Connector 和注册模型

安装 awscurl 用于 IAM SigV4 认证：

```bash
pip install awscurl
```

创建 SageMaker Batch Connector：

```bash
cat > /tmp/connector.json << 'EOF'
{
  "name": "DJL SageMaker Batch Connector: all-MiniLM-L6-v2",
  "version": "1",
  "description": "Connector to SageMaker embedding model for batch inference",
  "protocol": "aws_sigv4",
  "credential": {
    "roleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/osi-batch-sagemaker-role"
  },
  "parameters": {
    "region": "us-east-1",
    "service_name": "sagemaker",
    "DataProcessing": {"InputFilter": "$.text", "JoinSource": "Input", "OutputFilter": "$"},
    "ModelName": "djl-text-embedding-minilm-batch",
    "TransformInput": {
      "ContentType": "application/json",
      "DataSource": {"S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": "s3://osi-batch-ai-inference-test-YOUR_ACCOUNT_ID/sagemaker/input/"}},
      "SplitType": "Line"
    },
    "TransformJobName": "osi-batch-test-job",
    "TransformOutput": {"AssembleWith": "Line", "Accept": "application/json", "S3OutputPath": "s3://osi-batch-ai-inference-test-YOUR_ACCOUNT_ID/sagemaker/output/"},
    "TransformResources": {"InstanceCount": 1, "InstanceType": "ml.m5.xlarge"},
    "BatchStrategy": "SingleRecord"
  },
  "actions": [
    {
      "action_type": "predict",
      "method": "POST",
      "url": "https://runtime.sagemaker.us-east-1.amazonaws.com/endpoints/placeholder/invocations",
      "request_body": "${parameters.input}",
      "pre_process_function": "connector.pre_process.default.embedding",
      "post_process_function": "connector.post_process.default.embedding"
    },
    {
      "action_type": "batch_predict",
      "method": "POST",
      "url": "https://api.sagemaker.us-east-1.amazonaws.com/CreateTransformJob",
      "request_body": "{ \"BatchStrategy\": \"${parameters.BatchStrategy}\", \"ModelName\": \"${parameters.ModelName}\", \"DataProcessing\": ${parameters.DataProcessing}, \"TransformInput\": ${parameters.TransformInput}, \"TransformJobName\": \"${parameters.TransformJobName}\", \"TransformOutput\": ${parameters.TransformOutput}, \"TransformResources\": ${parameters.TransformResources} }"
    }
  ]
}
EOF

python3 -m awscurl -X POST "$OS_ENDPOINT/_plugins/_ml/connectors/_create" \
  -H 'Content-Type: application/json' \
  -d @/tmp/connector.json \
  --region us-east-1 --service es
# 记录返回的 connector_id
```

注册模型：

```bash
python3 -m awscurl -X POST "$OS_ENDPOINT/_plugins/_ml/models/_register?deploy=true" \
  -H 'Content-Type: application/json' \
  -d '{"name":"SageMaker model for batch inference","function_name":"remote","description":"SageMaker hosted DJL text embedding model","connector_id":"<your-connector-id>"}' \
  --region us-east-1 --service es
# 记录返回的 model_id
```

### Step 7: 准备测试数据

创建 JSONL 格式的测试数据（每行一个 JSON 对象）：

```bash
cat > /tmp/batch_input.jsonl << 'EOF'
{"_id": "1", "text": "What is Amazon OpenSearch Service and how does it work?"}
{"_id": "2", "text": "How to configure vector search in OpenSearch?"}
{"_id": "3", "text": "What are the benefits of batch inference over real-time inference?"}
{"_id": "4", "text": "Explain the concept of semantic search using embeddings"}
{"_id": "5", "text": "How does SageMaker batch transform process large datasets?"}
{"_id": "6", "text": "What is ML Commons plugin in OpenSearch?"}
{"_id": "7", "text": "How to create an OpenSearch Ingestion pipeline?"}
{"_id": "8", "text": "What are the best practices for vector database design?"}
{"_id": "9", "text": "How to monitor machine learning jobs in AWS?"}
{"_id": "10", "text": "What is the difference between knn and neural search in OpenSearch?"}
EOF

aws s3 cp /tmp/batch_input.jsonl \
  s3://osi-batch-ai-inference-test-${ACCOUNT_ID}/sagemaker/input/batch_input.jsonl \
  --region us-east-1
```

### Step 8: 执行批量推理

调用 `_batch_predict` API 触发 SageMaker Transform Job：

```bash
python3 -m awscurl -X POST \
  "$OS_ENDPOINT/_plugins/_ml/models/<your-model-id>/_batch_predict" \
  -H 'Content-Type: application/json' \
  -d '{"parameters":{"TransformJobName":"osi-batch-test-job-001"}}' \
  --region us-east-1 --service es
```

响应将返回 `task_id` 和 `TransformJobArn`：

```json
{
  "task_id": "b4ZRM50B4RuvXIDyNYIF",
  "status": "CREATED",
  "remote_job": {
    "TransformJobArn": "arn:aws:sagemaker:us-east-1:595842667825:transform-job/osi-batch-test-job-001"
  }
}
```

### Step 9: 监控和验证

**监控 Transform Job 状态**：

```bash
aws sagemaker describe-transform-job \
  --transform-job-name osi-batch-test-job-001 \
  --query '{Status:TransformJobStatus, CreationTime:CreationTime}' \
  --region us-east-1
```

Job 完成后（约 3-5 分钟），检查 S3 输出：

```bash
aws s3 ls s3://osi-batch-ai-inference-test-${ACCOUNT_ID}/sagemaker/output/ --recursive
```

每条输出包含原始字段加 `SageMakerOutput`（384 维向量嵌入）：

```json
{
  "SageMakerOutput": [0.0123, -0.0456, ...],
  "_id": "1",
  "text": "What is Amazon OpenSearch Service and how does it work?"
}
```

### Step 10: 写入 OpenSearch 并验证搜索

**创建 knn 索引**：

```bash
python3 -m awscurl -X PUT "$OS_ENDPOINT/batch-nlp-index" \
  -H 'Content-Type: application/json' \
  -d '{"settings":{"index.knn":true,"number_of_shards":1},"mappings":{"properties":{"text":{"type":"text"},"embedding":{"type":"knn_vector","dimension":384,"method":{"name":"hnsw","space_type":"l2","engine":"lucene"}}}}}' \
  --region us-east-1 --service es
```

**批量写入数据**（用 Python 将 SageMaker 输出转为 Bulk 格式后用 `_bulk` API 写入）：

```python
import json

with open("batch_input.jsonl.out") as f:
    lines = f.readlines()

bulk = ""
for line in lines:
    r = json.loads(line)
    bulk += json.dumps({"index": {"_index": "batch-nlp-index", "_id": r["_id"]}}) + "\n"
    bulk += json.dumps({"text": r["text"], "embedding": r["SageMakerOutput"]}) + "\n"

with open("bulk_request.ndjson", "w") as f:
    f.write(bulk)

# 然后用 awscurl 提交 bulk 请求
```

**执行 knn 语义搜索**：

```bash
# 使用某条记录的向量作为查询向量
python3 -m awscurl -X POST "$OS_ENDPOINT/batch-nlp-index/_search" \
  -H 'Content-Type: application/json' \
  -d '{"size":5,"query":{"knn":{"embedding":{"vector":[...],"k":5}}},"_source":{"excludes":["embedding"]}}' \
  --region us-east-1 --service es
```

## 测试结果

### 批量推理性能

| 指标 | 值 |
|------|-----|
| 输入数据 | 20 条 JSONL 文本 |
| 模型 | all-MiniLM-L6-v2 (384 维) |
| 实例类型 | ml.m5.xlarge × 1 |
| Transform Job 耗时 | ~4 分钟 |
| 输出大小 | 95 KB |
| 向量维度 | 384 |

### 语义搜索验证

使用"OpenSearch Serverless"相关文本的向量进行 knn 搜索，Top-5 结果：

| 排名 | Score | 文本 |
|------|-------|------|
| 1 | 1.0000 | What is the difference between OpenSearch Service and OpenSearch Serverless? |
| 2 | 0.6021 | What is Amazon OpenSearch Service and how does it work? |
| 3 | 0.5380 | What is the difference between knn and neural search in OpenSearch? |
| 4 | 0.5355 | What is ML Commons plugin in OpenSearch? |
| 5 | 0.5292 | How does Amazon Bedrock integrate with OpenSearch? |

**分析**：语义搜索结果按相关性正确排序。Score 从精确匹配（1.0）逐步递减到较泛的相关内容，说明嵌入质量良好。

### 边界测试

| 测试 | 输入 | 结果 |
|------|------|------|
| 无效 JSON 混入 | 6 行中 2 行格式错误 | ❌ 整个 Transform Job 失败 |
| 空文件 | 0 字节 JSONL | ❌ Transform Job 失败 |

!!! note "发现"
    SageMaker Batch Transform 对输入数据格式**零容忍**——一行无效 JSON 就会导致整个 Job 失败，不会跳过无效行继续处理。生产环境中务必做好数据预处理和校验。

## 踩坑记录

!!! warning "踩坑 1: 创建 Connector 需要 IAM SigV4 认证"
    使用 OpenSearch 内部用户（HTTP Basic Auth）创建 Connector 会触发 `iam:PassRole` 错误。**必须使用 IAM 认证**（如 awscurl + SigV4）并确保 IAM 用户/角色已映射到 `ml_full_access`。
    
    **状态**: 已查文档确认 — 这是 OpenSearch Service 的设计，Connector 创建涉及 IAM 角色传递。

!!! warning "踩坑 2: OSI 管道创建需要 describeDomain 权限"
    OSI 管道创建时会验证 sink 域的访问权限。管道角色不仅需要 `es:ESHttp*` 权限，还需要 `es:DescribeDomain` 和 `es:DescribeDomains` 权限，否则 CreatePipeline 会报 ValidationException。
    
    **状态**: 已查文档确认 — 创建管道时的错误信息明确指出。

!!! warning "踩坑 3: 端到端 IAM 权限涉及 5 处配置"
    完整流程需要配置：① SageMaker 角色信任策略（sagemaker + opensearchservice） ② SageMaker 角色权限策略 ③ OSI 管道角色（osis-pipelines service principal） ④ OpenSearch 后端角色映射（ml_full_access + all_access） ⑤ 调用者的 iam:PassRole 权限。遗漏任何一处都会导致不同阶段的权限错误。
    
    **状态**: 实测总结，官方文档分散在不同章节。

!!! warning "踩坑 4: Task API 查状态报 UnknownOperationException"
    SageMaker connector blueprint 中 `batch_predict_status` action 使用 GET 方法调用 DescribeTransformJob，但实际该 API 可能需要不同的请求格式，导致通过 ML Commons Task API 查状态时报错。建议直接使用 `aws sagemaker describe-transform-job` CLI 命令监控 Job 状态。
    
    **状态**: 实测发现，官方未记录。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| OpenSearch t3.small.search | $0.036/hr | ~1.5 hr | $0.054 |
| SageMaker ml.m5.xlarge Transform | $0.23/hr | ~0.1 hr | $0.023 |
| S3 存储 + 请求 | - | < 1 MB | $0.001 |
| OSI 管道 (1 OCU) | $0.24/hr | ~0.5 hr | $0.12 |
| **合计** | | | **~$0.20** |

## 清理资源

```bash
# 1. 删除 OSI 管道
aws osis delete-pipeline --pipeline-name osi-batch-ingest --region us-east-1

# 2. 删除 OpenSearch 域
aws opensearch delete-domain --domain-name osi-batch-test --region us-east-1

# 3. 删除 SageMaker 模型
aws sagemaker delete-model --model-name djl-text-embedding-minilm-batch --region us-east-1

# 4. 清空并删除 S3 Bucket
aws s3 rm s3://osi-batch-ai-inference-test-${ACCOUNT_ID} --recursive --region us-east-1
aws s3 rb s3://osi-batch-ai-inference-test-${ACCOUNT_ID} --region us-east-1

# 5. 删除 IAM 角色
aws iam delete-role-policy --role-name osi-batch-sagemaker-role \
  --policy-name sagemaker-batch-policy
aws iam delete-role --role-name osi-batch-sagemaker-role

aws iam delete-role-policy --role-name osi-batch-pipeline-role \
  --policy-name osi-pipeline-policy
aws iam delete-role --role-name osi-batch-pipeline-role

# 6. 删除调用者的 PassRole 策略（如果添加过）
aws iam delete-user-policy --user-name YOUR_IAM_USER \
  --policy-name passrole-sagemaker
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。OpenSearch 域按小时计费（t3.small.search ≈ $0.036/hr），不清理将持续产生费用。

## 结论与建议

### 适用场景

- ✅ **历史数据批量向量化**：百万到十亿级文档的嵌入生成
- ✅ **离线数据管道**：定时将新数据批量嵌入并写入搜索索引
- ✅ **成本敏感场景**：比实时推理端点更经济（按 Job 付费而非持续运行端点）

### 不适用场景

- ❌ 低延迟查询时嵌入（使用实时 `predict` 模式）
- ❌ 实时流数据处理（使用 OSI 实时推理管道）

### 生产建议

1. **数据预处理是关键**：SageMaker Batch Transform 对无效输入零容忍，务必在上游做好 JSONL 格式校验
2. **IAM 权限提前规划**：5 处权限配置容易遗漏，建议使用 CloudFormation 模板一键部署（OpenSearch 控制台 Integrations 已提供模板）
3. **监控用 SageMaker CLI**：ML Commons Task API 的状态查询存在兼容性问题，建议用 `aws sagemaker describe-transform-job` 直接监控
4. **分片并行**：大规模数据建议拆分为多个 JSONL 文件，利用 SageMaker 的 `MaxConcurrentTransforms` 和多实例并行加速

## 参考链接

- [官方文档: Using an OpenSearch Ingestion pipeline with ML offline batch inference](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/configure-clients-ml-commons-batch.html)
- [AWS What's New: Amazon OpenSearch Ingestion now supports batch AI inference](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-opensearch-service-supports-batch-ai-inference/)
- [ML Commons Batch Inference SageMaker Connector Blueprint](https://github.com/opensearch-project/ml-commons/blob/main/docs/remote_inference_blueprints/batch_inference_sagemaker_connector_blueprint.md)
- [SageMaker Batch Transform Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/batch-transform.html)
