---
tags:
  - MCP
  - MSK
  - What's New
---

# MCP Server for Amazon MSK：用自然语言管理 Kafka 集群

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

2025 年 7 月，AWS 发布了 MCP Server for Amazon MSK —— 基于 Anthropic 的 Model Context Protocol (MCP) 开源协议，让 AI Agent 能够通过标准化接口管理和监控 Amazon MSK 集群。

传统的 MSK 运维需要记忆大量 AWS CLI 命令和 API 参数。MCP Server 将这些操作封装成 AI 可调用的 Tools，更重要的是它提供了**聚合视图**和**内置最佳实践引擎**——不只是 API 的简单封装，还能基于集群实际状态给出有上下文感知的运维建议。

本文将实测验证 MCP Server for Amazon MSK 的核心能力，对比 Read-only 和 Write 两种运行模式，并发现一些文档未记录的有趣安全机制。

## 前置条件

- AWS 账号（需要 MSK、EC2、VPC 相关权限）
- AWS CLI v2 已配置
- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（Python 包管理工具）
- MCP 客户端工具（本文使用 [mcporter](https://github.com/nicobailon/mcporter)）

## 核心概念

### MCP Server 是什么？

MCP (Model Context Protocol) 是 Anthropic 开源的协议，标准化了 AI Agent 与外部系统的交互方式。AWS MSK MCP Server 是一个**本地运行的开源服务器**，通过 AWS SDK 与 MSK API 交互。

### 与直接使用 AWS CLI 的区别

| 维度 | AWS CLI | MCP Server |
|------|---------|------------|
| 信息获取 | 每个 API 需要单独调用 | `info_type="all"` 一次获取 8 个维度信息 |
| 运维建议 | 无 | 内置 Best Practices 引擎，返回 14 项可量化指标 |
| 约束感知 | 需要自己查 quota/limits | 自动检查约束和依赖关系 |
| 安全 | 依赖 IAM 权限 | IAM + Read-only 模式 + MCP Generated 标签三重防护 |
| 工具数量 | 数十个独立命令 | 33 个结构化 Tools（Write 模式） |

### 两种运行模式

- **Write 模式**（`--allow-writes`）：33 个 Tools，包含集群创建、配置变更、资源删除等写操作
- **Read-only 模式**（默认）：12 个 Tools，仅包含查询和监控操作，保护生产环境

## 动手实践

### Step 1: 安装和配置 MCP Server

安装 uv（如果未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

创建 MCP Server 配置文件：

```json
// msk-mcp-config.json
{
  "mcpServers": {
    "aws-msk": {
      "command": "uvx",
      "args": ["awslabs.aws-msk-mcp-server@latest", "--allow-writes"],
      "env": {
        "FASTMCP_LOG_LEVEL": "ERROR",
        "AWS_PROFILE": "your-profile",
        "AWS_REGION": "us-east-1"
      }
    }
  },
  "imports": []
}
```

验证连接：

```bash
mcporter --config msk-mcp-config.json list
# 输出: aws-msk (33 tools, ~2s)
```

### Step 2: 查看全局信息和 Kafka 版本

查看账户内 MSK 集群列表：

```bash
mcporter --config msk-mcp-config.json call aws-msk get_global_info \
  --args '{"info_type": "clusters", "region": "us-east-1"}'
```

查看可用的 Kafka 版本：

```bash
mcporter --config msk-mcp-config.json call aws-msk get_global_info \
  --args '{"info_type": "kafka_versions", "region": "us-east-1"}'
```

实测可用版本（截至 2026-03）：从 1.1.1 到 **4.1.x.kraft**，支持 KRaft 模式的版本标记为 `.kraft` 后缀。

### Step 3: 获取最佳实践建议

这是 MCP Server 最有价值的 Tool 之一：

```bash
mcporter --config msk-mcp-config.json call aws-msk get_cluster_best_practices \
  --args '{"region": "us-east-1", "instance_type": "kafka.m5.large", "number_of_brokers": 3}'
```

返回结果包含 14 项可量化的运维建议：

| 指标 | 值 | 说明 |
|------|-----|------|
| vCPU per Broker | 2 | 可用 CPU 核心 |
| Memory (GB) per Broker | 8 | 可用内存 |
| 推荐 Ingress 吞吐 | 4.8 MBps | 持续运行推荐值 |
| 最大 Ingress 吞吐 | 7.2 MBps | 超过会性能下降 |
| 推荐 Egress 吞吐 | 9.6 MBps | 持续运行推荐值 |
| 最大 Egress 吞吐 | 18.0 MBps | 超过会性能下降 |
| 推荐 Partitions/Broker | 1000 | 每 broker 分区数 |
| 最大 Partitions/Broker | 1500 | 含 3 副本 |
| CPU 利用率 | < 60% | 常规运行不超过 60%，绝不超过 70% |
| 磁盘利用率 | < 85% | 85% 告警，90% 严重 |
| Replication Factor | 3 | 推荐值 |
| Min In-Sync Replicas | 2 | 推荐值 |
| Leader Imbalance | < 10% | 允许的不平衡比例 |

!!! tip "为什么这比查文档更好？"
    AWS 文档中的 quota 和 best practices 分散在多个页面，且不会针对你选择的实例类型做计算。MCP Server 的 `get_cluster_best_practices` 会根据输入的 `instance_type` 和 `number_of_brokers` 直接返回**计算后的具体数值**。

### Step 4: 创建 MSK Serverless 集群

首先确认 VPC 和子网信息：

```bash
# 获取默认 VPC ID
aws ec2 describe-vpcs --region us-east-1 \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text

# 获取子网列表（至少需要 2 个，跨不同 AZ）
aws ec2 describe-subnets --region us-east-1 \
  --filters Name=vpc-id,Values=<your-vpc-id> \
  --query 'Subnets[*].[SubnetId,AvailabilityZone]' --output table

# 获取默认安全组
aws ec2 describe-security-groups --region us-east-1 \
  --filters Name=vpc-id,Values=<your-vpc-id> Name=group-name,Values=default \
  --query 'SecurityGroups[0].GroupId' --output text
```

创建 Serverless 集群：

```bash
aws kafka create-cluster-v2 \
  --cluster-name msk-mcp-test \
  --serverless '{
    "VpcConfigs": [{
      "SubnetIds": ["subnet-xxx", "subnet-yyy", "subnet-zzz"],
      "SecurityGroupIds": ["sg-xxx"]
    }],
    "ClientAuthentication": {
      "Sasl": {"Iam": {"Enabled": true}}
    }
  }' \
  --region us-east-1
```

!!! warning "为什么用 CLI 而不是 MCP create_cluster？"
    实测发现 MCP Server 的 `create_cluster` Tool 在处理 Serverless 集群的 `kwargs` 参数时存在格式映射问题，会返回 `BadRequestException: Invalid request body`。这是 MCP Server 当前版本的已知局限。对于 Provisioned 集群，参数结构更简单，成功率更高。

等待约 1 分钟后集群变为 ACTIVE 状态。

### Step 5: 使用 MCP Server 查询集群信息

**一次性获取集群全貌**（聚合查询）：

```bash
mcporter --config msk-mcp-config.json call aws-msk get_cluster_info \
  --args '{"cluster_arn": "<your-cluster-arn>", "info_type": "all", "region": "us-east-1"}'
```

这一次调用会同时获取：metadata、brokers、nodes、compatible_versions、policy、operations、client_vpc_connections、scram_secrets 等 8 个维度的信息。

**获取 Bootstrap Brokers 端点**：

```bash
mcporter --config msk-mcp-config.json call aws-msk get_cluster_info \
  --args '{"cluster_arn": "<your-cluster-arn>", "info_type": "brokers", "region": "us-east-1"}'
```

返回 IAM 认证端点：`boot-xxxxx.c1.kafka-serverless.us-east-1.amazonaws.com:9098`

### Step 6: 标签管理

添加标签：

```bash
mcporter --config msk-mcp-config.json call aws-msk tag_resource \
  --args '{"resource_arn": "<your-cluster-arn>", "region": "us-east-1", "tags": {"Environment": "test", "Project": "mcp-hands-on"}}'
```

查询标签：

```bash
mcporter --config msk-mcp-config.json call aws-msk list_tags_for_resource \
  --args '{"arn": "<your-cluster-arn>", "region": "us-east-1"}'
```

!!! note "参数名不一致"
    注意 `tag_resource` 使用 `resource_arn`，而 `list_tags_for_resource` 使用 `arn`。不同 Tool 的参数命名不完全统一，遇到错误时注意检查参数名。

## 测试结果

### Read-only vs Write 模式 Tool 对比

| Read-only 模式（12 Tools） | Write 模式额外 Tools（+21） |
|---------------------------|--------------------------|
| describe_cluster_operation | create_cluster |
| get_cluster_info | update_broker_storage / type / count |
| get_global_info | update_cluster_configuration |
| describe_vpc_connection | update_monitoring / security |
| get_configuration_info | put_cluster_policy |
| list_tags_for_resource | associate / disassociate_scram_secret |
| list_topics | reboot_broker |
| describe_topic / partitions | create / update_configuration |
| get_cluster_telemetry | tag_resource / untag_resource |
| list_customer_iam_access | create / update / delete_topic |
| get_cluster_best_practices | create / delete_vpc_connection |
| | reject_client_vpc_connection |

### Serverless 集群功能限制

| 操作 | 支持 | 错误信息 |
|------|------|---------|
| get_cluster_info | ✅ | 部分 info_type 不支持（nodes, compatible_versions） |
| get_cluster_best_practices | ✅ | 需要指定 instance_type（适用于 Provisioned 规划） |
| get_cluster_telemetry | ❌ | "This operation cannot be performed on serverless clusters" |
| list_topics / create_topic | ❌ | "Topic APIs are not supported on serverless clusters" |
| list_nodes | ❌ | "This operation cannot be performed on serverless clusters" |
| tag_resource | ✅ | 正常工作 |
| client_vpc_connections | ❌ | 部分 Region 不支持 Serverless VPC 连接 |

## 踩坑记录

!!! warning "踩坑 1：create_cluster 的 kwargs 参数格式"
    MCP Server 的 `create_cluster` Tool 将可选参数通过 `kwargs` JSON string 传递。对于 Serverless 集群，`vpc_configs` 的嵌套结构（包含 PascalCase 的 `SubnetIds`、`SecurityGroupIds`）在 kwargs → dict → boto3 params 的转换链路中容易出错。

    **解决方案**：使用 AWS CLI 直接创建集群，MCP Server 负责后续管理和监控。实测发现的 bug，已提交 issue。

!!! warning "踩坑 2：MCP Generated 标签安全机制（文档未记录）"
    MCP Server 对部分写操作（如 Topic 管理）强制要求资源带有 `MCP Generated` 标签。这意味着只有通过 MCP Server 创建的资源才能被完全管理。对于已有集群，需要手动添加此标签。

    这是一个**安全设计**：防止 AI Agent 意外修改非 MCP 管理的集群。

!!! warning "踩坑 3：list_customer_iam_access 内部错误"
    调用此 Tool 返回 `AWSClientManager.get_client() missing 1 required positional argument: 'service_name'`。这是 MCP Server 的代码 bug，不影响核心功能使用。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| MSK Serverless Cluster | ~$0.75/hr | ~5 min | < $0.10 |
| MCP Server (本地运行) | 免费 | - | $0.00 |
| **合计** | | | **< $0.50** |

## 清理资源

```bash
# 删除 MSK 集群
aws kafka delete-cluster \
  --cluster-arn <your-cluster-arn> \
  --region us-east-1

# 确认删除状态
aws kafka describe-cluster-v2 \
  --cluster-arn <your-cluster-arn> \
  --region us-east-1 \
  --query 'ClusterInfo.State'
```

!!! danger "务必清理"
    MSK Serverless 按 cluster hour 计费，即使没有数据流入也会产生费用。Lab 完成后请立即删除集群。

## 结论与建议

### 适合场景

- **运维自动化**：将 MCP Server 集成到 IDE（Cursor、VS Code、Kiro），让 AI 助手直接管理 MSK 集群
- **集群规划**：`get_cluster_best_practices` 提供针对性的 sizing 建议
- **日常巡检**：Read-only 模式下安全地获取集群状态聚合视图
- **Provisioned 集群管理**：Topic 管理、配置变更、监控等全面支持

### 当前限制

1. Serverless 集群支持有限（无 Topic API、无 Telemetry）
2. `create_cluster` 的 Serverless 参数映射存在 bug
3. 部分 Tool 有代码级 bug（如 `list_customer_iam_access`）
4. 参数命名不完全统一

### 生产建议

- 生产环境**始终使用 Read-only 模式**（不加 `--allow-writes`），仅在维护窗口启用 Write 模式
- 利用 `MCP Generated` 标签机制，将 MCP 管理的资源和手动管理的资源隔离
- `get_cluster_best_practices` 可作为定期巡检工具，集成到运维流程中

## 参考链接

- [What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/07/mcp-server-amazon-msk/)
- [GitHub: awslabs/mcp](https://github.com/awslabs/mcp)
- [PyPI: awslabs.aws-msk-mcp-server](https://pypi.org/project/awslabs.aws-msk-mcp-server/)
- [Amazon MSK 开发者指南](https://docs.aws.amazon.com/msk/latest/developerguide/what-is-msk.html)
- [MSK Serverless](https://docs.aws.amazon.com/msk/latest/developerguide/serverless.html)
- [MSK Quota](https://docs.aws.amazon.com/msk/latest/developerguide/limits.html)
