---
tags:
  - Analytics
---

# Amazon Managed Service for Apache Flink 2.2 实战：首个大版本升级全面解析

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: $2.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-01

## 背景

2026 年 3 月 31 日，AWS 宣布 Amazon Managed Service for Apache Flink（MSF）支持 Apache Flink 2.2。这是该服务自发布以来的**首个大版本升级**（1.x → 2.x），带来了显著的 runtime 改进和多项 breaking changes。

对于已有 Flink 应用的用户来说，这不是简单的小版本更新——DataSet API、Scala API、Java 11 都被彻底移除，状态序列化也有兼容性风险。本文通过实际动手测试，验证新应用创建、版本升级、指标变化等关键场景，帮你在升级前做好充分准备。

## 前置条件

- AWS 账号（需要 `kinesisanalytics`、`iam`、`s3`、`logs` 权限）
- AWS CLI v2 已配置
- Docker（用于编译 Flink JAR）
- Maven 镜像：`maven:3.9-eclipse-temurin-17`

## 核心概念

### Flink 2.2 关键变化一览

| 类别 | 变化 | 影响 |
|------|------|------|
| **Runtime** | Java 17 默认，Java 11 移除 | 必须重新编译为 Java 17 |
| **Runtime** | Python 3.12 默认，Python 3.8 移除 | PyFlink 用户需升级 |
| **性能** | RocksDB 8.10.0 | State Backend I/O 性能提升 |
| **序列化** | Kryo 2.24 → 5.6，Map/List/Set 专用序列化器 | ⚠️ 状态兼容性风险 |
| **API 移除** | DataSet API 完全移除 | 必须迁移到 DataStream API |
| **API 移除** | Scala API 移除 | 使用 Java API（Scala 代码仍可调用） |
| **API 移除** | SourceFunction/SinkFunction 移除 | 使用 FLIP-27 Source + FLIP-143 Sink |
| **安全** | 只读根文件系统 | 仅 `/tmp` 可写 |
| **安全** | 非凭证 IMDS 调用被阻断 | 不能再用 EC2MetadataUtils 获取实例信息 |
| **指标** | `fullRestarts` 移除 | 改用 `numRestarts` |
| **指标** | `uptime`/`downtime` deprecated | 改用 `runningTime`/`restartingTime` |
| **SQL** | VARIANT 数据类型、Delta Join、ML_PREDICT | 新的流处理 SQL 能力 |

### 升级路径

AWS 提供三种升级路径，取决于你的应用兼容性：

```
Path 1: 兼容二进制 + 兼容状态 → 平滑升级（RUNNING → UPDATING → RUNNING）
Path 2: 二进制不兼容 → 升级失败，auto-rollback 自动回退
Path 3: 状态不兼容 → 升级看似成功但进入重启循环，需手动 rollback
```

!!! warning "升级是单向的"
    对于**运行中带状态**的应用，2.2 的状态无法带回 1.x。但在 READY 状态（无运行状态数据）的应用可以双向切换版本——这是我们实测发现的，官方文档未明确说明。

## 动手实践

### Step 1: 准备 IAM Role 和日志

```bash
# 创建信任策略
cat > /tmp/flink-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "kinesisanalytics.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# 创建 IAM Role
aws iam create-role \
  --role-name msf-flink-test-role \
  --assume-role-policy-document file:///tmp/flink-trust-policy.json \
  --region us-east-1

# 附加权限策略（CloudWatch Logs + S3 + Kinesis）
cat > /tmp/flink-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
        "logs:CreateLogStream"
      ],
      "Resource": "arn:aws:logs:us-east-1:*:log-group:/aws/kinesis-analytics/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name msf-flink-test-role \
  --policy-name flink-test-policy \
  --policy-document file:///tmp/flink-policy.json

# 创建 CloudWatch Log Group 和 Stream
aws logs create-log-group \
  --log-group-name /aws/kinesis-analytics/flink-test \
  --region us-east-1

aws logs create-log-stream \
  --log-group-name /aws/kinesis-analytics/flink-test \
  --log-stream-name flink-22-stream \
  --region us-east-1
```

