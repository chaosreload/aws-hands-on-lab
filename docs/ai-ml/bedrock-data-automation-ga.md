---
tags:
  - Bedrock
  - Data Automation
  - What's New
---

# Amazon Bedrock Data Automation 实战：从文档解析到多模态智能处理

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0.50-2.00
    - **Region**: us-east-1 / us-west-2
    - **最后验证**: 2026-03-25

## 背景

过去要从非结构化文档中提取结构化数据，通常需要组合多个 AWS 服务（Textract + Comprehend + 自定义 Lambda），再加上大量的编排逻辑。Amazon Bedrock Data Automation (BDA) 在 2025 年 3 月正式 GA，提供了一个统一的多模态处理接口——文档、图片、视频、音频都通过同一个 API 处理，底层自动选择最佳模型，开发者不再需要管理模型编排。

**核心价值**：一个 API 替代过去的多服务编排，同时支持标准输出和自定义提取规则（Blueprint）。

## 前置条件

- AWS 账号（需要 Bedrock 相关权限）
- AWS CLI v2 已配置
- Python 3.8+ 和 boto3
- **⚠️ 重要：首次使用需要通过 AWS Console 访问 BDA 功能，以创建 data-automation-profile（详见踩坑记录）**

## 核心概念

### BDA 架构一览

```
                    ┌─────────────────────────┐
                    │   InvokeDataAutomation   │
                    │   (Sync / Async API)     │
                    └──────────┬──────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
       ┌──────────┐    ┌──────────┐     ┌──────────────┐
       │ Standard │    │ Blueprint│     │   Project     │
       │ Output   │    │ (Custom) │     │ (组合配置)    │
       └──────────┘    └──────────┘     └──────────────┘
              │                │
              ▼                ▼
     ┌────────────────────────────────┐
     │      多模态处理引擎              │
     │  文档 │ 图片 │ 视频 │ 音频      │
     └────────────────────────────────┘
```

### 三个核心概念

| 概念 | 说明 | 类比 |
|------|------|------|
| **Standard Output** | 默认提取结果，按文件类型返回常见信息 | "开箱即用"模式 |
| **Blueprint** | 自定义提取规则，定义字段名、类型、提取逻辑 | "定制模具" |
| **Project** | 组织 Standard + Blueprint 配置，支持多模态 | "生产线配置" |

### BDA vs 传统方案对比

| 维度 | Textract + Lambda | BDA |
|------|-------------------|-----|
| 支持模态 | 仅文档/图片 | 文档 + 图片 + 视频 + 音频 |
| 模型管理 | 需自行编排 | 自动选择最佳模型 |
| 自定义提取 | 需要 Queries 或自定义代码 | Blueprint 声明式定义 |
| 置信度 | ✅ | ✅ + Visual Grounding |
| RAG 集成 | 需额外开发 | 原生支持 Knowledge Bases |
| API 数量 | 多个（Textract + Comprehend + ...） | 统一 1 个 API |
| 预制模板 | 无 | 41 个 Catalog Blueprint |

## 动手实践

### Step 1: 了解 API 结构

BDA 有两个 service endpoint：

- **bedrock-data-automation** — 管理面（Create/Get/Update/Delete Blueprint 和 Project）
- **bedrock-data-automation-runtime** — 数据面（InvokeDataAutomation / InvokeDataAutomationAsync）

```bash
# 查看管理面可用操作
aws bedrock-data-automation help

# 查看运行时可用操作
aws bedrock-data-automation-runtime help
```

### Step 2: 探索 Blueprint Catalog

BDA 提供了 41 个预制 Blueprint，覆盖金融、身份证件、税务表单、媒体分析等场景：

```python
import boto3
import json

session = boto3.Session(region_name='us-east-1')
bda = session.client('bedrock-data-automation')

# 列出所有预制 Blueprint
blueprints = bda.list_blueprints(resourceOwner='SERVICE')
for bp in blueprints['blueprints']:
    print(f"  {bp['blueprintName']:30s} | {bp['blueprintArn']}")
```

