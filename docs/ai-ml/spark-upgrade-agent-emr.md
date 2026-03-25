# Apache Spark Upgrade Agent：用 AI + MCP 自动化 Spark 版本升级

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $0 - $10（Agent 免费，仅 EMR 验证作业收费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-25

## 背景

Spark 版本升级一直是数据工程团队的痛点：从 Spark 2.4 升级到 3.5 意味着大量 API 变更、弃用方法替换、依赖冲突解决，以及漫长的回归测试。一个中等规模的 Spark 项目，手动升级通常需要 **数周到数月** 的工程投入。

AWS 推出的 **Apache Spark Upgrade Agent** 是一个 AI 驱动的自动化工具，通过 **MCP（Model Context Protocol）** 协议与 SageMaker Unified Studio 后端交互，将这个过程大幅缩短。它能自动分析代码中的版本不兼容问题，生成修复建议，并通过 EMR 集群验证升级后的应用。

本文将实际部署并测试这个 Agent，重点验证：

1. MCP 驱动的 Agent 架构如何工作
2. 代码分析和自动修复的质量
3. 升级流程的完整性和易用性

## 前置条件

- AWS 账号（需要 IAM、CloudFormation、S3、EMR 权限）
- AWS CLI v2 已配置
- Python 3.10+
- [uv 包管理器](https://docs.astral.sh/uv/getting-started/installation/)（用于运行 MCP Proxy）
- 可选：[Kiro CLI](https://kiro.dev/docs/cli/)、Cline、Claude Code 或其他 MCP 兼容客户端

## 核心概念

### 架构

```
┌─────────────────┐     MCP Protocol     ┌───────────────────────┐     SigV4     ┌──────────────────────────┐
│   MCP Client    │ ◄──────────────────► │  MCP Proxy for AWS    │ ◄──────────► │  SageMaker Unified Studio │
│ (Kiro/Cline/    │     stdio            │  (mcp-proxy-for-aws)  │    HTTPS     │  Managed MCP Server       │
│  Claude Code)   │                      │                       │              │  (Spark Upgrade Tools)    │
└─────────────────┘                      └───────────────────────┘              └──────────────────────────┘
```

- **MCP Client**：任何支持 MCP 协议的 AI 助手（Kiro CLI、Cline、Claude Code、GitHub Copilot 等）
- **MCP Proxy for AWS**：处理 SigV4 认证，将本地 MCP stdio 协议转换为 HTTPS 请求
- **SageMaker Unified Studio MCP Server**：云端 AI 引擎，提供 16 个升级专用工具

### 支持范围

| 维度 | 详情 |
|------|------|
| **源版本** | EMR on EC2: 5.20.0+；EMR Serverless: 6.6.0+ |
| **目标版本** | EMR 7.12.0 及更早 |
| **Spark 版本** | 2.4 → 3.5 |
| **语言** | Python (PySpark)、Scala |
| **构建系统** | Maven、SBT、requirements.txt、Pipfile、Setuptools |
| **费用** | Agent 免费，仅收 EMR 验证作业费用 |
| **可用 Region** | 15 个 Region（含 us-east-1, eu-west-1, ap-northeast-1 等） |

### 升级流程（5 阶段）

1. **Planning** — 分析项目结构，自动生成升级计划
2. **Compile & Build** — 更新构建配置和依赖
3. **Spark Code Edit** — 修复版本不兼容的 API 调用
4. **Execute & Validation** — 在目标 EMR 上提交验证作业
5. **Data Quality** — 对比升级前后数据输出质量

## 动手实践

### Step 1: 部署基础设施（CloudFormation）

下载官方 CloudFormation 模板并部署 IAM Role 和 S3 Staging Bucket：

```bash
# 下载 CloudFormation 模板
wget -q https://raw.githubusercontent.com/aws-samples/aws-emr-utilities/03c20fece616de23ec0ea5389f0113a5bc65fc3a/utilities/apache-spark-agents/spark-upgrade-agent-cloudformation/spark-upgrade-mcp-setup.yaml \
  -O /tmp/spark-upgrade-mcp-setup.yaml

# 部署 Stack
aws cloudformation deploy \
  --template-file /tmp/spark-upgrade-mcp-setup.yaml \
  --stack-name spark-upgrade-mcp-setup \
  --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnableEMREC2=false \
    EnableEMRServerless=true
```

获取输出的环境变量：

```bash
# 获取 ExportCommand
aws cloudformation describe-stacks \
  --stack-name spark-upgrade-mcp-setup \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ExportCommand'].OutputValue" \
  --output text

# 输出类似：
# export SMUS_MCP_REGION=us-east-1 && export IAM_ROLE=arn:aws:iam::XXXX:role/spark-upgrade-role-xxx && export STAGING_BUCKET_PATH=spark-upgrade-xxx
```

### Step 2: 配置 AWS CLI Profile

```bash
# 执行上一步获取的 export 命令后配置 Profile
aws configure set profile.spark-upgrade-profile.role_arn ${IAM_ROLE}
aws configure set profile.spark-upgrade-profile.source_profile default
aws configure set profile.spark-upgrade-profile.region ${SMUS_MCP_REGION}

# 验证角色切换
aws sts get-caller-identity --profile spark-upgrade-profile
```

### Step 3: 安装 MCP Proxy

```bash
# 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# MCP Proxy 通过 uvx 按需运行，无需预安装
uvx mcp-proxy-for-aws@latest --help
```

### Step 4: 准备示例 Spark 2.4 代码

创建一个包含 Spark 2.4 弃用 API 的 PySpark 脚本：

```python
# sales_analysis.py - Spark 2.4 风格代码
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, udf, pandas_udf, PandasUDFType
from pyspark.sql.types import DoubleType, StringType

# 弃用 API 1: PandasUDFType.SCALAR（Spark 3.0 弃用）
@pandas_udf(DoubleType(), PandasUDFType.SCALAR)
def calculate_tax(amount):
    return amount * 0.08

def register_tables(spark, df):
    # 弃用 API 2: registerTempTable（Spark 3.5 移除）
    df.registerTempTable("sales_data")
    return spark.sql("SELECT * FROM sales_data WHERE amount > 100")
```

```
# requirements.txt
pyspark==2.4.8
pandas==1.3.5
numpy==1.21.6
```

### Step 5: 连接 MCP Server 并生成升级计划

将以下 MCP 配置添加到你的 AI 助手（Kiro CLI、Cline、Claude Code 等）：

```json
{
  "mcpServers": {
    "spark-upgrade": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "https://sagemaker-unified-studio-mcp.us-east-1.api.aws/spark-upgrade/mcp",
        "--service", "sagemaker-unified-studio-mcp",
        "--profile", "spark-upgrade-profile",
        "--region", "us-east-1",
        "--read-timeout", "180"
      ],
      "timeout": 180000
    }
  }
}
```

在 MCP 客户端中发起升级请求：

```
Upgrade my Spark application /path/to/project from EMR version 6.10.0 to 7.12.0.
Use EMR-Serverless and s3://spark-upgrade-xxx to store artifacts.
```

Agent 会自动：

1. 分析项目结构（检测到 Python 构建系统、1 个源文件、requirements.txt）
2. 映射 EMR 版本到 Spark 版本（6.10.0 → Spark 3.3.1, 7.12.0 → Spark 3.5.6）
3. 生成包含 6 个步骤的升级计划

### Step 6: 观察 Agent 的代码修复

当 Agent 遇到弃用 API 错误时，`fix_upgrade_failure` 工具会返回结构化的代码修复建议：

**修复 1: registerTempTable → createOrReplaceTempView**

```diff
- df.registerTempTable("sales_data")
+ df.createOrReplaceTempView("sales_data")
```

**修复 2: PandasUDFType 弃用语法**

```diff
- from pyspark.sql.functions import col, udf, pandas_udf, PandasUDFType
+ from pyspark.sql.functions import col, udf, pandas_udf

- @pandas_udf(DoubleType(), PandasUDFType.SCALAR)
+ @pandas_udf(returnType=DoubleType())
  def calculate_tax(amount):
      return amount * 0.08
```

**修复 3: requirements.txt 依赖更新**

```diff
- pyspark==2.4.8
+ pyspark==3.5.6
  pandas==1.3.5
  numpy==1.21.6
```

## 测试结果

### MCP 工具完整性

| 类别 | 工具数量 | 关键工具 |
|------|---------|---------|
| Planner | 2 | generate_spark_upgrade_plan, reuse_existing_spark_upgrade_plan |
| Build | 4 | update_build_configuration, compile_and_build_project, check_and_update_build/python_environment |
| Validation | 3 | run_validation_job, check_job_status, prepare_python_venv_on_emr |
| Code Fix | 1 | fix_upgrade_failure |
| Observability | 3 | list_upgrade_analyses, describe_upgrade_analysis, get_data_quality_summary |
| Reporting | 3 | post_build_result, post_test_result, post_upgrade_result |
| **合计** | **16** | 文档列 12 个，实测多 4 个 |

### 代码修复准确性

| 弃用 API | Agent 修复方案 | 正确性 |
|----------|---------------|--------|
| `registerTempTable()` | → `createOrReplaceTempView()` | ✅ 正确 |
| `PandasUDFType.SCALAR` | → `@pandas_udf(returnType=...)` + 移除 import | ✅ 正确 |
| `pyspark==2.4.8` | → `pyspark==3.5.6` | ✅ 正确（保留其他依赖不变） |

### 错误处理

| 场景 | 结果 |
|------|------|
| 错误的版本格式（`emr-5.36.0`） | ✅ 清晰报错，提示正确格式 |
| 错误的应用类型（`emr-serverless`） | ✅ 清晰报错，提示 `EMR-EC2` 或 `EMR-Serverless` |
| 降级请求（7.12.0 → 6.10.0） | ⚠️ 未拦截，生成了降级计划 |

## 踩坑记录

!!! warning "降级不拦截"
    Agent 不验证版本升级方向。我们测试了 EMR 7.12.0 → 6.10.0（降级），Agent 正常生成了从 Spark 3.5.6 → 3.3.1 的"升级"计划，没有任何警告。文档中 EMR-EC2 说"should be newer than EMR 5.20.0"，但 API 未强制校验。**实测发现，官方未记录。**

!!! warning "工具参数格式敏感"
    - `application_type` 必须是 `EMR-EC2` 或 `EMR-Serverless`（大小写敏感），小写 `emr-serverless` 会报错
    - `current_version` 使用 EMR Release 版本号（如 `6.10.0`），不是 Spark 版本号，也不要加 `emr-` 前缀
    - `relevant_code` 参数类型是 object（`{filename: code_content}`），不是 string。**已查文档确认参数 schema。**

!!! warning "MCP Server 处于 Preview"
    SageMaker Unified Studio Managed MCP Server 目前处于 **Preview 阶段**，API 可能变更。**已查文档确认。**

!!! tip "Kiro CLI 安装注意"
    npm 上的 `kiro-cli` 包（v0.0.1）是占位符，不是真正的 Kiro CLI。应该从 [kiro.dev](https://kiro.dev/downloads/) 官网下载安装。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudFormation Stack | 免费 | 1 个 | $0 |
| IAM Role | 免费 | 1 个 | $0 |
| S3 Staging Bucket | $0.023/GB | < 1 MB | ~$0 |
| Spark Upgrade Agent | **免费** | 多次调用 | $0 |
| EMR Serverless（可选） | ~$0.052/vCPU-hr | 按需 | $3-10 |
| **合计** | | | **$0 - $10** |

## 清理资源

```bash
# 1. 删除 CloudFormation Stack（自动清理 IAM Role + S3 Bucket）
aws cloudformation delete-stack \
  --stack-name spark-upgrade-mcp-setup \
  --region us-east-1

# 等待删除完成
aws cloudformation wait stack-delete-complete \
  --stack-name spark-upgrade-mcp-setup \
  --region us-east-1

# 2. 删除本地 AWS CLI Profile
aws configure set profile.spark-upgrade-profile.role_arn ""

# 3. 清理本地临时文件
rm -rf /tmp/spark-upgrade-demo
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。CloudFormation Stack 包含 S3 Bucket，如果 Bucket 非空需要先清空再删除 Stack。

## 结论与建议

### 适合场景

- **大规模 Spark 应用升级**：团队有数十个 Spark 作业需要从 2.4 升级到 3.5，Agent 可以显著减少工程时间
- **PySpark/Scala 项目**：Agent 对两种语言的 API 变更都有良好的识别和修复能力
- **有 EMR 基础设施的团队**：Agent 可以直接在 EMR 上验证升级后的代码

### 核心价值

1. **AI + MCP 架构创新**：这是 AWS 首批通过 MCP 协议暴露的托管服务之一，架构设计具有前瞻性
2. **错误驱动方法论**：一次修一个错误的策略确保每个问题都被正确处理
3. **免费使用**：Agent 本身不收费，降低了采用门槛
4. **IDE 无关**：通过 MCP 协议，任何兼容的 AI 助手都能使用

### 注意事项

- MCP Server 仍在 Preview，生产环境使用需谨慎
- Agent 不处理 Bootstrap Actions 和私有依赖
- 目标 EMR 集群需要用户自行创建和管理
- 降级场景无校验保护，用户需自行确认版本方向

## 参考链接

- [Apache Spark Upgrade Agent 官方文档](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/spark-upgrades.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/12/apache-spark-upgrade-agent-amazon-emr/)
- [MCP Proxy for AWS (GitHub)](https://github.com/aws/mcp-proxy-for-aws)
- [CloudFormation 模板 (GitHub)](https://github.com/aws-samples/aws-emr-utilities/tree/main/utilities/apache-spark-agents/spark-upgrade-agent-cloudformation)
- [Setup 指南](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-setup.html)
- [Features and Capabilities](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-features.html)
