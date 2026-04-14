---
tags:
  - Security
---

# Amazon CloudFront 后量子 TLS 实测：零配置启用量子安全加密

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: < $0.50（含清理）
    - **Region**: us-east-1（CloudFront 为全球服务）
    - **最后验证**: 2026-03-26

## 背景

量子计算的发展对现有加密体系构成了潜在威胁。当前广泛使用的 RSA 和 ECC 密钥交换算法可能被未来的量子计算机在多项式时间内破解（Shor 算法）。更令人担忧的是 **"Harvest Now, Decrypt Later"（现在截获，未来解密）** 攻击——攻击者现在截获加密流量，等量子计算机成熟后再解密。

为应对这一威胁，AWS 在 CloudFront 上实现了**混合后量子密钥交换**，将经典的椭圆曲线密钥交换（ECDH）与 NIST 标准化的后量子算法 ML-KEM（Module-Lattice-based Key-Encapsulation Mechanism）组合使用。即使其中一种算法被破解，另一种仍然提供保护——这就是"混合"的含义。

2025 年 9 月，CloudFront 宣布所有现有 TLS 安全策略自动支持后量子密钥交换，同时新增 `TLSv1.3_2025` 纯 TLS 1.3 安全策略。本文将通过实际操作验证 PQC 握手行为，并量化性能影响。

## 前置条件

- AWS 账号（需要 CloudFront、S3 权限）
- AWS CLI v2 已配置
- Docker（用于运行支持 PQC 的 OpenSSL/curl）
- 基本的 TLS/SSL 知识

## 核心概念

### 为什么需要后量子密码学？

| 威胁 | 说明 | 影响 |
|------|------|------|
| Shor 算法 | 量子计算机可在多项式时间内分解大整数和求解离散对数 | RSA、ECC 密钥交换被破解 |
| Harvest Now, Decrypt Later | 攻击者现在截获加密流量，未来用量子计算机解密 | 今天的加密数据未来可能暴露 |
| 合规要求 | NIST、CISA 等机构要求向后量子密码学迁移 | 需要尽早部署 PQC |

### CloudFront PQC 实现

CloudFront 使用**混合密钥交换**模式，在 TLS 1.3 握手中同时执行经典和后量子密钥协商：

- **X25519MLKEM768** = X25519（经典）+ ML-KEM-768（后量子）
- **SecP256r1MLKEM768** = P-256（经典）+ ML-KEM-768（后量子）

关键特性：

- ✅ **零配置**：所有现有安全策略自动启用 PQC，无需任何操作
- ✅ **无额外费用**：PQC 不产生额外费用
- ✅ **向后兼容**：不支持 PQC 的客户端自动降级到经典密钥交换
- ⚠️ **仅 TLS 1.3**：PQC 密钥交换仅在 TLS 1.3 中可用

### 安全策略对比（2025 新增）

| 安全策略 | 支持的最低 TLS 版本 | PQC 支持 | 特点 |
|---------|-------------------|---------|------|
| TLSv1.2_2021 | TLS 1.2 | ✅（TLS 1.3 连接时） | 推荐的通用策略 |
| TLSv1.2_2025 | TLS 1.2 | ✅（TLS 1.3 连接时） | 去掉 CHACHA20（TLS 1.2 部分）和 SHA224 签名 |
| **TLSv1.3_2025** | **TLS 1.3** | ✅ | **新增**，仅允许 TLS 1.3，最严格 |

## 动手实践

### Step 1: 创建 S3 源站

```bash
# 创建 S3 桶作为 CloudFront 源站
aws s3 mb s3://pqc-tls-test-$(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1

# 上传测试页面
echo '<html><body><h1>PQC TLS Test</h1><p>Post-quantum cryptography is working!</p></body></html>' > /tmp/index.html
aws s3 cp /tmp/index.html s3://pqc-tls-test-$(aws sts get-caller-identity --query Account --output text)/index.html \
  --content-type "text/html" --region us-east-1
```

### Step 2: 创建 CloudFront Distribution

