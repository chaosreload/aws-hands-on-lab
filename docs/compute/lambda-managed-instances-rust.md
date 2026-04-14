---
description: "Test AWS Lambda Managed Instances with Rust on Graviton4 — benchmark parallel request handling and EC2-backed Lambda performance."
tags:
  - Compute
---
# Lambda Managed Instances + Rust：实测 Graviton4 性能对比

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: ~$1-2（3x m8g.xlarge 运行约 10 分钟）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-19

## 背景

Lambda Managed Instances 是 AWS 在 2025 年推出的 Lambda 新计算模式 — 函数跑在你账号里的 EC2 实例上，但 AWS 管理实例生命周期、路由、负载均衡和扩缩容。2026 年 3 月 13 日，这个模式新增了 Rust 支持，开启了 **Rust 多请求并行处理** 能力。

**核心卖点**：Lambda 的开发体验 + EC2 的硬件选择和定价优势 + Rust 的极致性能。

## 前置条件

- AWS 账号（需要 Lambda、EC2、IAM 权限）
- Rust 1.84.0+（`rustup target add aarch64-unknown-linux-gnu`）
- `gcc-aarch64-linux-gnu`（交叉编译）

## 核心概念

### Lambda Managed Instances vs 标准 Lambda

| 维度 | 标准 Lambda | Managed Instances |
|------|-----------|-------------------|
| 并发模型 | 1 请求 / 执行环境 | **多请求并行** / 执行环境 |
| 底层硬件 | 共享 Lambda 集群 | **你的 EC2 实例**（Graviton4 等） |
| 隔离 | Firecracker microVM | EC2 Nitro 容器 |
| 定价 | 按请求时长 | EC2 价格 + 15% 管理费 |
| 扩缩容 | 冷启动触发，可缩到 0 | CPU 利用率触发，最小 3 实例 |
| 冷启动 | 有 | **无**（实例预热） |

### 架构

```
Capacity Provider
├── m8g.xlarge (AZ-a)
│   └── 执行环境
│       ├── Worker 1 → request A
│       ├── Worker 2 → request B
│       └── Worker N → request N (默认 8/vCPU)
├── m8g.xlarge (AZ-b)
└── m8g.xlarge (AZ-c)
```

### Rust 并行模式

```rust
// Cargo.toml
lambda_runtime = { version = "1", features = ["concurrency-tokio"] }

// main.rs - 使用 run_concurrent 替代 run
run_concurrent(service_fn(handler)).await
```

- 默认 8 并发 / vCPU，可配置 `PerExecutionEnvironmentMaxConcurrency`
- Handler 必须 `Clone + Send`（线程安全）
- 共享状态用 `Arc`，AWS SDK 客户端可直接 clone

## 动手实践

### Step 1: 创建 Rust Lambda 项目

```bash
cargo init --name rust-mi-lab
```

`Cargo.toml`:

```toml
[dependencies]
lambda_runtime = { version = "1", features = ["concurrency-tokio"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["full"] }
tracing = "0.1"
tracing-subscriber = "0.3"
```

`src/main.rs`:

```rust
use lambda_runtime::{run_concurrent, service_fn, Error, LambdaEvent};
use serde::{Deserialize, Serialize};
use std::time::Instant;

#[derive(Deserialize)]
struct Request {
    #[serde(default)]
    action: String,
    #[serde(default)]
    sleep_ms: u64,
}

#[derive(Serialize)]
struct Response {
    message: String,
    request_id: String,
    elapsed_ms: u128,
}

async fn handler(event: LambdaEvent<Request>) -> Result<Response, Error> {
    let start = Instant::now();
    let message = match event.payload.action.as_str() {
        "sleep" => {
            tokio::time::sleep(std::time::Duration::from_millis(
                event.payload.sleep_ms.max(1)
            )).await;
            format!("Slept for {}ms", event.payload.sleep_ms)
        }
        "cpu" => {
            let result = fib(35);
            format!("fib(35) = {}", result)
        }
        _ => "Hello from Rust MI!".into(),
    };
    Ok(Response {
        message,
        request_id: event.context.request_id,
        elapsed_ms: start.elapsed().as_millis(),
    })
}

fn fib(n: u64) -> u64 {
    if n <= 1 { return n; }
    fib(n - 1) + fib(n - 2)
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    tracing_subscriber::fmt().json().init();
    run_concurrent(service_fn(handler)).await
}
```

