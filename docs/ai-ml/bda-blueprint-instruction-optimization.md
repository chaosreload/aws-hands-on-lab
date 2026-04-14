---
tags:
  - Bedrock
  - Data Automation
  - What's New
---

# Amazon Bedrock Data Automation Blueprint 指令优化：用样本数据自动提升文档提取准确率

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

在智能文档处理（IDP）场景中，使用 Amazon Bedrock Data Automation (BDA) 从发票、合同、税表等文档中提取结构化数据时，一个常见挑战是：**如何让提取指令足够精确，以匹配你的具体业务需求？**

传统做法是反复手工调整 blueprint 中的 `instruction` 字段——描述每个字段该如何提取。这个过程耗时且需要经验。

2025 年 12 月，BDA 推出了 **Blueprint Instruction Optimization** 功能：你只需提供几个带标注的样本文档（ground truth），系统就能自动分析当前提取结果与期望值的差距，并**自动改写 blueprint 中的指令**，提升提取准确率——无需模型训练或微调，分钟级完成。

## 前置条件

- AWS 账号（需要 `bedrock:*` 和 `s3:*` 权限）
- AWS CLI v2 已配置
- Python 3 + `reportlab` 库（用于生成测试 PDF）

## 核心概念

### BDA Blueprint 是什么？

Blueprint 是 BDA 中定义文档提取规则的模板。核心是一个 JSON schema，包含：

| 字段 | 说明 |
|------|------|
| `class` | 文档类别名称 |
| `properties` | 要提取的字段定义 |
| `instruction` | **自然语言提取指令**（优化的核心目标） |
| `inferenceType` | `explicit`（直接提取）或其他 |
| `definitions` | 可复用的结构定义（如行项目） |

### Instruction Optimization 工作流程

```
创建 Blueprint → 准备样本+Ground Truth → 调用优化 API → 获取评估指标 → 应用优化后的 Blueprint
```

优化过程会：

1. 用当前 blueprint 对每个样本做推理
2. 对比推理结果与 ground truth
3. 自动改写 `instruction` 字段以缩小差距
4. 输出 before/after 的评估指标（exact match、F1、confidence）

**关键限制**：

- 最多 10 个样本文档
- 仅支持 DOCUMENT 类型的 blueprint
- 需要 `dataAutomationProfileArn`（AWS managed profile: `arn:aws:bedrock:REGION:aws:data-automation-profile/us.data-automation-v1`）

## 动手实践

### Step 1: 创建 S3 Bucket

```bash
aws s3 mb s3://bda-optimization-test-$(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1
```

### Step 2: 准备样本发票 PDF 和 Ground Truth

创建 Python 脚本生成测试发票：

