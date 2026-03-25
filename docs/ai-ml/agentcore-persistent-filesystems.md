# Amazon Bedrock AgentCore Runtime Session Storage 实测：跨 Stop/Resume 的持久化文件系统

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: < $0.10（含清理）
    - **Region**: us-west-2
    - **最后验证**: 2026-03-25

## 背景

现代 AI Agent 不仅仅是聊天 —— 它们需要写代码、安装依赖包、生成制品、管理状态。然而 AgentCore Runtime 的计算环境默认是临时的：每个 session 启动时获得干净的文件系统，一旦 stop 或终止，所有数据都会丢失。

这意味着如果你的 Coding Agent 在上一轮会话中安装了 `node_modules`、生成了 build 产物、甚至执行了 `git clone`，下次 resume 时这些全都需要重新来过。

**AgentCore Runtime Session Storage（Preview）** 解决了这个问题：一个完全由服务管理的持久化文件系统，透明地在 stop/resume 间保持文件状态。本文通过 7 个实测场景，验证这个新功能的行为、性能和边界。

## 前置条件

- AWS 账号，具备 IAM、S3、Bedrock AgentCore 权限
- AWS CLI v2 已配置
- Python 3.10+ 和 boto3（需要最新版本以支持 `filesystemConfigurations` 参数）
- uv（Python 包管理器，用于构建 arm64 部署包）

## 核心概念

### Session Storage 工作原理

```
首次调用 → 空目录 → Agent 写文件 → 异步复制到持久存储
                                     ↓
                              Session Stop → Flush 到持久存储
                                     ↓
                              Resume → 新计算实例 → 恢复文件系统状态
```

**关键特性**：

| 特性 | 说明 |
|------|------|
| 配置方式 | `filesystemConfigurations` 参数中设置 `sessionStorage.mountPath` |
| 存储容量 | 1 GB/session（不可调整） |
| 文件数上限 | 约 100,000-200,000 个（metadata 上限 ~50MB） |
| 数据隔离 | 严格按 session 隔离，不同 session 间不可互访 |
| 数据生命周期 | 14 天不活跃自动清理；runtime 版本更新重置 |
| 文件系统类型 | 标准 Linux 文件系统（实测为本地 NFS 挂载） |
| 支持的操作 | 文件/目录/symlinks, chmod, stat, readdir 等标准 POSIX 操作 |
| 不支持 | hard links, device files, FIFOs, UNIX sockets, xattr, fallocate |

### 与之前的对比

| | 之前（无 Session Storage） | 现在（有 Session Storage） |
|---|---|---|
| Stop 后文件 | ❌ 全部丢失 | ✅ 持久化保留 |
| Resume 行为 | 全新干净环境 | 恢复到停止时的状态 |
| 依赖安装 | 每次重新安装 | 安装一次，后续复用 |
| 开发者负担 | 需自己实现 checkpoint 逻辑 | 零代码改动，透明持久化 |

## 动手实践

### Step 1: 创建 IAM Role

```bash
# 创建信任策略
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

# 创建角色
aws iam create-role \
  --role-name AgentCoreRuntimeRole-persistent-fs \
  --assume-role-policy-document file:///tmp/trust-policy.json

# 附加权限策略（Bedrock 模型调用 + S3 代码包 + CloudWatch 日志）
cat > /tmp/runtime-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "arn:aws:bedrock:us-west-2::foundation-model/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::bedrock-agentcore-code-YOUR_ACCOUNT_ID-us-west-2",
        "arn:aws:s3:::bedrock-agentcore-code-YOUR_ACCOUNT_ID-us-west-2/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-west-2:YOUR_ACCOUNT_ID:*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name AgentCoreRuntimeRole-persistent-fs \
  --policy-name AgentCoreRuntimePolicy \
  --policy-document file:///tmp/runtime-policy.json
```

### Step 2: 准备 Agent 代码

创建一个简单的命令执行 Agent，用于测试文件系统操作：

```python
# main.py
import os
import json
import subprocess
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

@app.entrypoint
def handle_request(payload):
    prompt = payload.get("prompt", "")
    results = []
    
    if "SHELL:" in prompt:
        cmd = prompt.split("SHELL:")[1].strip()
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        results.append(f"stdout: {result.stdout}")
        if result.stderr:
            results.append(f"stderr: {result.stderr}")
        results.append(f"returncode: {result.returncode}")
    else:
        results.append(f"Echo: {prompt}")
    
    return {"response": "\n".join(results)}

if __name__ == "__main__":
    app.run()
```