### Step 2: 交叉编译为 arm64

```bash
rustup target add aarch64-unknown-linux-gnu

# .cargo/config.toml
mkdir -p .cargo && cat > .cargo/config.toml << 'EOF'
[target.aarch64-unknown-linux-gnu]
linker = "aarch64-linux-gnu-gcc"
EOF

cargo build --release --target aarch64-unknown-linux-gnu
cp target/aarch64-unknown-linux-gnu/release/rust-mi-lab bootstrap
zip lambda.zip bootstrap
```

二进制仅 **2.7MB**（压缩后 984KB）。

### Step 3: 创建 Capacity Provider

```bash
# 创建 Operator IAM Role
aws iam create-role --role-name LambdaMIOperatorRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 创建 Capacity Provider
aws lambda create-capacity-provider \
  --capacity-provider-name my-rust-cp \
  --vpc-config 'SubnetIds=subnet-xxx,subnet-yyy,subnet-zzz,SecurityGroupIds=sg-xxx' \
  --permissions-config 'CapacityProviderOperatorRoleArn=arn:aws:iam::ACCOUNT:role/LambdaMIOperatorRole' \
  --instance-requirements 'Architectures=arm64' \
  --capacity-provider-scaling-config 'ScalingMode=Auto'
```

### Step 4: 创建 Lambda 函数（关联 Capacity Provider）

```bash
aws lambda create-function \
  --function-name rust-mi-lab \
  --runtime provided.al2023 \
  --architectures arm64 \
  --handler bootstrap \
  --role 'arn:aws:iam::ACCOUNT:role/LambdaMIExecRole' \
  --zip-file fileb://lambda.zip \
  --timeout 30 \
  --memory-size 2048 \
  --capacity-provider-config '{
    "LambdaManagedInstancesCapacityProviderConfig": {
      "CapacityProviderArn": "arn:aws:lambda:us-east-1:ACCOUNT:capacity-provider:my-rust-cp",
      "PerExecutionEnvironmentMaxConcurrency": 16
    }
  }'
```

!!! warning "内存最小 2048MB"
    Managed Instance 函数的 memory-size 最小 2048MB，不能像标准 Lambda 那样设 128MB。

### Step 5: 发布版本并等待就绪

```bash
aws lambda publish-version --function-name rust-mi-lab --description 'v1'
# 等待约 2 分钟，EC2 实例启动 + 执行环境初始化
```

发布后，AWS 自动启动 **3 个 EC2 实例**（跨 AZ），我们的测试中自动选择了 `m8g.xlarge`（Graviton4, 4 vCPU, 16GB）。

### Step 6: 调用测试

