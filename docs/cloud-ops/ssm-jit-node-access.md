# AWS Systems Manager Just-in-Time Node Access：零常驻权限的节点访问管理

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 60 分钟
    - **预估费用**: < $2.00（含清理）
    - **Region**: us-east-1（16 个 Region 可用）
    - **最后验证**: 2026-03-28

## 背景

在企业环境中，运维人员通常拥有对服务器的长期 SSH/RDP 访问权限。这种「常驻权限」模式带来两个问题：

1. **权限膨胀** — 即使不需要访问服务器，权限始终存在
2. **审计困难** — 很难回溯「谁在什么时候因为什么原因访问了哪台服务器」

2025 年 4 月 29 日，AWS Systems Manager 发布了 **Just-in-Time (JIT) Node Access**，让运维人员在需要连接节点时提交访问请求，经审批后获得时间窗口内的临时访问权限。目标：**零常驻权限（Zero Standing Privileges）**。

## 前置条件

- AWS Organizations（需要 management account + 至少一个 member account）
- AWS CLI v2 已配置
- Session Manager Plugin 已安装
- SSM unified console 已通过控制台完成初始设置
- IAM 管理员权限

!!! warning "重要前置条件"
    JIT Node Access 必须先完成 **SSM unified console** 设置，这是通过 AWS 控制台完成的组织级配置。纯 CLI 无法完成此步骤——详见踩坑记录。

## 核心概念

### JIT vs 传统 Session Manager

| 维度 | Session Manager | JIT Node Access |
|------|----------------|-----------------|
| 权限模型 | IAM 策略持续授权 | 按需申请，策略审批 |
| 访问控制 | `ssm:StartSession` | `ssm:StartAccessRequest` + 审批策略 |
| 审批流程 | 无 | 自动/手动/拒绝三种策略 |
| 访问时间 | 无限制 | 时间窗口限定 |
| 审计追踪 | CloudTrail | CloudTrail + Access Request 保留 1 年 |
| RDP 录制 | 不支持 | 支持录制到 S3 |

### 三种审批策略

JIT Node Access 通过 **审批策略（Approval Policies）** 控制访问，按以下优先级评估：

```
① Deny-access（最高优先级）→ ② Auto-approval → ③ Manual approval
```

- **Auto-approval**：符合条件自动批准，适合低敏感节点（如开发/测试环境）
- **Manual approval**：需要指定审批人批准，适合高敏感节点（如数据库服务器）
- **Deny-access**：显式拒绝，全 Organization 生效，覆盖 auto-approval

!!! tip "策略设计建议"
    组合使用：对 presentation tier 设置 auto-approval，对 database tier 设置 manual approval，对 production 的关键基础设施设置 deny-access 防止自动审批。

### CLI 访问流程（4 步）

```
start-access-request → (等待审批) → get-access-token → export AWS_SESSION_TOKEN → start-session
```

## 动手实践

### Step 1: 设置 SSM Unified Console（控制台操作）

JIT Node Access 的前置条件是完成 SSM unified console 设置。这必须通过 AWS 控制台完成：

1. 登录 **management account** 的 AWS 控制台
2. 打开 **Systems Manager** → **Settings**
3. 选择 **Get started** 开始设置
4. 指定 **delegated administrator account**
5. 选择目标 **OUs** 和 **Regions**
6. 完成设置（系统会通过 CloudFormation StackSets 部署必要资源）

!!! warning "不支持纯 CLI 设置"
    SSM QuickSetup API（`create-configuration-manager`）在新建的 Organization 上会持续报 `Invalid target OU` 错误。这可能需要通过控制台完成初始化后才能使用 CLI。

### Step 2: 启用 JIT Node Access

完成 unified console 设置后，在 SSM 控制台启用 JIT：

1. 在 **Settings** 中选择 **Just-in-time node access** 标签
2. 选择目标 OUs 和 Regions（必须是 unified console 已覆盖的子集）
3. 启用 JIT Node Access

### Step 3: 创建审批策略

创建一条 auto-approval 策略（适合测试）：

1. 在 SSM 控制台 → **Approval policies**
2. 创建策略：
    - **类型**: Auto-approval
    - **目标**: 按标签匹配节点（如 `Environment=test`）
    - **访问时间窗口**: 例如 1 小时
3. 保存策略

### Step 4: 准备测试节点