### Step 3: 构建部署包并上传

```bash
# 安装 uv（如果未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 初始化项目
uv init agentcore-fs-test --python 3.13
cd agentcore-fs-test

# 安装依赖（arm64 架构 — AgentCore Runtime 仅支持 arm64）
uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --target=deployment_package \
  --only-binary=:all: \
  bedrock-agentcore

# 打包
cd deployment_package && zip -r ../deployment_package.zip . -q
cd .. && zip deployment_package.zip main.py

# 创建 S3 桶并上传
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://bedrock-agentcore-code-${ACCOUNT_ID}-us-west-2 --region us-west-2
aws s3 cp deployment_package.zip \
  s3://bedrock-agentcore-code-${ACCOUNT_ID}-us-west-2/persistent-fs-test/deployment_package.zip
```

### Step 4: 创建带 Session Storage 的 Agent Runtime

!!! warning "CLI 不支持 filesystemConfigurations"
    截至 AWS CLI v2.34.14，`create-agent-runtime` 命令尚不支持 `--filesystem-configurations` 参数。必须使用 boto3 SDK。

```python
import boto3
import json

client = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
ACCOUNT_ID = '595842667825'  # 替换为你的账号 ID

response = client.create_agent_runtime(
    agentRuntimeName='persistentFsTestAgent',  # 仅允许 [a-zA-Z][a-zA-Z0-9_]{0,47}
    agentRuntimeArtifact={
        'codeConfiguration': {
            'code': {
                's3': {
                    'bucket': f'bedrock-agentcore-code-{ACCOUNT_ID}-us-west-2',
                    'prefix': 'persistent-fs-test/deployment_package.zip'
                }
            },
            'runtime': 'PYTHON_3_13',
            'entryPoint': ['main.py']
        }
    },
    networkConfiguration={'networkMode': 'PUBLIC'},
    roleArn=f'arn:aws:iam::{ACCOUNT_ID}:role/AgentCoreRuntimeRole-persistent-fs',
    lifecycleConfiguration={
        'idleRuntimeSessionTimeout': 300,
        'maxLifetime': 1800
    },
    # 关键配置：启用 Session Storage
    filesystemConfigurations=[{
        'sessionStorage': {
            'mountPath': '/mnt/workspace'
        }
    }]
)
print(f"Runtime ARN: {response['agentRuntimeArn']}")
print(f"Status: {response['status']}")  # CREATING → 等待变为 READY
```

创建 Endpoint：

```python
endpoint = client.create_agent_runtime_endpoint(
    agentRuntimeId='YOUR_RUNTIME_ID',
    name='persistentFsTestEp',
    agentRuntimeVersion='1'
)
# 等待 status 变为 READY
```

### Step 5: 测试 Session Storage

```python
import boto3, json, time

client = boto3.client('bedrock-agentcore', region_name='us-west-2')
AGENT_ARN = 'arn:aws:bedrock-agentcore:us-west-2:ACCOUNT_ID:runtime/YOUR_RUNTIME_ID'
SESSION = 'my-persistent-session-test-00001'  # 最少 33 字符！

def invoke(prompt, session_id=SESSION):
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode()
    )
    body = b""
    for chunk in resp.get("response", []):
        if isinstance(chunk, dict):
            body += chunk.get("PayloadPart", {}).get("bytes", b"")
        elif isinstance(chunk, bytes):
            body += chunk
    return json.loads(body.decode())["response"]

# 1) 写入文件
invoke("SHELL:echo 'Hello persistent storage!' > /mnt/workspace/test.txt && "
       "mkdir -p /mnt/workspace/project && "
       "echo '{\"key\": \"value\"}' > /mnt/workspace/project/data.json && "
       "dd if=/dev/urandom of=/mnt/workspace/binary.bin bs=1M count=5 2>&1 && "
       "md5sum /mnt/workspace/binary.bin")

# 2) 停止 session（注意：stop_runtime_session 在数据面 client，不是 control client）
client.stop_runtime_session(agentRuntimeArn=AGENT_ARN, runtimeSessionId=SESSION)
time.sleep(20)  # 等待数据完全 flush

# 3) Resume — 验证文件还在
result = invoke("SHELL:cat /mnt/workspace/test.txt && md5sum /mnt/workspace/binary.bin")
print(result)  # 文件内容和 MD5 应与写入时完全一致
```

