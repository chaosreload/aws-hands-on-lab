# AWS Secrets Manager 实测：后量子 TLS 加密零配置启用与 CloudTrail 验证全解析

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 15 分钟
    - **预估费用**: < $0.10（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-04-15

## 背景

量子计算的发展让 "harvest now, decrypt later"（HNDL）成为现实威胁——攻击者今天截获加密流量，等量子计算机成熟后再破解。NIST 已于 2024 年发布 FIPS 203（ML-KEM）标准，各国监管机构要求 2030-2035 年前完成后量子密码迁移。

AWS Secrets Manager 现在支持混合后量子密钥交换（X25519MLKEM768），将经典 X25519 与 ML-KEM 结合保护 TLS 连接。对于使用最新 SDK 的客户端，这个保护**自动生效，无需代码改动**。

本文实测验证了三种客户端（AWS CLI、Python SDK、Node.js SDK）的 PQ TLS 启用状态，通过 CloudTrail `tlsDetails` 对比经典 TLS 与后量子 TLS 的差异，并给出迁移建议。

## 前置条件

- AWS 账号，IAM 用户/角色需要以下权限：
    - `secretsmanager:CreateSecret`
    - `secretsmanager:GetSecretValue`
    - `secretsmanager:DeleteSecret`
    - `cloudtrail:LookupEvents`
- AWS CLI v2（最新版本）
- Node.js 18+（测试 Node.js SDK）

## 核心概念

### PQ TLS 支持矩阵

| 客户端 | 最低版本 | PQ TLS 自动启用 | 备注 |
|--------|---------|:---:|------|
| Secrets Manager Agent | 2.0.0+ | ✅ | |
| Lambda Extension | v19+ | ✅ | |
| CSI Driver | 2.0.0+ | ✅ | |
| AWS SDK for Rust | latest | ✅ | |
| AWS SDK for Go | latest | ✅ | |
| AWS SDK for JavaScript (Node.js) | latest | ✅ | 本文实测验证 |
| AWS SDK for Kotlin | latest | ✅ | |
| AWS SDK for Python (boto3) | latest | ⚠️ | 需要 OpenSSL 3.5+ |
| AWS SDK for Java v2 | v2.35.11+ | ⚠️ | 可能需要配置 |
| AWS CLI v2 | 待更新 | ❌ | 捆绑的 awscrt 版本较旧 |

### 关键术语

| 术语 | 说明 |
|------|------|
| X25519MLKEM768 | 混合后量子密钥交换算法 = X25519（经典）+ ML-KEM-768（后量子） |
| ML-KEM | Module-Lattice-based Key-Encapsulation Mechanism，NIST FIPS 203 标准 |
| HNDL | Harvest Now, Decrypt Later — 现在截获密文，未来用量子计算机破解 |
| tlsDetails | CloudTrail 事件中记录 TLS 连接详情的字段 |

## 动手实践

### Step 1: 创建测试 Secret

```bash
aws secretsmanager create-secret \
  --name pq-tls-test-secret \
  --secret-string '{"test":"post-quantum-tls","timestamp":"2026-04-15"}' \
  --region us-east-1
```

**实测输出**：
```json
{
    "ARN": "arn:aws:secretsmanager:us-east-1:xxxxxxxxxxxx:secret:pq-tls-test-secret-xxxxx",
    "Name": "pq-tls-test-secret",
    "VersionId": "96fbc68f-0bb4-49f2-b832-08088256f60f"
}
```

### Step 2: AWS CLI 读取 Secret（基线测试）

```bash
aws secretsmanager get-secret-value \
  --secret-id pq-tls-test-secret \
  --region us-east-1
```

记录当前时间，稍后查 CloudTrail 验证 TLS 详情。

### Step 3: Node.js SDK 读取 Secret（PQ TLS 验证）

创建测试脚本 `test-pq.mjs`：

```javascript
import {
  SecretsManagerClient,
  GetSecretValueCommand,
} from "@aws-sdk/client-secrets-manager";

const client = new SecretsManagerClient({ region: "us-east-1" });

// 执行 3 次调用
for (let i = 0; i < 3; i++) {
  const resp = await client.send(
    new GetSecretValueCommand({ SecretId: "pq-tls-test-secret" })
  );
  console.log(`Call ${i + 1}: OK -`, resp.Name);
}
console.log("Done. Check CloudTrail in 5-15 minutes.");
```

