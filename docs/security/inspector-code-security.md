# Amazon Inspector Code Security 实战：在开发阶段拦截代码与依赖漏洞

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 30 分钟
    - **预估费用**: $0（15 天免费试用期内）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-27

## 背景

传统的安全扫描在应用部署到生产环境后才发现漏洞，修复成本高、周期长。**Shift-left security** 的核心思想是把安全检查前移到开发阶段——代码刚写完就扫描，而不是等到部署后。

2025 年 6 月 17 日，Amazon Inspector 发布了 **Code Security** 能力（GA），将漏洞管理从运行时（EC2/ECR/Lambda）扩展到**源代码仓库**。原生集成 GitHub 和 GitLab，支持三种扫描类型：

- **SAST**（Static Application Security Testing）：分析源代码中的安全漏洞（SQL 注入、XSS、硬编码密钥等）
- **SCA**（Software Composition Analysis）：检测第三方依赖中的已知 CVE
- **IaC**（Infrastructure as Code）：验证 CloudFormation、Terraform 等基础设施模板的安全配置

本文通过实际操作，演示 Inspector Code Security 的完整设置流程：创建包含已知漏洞的测试仓库、集成 GitHub、触发 SAST/SCA/IaC 三合一扫描、以及使用 `inspector-scan` API 进行独立依赖漏洞扫描。

## 前置条件

- AWS 账号（需要 `inspector2:*` 和 `inspector-scan:*` 权限）
- AWS CLI v2 已配置
- GitHub 或 GitLab 账号（用于代码仓库集成）

## 测试仓库

