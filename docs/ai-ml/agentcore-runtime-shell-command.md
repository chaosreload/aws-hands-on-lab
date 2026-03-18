# Bedrock AgentCore Runtime：InvokeAgentRuntimeCommand 实测

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（AgentCore Runtime 按使用计费）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-18

## 背景

AI Agent 的工作流经常需要在 LLM 推理之外执行确定性操作 — 跑测试、git 提交、安装依赖、编译代码。之前你必须在容器里自建一套命令执行逻辑：区分 agent 调用和 shell 命令、spawn 子进程、捕获 stdout/stderr、处理超时……全是重复劳动。

2026 年 3 月 17 日，AWS 发布了 `InvokeAgentRuntimeCommand` API — 一个平台级的命令执行接口。命令在 agent 同一个容器内运行，共享文件系统和环境，输出通过 HTTP/2 实时流回。

**一句话**：LLM 做推理（`InvokeAgentRuntime`），平台跑命令（`InvokeAgentRuntimeCommand`）。各司其职。

## 前置条件

- AWS 账号（需要 `bedrock-agentcore:*` 权限）
- AWS CLI v2 + boto3 ≥ 1.42.70（包含新 API）
- Python 3.10+
- `pip install bedrock-agentcore-starter-toolkit`
- `uv`（[安装指南](https://docs.astral.sh/uv/getting-started/installation/)）

## 核心概念

### 两个 API，一个 Session

```
                    同一 AgentCore Runtime Session
                    同一容器 / 文件系统 / 环境
                    ┌─────────────────────────────┐
InvokeAgentRuntime ─────→│  LLM 推理 + 工具调用        │
                         │                             │
InvokeAgentRuntimeCommand─→│  Shell 命令执行（确定性）    │
                         └─────────────────────────────┘
```

| 维度 | InvokeAgentRuntime | InvokeAgentRuntimeCommand |
|------|-------------------|---------------------------|
| 用途 | 推理、分析、创造性工作 | 确定性操作（测试、git、编译） |
| 输入 | prompt（自然语言） | command（shell 命令） |
| 执行 | LLM 驱动 | 直接 bash 执行 |
| 输出 | agent 响应流 | stdout/stderr 实时流 |
| 可预测性 | 不确定 | 同命令同结果 |

### 事件流结构

响应是 HTTP/2 event stream，包含三种事件：

| 事件 | 时机 | 内容 |
|------|------|------|
| `contentStart` | 首个事件 | 确认命令已开始 |
| `contentDelta` | 执行过程中 | `stdout` 和/或 `stderr` 输出 |
| `contentStop` | 最后一个事件 | `exitCode` + `status`（COMPLETED / TIMED_OUT） |

### 关键设计选择

- **一次性执行**：每条命令 spawn 新 bash 进程，无持久 shell session
- **跨命令无状态**：环境变量、shell history 不跨命令保留
- **文件系统持久**：同一 session 内，文件跨命令共享
- **不阻塞 agent**：命令执行和 agent 推理可以并发

### API 限制

| 参数 | 限制 | 说明 |
|------|------|------|
| command 大小 | 1 byte ~ 64 KB | 超过会返回 ValidationException |
| timeout | 1 ~ 3600 秒 | 最大 1 小时 |
| session ID | ≥ 33 字符 | 建议使用 UUID |
| API 速率 | 25 TPS | 超过会返回 ThrottlingException，需实现指数退避 |

## 动手实践

### Step 1: 创建 AgentCore 项目

```bash
pip install bedrock-agentcore-starter-toolkit
curl -LsSf https://astral.sh/uv/install.sh | sh

agentcore create -p shellLabAgent -t basic \
  --agent-framework Strands \
  --model-provider Bedrock \
  --non-interactive --no-venv
```

### Step 2: 部署到 AgentCore Runtime

```bash
cd shellLabAgent
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1

agentcore deploy
```

部署成功后，记录输出的 Agent ARN：

```
✅ Agent created/updated: arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/shellLabAgent-XXXXXXXXXX
```

!!! tip "部署方式"
    Starter toolkit 使用 **Direct Code Deploy** — 代码打包上传到 S3，无需 Docker。底层自动使用 arm64 运行时。

### Step 3: 执行 Shell 命令

```python
import boto3
import json
import time

session = boto3.Session(profile_name='your-profile', region_name='us-east-1')
client = session.client('bedrock-agentcore')

AGENT_ARN = 'arn:aws:bedrock-agentcore:us-east-1:ACCOUNT:runtime/YOUR-AGENT-ID'
SESSION_ID = f'lab-{int(time.time())}-000000000000000000000'  # ≥33 chars

def run_command(command, timeout=60):
    """执行命令并打印流式输出。"""
    response = client.invoke_agent_runtime_command(
        agentRuntimeArn=AGENT_ARN,
        runtimeSessionId=SESSION_ID,
        body={'command': command, 'timeout': timeout}
    )
    
    exit_code = None
    for event in response['stream']:
        chunk = event.get('chunk', event)
        if 'contentDelta' in chunk:
            delta = chunk['contentDelta']
            if 'stdout' in delta:
                print(delta['stdout'], end='')
            if 'stderr' in delta:
                print(f"[STDERR] {delta['stderr']}", end='')
        elif 'contentStop' in chunk:
            exit_code = chunk['contentStop'].get('exitCode')
            status = chunk['contentStop'].get('status')
    
    return exit_code, status

# 基本执行
exit_code, status = run_command('echo "Hello from AgentCore!"')
print(f"\n退出码: {exit_code}, 状态: {status}")
```

输出：

```
Hello from AgentCore!
退出码: 0, 状态: COMPLETED
```

### Step 4: 验证关键行为

#### 4.1 非零退出码

```python
exit_code, status = run_command('/bin/bash -c "exit 42"')
# exitCode=42, status=COMPLETED
```

#### 4.2 stdout 与 stderr 分离

```python
run_command('/bin/bash -c "echo normal-output && echo error-msg >&2"')
# [STDOUT] normal-output
# [STDERR] error-msg
```

#### 4.3 实时流式输出

```python
import time
start = time.time()
run_command('/bin/bash -c "for i in 1 2 3; do echo chunk-$i; sleep 1; done"', timeout=30)
print(f"总耗时: {time.time() - start:.1f}s")
# chunk-1  (t=0s)
# chunk-2  (t=1s)
# chunk-3  (t=2s)
# 总耗时: 3.3s
```

!!! success "真正的实时流"
    每行输出即时通过 `contentDelta` 事件推送，不是等命令结束才返回。3 个 sleep 1s 的 echo 产生 3 个独立 delta 事件。

#### 4.4 超时控制

```python
exit_code, status = run_command('sleep 300', timeout=5)
# exitCode=-1, status=TIMED_OUT
```

#### 4.5 文件系统跨命令共享

```python
# 命令 1: 写文件
run_command('/bin/bash -c "echo test-data > /tmp/myfile.txt"')

# 命令 2: 读文件（同一 session）
run_command('cat /tmp/myfile.txt')
# test-data  ✅ 文件持久化
```

#### 4.6 环境变量跨命令不共享

```python
# 命令 1: 设置变量
run_command('/bin/bash -c "export MY_VAR=hello && echo MY_VAR=$MY_VAR"')
# MY_VAR=hello  ✅

# 命令 2: 读取变量
run_command('/bin/bash -c "echo MY_VAR=${MY_VAR:-EMPTY}"')
# MY_VAR=EMPTY  ✅ 无状态，符合设计
```

!!! info "需要跨命令状态？"
    把状态编码进命令本身：`cd /workspace && export NODE_ENV=test && npm test`

#### 4.7 大输出无截断

```python
run_command('/bin/bash -c "seq 1 100000"', timeout=60)
# 100,000 行，588,895 字符，全部完整返回
# 100,000 个 contentDelta 事件，每行一个
```

## 实测数据

| 指标 | 数值 | 备注 |
|------|------|------|
| 冷启动延迟 | ~2s | Session 首次命令（microVM 启动） |
| 后续命令延迟 | ~0.3s | 同 session 内 |
| 流式延迟 | < 50ms | 从输出产生到客户端收到 |
| 超时精度 | ±0.3s | timeout=5 → 实际 5.26s |
| 最大输出验证 | 100k 行 / 589KB | 无截断，每行一个 delta |
| contentDelta 粒度 | 每行一个事件 | stdout 和 stderr 各自独立事件 |
| command 大小上限 | 64 KB | 官方文档限制（ValidationException） |
| timeout 上限 | 3600s（1 小时） | 官方文档限制 |
| API 速率限制 | 25 TPS | 超过返回 ThrottlingException |

## 踩坑记录

!!! warning "Agent 必须实现标准端点契约"
    AgentCore Runtime 要求 agent 容器实现 `/invocations` POST 和 `/ping` GET 两个端点（[服务契约文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html)）。你可以用任何 Web 框架（FastAPI、Flask 等）实现，也可以用官方 `bedrock-agentcore` SDK 的 `BedrockAgentCoreApp`（自动处理端点注册）。如果使用 Direct Code Deploy，则需要 `@app.entrypoint` 注解或手动实现端点。

!!! warning "仅支持 arm64 架构"
    AgentCore Runtime microVM 运行在 arm64（Graviton）上。如果用容器部署，Docker 镜像必须是 `linux/arm64`。使用 Direct Code Deploy 则由平台自动处理。

!!! warning "默认环境无开发工具"
    microVM 不包含 `git`、`npm`、`python`、`which` 等工具。需要在容器镜像的 Dockerfile 中安装，或在代码部署的依赖中包含。

!!! warning "新 API 需要最新 SDK"
    AWS CLI v2.33 尚未包含 `invoke-agent-runtime-command` 子命令。需要通过 boto3 ≥ 1.42.70 直接调用 Python API。

!!! warning "注意 ARN 和 ECR URI 中的变量替换"
    使用 AWS CLI 或脚本部署时，确保 IAM Role ARN 和 ECR URI 中的 Account ID、Region 正确填写。如果使用 shell 变量（如 `$ACCOUNT_ID`、`$REGION`），务必确认变量已正确导出，否则会得到 `AccessDeniedException`（ARN 格式错误导致鉴权失败）。本 Lab 支持 [14 个 Region](https://aws.amazon.com/about-aws/whats-new/2026/03/bedrock-agentcore-runtime-shell-command/)，包括 Singapore（ap-southeast-1）。

!!! tip "response['stream'] 不是 response['body']"
    boto3 返回的事件流在 `response['stream']` 中（EventStream 类型），不是 `response['body']`。`contentDelta` 的输出字段是 `stdout` 和 `stderr`（不是 `text`）。

## 费用明细

| 资源 | 说明 | 费用 |
|------|------|------|
| AgentCore Runtime | 按 session 活跃时间计费 | < $0.50 |
| S3（代码包） | 部署包存储 ~52MB | < $0.01 |
| CloudWatch Logs | 可观测性日志 | < $0.05 |
| ECR（如用容器部署） | 镜像存储 | < $0.10 |
| **合计** | | **< $1.00** |

## 清理资源

```bash
# 方法 1: 使用 starter toolkit
cd shellLabAgent
agentcore destroy

# 方法 2: 手动清理
export AWS_PROFILE=your-profile
export REGION=us-east-1
export AGENT_ID=shellLabAgent-XXXXXXXXXX

# 删除 endpoint
aws bedrock-agentcore-control delete-agent-runtime-endpoint \
  --agent-runtime-id $AGENT_ID \
  --endpoint-name DEFAULT \
  --region $REGION

# 删除 runtime
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id $AGENT_ID \
  --region $REGION

# 删除 S3 部署包
aws s3 rm s3://bedrock-agentcore-codebuild-sources-ACCOUNT-$REGION/shellLabAgent/ --recursive

# 删除 ECR repo（如有）
aws ecr delete-repository --repository-name agentcore-test-agent --force --region $REGION

# 删除 IAM Role（如手动创建）
aws iam detach-role-policy --role-name AgentCoreTestRole --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess
aws iam detach-role-policy --role-name AgentCoreTestRole --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam delete-role --role-name AgentCoreTestRole
```

!!! danger "务必清理"
    AgentCore Runtime 按 session 活跃时间计费。Lab 完成后停止 session 并删除 Runtime，避免持续产生费用。

## 结论与建议

### 适合场景

- **Coding Agent**：agent 写代码 → 平台跑测试 → agent 看结果 → 迭代
- **CI/CD 集成**：agent 决策 + 平台执行 git/build/deploy
- **环境引导**：agent 工作前，先用命令装依赖、拉代码
- **数据管道**：agent 分析 + 命令执行 ETL

### 使用建议

1. **推理用 InvokeAgentRuntime，确定性操作用 InvokeAgentRuntimeCommand** — 不要让 LLM 做它不擅长的事
2. **利用流式输出做早期失败检测** — 测试失败不用等全部跑完，前几秒就能判断
3. **状态编码进命令** — 跨命令无状态，用 `cd /x && export Y=Z && cmd` 模式
4. **容器镜像装好工具** — 默认环境很精简，生产环境要在 Dockerfile 里装好所有依赖
5. **设合理超时** — timeout 范围 1~3600 秒（最大 1 小时），编译或大规模测试按需调大

### 与现有方案对比

| 方案 | 命令执行 | 隔离性 | 流式输出 | 与 Agent 共享环境 |
|------|---------|--------|---------|-----------------|
| 自建（容器内 subprocess） | ✅ | ❌ 自管 | ❌ 需实现 | ✅ |
| Lambda | ✅ | ✅ | ❌ | ❌ 独立环境 |
| ECS Run Task | ✅ | ✅ | ❌ | ❌ 独立容器 |
| **InvokeAgentRuntimeCommand** | ✅ | ✅ microVM | ✅ HTTP/2 | ✅ 同容器 |

InvokeAgentRuntimeCommand 的独特价值：**与 agent 共享同一容器环境 + 平台级实时流式输出**，无需自建。

## 参考链接

- [Execute shell commands in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html)
- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2026/03/bedrock-agentcore-runtime-shell-command/)
- [AgentCore Starter Toolkit](https://pypi.org/project/bedrock-agentcore-starter-toolkit/)
- [AgentCore 开发者指南](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)
