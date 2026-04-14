---
tags:
  - Security
---

# AWS KMS 导入密钥 On-Demand 轮换实战：无需更改 Key ID 的密钥材料原地轮换

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $1.00（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-28

## 背景

在使用 AWS KMS 的 BYOK（Bring Your Own Key）场景中，企业出于合规要求需要定期轮换密钥材料。然而，在此功能发布之前，导入密钥（EXTERNAL origin）无法原地轮换，只能通过"手动轮换"方式——创建一个新的 KMS key，导入新材料，然后更新所有引用旧 key 的应用和服务。这个过程既复杂又有业务中断风险。

2025 年 6 月，AWS KMS 宣布支持对导入密钥材料的对称加密 KMS key 进行 **on-demand rotation**。这意味着你可以向同一个 KMS key 导入多个密钥材料，通过 `RotateKeyOnDemand` API 切换当前使用的材料，**Key ID 和 ARN 保持不变**，实现零停机轮换。

本文将完整实测这一新功能的全流程：从导入多个密钥材料到执行轮换、验证加密解密兼容性，以及探索关键边界行为。

## 前置条件

- AWS 账号（需要 KMS 管理权限）
- AWS CLI v2 已配置
- OpenSSL（用于生成和加密密钥材料）

## 核心概念

### 之前 vs 现在

| 维度 | 之前（手动轮换） | 现在（On-Demand 轮换） |
|------|-----------------|----------------------|
| 轮换方式 | 创建新 KMS key + 更新所有引用 | 向同一 key 导入新材料 + RotateKeyOnDemand |
| Key ID | 变更（需要更新应用配置） | **不变**（零业务影响） |
| 旧密文解密 | 需保留旧 key 和 alias 映射 | KMS 自动选择正确材料解密 |
| 复杂度 | 高（应用改造 + 切换 + 验证） | 低（API 调用即可） |

### 密钥材料状态流转

```
首次导入 → CURRENT（可用于加密/解密）
导入新材料(NEW_KEY_MATERIAL) → PENDING_ROTATION（等待轮换确认）
RotateKeyOnDemand → 新材料 CURRENT / 旧材料 NON_CURRENT
```

### 关键限制

- 仅支持 **对称加密** KMS key（不支持非对称/HMAC/自定义密钥库）
- 每个 KMS key 最多 **25 次** on-demand rotation
- 同一时刻只能有 **一个** PENDING_ROTATION 材料
- 轮换定价封顶：第一次和第二次轮换后，后续不再额外收费

### 新增 API 参数

- `ImportKeyMaterial` 新增 `--import-type`：`NEW_KEY_MATERIAL`（轮换用）或 `EXISTING_KEY_MATERIAL`（重新导入用）
- `DescribeKey` 返回 `CurrentKeyMaterialId`
- `Decrypt` 返回 `KeyMaterialId`（标识实际使用的材料）
- `ListKeyRotations` 新增 `--include-key-material ALL_KEY_MATERIAL`

## 动手实践

### Step 1: 创建 EXTERNAL Origin KMS Key

```bash
aws kms create-key \
  --origin EXTERNAL \
  --description 'BYOK key for on-demand rotation test' \
  --region us-east-1
```

记录返回的 `KeyId`，后续步骤都需要用到：

```bash
# 设置环境变量
KEY_ID="你的-key-id"
REGION="us-east-1"
```

此时 key 状态为 `PendingImport`，还不能用于加密操作。

### Step 2: 导入第一个密钥材料

**2.1 获取导入参数（wrapping key + import token）**

```bash
aws kms get-parameters-for-import \
  --key-id $KEY_ID \
  --wrapping-algorithm RSAES_OAEP_SHA_256 \
  --wrapping-key-spec RSA_2048 \
  --region $REGION \
  --output json > /tmp/import-params.json

# 提取 wrapping key 和 import token
python3 -c "
import json, base64
d = json.load(open('/tmp/import-params.json'))
open('/tmp/wrapping-key.bin','wb').write(base64.b64decode(d['PublicKey']))
open('/tmp/import-token.bin','wb').write(base64.b64decode(d['ImportToken']))
"
```