本文使用一个包含已知漏洞的测试仓库进行验证：[chaosreload/inspector-test-repo](https://github.com/chaosreload/inspector-test-repo)

```
inspector-test-repo/
├── app/
│   ├── vulnerable_app.py      # SQL 注入、硬编码凭证、命令注入
│   └── requirements.txt       # 有已知 CVE 的依赖（django@2.0、flask@1.0 等）
├── infra/
│   ├── insecure-sg.yaml       # 0.0.0.0/0 入站规则的 Security Group
│   └── insecure-s3.yaml       # 无加密、公开访问的 S3 Bucket
└── README.md
```

!!! warning "仅供测试"
    该仓库包含故意植入的安全漏洞，请勿在生产环境使用任何代码。

## 核心概念

### 架构概览

```
开发者代码仓库 (GitHub/GitLab)
        │
        ▼
┌──────────────────────────────────┐
│   Amazon Inspector Code Security │
│                                  │
│  ┌──────┐ ┌─────┐ ┌──────────┐ │
│  │ SAST │ │ SCA │ │ IaC Scan │ │
│  └──┬───┘ └──┬──┘ └────┬─────┘ │
│     │        │          │       │
│     ▼        ▼          ▼       │
│        Findings 聚合             │
└──────────────┬───────────────────┘
               │
    ┌──────────┴──────────┐
    ▼                     ▼
Inspector 控制台      SCM 平台反馈
(组织级聚合视图)    (PR 评论/Check)
```

### 之前 vs 现在

| 维度 | 之前 | 现在（Code Security） |
|------|------|----------------------|
| 扫描时机 | 运行时（部署后） | 开发阶段（push/PR 时） |
| 覆盖范围 | EC2/ECR/Lambda | + 源代码仓库 |
| 扫描类型 | SCA（依赖）+ 网络暴露 | SAST + SCA + IaC |
| 开发者反馈 | 需要登录控制台 | PR 内直接显示 |
| 管理视图 | 按资源聚合 | + 按代码仓库聚合 |

### 关键限制

- **Region 支持**：仅 10 个 Region（见下表）
- **SCM 平台**：仅支持 GitHub（github.com）和 GitLab Self-Managed
- **每账户一个默认扫描配置**：使用 `projectSelectionScope=ALL` 时，只能创建一个配置
- **GitHub 集成需要浏览器 OAuth**：CLI 可创建集成并获取授权 URL，但 OAuth 授权步骤必须在浏览器完成。推荐通过 Console 完成全流程

**支持的 Region**：

| Region | 名称 |
|--------|------|
| us-east-1 | N. Virginia |
| us-west-2 | Oregon |
| us-east-2 | Ohio |
| ap-southeast-2 | Sydney |
| ap-northeast-1 | Tokyo |
| eu-central-1 | Frankfurt |
| eu-west-1 | Ireland |
| eu-west-2 | London |
| eu-north-1 | Stockholm |
| ap-southeast-1 | Singapore |

## 动手实践

### Step 1: 启用 Inspector Code Repository 扫描

```bash
# 启用 CODE_REPOSITORY 扫描类型
aws inspector2 enable \
  --resource-types CODE_REPOSITORY \
  --region us-east-1

# 验证状态（等待约 10 秒变为 ENABLED）
aws inspector2 batch-get-account-status \
  --account-ids $(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1 \
  --query 'accounts[0].resourceState.codeRepository.status'
```

输出：
```
"ENABLED"
```

### Step 2: 创建 GitHub 集成

```bash
# 创建 GitHub 集成
aws inspector2 create-code-security-integration \
  --name my-github-integration \
  --type GITHUB \
  --region us-east-1
```

输出：
```json
{
    "integrationArn": "arn:aws:inspector2:us-east-1:123456789012:codesecurity-integration/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "authorizationUrl": "https://github.com/login/oauth/authorize?client_id=..."
}
```

!!! warning "需要浏览器完成 OAuth 授权"
    CLI 创建集成后返回 `authorizationUrl`，你需要在浏览器中打开这个 URL，登录 GitHub 并授权 Amazon Inspector GitHub App。授权完成后，集成状态才会变为 `ACTIVE`。

    也可以通过 **AWS Console** → Inspector → Code security → Connect to → GitHub 完成集成，Console 会引导你完成整个 OAuth 流程。

```bash
# 查看集成状态
aws inspector2 list-code-security-integrations \
  --region us-east-1 \
  --query 'integrations[].{Name:name,Status:status,Type:type}'
```

!!! info "GitHub App 是 Region-specific"
    每个 Region 有独立的 Inspector GitHub App（例如 N. Virginia 的 app slug 为 `amazon-inspector-n-virginia`）。GitHub App 请求的权限包括：`contents:read`、`issues:read`、`metadata:read`、`pull_requests:write`。

### Step 3: 创建扫描配置

通过 CLI 创建扫描配置：

```bash
# 创建全功能扫描配置（SAST + SCA + IaC）
aws inspector2 create-code-security-scan-configuration \
  --name "full-security-scan" \
  --level ACCOUNT \
  --configuration '{
    "continuousIntegrationScanConfiguration": {
      "supportedEvents": ["PULL_REQUEST", "PUSH"]
    },
    "periodicScanConfiguration": {
      "frequency": "WEEKLY"
    },
    "ruleSetCategories": ["SAST", "IAC", "SCA"]
  }' \
  --scope-settings '{"projectSelectionScope": "ALL"}' \
  --region us-east-1
```

**扫描配置参数说明**：

| 参数 | 选项 | 说明 |
|------|------|------|
| `ruleSetCategories` | `SAST`, `IAC`, `SCA` | 至少选择 1 种，可组合 |
| `supportedEvents` | `PULL_REQUEST`, `PUSH` | CI 触发事件 |
| `frequency` | `WEEKLY`, `MONTHLY`, `NEVER` | 定期扫描频率 |
| `projectSelectionScope` | `ALL` | 关联所有仓库 |

```bash
# 查看配置详情
aws inspector2 list-code-security-scan-configurations \
  --region us-east-1 \
  --query 'configurations[].{Name:name,Rules:ruleSetCategories,Frequency:periodicScanFrequency,Events:continuousIntegrationScanSupportedEvents}'
```

输出：
```json
[
    {
        "Name": "CodeSecurity-default-config",
        "Rules": ["SAST", "IAC", "SCA"],
        "Frequency": "WEEKLY",
        "Events": ["PULL_REQUEST", "PUSH"]
    }
]
```

### Step 4: 触发仓库扫描并查看 Findings

GitHub 集成完成且扫描配置关联后，可以手动触发扫描：

```bash
# 查看已关联的仓库项目 ID
aws inspector2 list-code-security-scan-configuration-associations \
  --scan-configuration-arn "YOUR_SCAN_CONFIG_ARN" \
  --region us-east-1
```

输出：
```json
{
    "associations": [
        {
            "resource": {
                "projectId": "project-66594973-ce57-42e7-90d4-a0c317f05c5c"
            }
        }
    ]
}
```

```bash
# 手动触发扫描
aws inspector2 start-code-security-scan \
  --resource "projectId=project-66594973-ce57-42e7-90d4-a0c317f05c5c" \
  --region us-east-1
```

输出：
```json
{
    "scanId": "4691197b-4608-40aa-95e2-e66e818e87c0",
    "status": "IN_PROGRESS"
}
```

```bash
# 查看扫描状态（约 40 秒完成）
aws inspector2 get-code-security-scan \
  --resource "projectId=project-66594973-ce57-42e7-90d4-a0c317f05c5c" \
  --scan-id "4691197b-4608-40aa-95e2-e66e818e87c0" \
  --region us-east-1
```

输出：
```json
{
    "scanId": "4691197b-4608-40aa-95e2-e66e818e87c0",
    "status": "SUCCESSFUL",
    "lastCommitId": "2bade059fe9bc13a60bece9fb060f08e130b7e00"
}
```

```bash
# 查看扫描发现的 Findings
aws inspector2 list-findings \
  --region us-east-1 \
  --query 'findings[].{Type:type,Severity:severity,Title:title}' \
  --output table
```

### Step 5: 使用 Inspector Scan API 扫描依赖漏洞

即使不设置 GitHub 集成，你也可以使用 `inspector-scan` API 对项目依赖进行即时扫描。这对 CI/CD 管道特别有用。

**准备测试 SBOM（CycloneDX 1.5 格式）**：

```bash
cat > /tmp/test-sbom.json << 'EOF'
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.5",
  "version": 1,
  "components": [
    {
      "type": "library",
      "name": "flask",
      "version": "1.0",
      "purl": "pkg:pypi/flask@1.0"
    },
    {
      "type": "library",
      "name": "django",
      "version": "2.0",
      "purl": "pkg:pypi/django@2.0"
    },
    {
      "type": "library",
      "name": "requests",
      "version": "2.19.0",
      "purl": "pkg:pypi/requests@2.19.0"
    },
    {
      "type": "library",
      "name": "jinja2",
      "version": "2.10",
      "purl": "pkg:pypi/jinja2@2.10"
    },
    {
      "type": "library",
      "name": "log4j-core",
      "version": "2.14.0",
      "purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.0"
    },
    {
      "type": "library",
      "name": "lodash",
      "version": "4.17.15",
      "purl": "pkg:npm/lodash@4.17.15"
    }
  ]
}
EOF
```

**执行扫描**：

```bash
# 扫描 SBOM 并以表格格式输出结果
aws inspector-scan scan-sbom \
  --sbom file:///tmp/test-sbom.json \
  --output-format INSPECTOR \
  --region us-east-1 \
  --query 'sbom.vulnerabilities[].{CVE:id,Severity:severity,Component:affects[0].installed_version,Fix:affects[0].fixed_version}' \
  --output table
```

### Step 6: 动态调整扫描配置

```bash
# 仅启用 SAST（关闭 SCA 和 IaC）
aws inspector2 update-code-security-scan-configuration \
  --scan-configuration-arn "arn:aws:inspector2:us-east-1:123456789012:owner/123456789012/codesecurity-configuration/xxxxx" \
  --configuration '{
    "continuousIntegrationScanConfiguration": {"supportedEvents": ["PULL_REQUEST"]},
    "periodicScanConfiguration": {"frequency": "MONTHLY"},
    "ruleSetCategories": ["SAST"]
  }' \
  --region us-east-1

# 恢复全功能扫描
aws inspector2 update-code-security-scan-configuration \
  --scan-configuration-arn "arn:aws:inspector2:us-east-1:123456789012:owner/123456789012/codesecurity-configuration/xxxxx" \
  --configuration '{
    "continuousIntegrationScanConfiguration": {"supportedEvents": ["PULL_REQUEST", "PUSH"]},
    "periodicScanConfiguration": {"frequency": "WEEKLY"},
    "ruleSetCategories": ["SAST", "IAC", "SCA"]
  }' \
  --region us-east-1
```

## 测试结果

### GitHub 仓库扫描结果

对 [chaosreload/inspector-test-repo](https://github.com/chaosreload/inspector-test-repo) 触发全量扫描（SAST + SCA + IaC），约 42 秒完成，共发现 **62 个 Findings**：

**按严重性分布**：

| 严重性 | 数量 | 占比 |
|--------|------|------|
| Critical | 9 | 14.5% |
| High | 28 | 45.2% |
| Medium | 25 | 40.3% |
| **总计** | **62** | 100% |

**按扫描类型分布**：

| 扫描类型 | Finding 类型 | 数量 | 示例 |
|---------|-------------|------|------|
| **SAST** (源码漏洞) | CODE_VULNERABILITY | 8 | SQL 注入、硬编码凭证、命令注入、资源泄漏 |
| **IaC** (基础设施) | CODE_VULNERABILITY | 10 | SG 0.0.0.0/0 开放、S3 无加密/无版本控制/公开访问 |
| **SCA** (依赖漏洞) | PACKAGE_VULNERABILITY | 44 | django 16 个 CVE、urllib3 12 个 CVE |

#### SAST 检测详情（源代码漏洞）

| 严重性 | 漏洞类型 | 文件 | 检测器 |
|--------|---------|------|--------|
| CRITICAL | CWE-798 硬编码凭证 (×2) | vulnerable_app.py | python/hardcoded-credentials@v1.0 |
| CRITICAL | CWE-94 代码注入 | vulnerable_app.py | python/code-injection@v1.0 |
| HIGH | CWE-89 SQL 注入 (×2) | vulnerable_app.py | python/sql-injection@v1.0 |
| HIGH | CWE-77/78/88 OS 命令注入 (×2) | vulnerable_app.py | python/os-command-injection@v1.0 |
| MEDIUM | CWE-400/664 资源泄漏 (×2) | vulnerable_app.py | python/resource-leak@v1.0 |

#### IaC 检测详情（基础设施配置）

| 严重性 | 问题 | 文件 | 检测器 |
|--------|------|------|--------|
| HIGH | Security Group 允许 0.0.0.0/0 SSH 访问 | insecure-sg.yaml | checkov-custom-restricted-ssh@v1.0 |
| HIGH | Security Group 允许不受限 TCP 入站 | insecure-sg.yaml | checkov-custom-restricted-ports@v1.0 |
| HIGH | S3 未启用 KMS 加密 | insecure-s3.yaml | checkov-custom-s3-default-encryption-kms@v1.0 |
| HIGH | S3 未启用 Object Lock | insecure-s3.yaml | cfn-custom-s3-default-lock-enabled@v1.0 |
| HIGH | S3 策略允许 HTTP 请求 | insecure-s3.yaml | checkov-custom-s3-bucket-ssl-request-only@v1.0 |
| HIGH | S3 未启用复制 | insecure-s3.yaml | checkov-custom-s3-bucket-replication-enabled@v1.0 |
| HIGH | S3 未启用版本控制 | insecure-s3.yaml | disabled-s3-versioning-cloudformation@v1.0 |
| MEDIUM | S3 未限制公共 Bucket | insecure-s3.yaml | s3-restr-public-false-cloudformation@v1.0 |
| MEDIUM | S3 未忽略公共 ACL | insecure-s3.yaml | s3-ignr-pubacls-false-cloudformation@v1.0 |

#### SCA 检测详情（依赖漏洞）

| 组件 | 漏洞数 | 关键 CVE | 严重性分布 |
|------|--------|---------|-----------|
| django@2.0 | 16 | CVE-2019-19844 (Critical, 密码重置) | 2C / 6H / 8M |
| urllib3@1.24.1 | 12 | CVE-2021-33503 (High, DoS) | 5H / 7M |
| jinja2@2.10 | 6 | CVE-2024-56326 (High, 沙箱逃逸) | 3H / 3M |
| requests@2.19.0 | 5 | CVE-2018-18074 (High, 凭证泄露) | 2H / 3M |
| pyyaml@5.1 | 3 | CVE-2020-14343 (Critical, 任意代码执行) | 3C |
| flask@1.0 | 2 | CVE-2023-30861 (High, Session 泄露) | 1H / 1M |

!!! tip "Log4Shell 与 SBOM 扫描"
    GitHub 仓库中的 `requirements.txt` 不包含 Java 依赖。如需检测 Log4Shell (CVE-2021-44228) 等跨语言漏洞，可使用 `inspector-scan scan-sbom` API 提交 CycloneDX 格式的 SBOM（见 Step 5）。

### SBOM 独立扫描结果

使用 `inspector-scan scan-sbom` API 对 6 个常用开源库的旧版本进行独立扫描（不依赖 GitHub 集成），共发现 **39 个漏洞**：

**按严重性分布**：

| 严重性 | 数量 | 占比 |
|--------|------|------|
| Critical | 5 | 12.8% |
| High | 14 | 35.9% |
| Medium | 20 | 51.3% |
| **总计** | **39** | 100% |

**按组件分布**：

| 组件 | 漏洞数 | 关键 CVE |
|------|--------|---------|
| django@2.0 | 16 | CVE-2019-19844 (Critical, 密码重置漏洞) |
| jinja2@2.10 | 6 | CVE-2025-27516 (High, 沙箱逃逸) |
| log4j-core@2.14.0 | 5 | CVE-2021-44228 (Critical, Log4Shell) |
| lodash@4.17.15 | 5 | CVE-2020-8203 (High, 原型污染) |
| requests@2.19.0 | 5 | CVE-2018-18074 (High, 凭证泄露) |
| flask@1.0 | 2 | CVE-2023-30861 (High, Session Cookie 泄露) |

!!! tip "Log4Shell 检测"
    Inspector 成功检测到 **CVE-2021-44228 (Log4Shell)** — 这是近年来影响最广的安全漏洞之一。扫描结果包含 CVSS 评分、修复版本建议和参考链接。

### 扫描配置动态调整测试

| 操作 | 结果 |
|------|------|
| 更新 ruleSetCategories 为 [SAST, IAC] | ✅ 成功，SCA 被移除 |
| 更新 frequency 为 MONTHLY | ✅ 成功，cron 自动调整 |
| 恢复为 [SAST, IAC, SCA] + WEEKLY | ✅ 成功 |
| 设置 ruleSetCategories 为 [] | ❌ ParamValidation 错误（最少 1 项） |
| 创建第二个默认配置 | ❌ ServiceQuotaExceededException |

## 踩坑记录

!!! warning "SCA 是独立的 ruleSetCategory"
    **实测发现**：CLI help 文档中 `ruleSetCategories` 只列出了 `SAST` 和 `IAC` 两个选项，但实际上 **`SCA` 是第三个有效选项**。Console 创建配置时默认勾选了 SAST、IAC 和 SCA 三项。如果只用 CLI 参照 help 信息，可能会遗漏 SCA。

!!! warning "每账户一个默认扫描配置"
    **已查文档确认**：当 `scopeSettings.projectSelectionScope` 设为 `ALL` 时，每个账户只能有一个扫描配置。尝试创建第二个会返回 `ServiceQuotaExceededException`。如果需要对不同仓库应用不同规则，需要使用 `batch-associate-code-security-scan-configuration` 逐仓库关联。

!!! warning "GitHub 集成需要浏览器 OAuth"
    **实测确认**：通过 CLI `create-code-security-integration` 创建 GitHub 集成后，返回的 `authorizationUrl` 需要在浏览器中打开并完成 GitHub 登录 + 授权。**无法用 GitHub PAT 完成此流程**，必须使用 GitHub 账户的用户名密码登录。建议通过 AWS Console 完成集成设置。

!!! warning "GitHub App 是 Region-specific"
    **实测发现，官方未明确记录**：Inspector 的 GitHub App 按 Region 区分。例如 us-east-1 的 app slug 为 `amazon-inspector-n-virginia`。如果你在多个 Region 使用 Code Security，需要在每个 Region 分别创建集成和授权。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Inspector CODE_REPOSITORY | 免费试用 15 天 | 1 天 | $0.00 |
| inspector-scan scan-sbom | 免费 | 数次 | $0.00 |
| **合计** | | | **$0.00** |

!!! info "定价说明"
    Amazon Inspector 提供 **15 天免费试用期**（包含 Code Security）。试用期后按扫描量计费，具体价格请参考 [Inspector 定价页面](https://aws.amazon.com/inspector/pricing/)。

## 清理资源

```bash
# 1. 删除扫描配置
aws inspector2 delete-code-security-scan-configuration \
  --scan-configuration-arn "YOUR_SCAN_CONFIG_ARN" \
  --region us-east-1

# 2. 删除 GitHub 集成
aws inspector2 delete-code-security-integration \
  --integration-arn "YOUR_INTEGRATION_ARN" \
  --region us-east-1

# 3. 禁用 CODE_REPOSITORY 扫描
aws inspector2 disable \
  --resource-types CODE_REPOSITORY \
  --region us-east-1

# 4. 验证已禁用
aws inspector2 batch-get-account-status \
  --account-ids $(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1 \
  --query 'accounts[0].resourceState.codeRepository.status'
```

!!! danger "务必清理"
    免费试用期结束后，Inspector 会按扫描量收费。如果不再使用 Code Security，请及时禁用 CODE_REPOSITORY 扫描类型。

## 结论与建议

### 适用场景

- ✅ **已经使用 Inspector 的团队**：Code Security 是现有 Inspector 的自然扩展，统一了从代码到运行时的漏洞管理
- ✅ **GitHub/GitLab 用户**：原生集成，Findings 直接在 PR 中展示，开发者无需切换工具
- ✅ **合规需求**：SAST + SCA + IaC 三合一，满足 SOC2、PCI-DSS 等合规审计需求
- ✅ **CI/CD 管道中的安全门控**：使用 `inspector-scan scan-sbom` 在管道中检查依赖漏洞

### 与现有工具对比

| 维度 | Inspector Code Security | GitHub Advanced Security | Snyk |
|------|------------------------|--------------------------|------|
| SAST | ✅ | ✅ (CodeQL) | ✅ |
| SCA | ✅ | ✅ (Dependabot) | ✅ |
| IaC | ✅ | ❌ | ✅ |
| 运行时扫描 | ✅ (EC2/ECR/Lambda) | ❌ | ✅ (容器) |
| AWS 原生集成 | ✅✅✅ | ❌ | ⚠️ |
| 统一管理视图 | ✅ (Organizations) | ❌ | ✅ |

### 生产环境建议

1. **先启用 SCA**：依赖漏洞是最常见的安全问题，ROI 最高
2. **PR 触发优先**：`supportedEvents: ["PULL_REQUEST"]` 在合并前拦截漏洞
3. **按需定期扫描**：`frequency: WEEKLY` 捕获新发布的 CVE
4. **结合 Security Hub**：将 Inspector Findings 推送到 Security Hub 统一管理
5. **注意 Region 覆盖**：Code Security 目前仅 10 个 Region，选择离你代码仓库最近的 Region

## 参考链接

- [Amazon Inspector Code Security 官方文档](https://docs.aws.amazon.com/inspector/latest/user/code-security-assessments.html)
- [Amazon Inspector 定价](https://aws.amazon.com/inspector/pricing/)
- [What's New: Amazon Inspector launches code security](https://aws.amazon.com/about-aws/whats-new/2025/06/amazon-inspector-code-security-shift-security-development/)
- [Inspector Code Security Region 可用性](https://docs.aws.amazon.com/inspector/latest/user/inspector_regions.html#ins-regional-feature-availability)