**输出示例**（部分）：

| 类别 | 可用 Blueprint |
|------|---------------|
| 金融文档 | Invoice, Receipt, Bank-Statement, Credit-Card-Statement, Payslip |
| 税务表单 | Form-1040, W2-Form, Form-1099-INT, Form-940, Form-941 |
| 身份证件 | US-Passport, US-Driver-License, Canada-Driver-License |
| 保险/医疗 | US-Medical-Insurance-Card, Dental-Insurance-Card, Prescription-Label |
| 账单 | Electricity-Bill, Water-And-Sewer-Bill, Cable-Bill |
| 媒体分析 | Advertisement, General-Image, General-Audio, Keynote-Highlight |

### Step 3: 查看 Blueprint Schema 结构

```python
# 获取发票 Blueprint 的详细 Schema
bp = bda.get_blueprint(
    blueprintArn='arn:aws:bedrock:us-east-1:aws:blueprint/bedrock-data-automation-public-invoice',
    blueprintStage='LIVE'
)
schema = json.loads(bp['blueprint']['schema'])
print(json.dumps(schema, indent=2)[:500])
```

Blueprint Schema 采用声明式 JSON 格式：

```json
{
  "class": "Invoices",
  "description": "An invoice document containing...",
  "definitions": {
    "LINEITEM": {
      "properties": {
        "quantity": {"type": "number", "inferenceType": "explicit"},
        "unit price": {"type": "number", "inferenceType": "explicit"},
        "amount": {"type": "number", "inferenceType": "explicit",
                   "instruction": "Unit Price * Quantity"}
      }
    }
  },
  "properties": {
    "invoice_number": {"type": "string", "inferenceType": "explicit"},
    "total_amount": {"type": "number", "inferenceType": "explicit"},
    "vendor_name": {"type": "string", "inferenceType": "explicit"}
  }
}
```

关键要素：

- **inferenceType**: `explicit`（文档中直接可见）或 `inferred`（需要推理/转换）
- **instruction**: 自然语言描述提取规则
- **definitions**: 定义可复用的复合类型（如行项目表格）
- **type**: 支持 `string`、`number`、`boolean`、`array of string`、`array of numbers`

### Step 4: 创建自定义 Blueprint

```python
custom_schema = {
    "class": "Custom Invoice",
    "description": "Extract key information from invoices",
    "properties": {
        "invoice_number": {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "The invoice number or ID"
        },
        "total_amount": {
            "type": "number",
            "inferenceType": "explicit",
            "instruction": "The total amount on the invoice"
        },
        "vendor_name": {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": "The vendor or company name"
        },
        "invoice_date": {
            "type": "string",
            "inferenceType": "inferred",
            "instruction": "Invoice date in YYYY-MM-DD format"
        }
    }
}

response = bda.create_blueprint(
    blueprintName='my-invoice-extractor',
    type='DOCUMENT',
    schema=json.dumps(custom_schema),
    blueprintStage='LIVE'
)
blueprint_arn = response['blueprint']['blueprintArn']
print(f"Blueprint ARN: {blueprint_arn}")
```

### Step 5: 创建 Project

Project 是 BDA 的核心配置单元，组合了 Standard Output 和 Custom Blueprint：