### Step 2: 构建 Flink 2.2 应用 JAR

创建一个最小化的 Flink 2.2 DataStream + Table API 应用：

```bash
mkdir -p /tmp/flink22-app/src/main/java/com/example
cd /tmp/flink22-app
```

**pom.xml**（关键：Flink 版本 2.2.0，Java 17）：

```xml
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>flink22-test</artifactId>
    <version>1.0</version>
    <properties>
        <flink.version>2.2.0</flink.version>
        <maven.compiler.source>17</maven.compiler.source>
        <maven.compiler.target>17</maven.compiler.target>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.apache.flink</groupId>
            <artifactId>flink-streaming-java</artifactId>
            <version>${flink.version}</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>org.apache.flink</groupId>
            <artifactId>flink-clients</artifactId>
            <version>${flink.version}</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>org.apache.flink</groupId>
            <artifactId>flink-connector-datagen</artifactId>
            <version>${flink.version}</version>
        </dependency>
    </dependencies>
    <!-- 使用 maven-shade-plugin 打包 fat JAR -->
</project>
```

**FlinkTest.java**：

```java
package com.example;

import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.table.api.bridge.java.StreamTableEnvironment;

public class FlinkTest {
    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env =
            StreamExecutionEnvironment.getExecutionEnvironment();
        StreamTableEnvironment tableEnv =
            StreamTableEnvironment.create(env);

        // DataGen source → Print sink
        tableEnv.executeSql(
            "CREATE TABLE datagen_source (" +
            "  id BIGINT, data STRING" +
            ") WITH ('connector'='datagen', 'rows-per-second'='1')");

        tableEnv.executeSql(
            "CREATE TABLE print_sink (" +
            "  id BIGINT, data STRING" +
            ") WITH ('connector'='print')");

        tableEnv.executeSql(
            "INSERT INTO print_sink SELECT * FROM datagen_source");
    }
}
```

使用 Docker 编译（无需本地安装 Java/Maven）：

```bash
docker run --rm -v "$(pwd)":/app -w /app \
  maven:3.9-eclipse-temurin-17 mvn package -q -DskipTests
```

### Step 3: 上传 JAR 并创建 Flink 2.2 应用

```bash
# 创建 S3 bucket 并上传 JAR
aws s3 mb s3://msf-flink-test-$(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1

aws s3 cp target/flink22-test-1.0.jar \
  s3://msf-flink-test-$(aws sts get-caller-identity --query Account --output text)/ \
  --region us-east-1

# 创建 Flink 2.2 应用（注意 RuntimeEnvironment 为 FLINK-2_2）
aws kinesisanalyticsv2 create-application \
  --application-name flink-22-test \
  --runtime-environment FLINK-2_2 \
  --service-execution-role arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/msf-flink-test-role \
  --application-configuration '{
    "ApplicationCodeConfiguration": {
      "CodeContent": {
        "S3ContentLocation": {
          "BucketARN": "arn:aws:s3:::msf-flink-test-'$(aws sts get-caller-identity --query Account --output text)'",
          "FileKey": "flink22-test-1.0.jar"
        }
      },
      "CodeContentType": "ZIPFILE"
    }
  }' \
  --region us-east-1
```

### Step 4: 启动应用并验证

```bash
# 启动应用
aws kinesisanalyticsv2 start-application \
  --application-name flink-22-test \
  --region us-east-1

# 监控启动状态（约 60-90 秒）
watch -n 10 "aws kinesisanalyticsv2 describe-application \
  --application-name flink-22-test \
  --region us-east-1 \
  --query 'ApplicationDetail.{Status:ApplicationStatus,Runtime:RuntimeEnvironment}'"
```