```bash
# 创建 IAM 角色（EC2 需要 SSM 管理权限）
aws iam create-role \
    --role-name SSM-JIT-TestRole \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --region us-east-1

aws iam attach-role-policy \
    --role-name SSM-JIT-TestRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name SSM-JIT-TestProfile
aws iam add-role-to-instance-profile \
    --instance-profile-name SSM-JIT-TestProfile \
    --role-name SSM-JIT-TestRole

# 等待 IAM 传播
sleep 10

# 启动 EC2 实例
aws ec2 run-instances \
    --image-id $(aws ssm get-parameter \
        --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" \
        --query "Parameter.Value" --output text --region us-east-1) \
    --instance-type t3.micro \
    --iam-instance-profile Name=SSM-JIT-TestProfile \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=jit-test-node},{Key=Environment,Value=test}]' \
    --metadata-options HttpTokens=required \
    --count 1 \
    --region us-east-1
```

验证 SSM Agent 上线：

```bash
aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=<INSTANCE_ID>" \
    --region us-east-1
```

预期输出中 `PingStatus` 应为 `Online`。

### Step 5: 配置 IAM 权限迁移

将用户从 Session Manager 权限迁移到 JIT 权限：

**迁移前（Session Manager）：**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:StartSession",
                "ssm:ResumeSession",
                "ssm:TerminateSession"
            ],
            "Resource": "*"
        }
    ]
}
```

**迁移后（JIT Node Access）：**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:StartAccessRequest",
                "ssm:GetAccessToken",
                "ssm:ResumeSession",
                "ssm:TerminateSession"
            ],
            "Resource": "*"
        }
    ]
}
```

关键变更：移除 `ssm:StartSession`，新增 `ssm:StartAccessRequest` + `ssm:GetAccessToken`。

### Step 6: 通过 CLI 发起 JIT 连接

```bash
# 1. 发起访问请求
aws ssm start-access-request \
    --targets Key=InstanceIds,Values=<INSTANCE_ID> \
    --reason "Troubleshooting networking issue" \
    --region us-east-1

# 如果 auto-approval 策略匹配，直接返回；否则进入手动审批流程
# 记录返回的 access-request-id (格式: oi-xxxxxxxxxxxx)

# 2. 获取临时访问令牌
aws ssm get-access-token \
    --access-request-id <ACCESS_REQUEST_ID> \
    --region us-east-1

# 3. 设置临时凭证
export AWS_SESSION_TOKEN=<TOKEN_FROM_STEP_2>

# 4. 建立会话
aws ssm start-session \
    --target <INSTANCE_ID> \
    --region us-east-1
```

### Step 7: 验证基线对比

```bash
# 验证：移除 StartSession 权限后直连失败
aws ssm start-session \
    --target <INSTANCE_ID> \
    --region us-east-1
# 预期: AccessDeniedException（无 ssm:StartSession 权限）

# 验证：未启用 JIT 时 start-access-request 报错
aws ssm start-access-request \
    --targets Key=InstanceIds,Values=<INSTANCE_ID> \
    --reason "Test" \
    --region us-east-1
# 预期: "Just-In-Time Node Access service is not enabled in this account."
```

## 测试结果

### 功能验证汇总

| 测试项 | 结果 | 说明 |
|--------|------|------|
| EC2 实例 SSM Agent 注册 | ✅ 通过 | Agent v3.3.3598.0，PingStatus: Online |
| RunCommand 基线验证 | ✅ 通过 | `AWS-RunShellScript` 成功执行 |
| Session Manager 基线连接 | ✅ 通过 | 传统直连正常工作 |
| JIT API 可用性 | ✅ 通过 | `start-access-request`、`get-access-token` CLI 命令存在 |
| JIT 未启用时的错误提示 | ✅ 通过 | 明确提示 "service is not enabled" |
| access-request-id 格式 | ✅ 验证 | `^(oi)-[0-9a-f]{12}$` |

### 审批策略优先级

| 优先级 | 策略类型 | 作用范围 |
|--------|---------|---------|
| 1（最高）| Deny-access | 全 Organization |
| 2 | Auto-approval | 当前账号 + Region |
| 3 | Manual approval | 当前账号 + Region |

### 关键限制

| 限制 | 详情 |
|------|------|
| 前置条件 | 必须先设置 SSM unified console（控制台操作） |
| 认证方式 | 仅支持 STS AssumeRole 临时凭证 |
| Windows RDP | 不支持 SSO auth type 连接 |
| 跨账号 | 不支持跨账号/Region 请求 |
| Tag 冲突 | 多策略 overlap 同一 tag → conflict → 无法请求 |
| 会话终止 | 不自动终止（需配置 max duration + idle timeout） |
| 数据保留 | Access request 保留 1 年 |

## 踩坑记录