```python
project = bda.create_data_automation_project(
    projectName='my-bda-project',
    projectStage='LIVE',
    standardOutputConfiguration={
        'document': {
            'extraction': {
                'granularity': {'types': ['DOCUMENT', 'PAGE', 'ELEMENT']},
                'boundingBox': {'state': 'ENABLED'}
            },
            'generativeField': {'state': 'ENABLED'},
            'outputFormat': {
                'textFormat': {'types': ['PLAIN_TEXT', 'MARKDOWN']},
                'additionalFileFormat': {'state': 'ENABLED'}
            }
        },
        'image': {
            'extraction': {
                'category': {
                    'state': 'ENABLED',
                    'types': ['CONTENT_MODERATION', 'TEXT_DETECTION', 'LOGOS']
                },
                'boundingBox': {'state': 'ENABLED'}
            },
            'generativeField': {
                'state': 'ENABLED',
                'types': ['IMAGE_SUMMARY', 'IAB']
            }
        }
    },
    customOutputConfiguration={
        'blueprints': [
            {
                'blueprintArn': blueprint_arn,
                'blueprintStage': 'LIVE'
            }
        ]
    }
)
project_arn = project['projectArn']
print(f"Project ARN: {project_arn}")
print(f"Status: {project['status']}")
```

!!! warning "Project 创建注意事项"
    - `image.generativeField` 必须包含 `types`（如 `IMAGE_SUMMARY`, `IAB`）
    - `document.extraction.granularity` 和 `document.outputFormat.additionalFileFormat` 是必填项
    - Project 创建后状态为 `IN_PROGRESS`，需等待变为 `COMPLETED` 后才能使用

### Step 6: 调用 BDA 处理文件

```python
runtime = session.client('bedrock-data-automation-runtime')

# 异步调用
response = runtime.invoke_data_automation_async(
    inputConfiguration={
        's3Uri': 's3://your-bucket/input/document.pdf'
    },
    outputConfiguration={
        's3Uri': 's3://your-bucket/output/'
    },
    dataAutomationConfiguration={
        'dataAutomationProjectArn': project_arn,
        'stage': 'LIVE'
    },
    dataAutomationProfileArn=profile_arn  # 见踩坑记录
)

invocation_arn = response['invocationArn']

# 轮询状态
import time
while True:
    status = runtime.get_data_automation_status(invocationArn=invocation_arn)
    state = status['status']
    print(f"Status: {state}")
    if state in ['SUCCESS', 'FAILED']:
        break
    time.sleep(5)
```

## 测试结果

### Blueprint Catalog 覆盖度

| 领域 | Blueprint 数量 | 典型用例 |
|------|---------------|---------|
| 金融文档 | 5 | 发票、收据、银行对账单 |
| 税务表单（美国） | 7 | 1040, W2, 1099, 940, 941 |
| 身份证件 | 4 | 护照、驾照（美/加） |
| 保险/医疗 | 4 | 医保卡、处方标签、疫苗卡 |
| 账单 | 5 | 水电煤、有线电视、HOA |
| 法律文档 | 5 | 出生/死亡/结婚证书 |
| 媒体分析 | 4 | 广告、图片、音频、视频 |
| **合计** | **41** | |

### Project 配置灵活性

| 特性 | 支持情况 |
|------|---------|
| LIVE / DEVELOPMENT 双 stage | ✅ |
| SYNC / ASYNC 两种项目类型 | ✅ |
| 单项目多模态配置 | ✅ |
| 文档自动拆分 | ✅（多文档 PDF 自动分割） |
| Blueprint 自动匹配 | ✅（最多 40 个文档 Blueprint） |
| KMS CMK 加密 | ✅ |
| 资源标签 | ✅ |

## 踩坑记录

!!! danger "关键：首次使用必须通过 Console 初始化"
    **问题**：调用 `InvokeDataAutomation` 或 `InvokeDataAutomationAsync` 时，`dataAutomationProfileArn` 是必填参数。但该 Profile 不是通过 API 创建的——它在你第一次通过 AWS Console 访问 BDA 功能时自动生成。

    **表现**：如果账号从未通过 Console 使用过 BDA，任何 profile ARN 格式（无论 `aws` 还是账号 ID）都会返回 `ValidationException: The provided ARN is invalid`。

    **解决方案**：在 AWS Console 中导航到 Bedrock → Data Automation，创建任意一个项目。Console 会自动完成 Profile 的初始化。

    **状态**：实测发现 + 第三方 GitHub 项目确认。**AWS 官方 API 文档未说明此前提条件。**