```bash
# 创建 Origin Access Control
aws cloudfront create-origin-access-control \
  --origin-access-control-config '{
    "Name": "pqc-tls-test-oac",
    "Description": "OAC for PQC TLS test",
    "OriginAccessControlOriginType": "s3",
    "SigningProtocol": "sigv4",
    "SigningBehavior": "always"
  }' --query 'OriginAccessControl.Id' --output text
# 记录输出的 OAC ID，下面会用到

# 创建分配配置文件
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
OAC_ID="<上一步输出的 OAC ID>"

cat > /tmp/cf-dist.json << EOF
{
  "CallerReference": "pqc-tls-test-$(date +%s)",
  "Comment": "PQC TLS Test Distribution",
  "DefaultRootObject": "index.html",
  "Enabled": true,
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "s3-pqc-tls-test",
      "DomainName": "pqc-tls-test-${ACCOUNT_ID}.s3.us-east-1.amazonaws.com",
      "OriginAccessControlId": "${OAC_ID}",
      "S3OriginConfig": { "OriginAccessIdentity": "" }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-pqc-tls-test",
    "ViewerProtocolPolicy": "https-only",
    "AllowedMethods": { "Quantity": 2, "Items": ["GET", "HEAD"] },
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "Compress": true
  },
  "ViewerCertificate": {
    "CloudFrontDefaultCertificate": true,
    "MinimumProtocolVersion": "TLSv1"
  },
  "PriceClass": "PriceClass_100"
}
EOF

# 创建分配
aws cloudfront create-distribution \
  --distribution-config file:///tmp/cf-dist.json \
  --query 'Distribution.{Id:Id,Domain:DomainName,Status:Status}' \
  --output table
```

!!! note "等待部署"
    CloudFront 分配通常需要 3-5 分钟完成部署。可使用以下命令等待：
    ```bash
    aws cloudfront wait distribution-deployed --id <DIST_ID>
    ```

### Step 3: 添加 S3 Bucket Policy

```bash
DIST_ID="<分配 ID>"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > /tmp/s3-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AllowCloudFrontOAC",
    "Effect": "Allow",
    "Principal": { "Service": "cloudfront.amazonaws.com" },
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::pqc-tls-test-${ACCOUNT_ID}/*",
    "Condition": {
      "StringEquals": {
        "AWS:SourceArn": "arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${DIST_ID}"
      }
    }
  }]
}
EOF

aws s3api put-bucket-policy \
  --bucket pqc-tls-test-${ACCOUNT_ID} \
  --policy file:///tmp/s3-policy.json \
  --region us-east-1
```

### Step 4: 准备 PQC 测试工具

系统自带的 OpenSSL（通常 3.0.x）不支持 ML-KEM 密钥交换。我们使用 Open Quantum Safe 项目提供的 Docker 镜像：

```bash
docker pull openquantumsafe/curl:latest

# 验证 PQC 算法支持
docker run --rm openquantumsafe/curl:latest \
  openssl list -kem-algorithms 2>&1 | grep -i mlkem
```

输出应包含：

```
mlkem768 @ oqsprovider
X25519MLKEM768 @ oqsprovider
SecP256r1MLKEM768 @ oqsprovider
```

### Step 5: 验证 PQC 握手

**实验 1：X25519MLKEM768 后量子握手**

```bash
CF_DOMAIN="<你的 CloudFront 域名>.cloudfront.net"

docker run --rm openquantumsafe/curl:latest \
  curl -v --curves X25519MLKEM768 -k https://${CF_DOMAIN}/ 2>&1 \
  | grep "SSL connection using"
```

预期输出：

```
* SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256 / X25519MLKEM768 / RSASSA-PSS
```

🎯 关键信息：`X25519MLKEM768` 出现在连接信息中，确认 PQC 密钥交换成功。

**实验 2：经典 X25519 对比**

```bash
docker run --rm openquantumsafe/curl:latest \
  curl -v --curves X25519 -k https://${CF_DOMAIN}/ 2>&1 \
  | grep "SSL connection using"
```

预期输出：

```
* SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256 / x25519 / RSASSA-PSS
```

对比可以看出：相同的 cipher（TLS_AES_128_GCM_SHA256），但密钥交换从经典 `x25519` 变成了后量子 `X25519MLKEM768`。

**实验 3：SecP256r1MLKEM768（P-256 + ML-KEM 混合）**

```bash
docker run --rm openquantumsafe/curl:latest \
  curl -v --curves SecP256r1MLKEM768 -k https://${CF_DOMAIN}/ 2>&1 \
  | grep "SSL connection using"
```

预期输出：