!!! warning "踩坑 1: QuickSetup CLI 无法在新建 Organization 上运行"
    **现象**：在新创建的 AWS Organization 上，所有 `ssm-quicksetup create-configuration-manager` 调用均报 `Invalid target OU`，无论使用 root、OU、还是 delegated admin 账号执行。

    **验证范围**：测试了 5 种 QuickSetup 类型（JITNA、SSM、DHMC、SSMHostMgmt、SSMOpsCenter），均报同样错误。

    **可能原因**：SSM QuickSetup 依赖 CloudFormation StackSets 的组织级部署能力，新 Organization 可能需要更长的传播时间，或者需要先通过 Console 完成初始化。

    **建议**：使用已有的 Organization 环境设置 SSM unified console，或通过 AWS 控制台完成初始设置后再使用 CLI。

    *实测发现，官方未记录。*

!!! warning "踩坑 2: Unified Console 是硬性前置条件"
    **现象**：直接调用 `ssm start-access-request` 返回 `Just-In-Time Node Access service is not enabled in this account.`

    **原因**：JIT Node Access 需要先通过 SSM unified console 启用，这是通过控制台进行的组织级设置。

    *已查文档确认 — FAQ 明确说明 unified console 是前置条件。*

!!! warning "踩坑 3: Tag 冲突导致访问完全阻断"
    如果一个节点有多个 tag，且不同 tag 匹配了不同的 manual approval 策略，会产生冲突——用户将无法请求访问该节点。创建审批策略时需仔细规划 tag 策略。

    *已查文档确认 — FAQ 明确说明 tag overlap 导致 conflict。*

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| EC2 t3.micro (us-east-1) | $0.0104/hr | 2 hr | $0.02 |
| SSM JIT Node Access | 30 天免费试用 | - | $0.00 |
| S3 (会话录制) | $0.023/GB | ~1 MB | < $0.01 |
| **合计** | | | **< $0.10** |

!!! note "JIT 定价"
    AWS Systems Manager 提供 JIT Node Access 30 天免费试用。试用期后产生费用，详见 [SSM 定价页面](https://aws.amazon.com/systems-manager/pricing/)。

## 清理资源

```bash
# 1. 终止 EC2 实例
aws ec2 terminate-instances \
    --instance-ids <INSTANCE_ID> \
    --region us-east-1

# 2. 等待实例终止
aws ec2 wait instance-terminated \
    --instance-ids <INSTANCE_ID> \
    --region us-east-1

# 3. 清理 IAM 资源
aws iam remove-role-from-instance-profile \
    --instance-profile-name SSM-JIT-TestProfile \
    --role-name SSM-JIT-TestRole

aws iam delete-instance-profile \
    --instance-profile-name SSM-JIT-TestProfile

aws iam detach-role-policy \
    --role-name SSM-JIT-TestRole \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam delete-role --role-name SSM-JIT-TestRole

# 4. 禁用 JIT Node Access（控制台操作）
# SSM Console → Settings → Just-in-time node access → Disable

# 5. 如果是测试用 Organization，清理 Organization 资源
# 注意：删除 Organization 前需先移除所有 member accounts
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免产生意外费用。特别注意 JIT 免费试用到期后的计费。

## 结论与建议

### 适用场景

- **合规要求严格的环境**：金融、医疗等需要证明「最小权限」的行业
- **多团队共享基础设施**：不同团队对不同节点有不同访问需求
- **安全事件响应**：紧急访问需要走审批 + 留痕
- **从 Session Manager 平滑过渡**：启用 JIT 不影响现有 Session Manager 配置

### 生产环境建议

1. **必须配置 session preferences**：设置 `max session duration` 和 `idle timeout`，JIT 不会自动终止会话
2. **分阶段迁移**：先在测试环境验证审批策略，再逐步推到生产
3. **合理设计 tag 策略**：避免 tag overlap 导致策略冲突
4. **配合 EventBridge 使用**：监控 failed access requests 和审批状态变化
5. **启用 RDP 录制**：对 Windows Server 节点启用 S3 会话录制满足合规需求
6. **利用 Amazon Q Developer 集成**：通过 Slack/Teams 接收审批通知

### 局限性

- 需要 AWS Organizations + unified console（单账号玩不转）
- 不支持跨账号/Region 请求
- 当前仅支持 STS AssumeRole 临时凭证

## 参考链接

- [AWS What's New: SSM JIT Node Access](https://aws.amazon.com/about-aws/whats-new/2025/04/aws-systems-manager-just-in-time-node-access/)
- [官方文档: Just-in-Time Node Access](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-just-in-time-node-access.html)
- [设置指南](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-just-in-time-node-access-setting-up.html)
- [从 Session Manager 迁移](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-just-in-time-node-access-moving-from-session-manager.html)
- [FAQ](https://docs.aws.amazon.com/systems-manager/latest/userguide/just-in-time-node-access-faq.html)
- [SSM 定价](https://aws.amazon.com/systems-manager/pricing/)