安装依赖并运行：

```bash
npm install @aws-sdk/client-secrets-manager
node test-pq.mjs
```

### Step 4: Python SDK 读取 Secret（对比测试）

```python
import boto3

client = boto3.client("secretsmanager", region_name="us-east-1")

for i in range(3):
    resp = client.get_secret_value(SecretId="pq-tls-test-secret")
    print(f"Call {i+1}: OK - {resp['Name']}")
print("Done. Check CloudTrail in 5-15 minutes.")
```

### Step 5: CloudTrail 验证（5-15 分钟后）

这是整个实验的**核心验证步骤**。等待 5-15 分钟后查询 CloudTrail：

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=GetSecretValue \
  --start-time "$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --max-results 10 \
  --region us-east-1 \
  --output json
```

从返回的 `CloudTrailEvent` JSON 中提取 `tlsDetails` 字段。

**Node.js SDK 的 CloudTrail 事件**（实测）：
```json
{
  "tlsDetails": {
    "tlsVersion": "TLSv1.3",
    "cipherSuite": "TLS_AES_128_GCM_SHA256",
    "clientProvidedHostHeader": "secretsmanager.us-east-1.amazonaws.com",
    "keyExchange": "X25519MLKEM768"
  }
}
```

**AWS CLI / Python SDK 的 CloudTrail 事件**（实测）：
```json
{
  "tlsDetails": {
    "tlsVersion": "TLSv1.3",
    "cipherSuite": "TLS_AES_128_GCM_SHA256",
    "clientProvidedHostHeader": "secretsmanager.us-east-1.amazonaws.com",
    "keyExchange": "x25519"
  }
}
```

!!! tip "快速提取 keyExchange 的脚本"
    用以下一行命令提取所有 GetSecretValue 调用的 TLS 密钥交换算法：

    ```bash
    aws cloudtrail lookup-events \
      --lookup-attributes AttributeKey=EventName,AttributeValue=GetSecretValue \
      --start-time "$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')" \
      --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      --max-results 50 --region us-east-1 --output json | \
    python3 -c "
    import json, sys
    data = json.load(sys.stdin)
    for e in data.get('Events', []):
        ct = json.loads(e['CloudTrailEvent'])
        ua = ct.get('userAgent', '')[:60]
        ke = ct.get('tlsDetails', {}).get('keyExchange', 'N/A')
        print(f'{ct[\"eventTime\"]} | {ua} | keyExchange: {ke}')
    "
    ```

### Step 6: 性能对比

在同一环境下对三种客户端各执行 20 次 `GetSecretValue`，测量端到端延迟（含 TLS 握手）：

| 客户端 | TLS 类型 | 平均延迟 | P50 | P95 |
|--------|---------|---------|-----|-----|
| Python + CRT | x25519（经典） | 226.9ms | 228.7ms | 239.1ms |
| Python 无 CRT | x25519（经典） | 234.3ms | 235.9ms | 243.3ms |
| Node.js SDK | X25519MLKEM768（PQ） | 226.4ms | 228.4ms | 231.4ms |

**结论：PQ TLS 握手开销可忽略不计。** Node.js SDK 使用后量子 TLS 的延迟与 Python SDK 使用经典 TLS 几乎相同。

## 测试结果

| # | 测试场景 | 客户端 | keyExchange | 结果 |
|---|---------|--------|-------------|------|
| 1 | AWS CLI v2 (awscrt 0.31.2) | CLI 2.34.29 | x25519 | ❌ 经典 TLS |
| 2 | Python SDK + CRT (awscrt 0.32.0) | boto3 1.42.87 | x25519 | ❌ 经典 TLS |
| 3 | Python SDK 禁用 CRT | boto3 1.42.87 | x25519 | ❌ 经典 TLS |
| 4 | Node.js SDK | @aws-sdk 3.1030.0 | X25519MLKEM768 | ✅ PQ TLS |
| 5 | 性能对比 | 全部 | — | ✅ PQ 无显著开销 |

## 踩坑记录

!!! warning "踩坑 1: Python SDK 需要 OpenSSL 3.5+ 才能启用 PQ TLS"
    公告声明 Python SDK "with OpenSSL 3.5+" 支持 PQ TLS。在我们的 Ubuntu 22.04 环境中（OpenSSL 3.0.2），即使安装了 awscrt 0.32.0 且该库包含 `TlsCipherPref.PQ_DEFAULT`，CloudTrail 仍显示 `keyExchange: x25519`。

    **影响**：大部分生产环境运行 Ubuntu 22.04/24.04（OpenSSL 3.0-3.3），无法自动获得 PQ TLS 保护。需要升级到支持 OpenSSL 3.5+ 的发行版，或使用 Node.js SDK 作为替代。

!!! warning "踩坑 2: AWS CLI v2 捆绑旧版 awscrt，不支持 PQ TLS"
    AWS CLI v2.34.29 捆绑的 awscrt 版本为 0.31.2（比 pip 安装的 0.32.0 旧），CloudTrail 显示 `keyExchange: x25519`。

    **影响**：通过 AWS CLI 管理 Secrets 的运维操作暂时无法获得 PQ TLS 保护。需要等待 CLI 更新捆绑的 CRT 版本。

!!! info "发现: CloudTrail tlsDetails 新增 keyExchange 字段"
    CloudTrail `tlsDetails` 中的 `keyExchange` 字段同时记录经典 TLS（`x25519`）和后量子 TLS（`X25519MLKEM768`）的密钥交换算法。这使得安全团队可以通过 CloudTrail 精确审计哪些 API 调用已受 PQ TLS 保护。

    **合规价值**：可以用 Athena/CloudTrail Lake 查询所有 `keyExchange != 'X25519MLKEM768'` 的事件，识别尚未迁移到 PQ TLS 的客户端。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Secrets Manager（1 个 Secret，按比例） | $0.40/月 | < 1 天 | < $0.02 |
| API 调用 (~100 次 GetSecretValue) | $0.05/10,000 | 100 次 | < $0.01 |
| CloudTrail 管理事件查询 | 免费 | — | $0 |
| **合计** | | | **< $0.05** |

## 清理资源

```bash
# 删除测试 Secret（立即删除，不等待恢复期）
aws secretsmanager delete-secret \
  --secret-id pq-tls-test-secret \
  --force-delete-without-recovery \
  --region us-east-1