!!! warning "Project 配置的必填字段比文档描述更多"
    创建 Project 时以下字段必须显式提供（CLI help 文档标记为 required，但容易遗漏）：

    - `document.extraction.granularity.types`
    - `document.outputFormat.additionalFileFormat`
    - `image.extraction.category`（需含 state + types）
    - `image.generativeField.types`（当 state=ENABLED 时）

!!! info "Blueprint Schema 格式 — 使用 JSON 声明式结构"
    自定义 Blueprint 的 schema 不是简单的字段列表，而是类似 JSON Schema 的声明式结构。
    最快的入门方式是获取 Catalog Blueprint 的 schema 作为模板，然后修改。已查文档确认。

## 费用明细

| 资源 | 操作 | 费用 |
|------|------|------|
| BDA Project 创建/管理 | 免费 | $0 |
| BDA Blueprint 创建/管理 | 免费 | $0 |
| BDA 文档处理 | 按页计费 | ~$0.01-0.05/页 |
| BDA 图片处理 | 按张计费 | ~$0.01-0.05/张 |
| BDA 视频处理 | 按分钟计费 | 按实际用量 |
| S3 存储 | 测试数据 | < $0.01 |
| **本次 Lab 合计** | | **< $0.50** |

## 清理资源

```python
import boto3

session = boto3.Session(region_name='us-east-1')
bda = session.client('bedrock-data-automation')
s3 = session.client('s3')

# 1. 删除 Project
bda.delete_data_automation_project(projectArn=project_arn)

# 2. 删除自定义 Blueprint
bda.delete_blueprint(blueprintArn=blueprint_arn)

# 3. 清空并删除 S3 Bucket
bucket = 'your-test-bucket'
objects = s3.list_objects_v2(Bucket=bucket)
for obj in objects.get('Contents', []):
    s3.delete_object(Bucket=bucket, Key=obj['Key'])
s3.delete_bucket(Bucket=bucket)
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。BDA Project 和 Blueprint 本身不产生持续费用，但 S3 存储和未清理的处理作业可能产生费用。

## 结论与建议

### 适用场景

- **IDP（智能文档处理）流水线**：发票、合同、表单的批量处理
- **RAG 数据预处理**：作为 Knowledge Bases 的 parser，提取多模态文档的结构化内容
- **媒体资产管理**：视频摘要、广告内容分析、品牌 Logo 检测
- **合规审核**：身份证件验证、表单数据提取

### 对比建议

| 场景 | 推荐方案 |
|------|---------|
| 简单 OCR / 文字提取 | Amazon Textract（更成熟、更便宜） |
| 结构化文档批量处理 | **BDA**（Blueprint 声明式 > 自定义代码） |
| 多模态内容理解 | **BDA**（唯一一个统一 API） |
| RAG 文档解析 | **BDA**（原生 Knowledge Bases 集成） |
| 视频/音频分析 | **BDA**（传统方案需要多个服务组合） |

### 生产环境建议

1. **先 Console 后 API**：首次使用务必通过 Console 初始化，确保 data-automation-profile 创建成功
2. **利用 Catalog Blueprint**：41 个预制模板覆盖了大部分常见文档类型，先试 Catalog 再考虑自定义
3. **DEVELOPMENT → LIVE**：使用 Project 的双 stage 机制，在 DEVELOPMENT 测试通过后再推到 LIVE
4. **异步优先**：生产环境建议使用 `InvokeDataAutomationAsync`，避免同步 API 的 15 字段限制
5. **启用 KMS CMK**：处理敏感文档时使用客户管理密钥加密

## 参考链接

- [Amazon Bedrock Data Automation 产品页](https://aws.amazon.com/bedrock/bda/)
- [BDA 官方文档](https://docs.aws.amazon.com/bedrock/latest/userguide/bda.html)
- [BDA API Reference - InvokeDataAutomationAsync](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation-runtime_InvokeDataAutomationAsync.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-data-automation-generally-available/)