```python
# gen_invoices.py
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import json, os

invoices = [
    {
        "invoice_number": "INV-2024-001",
        "invoice_date": "2024-01-15",
        "vendor_name": "Acme Corporation",
        "total_amount": "$1,250.00",
        "line_items": [
            {"description": "Widget A", "quantity": "10",
             "unit_price": "$50.00", "amount": "$500.00"},
            {"description": "Widget B", "quantity": "5",
             "unit_price": "$150.00", "amount": "$750.00"}
        ]
    },
    {
        "invoice_number": "INV-2024-002",
        "invoice_date": "2024-02-20",
        "vendor_name": "TechParts Inc.",
        "total_amount": "$3,475.50",
        "line_items": [
            {"description": "Circuit Board X1", "quantity": "25",
             "unit_price": "$89.00", "amount": "$2,225.00"},
            {"description": "Power Supply P3", "quantity": "5",
             "unit_price": "$250.10", "amount": "$1,250.50"}
        ]
    },
    {
        "invoice_number": "INV-2024-003",
        "invoice_date": "2024-03-10",
        "vendor_name": "Office Supplies Plus",
        "total_amount": "$432.75",
        "line_items": [
            {"description": "Printer Paper (case)", "quantity": "3",
             "unit_price": "$45.25", "amount": "$135.75"},
            {"description": "Ink Cartridge BK", "quantity": "2",
             "unit_price": "$89.00", "amount": "$178.00"},
            {"description": "Desk Organizer", "quantity": "1",
             "unit_price": "$119.00", "amount": "$119.00"}
        ]
    }
]

os.makedirs("bda-test", exist_ok=True)

for i, inv in enumerate(invoices):
    # 生成 PDF
    pdf_path = f"bda-test/invoice_{i+1}.pdf"
    c = canvas.Canvas(pdf_path, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, h-60, "INVOICE")
    c.setFont("Helvetica", 12)
    c.drawString(50, h-100, f"Invoice Number: {inv['invoice_number']}")
    c.drawString(50, h-120, f"Date: {inv['invoice_date']}")
    c.drawString(50, h-140, f"From: {inv['vendor_name']}")
    c.drawString(50, h-160, "Bill To: Test Customer LLC")

    y = h - 200
    c.setFont("Helvetica-Bold", 10)
    for header, x in [("Description",50), ("Qty",250),
                       ("Unit Price",320), ("Amount",420)]:
        c.drawString(x, y, header)
    c.line(50, y-5, 500, y-5)

    y -= 20
    c.setFont("Helvetica", 10)
    for item in inv["line_items"]:
        c.drawString(50, y, item["description"])
        c.drawString(250, y, item["quantity"])
        c.drawString(320, y, item["unit_price"])
        c.drawString(420, y, item["amount"])
        y -= 18

    c.line(50, y-5, 500, y-5)
    y -= 25
    c.setFont("Helvetica-Bold", 12)
    c.drawString(320, y, f"TOTAL: {inv['total_amount']}")
    c.save()

    # 生成 Ground Truth JSON
    gt = {k: v for k, v in inv.items()}
    with open(f"bda-test/ground_truth_{i+1}.json", "w") as f:
        json.dump(gt, f, indent=2)

print("Generated 3 invoices + ground truth files")
```

运行并上传：

```bash
pip install reportlab
python gen_invoices.py

BUCKET="bda-optimization-test-$(aws sts get-caller-identity --query Account --output text)"
aws s3 sync bda-test/ s3://$BUCKET/samples/ --region us-east-1
```

### Step 3: 创建自定义 Blueprint

创建 Blueprint schema 文件 `blueprint_schema.json`：

```json
{
  "class": "Custom Invoice",
  "description": "Extract key fields from invoice documents",
  "definitions": {
    "LINEITEM": {
      "properties": {
        "description": {
          "type": "string", "inferenceType": "explicit",
          "instruction": "Description of the item or service"
        },
        "quantity": {
          "type": "string", "inferenceType": "explicit",
          "instruction": "Quantity of the item"
        },
        "unit_price": {
          "type": "string", "inferenceType": "explicit",
          "instruction": "Unit price of the item including currency symbol"
        },
        "amount": {
          "type": "string", "inferenceType": "explicit",
          "instruction": "Total amount for this line item including currency symbol"
        }
      }
    }
  },
  "properties": {
    "invoice_number": {
      "type": "string", "inferenceType": "explicit",
      "instruction": "The invoice number or ID"
    },
    "invoice_date": {
      "type": "string", "inferenceType": "explicit",
      "instruction": "The date of the invoice"
    },
    "vendor_name": {
      "type": "string", "inferenceType": "explicit",
      "instruction": "The name of the vendor or seller company"
    },
    "total_amount": {
      "type": "string", "inferenceType": "explicit",
      "instruction": "The total amount due on the invoice including currency symbol"
    },
    "line_items": {
      "type": "array",
      "instruction": "Line items table listing all the items charged in the invoice",
      "items": { "$ref": "#/definitions/LINEITEM" }
    }
  }
}
```

!!! tip "Schema 格式提示"
    BDA Blueprint 使用自定义 schema 格式（非标准 JSON Schema）。可以参考 AWS 公开的内置 blueprint 来了解格式。查看发票公开 blueprint：
    ```bash
    aws bedrock-data-automation get-blueprint \
      --blueprint-arn "arn:aws:bedrock:us-east-1:aws:blueprint/bedrock-data-automation-public-invoice" \
      --region us-east-1 \
      --query 'blueprint.schema' --output text | python3 -m json.tool
    ```

创建 blueprint：

```bash
aws bedrock-data-automation create-blueprint \
  --blueprint-name invoice-extraction-test \
  --type DOCUMENT \
  --blueprint-stage DEVELOPMENT \
  --schema file://blueprint_schema.json \
  --region us-east-1
```

