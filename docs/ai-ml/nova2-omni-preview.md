# Amazon Nova 2 Omni Preview — 多模态推理 + 图像生成 All-in-One（受限预览）

!!! info "Lab 信息"
    - **难度**: N/A（受限预览，无法实操）
    - **预估时间**: N/A
    - **预估费用**: N/A
    - **Region**: 未公开
    - **最后验证**: 2026-03-27

## 背景

2025 年 12 月，AWS 发布了 **Amazon Nova 2 Omni**，定位为业界首个同时支持**文本 / 图像 / 视频 / 语音输入**以及**文本 + 图像输出**的推理模型。它与已 GA 的 Nova 2 Sonic（实时语音对话）形成互补——Sonic 专注 speech-to-speech，Omni 则是"什么都能进、什么都能出"的全模态模型。

核心亮点：

- **1M token 上下文窗口**
- **200+ 语言**文本处理，**10 语言**语音输入
- **图像生成与编辑**：角色一致性、图片内文字渲染、自然语言编辑指令
- **多说话人转录**
- **推理能力**：extended thinking 支持

!!! abstract "来源"
    [Amazon Nova 2 Omni is now available in preview](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-nova-2-omni-preview/)

## 为什么无法测试

Nova 2 Omni 目前处于 **Nova Forge 客户限定预览**，不对标准 Bedrock API 用户开放。我们通过以下三种方式确认了这一点：

### 1. 公告原文明确说明

> "Nova 2 Omni is in preview with early access available to all **Nova Forge customers**. Please reach out to your **AWS account team** for access."

Nova Forge 是 AWS 为大客户提供的定制化模型训练/部署通道，普通 AWS 账号无法自助开通。

### 2. Bedrock API 不可见

```bash
aws bedrock list-foundation-models --region us-east-1 \
  --query "modelSummaries[?contains(modelId, 'nova')].[modelId, modelName]" \
  --output table
```

返回的 Nova 2 系列模型只有：

| 模型 ID | 状态 |
|---------|------|
| `amazon.nova-2-lite-v1:0` | ✅ GA |
| `amazon.nova-2-sonic-v1:0` | ✅ GA |
| `amazon.nova-multimodal-embeddings-v1:0` | ✅ GA |
| `amazon.nova-2-omni-*` | ❌ **不存在** |

### 3. 官方文档未收录

`docs.aws.amazon.com/nova/latest/nova2-userguide/what-is-nova-2.html` 的模型列表中同样没有 Omni。

## Nova 2 Omni vs Nova 2 Sonic

既然无法实测 Omni，这里整理一下它与已 GA 的 Sonic 的定位差异，方便后续 GA 时快速上手：

| 维度 | Nova 2 Sonic（已 GA） | Nova 2 Omni（Preview） |
|------|----------------------|----------------------|
| **定位** | 实时对话 AI（speech-to-speech） | All-in-one 多模态推理 + 图像生成 |
| **输入** | 语音、文本 | 文本、图像、视频、语音 |
| **输出** | 语音、文本 | 文本、图像 |
| **核心能力** | 实时双向语音对话 | 多模态理解 + 图像生成/编辑 + 语音转录 |
| **独特特性** | Polyglot voices、async tool calling | 角色一致性图像、图片内文字渲染 |
| **语言** | 7 语言语音 | 200+ 语言文本、10 语言语音 |

## 后续计划

- 当 Nova 2 Omni 进入 **GA 或公开预览**后，第一时间进行实测
- 重点验证方向：图像生成质量 vs DALL-E/Stable Diffusion、多模态推理准确性、extended thinking 效果
- 关注 [nova.amazon.com](https://nova.amazon.com) 和 Bedrock 控制台更新

## 总结

Nova 2 Omni 在概念上很有吸引力——一个模型覆盖 text/image/video/audio 的输入输出，加上 1M 上下文和推理能力。但目前仅限 Nova Forge 客户预览，标准 Bedrock 用户无法访问。等 GA 后我们会补上完整的 Hands-on Lab。