```

!!! danger "务必清理"
    Secret 按月计费（$0.40/月），Lab 完成后请执行清理步骤。

## 结论与建议

### 现状总结

后量子 TLS 在 Secrets Manager 端已全面就绪，但**客户端侧的支持参差不齐**：

- **Node.js SDK**：✅ 已自动启用，零配置
- **Python SDK**：⚠️ 需要 OpenSSL 3.5+，大部分现有环境尚不满足
- **AWS CLI v2**：❌ 暂不支持，等待版本更新

### 迁移建议

| 场景 | 推荐方案 | 紧迫性 |
|------|---------|-------|
| 新应用（Node.js/Rust/Go/Kotlin） | 直接使用最新 SDK，自动启用 PQ | 立即 |
| 现有 Python 应用 | 等待 OpenSSL 3.5+ 环境或切换到 Node.js | 中期（2027 前） |
| Lambda 函数 | 使用 Lambda Extension v19+ | 立即 |
| K8s 工作负载 | 升级 CSI Driver 到 2.0.0+ | 立即 |
| CLI 运维操作 | 等待 CLI 更新；高安全场景可用 SDK 脚本替代 | 低 |

### 安全合规角度

```sql
-- CloudTrail Lake / Athena: 查找未使用 PQ TLS 的 Secrets Manager 调用
SELECT eventTime, userAgent,
       json_extract_scalar(tlsDetails, '$.keyExchange') as keyExchange
FROM cloudtrail_logs
WHERE eventSource = 'secretsmanager.amazonaws.com'
  AND json_extract_scalar(tlsDetails, '$.keyExchange') != 'X25519MLKEM768'
ORDER BY eventTime DESC
```

## 参考链接

- [AWS What's New: Secrets Manager Post-Quantum TLS](https://aws.amazon.com/about-aws/whats-new/2026/04/aws-secrets-manager-post-quantum-tls/)
- [AWS Secrets Manager 文档](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html)
- [AWS Post-Quantum Cryptography 迁移指南](https://aws.amazon.com/security/post-quantum-cryptography/migrating-to-post-quantum-cryptography/)
- [NIST FIPS 203 - ML-KEM](https://www.nist.gov/pqc)
- [AWS Common Runtime (CRT) Libraries](https://docs.aws.amazon.com/sdkref/latest/guide/common-runtime.html)