### Step 6: 验证 Session 间隔离

使用一个不同的 Session ID 调用同一个 Agent Runtime，验证 Session 之间的存储完全隔离：

```python
# 用一个全新的 Session ID
SESSION_B = 'persistent-fs-test-session-bravo-001'

# 检查新 session 的 /mnt/workspace 内容
result = invoke("SHELL:ls -la /mnt/workspace/", session_id=SESSION_B)
print(result)
# 预期输出：空目录 — Session A 写入的文件在 Session B 中完全不可见
```

**实测结果**：

```
total 4
drwxr-xr-x 2 root root    0 Mar 25 02:53 .
drwxr-xr-x 1 root root 4096 Mar 25 02:53 ..
```

Session B 看到的是全新的空目录，证实了 Session Storage 的严格隔离 — 每个 session 只能访问自己的存储空间。

## 测试结果

### 核心功能验证

| # | 测试项 | 结果 | 详情 |
|---|--------|------|------|
| 1 | 写入 → 读取（同 session） | ✅ 通过 | 文本、JSON、二进制、symlink 全部正常 |
| 2 | Stop → Resume 文件持久化 | ✅ 通过 | MD5 完全匹配，二进制完整性验证 |
| 3 | Session 间隔离 | ✅ 通过 | Session B 看到空目录 |
| 4 | 多次 Stop/Resume 循环 | ✅ 通过 | 3 轮循环，文件正确累积 |
| 5 | chmod 权限保留 | ✅ 通过 | 755 权限跨 resume 保持 |
| 6 | Hard link（不支持） | ✅ 预期失败 | error 524 (Operation not supported) |
| 7 | 存储限制 1GB | ✅ 验证 | 写满后 "No space left on device" |

### 性能数据

| 场景 | 耗时 | 备注 |
|------|------|------|
| Fresh session 冷启动 | 3.13-3.18s | 3 次采样，非常稳定 |
| 同 session 内后续调用 | 0.2-0.3s | 热路径极快 |
| Resume（小数据 <10MB） | 2.9s | Stop 后 resume |
| Resume（大数据 ~1GB） | 5.5s | 仍然非常快，推测使用了 lazy loading |
| Stop session | ~12s | 阻塞调用，等待 flush |

!!! tip "关键发现：Resume 速度惊人"
    即使 session 中有 1GB 数据，resume 也只需 5.5 秒。这暗示 AgentCore 使用了**按需加载（lazy loading）**策略，而不是在启动时下载全部数据。对于大型项目工作区，这意味着几乎零启动惩罚。

### 存储限制行为

```
写入 900MB → ✅ 成功（dd if=/dev/zero bs=100M count=9, 1.0 GB/s）
继续写 200MB → 部分成功：写入 124MB 后报错 "No space left on device"
总容量验证：900MB + 124MB = 1024MB = 精确 1GB
```

**df 输出**：

```
Filesystem         Size  Used Avail Use% Mounted on
127.0.0.1:/export  1.0G     0  1.0G   0% /mnt/workspace
```

!!! note "实测发现"
    - `df -h` 始终报告 `0% Used`（NFS 挂载的 quirk），需要用 `du -sh` 查看实际使用量
    - 底层实现为本地 NFS（`127.0.0.1:/export`），microVM 内透明处理

## 踩坑记录

!!! warning "踩坑 1：AWS CLI 不支持 filesystemConfigurations"
    截至 AWS CLI v2.34.14，`create-agent-runtime` 尚不支持 `--filesystem-configurations` 参数，传入会报 `Unknown options` 错误。**必须使用 boto3 SDK**，且需要最新版本。（实测发现，boto3 1.42.74 不支持，升级到 1.42.75 后正常）

!!! warning "踩坑 2：Runtime 命名规则严格"
    Agent Runtime 名称只允许 `[a-zA-Z][a-zA-Z0-9_]{0,47}`。使用连字符（如 `my-agent`）会被拒绝。改用驼峰命名或下划线。已查文档确认。

!!! warning "踩坑 3：Session ID 必须 ≥ 33 字符"
    `runtimeSessionId` 参数要求最少 33 个字符，短于此限制会直接报错 `ParamValidationError`。建议使用 UUID 格式确保长度。已查文档确认：API 约束。