**2.2 生成 256-bit 对称密钥材料**

```bash
openssl rand 32 > /tmp/key-material-1.bin
```

**2.3 用 wrapping key 加密密钥材料**

```bash
openssl pkeyutl -encrypt \
  -in /tmp/key-material-1.bin \
  -out /tmp/encrypted-key-material-1.bin \
  -inkey /tmp/wrapping-key.bin \
  -pubin -keyform DER \
  -pkeyopt rsa_padding_mode:oaep \
  -pkeyopt rsa_oaep_md:sha256 \
  -pkeyopt rsa_mgf1_md:sha256
```

**2.4 导入密钥材料**

```bash
aws kms import-key-material \
  --key-id $KEY_ID \
  --encrypted-key-material fileb:///tmp/encrypted-key-material-1.bin \
  --import-token fileb:///tmp/import-token.bin \
  --expiration-model KEY_MATERIAL_DOES_NOT_EXPIRE \
  --region $REGION
```

返回中可以看到新字段 `KeyMaterialId`——这是材料的唯一标识符。

**2.5 验证 key 已就绪**

```bash
aws kms describe-key --key-id $KEY_ID --region $REGION \
  --query "KeyMetadata.{KeyState:KeyState,CurrentKeyMaterialId:CurrentKeyMaterialId}"
```

预期输出 `KeyState: Enabled` 和 `CurrentKeyMaterialId`。

### Step 3: 用第一个材料加密数据

```bash
echo "Hello from key material 1" > /tmp/plaintext1.txt

aws kms encrypt \
  --key-id $KEY_ID \
  --plaintext fileb:///tmp/plaintext1.txt \
  --region $REGION \
  --output json > /tmp/ciphertext-1.json

# 保存 ciphertext 为二进制文件（后续解密用）
python3 -c "
import json, base64
d = json.load(open('/tmp/ciphertext-1.json'))
open('/tmp/ciphertext1.bin','wb').write(base64.b64decode(d['CiphertextBlob']))
"
```

### Step 4: 导入第二个密钥材料（轮换准备）

重复导入流程，但使用 `--import-type NEW_KEY_MATERIAL`：

```bash
# 获取新的导入参数
aws kms get-parameters-for-import \
  --key-id $KEY_ID \
  --wrapping-algorithm RSAES_OAEP_SHA_256 \
  --wrapping-key-spec RSA_2048 \
  --region $REGION \
  --output json > /tmp/import-params-2.json

python3 -c "
import json, base64
d = json.load(open('/tmp/import-params-2.json'))
open('/tmp/wrapping-key-2.bin','wb').write(base64.b64decode(d['PublicKey']))
open('/tmp/import-token-2.bin','wb').write(base64.b64decode(d['ImportToken']))
"

# 生成新密钥材料
openssl rand 32 > /tmp/key-material-2.bin

# 加密
openssl pkeyutl -encrypt \
  -in /tmp/key-material-2.bin \
  -out /tmp/encrypted-key-material-2.bin \
  -inkey /tmp/wrapping-key-2.bin \
  -pubin -keyform DER \
  -pkeyopt rsa_padding_mode:oaep \
  -pkeyopt rsa_oaep_md:sha256 \
  -pkeyopt rsa_mgf1_md:sha256

# 导入新材料（注意 --import-type NEW_KEY_MATERIAL）
aws kms import-key-material \
  --key-id $KEY_ID \
  --encrypted-key-material fileb:///tmp/encrypted-key-material-2.bin \
  --import-token fileb:///tmp/import-token-2.bin \
  --expiration-model KEY_MATERIAL_DOES_NOT_EXPIRE \
  --import-type NEW_KEY_MATERIAL \
  --region $REGION
```

**查看材料状态**：

```bash
aws kms list-key-rotations \
  --key-id $KEY_ID \
  --include-key-material ALL_KEY_MATERIAL \
  --region $REGION
```

此时可以看到两个材料：第一个为 `CURRENT`，第二个为 `PENDING_ROTATION`。

### Step 5: 执行 On-Demand 轮换