```
* SSL connection using TLSv1.3 / TLS_AES_128_GCM_SHA256 / SecP256r1MLKEM768 / RSASSA-PSS
```

### Step 6: 边界测试 — TLS 1.2 + PQC

PQC 密钥交换仅支持 TLS 1.3。强制 TLS 1.2 时会发生什么？

```bash
# 强制 TLS 1.2 + PQC group → 应该失败
docker run --rm openquantumsafe/curl:latest \
  curl -v --tls-max 1.2 --curves X25519MLKEM768 -k https://${CF_DOMAIN}/ 2>&1 \
  | grep -i "error\|SSL connection"
```

预期输出：

```
* TLS connect error: error:0A00017A:SSL routines::wrong curve
```

确认 TLS 1.2 不支持 PQC 密钥交换。这符合 TLS 协议规范：ML-KEM 的 `named_group` 扩展仅在 TLS 1.3 的 `key_share` 中定义。

```bash
# TLS 1.2 + 经典曲线 → 正常工作
docker run --rm openquantumsafe/curl:latest \
  curl -v --tls-max 1.2 --curves X25519 -k https://${CF_DOMAIN}/ 2>&1 \
  | grep "SSL connection using"
```

预期输出：

```
* SSL connection using TLSv1.2 / ECDHE-RSA-AES128-GCM-SHA256 / x25519 / RSASSA-PSS
```

## 测试结果

### 握手延迟对比（10 次测量）

| 指标 | PQC (X25519MLKEM768) | 经典 (X25519) | 差异 |
|------|---------------------|--------------|------|
| 平均 TLS 握手时间 | 229.0 ms | 229.4 ms | -0.4 ms (-0.2%) |
| 中位数 | 229.3 ms | 228.8 ms | +0.5 ms |
| 标准差 | 3.5 ms | 2.4 ms | - |

**结论：PQC 握手延迟与经典握手几乎无差异**，在测量误差范围内。ML-KEM 的计算开销极低，不构成性能瓶颈。

### ClientHello / ServerHello 大小对比

| 密钥交换算法 | 客户端发送 (bytes) | 服务器响应 (bytes) | 额外开销 |
|-------------|-------------------|-------------------|---------|
| X25519MLKEM768 | 1,670 | 6,591 | — |
| SecP256r1MLKEM768 | 1,703 | 6,624 | — |
| 经典 X25519 | 494 | 5,503 | — |
| **PQC 额外开销** | **+1,176** | **+1,088** | **~2.3 KB** |

PQC 握手额外传输约 2.3 KB 数据（主要是 ML-KEM key share），对现代网络带宽而言完全可忽略。

### 密钥交换行为总结

| 场景 | TLS 版本 | 密钥交换 | 结果 |
|------|---------|---------|------|
| PQC 客户端 + CF 默认证书 | TLS 1.3 | X25519MLKEM768 | ✅ PQC 握手成功 |
| PQC 客户端 + CF 默认证书 | TLS 1.3 | SecP256r1MLKEM768 | ✅ PQC 握手成功 |
| 经典客户端 + CF 默认证书 | TLS 1.3 | X25519 | ✅ 经典握手成功 |
| PQC 客户端 + 强制 TLS 1.2 | TLS 1.2 | X25519MLKEM768 | ❌ 连接失败（PQC 不支持 TLS 1.2） |
| 经典客户端 + 强制 TLS 1.2 | TLS 1.2 | X25519 | ✅ 经典握手成功 |

## 踩坑记录

!!! warning "系统 OpenSSL 不支持 ML-KEM"
    Ubuntu 22.04 自带的 OpenSSL 3.0.2 不支持 ML-KEM 密钥交换算法。需要使用 OpenSSL 3.5+ 或 OQS Provider。最简单的方案是使用 `openquantumsafe/curl` Docker 镜像。
    **已查文档确认**：这是 OpenSSL 版本限制，不是 CloudFront 问题。

!!! warning "TLS 1.2 客户端无法使用 PQC"
    当客户端仅支持 TLS 1.2 或通过 `--tls-max 1.2` 强制 TLS 1.2 时，PQC 密钥交换不可用。这是 TLS 协议层面的限制，不是 CloudFront 的配置问题。
    **已查文档确认**：官方文档明确注明 "Quantum-safe key exchanges are only supported with TLS 1.3."