记录返回的 `blueprintArn`，后续步骤需要使用。

### Step 4: 运行 Blueprint 优化

```bash
BLUEPRINT_ARN="<上一步返回的 blueprintArn>"
BUCKET="bda-optimization-test-$(aws sts get-caller-identity --query Account --output text)"

aws bedrock-data-automation invoke-blueprint-optimization-async \
  --blueprint "{\"blueprintArn\": \"$BLUEPRINT_ARN\", \"stage\": \"DEVELOPMENT\"}" \
  --samples "[
    {\"assetS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/invoice_1.pdf\"},
     \"groundTruthS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/ground_truth_1.json\"}},
    {\"assetS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/invoice_2.pdf\"},
     \"groundTruthS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/ground_truth_2.json\"}},
    {\"assetS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/invoice_3.pdf\"},
     \"groundTruthS3Object\": {\"s3Uri\": \"s3://$BUCKET/samples/ground_truth_3.json\"}}
  ]" \
  --output-configuration "{\"s3Object\": {\"s3Uri\": \"s3://$BUCKET/output/optimization/\"}}" \
  --data-automation-profile-arn "arn:aws:bedrock:us-east-1:aws:data-automation-profile/us.data-automation-v1" \
  --region us-east-1
```

### Step 5: 查看优化结果

轮询状态直到完成：

```bash
INVOCATION_ARN="<上一步返回的 invocationArn>"

# 轮询状态
aws bedrock-data-automation get-blueprint-optimization-status \
  --invocation-arn "$INVOCATION_ARN" \
  --region us-east-1
```

状态值：`Created` → `InProgress` → `Success`（或 `ClientError` / `ServiceError`）

成功后下载结果：

```bash
# 从状态响应中获取 outputConfiguration.s3Object.s3Uri
aws s3 cp s3://$BUCKET/output/optimization/<invocation-id>/0/optimization_results.json - \
  --region us-east-1 | python3 -m json.tool
```

### Step 6: 验证优化后的 Blueprint

查看优化后的 blueprint：

```bash
aws bedrock-data-automation get-blueprint \
  --blueprint-arn "$BLUEPRINT_ARN" \
  --blueprint-stage DEVELOPMENT \
  --region us-east-1
```

注意观察新增的字段：`optimizationSamples`（优化使用的样本列表）和 `optimizationTime`（优化完成时间）。

## 测试结果

### 实验 1: 清晰 PDF + 详细指令 → 优化对比

| 指标 | 优化前 | 优化后 | 变化 |
|------|--------|--------|------|
| Exact Match | 1.0 | 1.0 | 无变化 |
| F1 Score | 1.0 | 1.0 | 无变化 |
| Avg Confidence | 0.860 | 0.860 | 无变化 |

**各样本 Confidence 分布：**

| 样本 | Confidence (Before) | Confidence (After) |
|------|--------------------|--------------------|
| invoice_1.pdf | 0.836 | 0.836 |
| invoice_2.pdf | 0.856 | 0.856 |
| invoice_3.pdf | 0.887 | 0.887 |

### 实验 2: 清晰 PDF + 模糊指令（如 "qty", "item name"）→ 优化对比

| 指标 | 优化前 | 优化后 | 变化 |
|------|--------|--------|------|
| Exact Match | 1.0 | 1.0 | 无变化 |
| F1 Score | 1.0 | 1.0 | 无变化 |
| Avg Confidence | 0.859 | 0.859 | 无变化 |

### 实验 3: 边界测试 — 仅 1 个样本

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| Exact Match | 1.0 | 1.0 |
| Confidence | 0.836 | 0.835 |

**关键发现：最少 1 个样本即可运行优化。**

### 结果分析

三组实验中 Schema 均未被修改，**原因是 BDA 已经在基线推理中达到了 100% 的准确率**。这说明：

1. **优化器是保守的**：只有当 before 结果与 ground truth 存在差距时，才会改写指令
2. **BDA 底层模型对标准格式文档的提取能力很强**：即使指令很简略（如 "qty"），也能正确提取
3. **优化的真正价值体现在复杂场景**：扫描件、手写文档、非标准布局、多语言混排等场景中，指令优化的改进效果会更显著