```bash
aws kms rotate-key-on-demand \
  --key-id $KEY_ID \
  --region $REGION
```

**监控轮换进度**（轮换期间）：

```bash
aws kms get-key-rotation-status \
  --key-id $KEY_ID \
  --region $REGION
```

轮换进行中会返回 `OnDemandRotationStartDate` 字段，完成后该字段消失。

!!! note "轮换耗时"
    实测轮换耗时约 20-30 秒。`ListKeyRotations` 在轮换完成前仍显示旧状态。

**轮换完成后确认**：

```bash
# 检查材料状态
aws kms list-key-rotations \
  --key-id $KEY_ID \
  --include-key-material ALL_KEY_MATERIAL \
  --region $REGION

# 检查 CurrentKeyMaterialId 已更新
aws kms describe-key --key-id $KEY_ID --region $REGION \
  --query "KeyMetadata.CurrentKeyMaterialId"
```

### Step 6: 验证轮换后的加密/解密兼容性

**用新材料加密新数据**：

```bash
echo "Hello from key material 2" > /tmp/plaintext2.txt

aws kms encrypt \
  --key-id $KEY_ID \
  --plaintext fileb:///tmp/plaintext2.txt \
  --region $REGION \
  --output json > /tmp/ciphertext-2.json

python3 -c "
import json, base64
d = json.load(open('/tmp/ciphertext-2.json'))
open('/tmp/ciphertext2.bin','wb').write(base64.b64decode(d['CiphertextBlob']))
"
```

**解密两个不同材料加密的密文**：

```bash
# 解密用材料 2 加密的数据
aws kms decrypt \
  --ciphertext-blob fileb:///tmp/ciphertext2.bin \
  --region $REGION

# 解密用材料 1 加密的旧数据（应自动使用旧材料）
aws kms decrypt \
  --ciphertext-blob fileb:///tmp/ciphertext1.bin \
  --region $REGION
```

两次解密都成功，且返回的 `KeyMaterialId` 分别对应不同的材料——KMS 通过 ciphertext 中嵌入的标识自动选择正确的密钥材料。

## 测试结果

### 核心功能验证

| 测试场景 | 结果 | 关键观察 |
|---------|------|---------|
| 创建 EXTERNAL key + 导入材料 | ✅ 成功 | DescribeKey 返回 CurrentKeyMaterialId |
| 加密/解密（单材料） | ✅ 成功 | Decrypt 返回 KeyMaterialId |
| 导入第二材料 (NEW_KEY_MATERIAL) | ✅ 成功 | 新材料状态 PENDING_ROTATION |
| RotateKeyOnDemand | ✅ 成功 | 约 28 秒完成 |
| 轮换后加密（使用新材料） | ✅ 成功 | KeyMaterialId 为新材料 |
| 轮换后解密旧密文（自动选材料） | ✅ 成功 | 自动使用旧材料解密 |

### 对比实验：轮换前后

| 维度 | 轮换前 | 轮换后 |
|------|--------|--------|
| CurrentKeyMaterialId | 材料 1 | 材料 2 |
| 新加密使用的材料 | 材料 1 | 材料 2 |
| 旧密文解密 | 使用材料 1 | **仍使用材料 1**（自动选择） |
| Key ID / ARN | 不变 | 不变 |

### 边界测试：删除 NON_CURRENT 材料

| 操作 | 结果 |
|------|------|
| 删除 NON_CURRENT 材料 | 成功 |
| 删除后 Key State | ⚠️ **PendingImport**（整个 key 不可用！） |
| 删除后解密旧密文 | ❌ `KMSInvalidStateException` |
| 删除后解密新密文 | ❌ `KMSInvalidStateException` |
| 删除后加密 | ❌ `KMSInvalidStateException` |
| 重新导入已删除材料 | ✅ key 恢复 Enabled |

## 踩坑记录