!!! tip "浏览器 PQC 支持"
    主流浏览器已支持 PQC：Chrome 124+、Firefox 128+ 默认启用 X25519MLKEM768。用户无需任何操作即可享受后量子保护。
    **实测发现，官方未记录具体浏览器版本要求**。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| CloudFront 分配 | 免费 | 1 个 | $0.00 |
| CloudFront 请求 | $0.0075/10K | ~100 次 | ~$0.00 |
| S3 存储 | $0.023/GB | < 1 KB | ~$0.00 |
| PQC 功能 | 免费 | - | $0.00 |
| **合计** | | | **< $0.01** |

## 清理资源

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
DIST_ID="<你的分配 ID>"
OAC_ID="<你的 OAC ID>"

# 1. 禁用 CloudFront 分配
aws cloudfront get-distribution-config --id ${DIST_ID} --query 'DistributionConfig' --output json > /tmp/cf-config.json
# 编辑 /tmp/cf-config.json，将 "Enabled": true 改为 "Enabled": false
ETAG=$(aws cloudfront get-distribution-config --id ${DIST_ID} --query 'ETag' --output text)
# 使用 sed 修改配置
sed -i 's/"Enabled": true/"Enabled": false/' /tmp/cf-config.json
aws cloudfront update-distribution --id ${DIST_ID} --if-match ${ETAG} --distribution-config file:///tmp/cf-config.json

# 2. 等待分配部署完成后删除
aws cloudfront wait distribution-deployed --id ${DIST_ID}
ETAG=$(aws cloudfront get-distribution --id ${DIST_ID} --query 'ETag' --output text)
aws cloudfront delete-distribution --id ${DIST_ID} --if-match ${ETAG}

# 3. 删除 OAC
aws cloudfront delete-origin-access-control --id ${OAC_ID} --if-match ETVPDKIKX0DER

# 4. 清空并删除 S3 桶
aws s3 rm s3://pqc-tls-test-${ACCOUNT_ID} --recursive --region us-east-1
aws s3 rb s3://pqc-tls-test-${ACCOUNT_ID} --region us-east-1

# 5. 清理 Docker 镜像（可选）
docker rmi openquantumsafe/curl:latest
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。CloudFront 分配必须先禁用再删除。

## 结论与建议

### 关键发现

1. **零配置 PQC**：CloudFront 的 PQC 支持无需任何配置变更。只要客户端支持 PQC 密钥交换（如 X25519MLKEM768），CloudFront 会自动协商使用后量子安全的密钥交换
2. **零性能损失**：实测表明 PQC 握手延迟与经典握手几乎一致（差异 <1ms），额外的 ~2.3KB 握手数据对现代网络无感知
3. **自动向后兼容**：不支持 PQC 的客户端会自动降级到经典密钥交换，不影响现有连接

### 生产环境建议

- **现有 CloudFront 用户**：无需任何操作！PQC 已自动启用。确保客户端（浏览器、SDK）保持更新即可
- **安全合规要求高的场景**：考虑使用 `TLSv1.3_2025` 安全策略，仅允许 TLS 1.3 连接，确保所有连接都能协商 PQC
- **监控建议**：关注 CloudFront 的 Connection Logs，可以观察到 TLS 协议版本和 cipher 信息，帮助评估 PQC 采用率

### PQC 迁移路线图

CloudFront 的 PQC 支持是 AWS 更广泛的后量子密码学迁移计划的一部分。AWS 已在 KMS、S3、CloudFront 等服务部署了 PQC，底层使用 AWS-LC 密码学库（首个通过 FIPS 140-3 验证并包含 ML-KEM 的开源密码学模块）。建议关注 [AWS Post-Quantum Cryptography](https://aws.amazon.com/security/post-quantum-cryptography/) 页面了解最新进展。

## 参考链接

- [CloudFront TLS 安全策略和 PQC 密钥交换文档](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/secure-connections-supported-viewer-protocols-ciphers.html)
- [AWS What's New: CloudFront TLS Policy Post-Quantum Support](https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-cloudfront-TLS-policy-post-quantum-support/)
- [AWS Post-Quantum Cryptography](https://aws.amazon.com/security/post-quantum-cryptography/)
- [NIST FIPS 203: ML-KEM Standard](https://csrc.nist.gov/pubs/fips/203/final)
- [Open Quantum Safe Project](https://openquantumsafe.org/)