!!! warning "踩坑 4：stop_runtime_session 在数据面"
    `stop_runtime_session` API 在 `bedrock-agentcore`（数据面 client）上，而不是 `bedrock-agentcore-control`（控制面 client）。搞混会报 `AttributeError`。

!!! warning "踩坑 5：df 报告不准确"
    `df -h /mnt/workspace/` 显示 `0% Used` 即使写入了近 1GB 数据。这是 NFS 挂载的特性，**必须用 `du -sh` 来检查实际使用量**。实测发现，官方未记录。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| AgentCore Runtime 计算 | $0.0895/vCPU-hour | ~20 次短时调用 | < $0.05 |
| S3 存储（部署包） | $0.023/GB-month | 44MB × 1 小时 | < $0.01 |
| CloudWatch Logs | $0.50/GB | 极少量 | < $0.01 |
| **合计** | | | **< $0.10** |

!!! note
    本次测试使用简化的命令执行 Agent（无 LLM 调用），如果使用 Claude Sonnet 等模型，每次调用额外约 $0.003-0.015。

## 清理资源

```bash
# 1. 删除 Endpoint
aws bedrock-agentcore-control delete-agent-runtime-endpoint \
  --agent-runtime-id YOUR_RUNTIME_ID \
  --endpoint-name persistentFsTestEp \
  --region us-west-2

# 等待 Endpoint 删除完成后再删除 Runtime
sleep 15

# 2. 删除 Agent Runtime（会同时删除所有 session storage 数据）
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id YOUR_RUNTIME_ID \
  --region us-west-2

# 3. 清理 S3
aws s3 rm s3://bedrock-agentcore-code-YOUR_ACCOUNT_ID-us-west-2/ --recursive
aws s3 rb s3://bedrock-agentcore-code-YOUR_ACCOUNT_ID-us-west-2

# 4. 清理 IAM
aws iam delete-role-policy \
  --role-name AgentCoreRuntimeRole-persistent-fs \
  --policy-name AgentCoreRuntimePolicy
aws iam delete-role --role-name AgentCoreRuntimeRole-persistent-fs
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤。虽然 AgentCore Runtime 按调用计费，但 Session Storage 数据会保留 14 天（即使无调用），建议主动删除 Runtime 清理所有存储。

## 结论与建议

### 适用场景

| 场景 | 推荐度 | 理由 |
|------|--------|------|
| Coding Agent（代码生成+测试） | ⭐⭐⭐ 强烈推荐 | 依赖包、build 产物跨 session 复用 |
| 长期数据分析 Agent | ⭐⭐⭐ 强烈推荐 | 中间结果和 checkpoint 自动持久化 |
| 短对话 Agent | ⭐ 不需要 | 无持久化需求，增加配置复杂度 |
| 需要 >1GB 存储的 Agent | ⚠️ 有限制 | 1GB 硬限制不可调整 |

### 生产环境建议

1. **优先使用 SDK**：CLI 尚未支持 `filesystemConfigurations`，SDK 是唯一选择
2. **Session ID 设计**：使用 UUID 格式确保 ≥ 33 字符，建议语义化命名如 `project-xyz-user-001-{uuid}`
3. **监控存储使用量**：用 `du -sh` 而非 `df`，因为 NFS 挂载的 `df` 报告不准确
4. **Stop 必须等完成**：`stop_runtime_session` 是阻塞调用（~12s），返回后再 resume 确保数据一致
5. **版本更新会重置**：更新 Agent Runtime 版本会清空所有 session storage，发布新版本时需要用户重新初始化工作区

### 与自建方案对比

| | Session Storage | 自建 EFS/S3 + 自定义逻辑 |
|---|---|---|
| 配置复杂度 | 一行配置 | 需要 VPC、挂载点、权限管理 |
| 隔离性 | 自动按 session 隔离 | 需自己实现隔离逻辑 |
| 容量 | 1GB（不可调） | 灵活配置 |
| 成本 | 含在 Runtime 费用中 | 额外 EFS/S3 费用 |
| 数据持久性 | 14 天 TTL | 永久（需手动管理） |

## 参考链接

- [Session Storage 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-persistent-filesystems.html)
- [AgentCore Runtime Quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
- [AgentCore Runtime Getting Started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-getting-started.html)
- [AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