## 踩坑记录

!!! warning "Blueprint Schema 格式不是标准 JSON Schema"
    BDA 使用自定义 schema 格式，必须包含 `class`、`description`、`properties`（含 `instruction` 和 `inferenceType`）等字段。**不要**用标准 JSON Schema 的 `$schema`、`required` 等关键字。参考 AWS 内置的公开 blueprint 来了解正确格式。
    **状态**: 实测发现，官方文档未明确说明 schema 格式规范。

!!! warning "dataAutomationProfileArn 必须使用 AWS managed profile"
    优化 API 需要 `dataAutomationProfileArn` 参数。使用 AWS 托管的默认 profile：
    ```
    arn:aws:bedrock:{region}:aws:data-automation-profile/us.data-automation-v1
    ```
    **状态**: 实测发现，官方文档未明确记录默认 profile 名称。

!!! warning "InvokeDataAutomation Runtime API 可能需要额外权限"
    使用 `bedrock-data-automation-runtime` 的 `invoke-data-automation` API 可能会遇到 `AccessDeniedException`，即使 IAM 用户有 `AdministratorAccess`。但 Build-time API（`create-blueprint`、`invoke-blueprint-optimization-async`）工作正常。可能需要检查是否有 Service-Linked Role 或额外的 opt-in 步骤。
    **状态**: 实测发现，具体原因待确认。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| S3 存储 | $0.023/GB | < 1 MB | ~$0.00 |
| BDA 优化推理 | 按页计费 | 3 页 × 3 次优化 | ~$1-3 |
| **合计** | | | **< $5** |

## 清理资源

```bash
# 1. 删除 Blueprints
aws bedrock-data-automation delete-blueprint \
  --blueprint-arn "$BLUEPRINT_ARN" \
  --region us-east-1

# 2. 清空并删除 S3 Bucket
BUCKET="bda-optimization-test-$(aws sts get-caller-identity --query Account --output text)"
aws s3 rb s3://$BUCKET --force --region us-east-1
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。BDA Blueprint 存储不收费，但 S3 存储会产生少量费用。

## 结论与建议

### 适用场景

- **IDP 工作流优化**：当你已有一批带标注的文档样本（发票、合同、税表等），用优化功能可以快速提升 blueprint 的提取准确率
- **新 Blueprint 开发**：先写一个粗略的 schema，然后用样本数据自动优化指令，比手工调整效率高
- **质量验证**：即使不需要优化，也可以用这个功能获取 exact match 和 F1 指标，量化评估 blueprint 的提取质量

### 生产环境建议

1. **准备高质量的 ground truth** — 这是优化效果的关键。确保标注准确且覆盖各种文档变体
2. **使用 5-10 个样本** — 实测 1 个也能跑，但更多样本能覆盖更多变体
3. **关注 confidence 分数** — 即使 exact match 为 1.0，confidence 低可能意味着模型"猜对了但不确定"
4. **对比 before/after schema** — 查看优化器改写了哪些指令，理解提取逻辑
5. **优化完成后创建 Blueprint Version** — 使用 `create-blueprint-version` 固化优化后的 schema

### 与现有方案对比

| 方案 | 耗时 | 成本 | 准确率改进 |
|------|------|------|-----------|
| 手工调整指令 | 数小时-数天 | 人力成本 | 依赖经验 |
| **Blueprint 优化** | **分钟级** | **< $5** | **数据驱动** |
| 模型微调 | 数天 | 数百$ | 最高 |

Blueprint Instruction Optimization 填补了"手工调整"和"模型微调"之间的空白——用最小的成本和时间，获得数据驱动的指令改进。

## 参考链接

- [Amazon Bedrock Data Automation 用户指南](https://docs.aws.amazon.com/bedrock/latest/userguide/bda.html)
- [InvokeBlueprintOptimizationAsync API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation_InvokeBlueprintOptimizationAsync.html)
- [GetBlueprintOptimizationStatus API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation_GetBlueprintOptimizationStatus.html)
- [Amazon Bedrock 定价](https://aws.amazon.com/bedrock/pricing/)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/bedrock-data-automation-optimization-document-blueprints/)
