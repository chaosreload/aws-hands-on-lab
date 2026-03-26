# Amazon Q Developer CLI Agent 实战：终端中的 AI 编程助手（现已演进为 Kiro CLI）

!!! info "Lab 信息"
    - **难度**: ⭐ 入门
    - **预估时间**: 30 分钟
    - **预估费用**: $0（Free tier）
    - **Region**: 所有 Q Developer 可用 Region
    - **最后验证**: 2026-03-26

## 背景

2025 年 3 月 6 日，AWS 宣布 Amazon Q Developer CLI 新增增强型 Agent 功能。这是 Q Developer 家族中首个将 agentic 能力带入命令行的产品——Agent 不再只是回答问题，而是能直接在你的终端中读写文件、执行命令、查询 AWS 资源，真正实现"从 prompt 到代码到部署"的全流程自动化。

!!! note "品牌演进说明"
    2025 年下半年，Amazon Q Developer CLI 已品牌升级为 **Kiro CLI**（[kiro.dev](https://kiro.dev/cli/)）。AWS 官方文档已完成重定向。本文基于原始公告功能进行验证，使用当前最新的 Kiro CLI v1.28.1 实测。核心功能一脉相承，命令从 `q` 变为 `kiro-cli`。

## 前置条件

- Linux 或 macOS 系统（Windows 需要 WSL）
- 网络连接（安装和认证需要）
- 浏览器访问权限（用于 Builder ID 认证，headless 环境需要 device code flow）

## 核心概念

### Q Developer CLI 做了什么？

在增强型 Agent 发布之前，Q Developer CLI 的能力主要是：

| 能力 | 原 Q CLI | 增强型 Agent（2025-03） |
|------|---------|----------------------|
| 命令补全 | ✅ S3 桶名、Git 分支等 | ✅ 保留 |
| 自然语言转 Shell | ✅ `q translate` | ✅ 保留 |
| 聊天问答 | ✅ 但只提供建议 | ✅ **可以直接执行** |
| 读写本地文件 | ❌ | ✅ |
| 执行系统命令 | ❌ | ✅ npm、git、aws cli 等 |
| 多轮对话 | ❌ | ✅ 保持上下文迭代 |
| 基于反馈迭代 | ❌ | ✅ 根据你的意见修改代码 |

### 关键技术架构

- **底层模型**：发布时使用 Claude 3.7 Sonnet（通过 Amazon Bedrock），当前版本支持多模型选择
- **工具使用**：Agent 可以调用你系统上安装的任何 CLI 工具——编译器、包管理器、AWS CLI、Docker、Git 等
- **权限控制**：默认需要用户确认每个操作，可通过 `--trust-all-tools` 跳过确认（适合可控环境）
- **认证方式**：Builder ID（Free tier）或 IAM Identity Center（Pro tier）

### Kiro CLI 定价（截至 2026-03）

| 套餐 | 价格 | Credits/月 | 适用场景 |
|------|------|-----------|---------|
| Free | $0 | 50 credits | 个人试用 |
| Pro | $20/月 | 1,000 credits | 日常开发 |
| Pro+ | $40/月 | 2,000 credits | 重度使用 |
| Power | $200/月 | 10,000 credits | 团队/企业 |

新注册可获 500 bonus credits（30 天内使用）。

## 动手实践

### Step 1: 安装 Kiro CLI

```bash
# 一键安装（macOS / Linux）
curl -fsSL https://cli.kiro.dev/install | bash
```

安装输出：

```
Kiro CLI installer:
Downloading package...
✓ Downloaded and extracted
✓ Package installed successfully
🎉 Installation complete! Happy coding!
```

将安装路径加入 PATH：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

验证安装：

```bash
kiro-cli --version
# kiro-cli 1.28.1
```

!!! tip "安装包组成"
    安装后 `~/.local/bin/` 下会出现三个二进制文件：
    
    - `kiro-cli`（~112MB）— 主程序
    - `kiro-cli-chat`（~369MB）— chat agent 引擎
    - `kiro-cli-term`（~81MB）— 终端 UI 组件

### Step 2: 认证登录

**方式一：有浏览器的环境**

```bash
kiro-cli login --license free
```

会自动打开浏览器，使用 Builder ID、Google 或 GitHub 账号登录。

**方式二：Headless / SSH 远程环境（Device Code Flow）**

```bash
kiro-cli login --license free --use-device-flow
```

输出示例：

```
Confirm the following code in the browser
Code: NDGD-CTNL
Open this URL: https://view.awsapps.com/start/#/device?user_code=NDGD-CTNL
```

在任何有浏览器的设备上打开该 URL，输入 code 确认即可。

!!! warning "踩坑记录"
    - headless 环境（如 EC2）必须使用 `--use-device-flow`，否则会尝试打开不存在的浏览器
    - 认证走的是 AWS SSO（view.awsapps.com），不是标准 AWS IAM
    - Device code 有效期约 5 分钟，超时需重新生成

**方式三：企业用户（IAM Identity Center）**

```bash
kiro-cli login --license pro \
  --identity-provider https://d-xxxxxxxxxx.awsapps.com/start \
  --region us-east-1
```

验证登录状态：

```bash
kiro-cli whoami
```

### Step 3: 探索 CLI 功能

**查看所有可用命令**：

```bash
kiro-cli --help-all
```

核心子命令：

| 命令 | 功能 |
|------|------|
| `kiro-cli chat` | 启动 AI 对话（核心功能） |
| `kiro-cli chat --tui` | 使用新的 TUI 界面 |
| `kiro-cli translate` | 自然语言转 Shell 命令 |
| `kiro-cli agent list` | 列出可用的 agent |
| `kiro-cli agent create` | 创建自定义 agent |
| `kiro-cli mcp add` | 添加 MCP server |
| `kiro-cli doctor` | 诊断安装问题 |
| `kiro-cli settings` | 自定义外观和行为 |

### Step 4: 启动 Agent Chat

```bash
# 基础启动
kiro-cli chat

# 带初始问题启动
kiro-cli chat "创建一个 Python Flask hello world 应用"

# 使用特定 agent
kiro-cli chat --agent my-agent

# 恢复上次对话
kiro-cli chat --resume

# 信任所有工具（跳过确认提示）
kiro-cli chat --trust-all-tools
```

### Step 5: Agent 核心能力演示

以下是 Agent 的核心工作流程示例（来自官方 blog 验证）：

**示例 1：项目脚手架**

```
> 用 React 和 Vite 创建一个新项目 call-for-content，然后提交到 Git
```

Agent 会依次执行：

1. `npm create vite@latest call-for-content -- --template react`
2. `cd call-for-content && npm install`
3. `git init && git add . && git commit -m "Initial scaffold with React + Vite"`

**示例 2：查询 AWS 资源**

```
> 列出我的 S3 桶，并告诉我哪些桶开启了版本控制
```

Agent 会调用 `aws s3api list-buckets` 和 `aws s3api get-bucket-versioning`。

**示例 3：多轮迭代开发**

```
> 给这个应用添加一个提交表单页面
# ... Agent 生成代码 ...
> 表单标题颜色改成蓝色
# ... Agent 修改 CSS ...
> 运行开发服务器
# ... Agent 执行 npm run dev ...
```

### Step 6: 创建自定义 Agent

```bash
# 创建一个自定义 agent
kiro-cli agent create my-aws-helper

# 从模板创建
kiro-cli agent create aws-reviewer --from default

# 查看已有 agent
kiro-cli agent list
```

### Step 7: 配置 MCP Server

```bash
# 添加 MCP server
kiro-cli mcp add my-server --command "npx @aws/mcp-server-bedrock"

# 查看已配置的 server
kiro-cli mcp list

# 检查 server 状态
kiro-cli mcp status
```

## 测试结果

| 测试项 | 结果 | 备注 |
|--------|------|------|
| Linux 安装 | ✅ 成功 | Ubuntu 22.04 x86_64, 一键脚本 |
| 版本确认 | ✅ v1.28.1 | 三个二进制文件总计 ~562MB |
| Device code 认证 | ✅ 可用 | headless 环境必选 |
| CLI 子命令体系 | ✅ 丰富 | 29 个子命令覆盖 chat/agent/mcp/acp |
| Chat 选项 | ✅ 灵活 | 支持 resume、agent 切换、模型选择、TUI |
| Trust 控制 | ✅ 细粒度 | `--trust-all-tools` 或 `--trust-tools=fs_read,fs_write` |

## 踩坑记录

!!! warning "实测踩坑"

    **1. 命令名变更（已查文档确认）**
    
    原 Q Developer CLI 使用 `q` 命令，现在改为 `kiro-cli`。所有网上早期教程中的 `q chat` 需要替换为 `kiro-cli chat`。
    
    **2. PATH 需要手动配置**
    
    安装脚本不会自动修改 shell 配置文件，需要手动添加 `$HOME/.local/bin` 到 PATH。
    
    **3. headless 认证陷阱**
    
    不加 `--use-device-flow` 时，CLI 会尝试打开本地浏览器，在 SSH 环境下会静默失败。这个行为在文档中有说明但不够显眼。
    
    **4. 安装体积较大**
    
    三个二进制文件合计约 562MB。在带宽受限的环境下需要注意。

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| Kiro CLI Free tier | $0 | 50 credits/月 | $0 |
| AWS 资源查询 | $0 | 只读 API 调用 | $0 |
| **合计** | | | **$0** |

## 清理资源

```bash
# Kiro CLI 是本地工具，无需清理 AWS 资源
# 如需卸载 CLI：
rm -f ~/.local/bin/kiro-cli ~/.local/bin/kiro-cli-chat ~/.local/bin/kiro-cli-term
rm -rf ~/.kiro

# 清理测试文件
rm -rf /tmp/kiro-test
```

## 结论与建议

### 核心价值

Amazon Q Developer CLI Agent（现 Kiro CLI）的发布标志着 AI 编程助手从"建议者"到"执行者"的关键转变：

1. **无需离开终端**：对于偏爱 CLI 的开发者，这消除了在 IDE 和终端之间切换的摩擦
2. **工具链整合**：直接调用系统上的 git、npm、aws cli、docker 等，不需要额外适配
3. **安全的权限模型**：默认需要确认每个操作，`--trust-tools` 支持细粒度工具白名单

### 适用场景

- **快速原型开发**：用自然语言描述需求，Agent 自动搭建项目脚手架
- **AWS 资源管理**：询问 Agent 帮你查询、分析 AWS 资源状态
- **调试排错**：粘贴错误信息，Agent 分析并执行修复命令
- **多轮迭代**：在对话中逐步完善代码，无需手动复制粘贴

### 生产环境建议

- 不要在生产环境使用 `--trust-all-tools`，应该使用 `--trust-tools` 指定允许的工具
- 企业环境建议使用 IAM Identity Center（Pro tier）而非 Builder ID
- 搭配 [Steering files](https://kiro.dev/docs/cli/steering/) 配置 Agent 行为约束
- 考虑使用 [Custom Agents](https://kiro.dev/docs/cli/custom-agents/) 创建团队统一的 agent 配置

## 参考链接

- [AWS What's New 公告](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-q-developer-cli-agent-command-line/)
- [AWS DevOps Blog：Introducing the Enhanced CLI in Amazon Q Developer](https://aws.amazon.com/blogs/devops/introducing-the-enhanced-command-line-interface-in-amazon-q-developer/)
- [Kiro CLI 官方文档](https://kiro.dev/docs/cli/)
- [Kiro CLI 定价](https://kiro.dev/pricing/)
- [Q Developer Region 可用性](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/regions.html)