### Step 5: 验证升级路径（1.20 → 2.2）

```bash
# 先创建一个 Flink 1.20 应用（需要对应的 1.20 JAR）
aws kinesisanalyticsv2 create-application \
  --application-name flink-120-test \
  --runtime-environment FLINK-1_20 \
  --service-execution-role arn:aws:iam::<ACCOUNT_ID>:role/msf-flink-test-role \
  --application-configuration '{...同上，JAR 用 1.20 版本编译...}' \
  --region us-east-1

# In-place 升级到 2.2
aws kinesisanalyticsv2 update-application \
  --application-name flink-120-test \
  --current-application-version-id 1 \
  --runtime-environment-update FLINK-2_2 \
  --application-configuration-update '{
    "ApplicationCodeConfigurationUpdate": {
      "CodeContentUpdate": {
        "S3ContentLocationUpdate": {
          "FileKeyUpdate": "flink22-test-1.0.jar"
        }
      }
    }
  }' \
  --region us-east-1
```

## 测试结果

### 创建与启动

| 测试场景 | 结果 | 耗时 | 备注 |
|---------|------|------|------|
| 创建 FLINK-2_2 STREAMING 应用 | ✅ 成功 | 即时 | RuntimeEnvironment=FLINK-2_2 |
| 创建 FLINK-2_2 INTERACTIVE 应用 | ❌ 失败 | - | Studio Notebook 不支持 2.2 |
| PLAINTEXT 代码方式 | ❌ 失败 | - | Flink 只接受 ZIPFILE |
| 启动 2.2 应用 | ✅ 成功 | ~88 秒 | READY → STARTING → RUNNING |

### 版本升级与降级

| 测试场景 | 结果 | 备注 |
|---------|------|------|
| 1.20 → 2.2 升级 (READY 状态) | ✅ 成功 | Version 1→2 |
| 2.2 → 1.20 降级 (READY 状态) | ✅ 成功 | Version 2→3 |

### 指标验证

| 指标名 | Flink 2.2 行为 | 官方文档 | 实测一致 |
|--------|--------------|---------|---------|
| `numRestarts` | ✅ 有数据 (0.0) | 替代 fullRestarts | ✅ |
| `fullRestarts` | ❌ 无数据 | 已移除 | ✅ |
| `uptime` | ⚠️ 仍有数据 (13424ms) | "deprecated, 即将移除" | ✅ |

### 1.20 vs 2.2 默认配置对比

| 配置项 | FLINK-1_20 | FLINK-2_2 |
|--------|-----------|-----------|
| Parallelism | 1 | 1 |
| AutoScalingEnabled | true | true |
| CheckpointInterval | 60000ms | 60000ms |
| MinPauseBetweenCheckpoints | 5000ms | 5000ms |
| CheckpointingEnabled | true | true |
| RollbackEnabled | false | false |

默认配置完全一致，说明 AWS 没有因版本升级改变默认行为。

## 踩坑记录

!!! warning "踩坑 1: Studio Notebook 不支持 Flink 2.2"
    尝试创建 `ApplicationMode: INTERACTIVE` + `RuntimeEnvironment: FLINK-2_2` 会报错：
    ```
    InvalidArgumentException: ApplicationMode 'INTERACTIVE' is not applicable
    to runtime environment : FLINK-2_2
    ```
    **影响**：无法使用 Zeppelin Notebook 交互式探索 Flink 2.2 的 SQL 新特性（如 VARIANT）。如果你依赖 Studio Notebook 做开发/测试，暂时只能停留在 1.x。
    （⚠️ 实测发现，官方文档未明确记录此限制）

!!! warning "踩坑 2: READY 状态应用可以双向切换版本"
    官方文档说 "Major version upgrade is uni-directional"，但我们实测发现：**未启动过的 READY 状态应用**可以从 2.2 降级回 1.20。单向限制仅适用于有运行状态数据的应用。
    （⚠️ 实测发现，文档表述可能造成误解）