!!! warning "踩坑 1：删除任何永久关联的材料会导致整个 key 不可用"
    这是最关键的注意事项。即使你只删除一个 NON_CURRENT 材料，整个 KMS key 也会变成 `PendingImport` 状态——**不仅旧密文不能解密，连新密文和新加密都不行了**。

    这是 AWS 的设计：每个永久关联的材料都必须保持 IMPORTED 状态，key 才能正常工作。**已查文档确认**。

    **应对方案**：如果误删，可以重新导入相同的密钥材料（使用 `--import-type EXISTING_KEY_MATERIAL`）恢复 key。前提是你仍保留着原始密钥材料的副本。

!!! warning "踩坑 2：PENDING_ROTATION 材料的删除是安全的"
    与永久关联材料不同，处于 `PENDING_ROTATION` 状态的材料可以安全删除，不会影响 key 的可用性。**已查文档确认**。

!!! note "观察：轮换不是瞬时完成的"
    `RotateKeyOnDemand` API 返回后，轮换仍需约 20-30 秒才能完成。期间 `GetKeyRotationStatus` 会返回 `OnDemandRotationStartDate`，完成后该字段消失。**实测发现，官方文档仅提到"subject to eventual consistency"**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| KMS Key (EXTERNAL) | $1.00/月 | 按天计 | < $0.10 |
| 轮换后额外密钥材料 | 封顶 2 次收费 | 1 次 | ~$1.00/月 |
| API 调用 (encrypt/decrypt/etc.) | $0.03/10,000 次 | ~20 次 | < $0.01 |
| **合计** | | | **< $1.00** |

## 清理资源

```bash
# 计划删除 KMS key（最短等待期 7 天）
aws kms schedule-key-deletion \
  --key-id $KEY_ID \
  --pending-window-in-days 7 \
  --region $REGION
```

!!! danger "务必清理"
    KMS key 按月计费。即使不使用，保留的 key 也会产生 $1/月的费用（加上轮换材料的额外费用）。请在 Lab 完成后安排删除。

## 结论与建议

### 适用场景

- **合规驱动的密钥轮换**：满足 PCI-DSS、HIPAA 等要求定期轮换密钥的合规标准
- **BYOK + 零停机轮换**：企业使用自有密钥材料，需要在不中断业务的前提下轮换
- **安全事件响应**：怀疑密钥材料泄露时，可立即导入新材料并轮换，无需修改任何应用

### 生产环境建议

1. **永远保留密钥材料副本**：删除任何永久关联的材料都会导致 key 不可用。确保在安全位置（如 HSM 或加密存储）保留所有已导入材料的备份
2. **自动化轮换流程**：将 GetParametersForImport → 加密 → ImportKeyMaterial → RotateKeyOnDemand 封装为自动化脚本或 Step Functions 工作流
3. **监控轮换事件**：通过 CloudTrail 监控 `RotateKey` 事件，结合 CloudWatch Alarms 实现轮换审计
4. **理解定价模型**：轮换收费在第二次后封顶，后续轮换不再额外收费——长期来看，频繁轮换的成本是可控的

### 与自动轮换的对比

| 维度 | 自动轮换 (AWS_KMS origin) | On-Demand 轮换 (EXTERNAL origin) |
|------|--------------------------|----------------------------------|
| 触发方式 | 定期自动 | 手动 API 调用 |
| 密钥材料来源 | AWS 生成 | 用户自己导入 |
| 轮换频率 | 可配置（默认 365 天） | 按需，最多 25 次 |
| 适用场景 | 标准合规 | BYOK + 自定义策略 |

## 参考链接

- [AWS What's New: AWS KMS launches on-demand key rotation for imported keys](https://aws.amazon.com/about-aws/whats-new/2025/06/aws-kms-on-demand-key-rotation-imported-keys/)
- [AWS Security Blog: How to use on-demand rotation for AWS KMS imported keys](https://aws.amazon.com/blogs/security/how-to-use-on-demand-rotation-for-aws-kms-imported-keys/)
- [AWS KMS Developer Guide: Perform on-demand key rotation](https://docs.aws.amazon.com/kms/latest/developerguide/rotating-keys-on-demand.html)
- [AWS KMS Developer Guide: Import key material](https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys-import-key-material.html)
- [AWS KMS Developer Guide: Delete imported key material](https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys-delete-key-material.html)