```bash
aws lambda invoke --function-name rust-mi-lab --qualifier 1 \
  --payload '{"action": "cpu"}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

## 实测数据

### 性能对比：Standard Lambda vs Managed Instance

| 测试 | Standard Lambda (256MB) | MI (m8g.xlarge) | 差距 |
|------|------------------------|-----------------|------|
| fib(35) | 197ms | **17ms** | **11.6x 更快** |
| hello (no-op) | < 1ms | < 1ms | 相当 |
| sleep(100ms) | 101ms | 101ms | 相当（IO bound） |

!!! success "CPU 密集型任务差距显著"
    Standard Lambda 256MB ≈ 0.17 vCPU。Managed Instance m8g.xlarge = 4 vCPU（Graviton4）。
    CPU 密集型任务差距 11.6x，这是硬件能力的直接体现。

### 并发处理

| 测试 | 描述 | 每请求耗时 | 总 wall time |
|------|------|-----------|-------------|
| 10x sleep(200ms) | IO 并发 | ~201ms | 1.9s |
| 5x fib(35) | CPU 并发 | ~17ms | 1.4s |

### 基础设施

| 指标 | 数值 |
|------|------|
| 自动选择实例 | m8g.xlarge (Graviton4) |
| 实例数量 | 3（跨 AZ） |
| 版本发布到可用 | ~2 分钟 |
| 并发度配置 | 16 / 执行环境 |
| 二进制大小 | 2.7MB (984KB zipped) |

## 踩坑记录

!!! warning "必须在 create-function 时指定 Capacity Provider"
    不能先创建标准 Lambda 再 update 为 Managed Instance。必须在 `create-function` 时通过 `--capacity-provider-config` 指定。

!!! warning "Memory 最小 2048MB"
    标准 Lambda 最小 128MB，Managed Instance 最小 2048MB。

!!! warning "$LATEST 不可调用"
    MI 函数的 `$LATEST` 状态为 `ActiveNonInvocable`。必须 `publish-version` 创建版本号后才能调用。

!!! warning "3 个 EC2 实例持续运行"
    发布版本后，默认启动 3 个 EC2 实例（AZ 冗余），即使没有流量也不会缩到 0。这是成本需要注意的地方。

!!! tip "交叉编译注意"
    macOS/x86 开发机交叉编译到 arm64 需要 `aarch64-linux-gnu-gcc` linker。在 `.cargo/config.toml` 中配置。

## 费用明细

| 资源 | 规格 | 费用 |
|------|------|------|
| 3x m8g.xlarge | ~10 分钟运行 | ~$0.15 |
| 15% 管理费 | 在 EC2 价格之上 | ~$0.02 |
| Lambda 调用 | 测试请求 | < $0.01 |
| **合计** | | **~$0.20** |

!!! info "生产环境定价优势"
    EC2 Savings Plans 和 Reserved Instances 可以应用到 Managed Instance 的 EC2 部分（不含 15% 管理费），对稳定负载有显著成本优势。

## 清理资源

```bash
# 删除函数（会释放版本与 capacity provider 的关联）
aws lambda delete-function --function-name rust-mi-lab

# 等待约 30 秒后删除 capacity provider（触发 EC2 实例终止）
sleep 30
aws lambda delete-capacity-provider --capacity-provider-name my-rust-cp

# 删除 IAM Roles
aws iam detach-role-policy --role-name LambdaMIOperatorRole --policy-arn ...
aws iam delete-role --role-name LambdaMIOperatorRole
aws iam delete-role --role-name LambdaMIExecRole
```

!!! danger "务必删除 Capacity Provider"
    Capacity Provider 不删除 = 3 个 EC2 实例持续运行计费！

## 结论与建议

### 适合场景

- **高吞吐 API**：Rust + 多请求并行 + Graviton4，极致性价比
- **稳定负载**：可用 Savings Plans / RI 降低成本
- **CPU 密集型**：视频编解码、图像处理、ML 推理
- **低延迟要求**：无冷启动

### 不适合场景

- **突发/稀疏流量**：不缩到 0 = 空闲也付费
- **简单函数**：如果 256MB Lambda 够用，没必要上 MI
- **快速原型**：标准 Lambda 上手更快

### 建议

1. **让 AWS 选实例类型** — 除非有特殊需求，`Architectures=arm64` 即可
2. **用 Rust 的多并发** — `concurrency-tokio` feature + `run_concurrent()`，这是 MI + Rust 的核心价值
3. **共享状态用 Arc** — /tmp 目录跨并发共享，注意文件名冲突
4. **监控 EC2 实例** — 通过 `aws:lambda:capacity-provider` tag 识别

## 参考链接

- [Lambda Managed Instances Rust 文档](https://docs.aws.amazon.com/lambda/latest/dg/lambda-managed-instances-rust.html)
- [Lambda Managed Instances 概述](https://docs.aws.amazon.com/lambda/latest/dg/lambda-managed-instances.html)
- [Capacity Providers](https://docs.aws.amazon.com/lambda/latest/dg/lambda-managed-instances-capacity-providers.html)
- [aws-lambda-rust-runtime v1.1.1](https://github.com/aws/aws-lambda-rust-runtime/releases/tag/lambda_runtime-v1.1.1)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-lambda-managed-instances-rust/)