!!! warning "踩坑 3: 只能用 ZIPFILE 方式提交代码"
    即使是简单的 SQL 应用，Flink 也不接受 PLAINTEXT 方式的代码提交。必须编译成 JAR 并通过 S3 上传。这增加了入门门槛。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| MSF KPU (flink-22-test-streaming) | $0.11/KPU/hr | 1 KPU × 0.05 hr | $0.006 |
| S3 存储 (JAR 文件) | $0.023/GB/月 | ~50KB | $0.00 |
| CloudWatch Logs | $0.50/GB | 微量 | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
# 1. 停止运行中的应用
aws kinesisanalyticsv2 stop-application \
  --application-name flink-22-test --force \
  --region us-east-1

# 2. 删除 Flink 应用
aws kinesisanalyticsv2 delete-application \
  --application-name flink-22-test \
  --create-timestamp $(aws kinesisanalyticsv2 describe-application \
    --application-name flink-22-test \
    --query 'ApplicationDetail.CreateTimestamp' --output text \
    --region us-east-1) \
  --region us-east-1

aws kinesisanalyticsv2 delete-application \
  --application-name flink-120-test \
  --create-timestamp $(aws kinesisanalyticsv2 describe-application \
    --application-name flink-120-test \
    --query 'ApplicationDetail.CreateTimestamp' --output text \
    --region us-east-1) \
  --region us-east-1

# 3. 清理 S3
aws s3 rm s3://msf-flink-test-<ACCOUNT_ID>/ --recursive --region us-east-1
aws s3 rb s3://msf-flink-test-<ACCOUNT_ID> --region us-east-1

# 4. 删除 CloudWatch 日志
aws logs delete-log-group \
  --log-group-name /aws/kinesis-analytics/flink-test \
  --region us-east-1

# 5. 删除 IAM Role
aws iam delete-role-policy \
  --role-name msf-flink-test-role \
  --policy-name flink-test-policy

aws iam delete-role --role-name msf-flink-test-role
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 READY 状态的应用不产生 KPU 费用，但建议及时删除以保持账号整洁。

## 结论与建议

### 适合谁升级？

- ✅ **新应用**：强烈建议直接使用 Flink 2.2，享受 Java 17、新 SQL 特性和性能改进
- ✅ **无状态应用 / 状态用 Avro/Protobuf**：升级风险低，平滑升级
- ⚠️ **有状态 + Kryo 序列化 / POJO 含集合类型**：需仔细评估状态兼容性
- ❌ **依赖 DataSet API / Scala API / SourceFunction**：必须先迁移代码

### 升级建议

1. **先在非生产环境测试**——升级是不可逆的（对于带状态的应用）
2. **启用 auto-rollback**——升级失败时自动回退
3. **升级前创建 snapshot**——保留回退点
4. **注意 Studio Notebook 不支持 2.2**——如果你的开发流程依赖它

### 关于 SQL 新特性

Flink 2.2 引入了 VARIANT 数据类型、Delta Join、ML_PREDICT 等强大的 SQL 能力，但由于 Studio Notebook 暂不支持 2.2，交互式探索这些特性需要通过编译 JAR 的方式，体验上不太方便。期待 AWS 后续更新 Studio 对 2.2 的支持。

## 参考链接

- [Amazon Managed Service for Apache Flink 2.2 文档](https://docs.aws.amazon.com/managed-flink/latest/java/flink-2-2.html)
- [升级到 Flink 2.2 完整指南](https://docs.aws.amazon.com/managed-flink/latest/java/flink-2-2-upgrade-guide.html)
- [状态兼容性指南](https://docs.aws.amazon.com/managed-flink/latest/java/state-compatibility.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/amazon-managed-service-flink-2-2/)
- [Apache Flink 2.0 Release Notes](https://nightlies.apache.org/flink/flink-docs-stable/release-notes/flink-2.0/)
