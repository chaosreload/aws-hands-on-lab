---
description: "Deploy a real-time voice agent on AWS Bedrock AgentCore Runtime using WebRTC protocol with KVS TURN relay and Nova Sonic."
tags:
  - AgentCore
  - Streaming
  - What's New
---
# AgentCore WebRTC 双向流：KVS TURN 实时语音 Agent 实战

!!! info "Lab 信息"
    - **难度**: ⭐⭐ 中级
    - **预估时间**: 45 分钟
    - **预估费用**: $3-5（含清理）
    - **Region**: us-east-1
    - **最后验证**: 2026-03-21

## 背景

Amazon Bedrock AgentCore Runtime 新增了 **WebRTC 协议支持**，为双向实时流（Bidirectional Streaming）提供了 WebSocket 之外的第二种选择。

WebRTC 使用 **UDP 传输**，在浏览器和移动端有原生 API 支持，是构建实时语音 Agent 的理想协议。但它也带来了额外的基础设施需求：**VPC 网络模式** + **TURN relay**。

本文将从零开始，部署一个基于 WebRTC 的语音 Agent 到 AgentCore Runtime，使用 Amazon Kinesis Video Streams（KVS）管理的 TURN 服务器进行媒体中继，并结合 Nova Sonic 实现实时语音对话。

## 前置条件

- AWS 账号，已配置 AWS CLI v2
- IAM 权限：`BedrockAgentCoreFullAccess`（或等效自定义策略）
- Python 3.10+（仅用于安装 agentcore CLI；Agent 本身在 Python 3.12 容器中运行）
- Git

## 核心概念

### WebSocket vs WebRTC：两种双向流协议

AgentCore Runtime 支持两种双向流协议，适用于不同场景：

| 维度 | WebSocket | WebRTC |
|------|-----------|--------|
| 传输层 | TCP | **UDP** |
| 适用场景 | 文本 + 音频流 | **实时音视频** |
| 延迟特性 | 可靠但延迟较高 | **低延迟**（容忍丢包） |
| 额外基础设施 | 无 | TURN relay + VPC 模式 |
| 认证方式 | SigV4 / OAuth 2.0 | 通过 TURN credentials |
| 客户端支持 | 需要 SDK | **浏览器原生 API** |

**选择建议**：如果你的 Agent 主要做文本对话或不需要极低延迟，WebSocket 更简单。如果是浏览器/移动端的实时语音场景，WebRTC 是更好的选择。

### WebRTC on AgentCore 的两个硬性要求

1. **VPC 网络模式**：AgentCore Runtime 必须配置 VPC 网络模式（PUBLIC 模式不支持 outbound UDP）
2. **TURN relay**：三种选项可选：

| TURN 选项 | 运维成本 | 适用场景 |
|-----------|---------|---------|
| **Amazon KVS managed TURN**（推荐） | 免运维，IAM 集成 | 大多数场景 |
| 第三方 managed TURN | 低 | 已有 TURN 供应商 |
| 自建 TURN（coturn） | 高 | 需要完全控制 |

### 架构概览

```
Browser (WebRTC API)
    ↕ UDP/TURN
KVS TURN Relay Server
    ↕ UDP/TURN
AgentCore Runtime (VPC 私有子网)
    → ENI → NAT Gateway → IGW → KVS TURN endpoints
    → Bedrock Nova Sonic (bidirectional stream)
```

**连接流程**（4 步）：

1. Client 调用 Agent 获取 KVS TURN credentials 和 ICE server 配置
2. Client 创建 WebRTC offer → Agent 创建 peer connection → 返回 answer
3. Client 和 Agent 交换 ICE candidates，通过 TURN server 建立连接
4. 连接建立后，Client 实时发送麦克风音频 → Agent 转发给 Nova Sonic → 语音回复流回 Client

## 动手实践

### Step 1: 创建 VPC 网络环境

AgentCore WebRTC Agent 需要一个带 NAT Gateway 的 VPC（私有子网通过 NAT Gateway 访问 KVS TURN endpoints）。

!!! warning "重要"
    AgentCore 使用**私有子网**创建 ENI，公共子网不提供 internet 连接。必须通过 NAT Gateway 路由出站流量。

```bash
export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=us-east-1

# 创建 VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=webrtc-agent-vpc}]' \
  --query 'Vpc.VpcId' --output text)

aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}'

# 创建 Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=webrtc-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID

# 创建公共子网（放 NAT Gateway）
PUB_SUBNET=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID --cidr-block 10.0.1.0/24 --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=webrtc-public}]' \
  --query 'Subnet.SubnetId' --output text)

# 创建私有子网（放 AgentCore ENI）
PRIV_SUBNET=$(aws ec2 create-subnet \
  --vpc-id $VPC_ID --cidr-block 10.0.2.0/24 --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=webrtc-private}]' \
  --query 'Subnet.SubnetId' --output text)

# 创建 NAT Gateway
EIP_ID=$(aws ec2 allocate-address --domain vpc \
  --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=webrtc-nat-eip}]' \
  --query 'AllocationId' --output text)

NAT_ID=$(aws ec2 create-nat-gateway \
  --subnet-id $PUB_SUBNET --allocation-id $EIP_ID \
  --tag-specifications 'ResourceType=natgateway,Tags=[{Key=Name,Value=webrtc-nat}]' \
  --query 'NatGateway.NatGatewayId' --output text)

echo "等待 NAT Gateway 就绪..."
aws ec2 wait nat-gateway-available --nat-gateway-ids $NAT_ID

# 配置路由表
# 公共子网 → IGW
PUB_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=webrtc-public-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PUB_RT --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID
aws ec2 associate-route-table --route-table-id $PUB_RT --subnet-id $PUB_SUBNET

# 私有子网 → NAT Gateway
PRIV_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=webrtc-private-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PRIV_RT --destination-cidr-block 0.0.0.0/0 --nat-gateway-id $NAT_ID
aws ec2 associate-route-table --route-table-id $PRIV_RT --subnet-id $PRIV_SUBNET

# 创建安全组
SG_ID=$(aws ec2 create-security-group \
  --group-name webrtc-agent-sg \
  --description "WebRTC agent - allow all outbound for TURN" \
  --vpc-id $VPC_ID \
  --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=webrtc-agent-sg}]' \
  --query 'GroupId' --output text)

echo "VPC=$VPC_ID  PRIV_SUBNET=$PRIV_SUBNET  SG=$SG_ID"
```

记下 `PRIV_SUBNET` 和 `SG_ID`，后续部署 Agent 时需要。

### Step 2: 克隆示例代码

```bash
git clone --depth 1 https://github.com/awslabs/amazon-bedrock-agentcore-samples.git
cd amazon-bedrock-agentcore-samples/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming-webrtc
```

项目结构：

```
agent/
  bot.py           # FastAPI 服务，WebRTC offer/answer、ICE 处理
  kvs.py           # KVS signaling channel 和 TURN server 管理
  audio.py         # 音频重采样（av）和 WebRTC 输出 track
  nova_sonic.py    # Nova Sonic 双向流 session
  requirements.txt
  Dockerfile
server/
  index.html       # 浏览器客户端（WebRTC + AgentCore Runtime 调用）
  server.py        # 静态文件服务
```

!!! tip "Docker Hub 限速问题"
    如果遇到 Docker Hub 429 Too Many Requests，修改 `agent/Dockerfile` 第一行：
    ```dockerfile
    # 改前
    FROM python:3.12-slim
    # 改后
    FROM public.ecr.aws/docker/library/python:3.12-slim
    ```

### Step 3: 配置并部署 Agent

```bash
pip install bedrock-agentcore-starter-toolkit

cd agent

# 配置 AgentCore（VPC 模式）
agentcore configure \
  -e bot.py \
  --deployment-type container \
  --disable-memory \
  --vpc \
  --subnets $PRIV_SUBNET \
  --security-groups $SG_ID \
  --non-interactive

# 部署到 AgentCore Runtime（CodeBuild 远程构建 ARM64 镜像）
agentcore deploy \
  --env KVS_CHANNEL_NAME=voice-agent-webrtc \
  --env AWS_REGION=us-east-1
```

部署过程约 1-2 分钟（CodeBuild 构建 + Agent Runtime 创建）。完成后记下输出的 Agent ARN。

### Step 4: 附加 IAM 权限

Agent 的执行角色需要 KVS 和 Bedrock 权限：

```bash
# 从 agentcore status 或部署输出中获取角色名
ROLE_NAME=AmazonBedrockAgentCoreSDKRuntime-us-east-1-xxxxxxxxxx

# KVS 权限（TURN server 访问）
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name kvs-access \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "kinesisvideo:DescribeSignalingChannel",
        "kinesisvideo:CreateSignalingChannel",
        "kinesisvideo:GetSignalingChannelEndpoint",
        "kinesisvideo:GetIceServerConfig"
      ],
      "Resource": "arn:aws:kinesisvideo:us-east-1:*:channel/*"
    }]
  }'

# Bedrock Nova Sonic 权限
aws iam put-role-policy \
  --role-name $ROLE_NAME \
  --policy-name bedrock-nova-sonic \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "bedrock:InvokeModelWithBidirectionalStream",
      "Resource": "arn:aws:bedrock:us-east-1:*:foundation-model/*"
    }]
  }'
```

### Step 5: 验证 Agent 部署

```bash
# 检查 Agent 状态
AGENT_ID=your-agent-id  # 从部署输出获取
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id $AGENT_ID \
  --region us-east-1 \
  --query '{status:status,arn:agentRuntimeArn}'
```

输出应显示 `"status": "READY"`。

通过 CLI 测试 ICE config 获取：

```bash
AGENT_ARN="arn:aws:bedrock-agentcore:us-east-1:ACCOUNT_ID:runtime/$AGENT_ID"
SESSION_ID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$AGENT_ARN" \
  --runtime-session-id "$SESSION_ID" \
  --content-type "application/json" \
  --accept "application/json" \
  --payload $(echo -n '{"action":"ice_config"}' | base64) \
  --region us-east-1 \
  /tmp/ice-response.json

cat /tmp/ice-response.json | python3 -m json.tool
```

成功响应会返回 KVS TURN server 列表，包含 TURN/TURNS 的 UDP 和 TCP URL。

### Step 6: 浏览器测试语音对话

启动本地静态文件服务器：

```bash
cd ../server
pip install -r requirements.txt
python server.py  # http://localhost:7860
```

打开浏览器访问 `http://localhost:7860`：

1. 在 **Agent Runtime ARN** 输入框中填入 Agent ARN
2. 填入 AWS Access Key / Secret Key（需要 `bedrock-agentcore:InvokeAgentRuntime` 权限）
3. 点击 **Connect**
4. 授权麦克风访问
5. 开始说话 — Agent 会通过 Nova Sonic 实时语音回复

## 测试结果

### AgentCore Runtime 调用延迟

| 指标 | 冷启动（新 Session） | 热调用（同 Session） |
|------|---------------------|---------------------|
| 样本数 | 10 | 4 |
| 平均延迟 | **8,747ms** | **1,394ms** |
| 最小延迟 | 8,672ms | 1,380ms |
| 最大延迟 | 8,855ms | 1,410ms |

- **冷启动 ~8.7s**：包含容器初始化 + VPC ENI 分配，是 VPC 模式的固定开销
- **热调用 ~1.4s**：容器已就绪后，单次 API 调用延迟。包含 KVS GetIceServerConfig API 调用时间

!!! note "冷启动优化"
    实际语音对话场景中，冷启动只发生在第一次连接。后续的 WebRTC 音频帧传输走的是 UDP 直连（通过 TURN），不经过 AgentCore invoke API，延迟远低于上表数字。

### KVS TURN Server 配置

| 参数 | 值 |
|------|-----|
| TURN 服务器数量 | 2（高可用） |
| 每个服务器 URL 数 | 3（TURN UDP / TURNS UDP / TURNS TCP） |
| Credential TTL | **300 秒（5 分钟）** |
| 端口 | 443（统一，防火墙友好） |

### 音频参数

| 参数 | 值 |
|------|-----|
| 输入采样率 | 16kHz |
| 输出采样率 | 24kHz |
| 格式 | 16-bit PCM mono |
| 模型 | amazon.nova-2-sonic-v1:0 |
| 语音 | matthew |
| WebRTC 帧大小 | 20ms |

## 踩坑记录

!!! warning "踩坑 1: Docker Hub Rate Limit"
    CodeBuild 默认从 docker.io 拉取 base image，频繁构建会触发 429 Too Many Requests。
    **解决**：将 Dockerfile 的 `FROM python:3.12-slim` 改为 `FROM public.ecr.aws/docker/library/python:3.12-slim`。
    _已查文档确认：这是 Docker Hub 的限制，与 AWS 无关。_

!!! warning "踩坑 2: Nova Sonic Region 可用性"
    `amazon.nova-2-sonic-v1:0` 并非所有 Region 都可用。本文测试时 ap-southeast-1（Singapore）无法使用，us-east-1 可用。
    **建议**：先用 `aws bedrock list-foundation-models` 确认目标 Region 是否有 Nova Sonic。
    _实测发现，官方 Region 可用性列表需要分别确认 AgentCore 和 Bedrock FM 两个维度。_

!!! warning "踩坑 3: Agent 更新冲突"
    如果 Agent Runtime 已存在，`agentcore deploy` 默认不覆盖，需要加 `--auto-update-on-conflict`。
    如果 Agent 正在 CREATING/UPDATING 状态，更新会失败，需等状态变为 READY。
    _实测发现，官方未记录。_

!!! warning "踩坑 4: Session ID 长度限制"
    `runtimeSessionId` 最少 33 个字符。使用 UUID（36 字符）可以满足要求。
    _已查文档确认：API 参数限制。_

## 费用明细

| 资源 | 单价 | 用量 | 费用 |
|------|------|------|------|
| NAT Gateway | $0.045/hr | 2 hr | $0.09 |
| NAT Gateway 数据处理 | $0.045/GB | ~0.1 GB | $0.005 |
| KVS Signaling Channel | $0.0012/channel/month | 1 channel | ~$0.00 |
| AgentCore Runtime 调用 | 按调用计费 | ~20 次 | ~$1.00 |
| Bedrock Nova Sonic | 按 token 计费 | 测试量 | ~$0.50 |
| EIP（NAT 关联） | $0.00（关联状态） | - | $0.00 |
| **合计** | | | **~$1.60** |

## 清理资源

```bash
export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=us-east-1

# 1. 销毁 AgentCore Agent
agentcore destroy

# 2. 删除 KVS Signaling Channel
CHANNEL_ARN=$(aws kinesisvideo describe-signaling-channel \
  --channel-name voice-agent-webrtc \
  --query 'ChannelInfo.ChannelARN' --output text)
aws kinesisvideo delete-signaling-channel --channel-arn $CHANNEL_ARN

# 3. 检查 ENI 残留（VPC 模式的 ENI 可能保持长达 8 小时）
aws ec2 describe-network-interfaces \
  --filters "Name=group-id,Values=$SG_ID" \
  --query 'NetworkInterfaces[].{Id:NetworkInterfaceId,Status:Status,Description:Description}'
# 如果有残留 ENI，等待自动释放或手动 detach 后删除

# 4. 删除 VPC 资源（按依赖顺序）
aws ec2 delete-nat-gateway --nat-gateway-id $NAT_ID
echo "等待 NAT Gateway 删除..."
aws ec2 wait nat-gateway-deleted --nat-gateway-ids $NAT_ID 2>/dev/null || sleep 60

aws ec2 release-address --allocation-id $EIP_ID
aws ec2 delete-subnet --subnet-id $PRIV_SUBNET
aws ec2 delete-subnet --subnet-id $PUB_SUBNET
aws ec2 delete-route-table --route-table-id $PRIV_RT
aws ec2 delete-route-table --route-table-id $PUB_RT
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID
aws ec2 delete-security-group --group-id $SG_ID
aws ec2 delete-vpc --vpc-id $VPC_ID

# 5. 验证清理完成
aws ec2 describe-vpcs --vpc-ids $VPC_ID 2>&1 | grep -q "does not exist" && echo "VPC 已删除 ✅"
```

!!! danger "务必清理"
    Lab 完成后请执行清理步骤，避免 NAT Gateway 持续计费（$0.045/hr ≈ $32/月）。

## 端到端语音对话验证（Python WebRTC 客户端）

上面的测试验证了 API 层面的 ICE config 获取和 SDP 交换，但没有真正发送和接收音频。本节使用 Python 脚本替代浏览器，实现完整的端到端语音对话验证。

### 测试架构

```
┌─────────────────────────┐
│  Python WebRTC Client   │
│  (dev-server)           │
│                         │
│  1. edge-tts 生成音频    │
│  2. aiortc WebRTC 连接   │
│  3. 发送 PCM 音频        │
│  4. 录制响应音频          │
└────────┬────────────────┘
         │ UDP (TURN relay)
         ▼
┌─────────────────────────┐
│  KVS TURN Server        │
│  (AWS managed)          │
└────────┬────────────────┘
         │ UDP (TURN relay)
         ▼
┌─────────────────────────┐
│  AgentCore Runtime      │
│  (VPC 私有子网)          │
│                         │
│  bot.py (aiortc)        │
│    → Nova Sonic 双向流    │
│    → 语音识别 + 生成      │
└─────────────────────────┘
```

### 依赖安装

```bash
pip install edge-tts aiortc aiohttp av
```

### Step 1: 生成测试音频

使用 Microsoft Edge TTS 生成中文问题音频，转换为 Nova Sonic 要求的 16kHz/16-bit/mono PCM：

```python
import edge_tts
import av
import struct

# 生成中文语音
text = "请介绍一下你自己"
communicate = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural")
await communicate.save("/tmp/question.mp3")

# 转换为 16kHz PCM WAV
container = av.open("/tmp/question.mp3")
resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
pcm_data = bytearray()
for frame in container.decode(audio=0):
    for rf in resampler.resample(frame):
        pcm_data.extend(bytes(rf.planes[0]))

# 写入 WAV 文件（省略 header 代码）
```

生成结果：2.88 秒，92,224 字节。

### Step 2: Python WebRTC 客户端核心代码

关键组件：

**FileAudioTrack** — 从 WAV 文件读取 PCM 数据，按 20ms 帧率实时发送：

```python
class FileAudioTrack(MediaStreamTrack):
    kind = "audio"

    async def recv(self):
        # 按实时节奏返回 20ms 音频帧
        frame_bytes = 320 * 2  # 16kHz * 20ms * 16bit
        chunk = self._pcm_data[self._offset:self._offset + frame_bytes]
        # ... 设置 pts、time_base，返回 AudioFrame
```

**AudioRecorder** — 录制远程音频 track，检测非静音帧：

```python
class AudioRecorder:
    def add_frame(self, frame):
        pcm = bytes(resampled_frame.planes[0])
        samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
        max_amp = max(abs(s) for s in samples)
        if max_amp > 100:  # 非静音阈值
            self._last_audio_time = time.time()
        self._chunks.append(pcm)
```

**信令流程** — 通过 AgentCore Runtime API 完成 ICE + SDP 交换：

```python
# 1. 获取 TURN credentials
ice_response = invoke_agent(session, agent_arn, session_id, {"action": "ice_config"})

# 2. 创建 PeerConnection，配置 TURN
pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
pc.addTrack(FileAudioTrack("/tmp/question.wav"))

# 3. SDP 交换
offer = await pc.createOffer()
await pc.setLocalDescription(offer)
answer = invoke_agent(session, agent_arn, session_id, {
    "action": "offer",
    "data": {"sdp": offer.sdp, "type": offer.type, "turnOnly": True}
})
await pc.setRemoteDescription(RTCSessionDescription(**answer))

# 4. 等待连接 + 自动流式传输音频
# FileAudioTrack.recv() 按 20ms 节奏自动发送
# on("track") 回调自动录制响应
```

!!! tip "payload 编码"
    Python SDK (`boto3`) 的 `invoke_agent_runtime` 的 `payload` 参数接受 **raw bytes**（不是 base64），响应在 `response` 字段（不是 `body`）。这与 CLI 的 `--payload`（接受 base64）不同。

### 测试结果

| 指标 | 值 |
|------|-----|
| ICE config 延迟 | **1.35s** |
| SDP 交换延迟 | **0.88s** |
| WebRTC 连接建立 | **1.50s** |
| 发送音频时长 | 2.23s |
| 接收响应时长 | **20.70s** |
| 首个响应音频 | 连接建立后 **3.90s** |
| 响应帧数 | 1,399 帧 |
| 端到端总时间 | 33.32s |

#### 响应音频分析

| 指标 | 值 |
|------|-----|
| 平均音量 | -25.9 dB |
| 峰值音量 | -8.7 dB（无 clipping） |
| 有效语音 | ~20.7 秒 |
| 采样率 | 48kHz（Opus 解码原始），mono PCM |
| 格式 | s16 packed stereo（aiortc 解码）→ deinterleave → mono PCM |

- **有效语音约 20.7 秒**，Nova Sonic 用英文（matthew 声音）回答了自我介绍
- 连接建立后约 3.9 秒开始收到响应音频（包含模型处理延迟）
- 峰值 -8.7 dB，远低于 0 dB clipping 阈值，音频质量良好

### 音频文件

| 文件 | 时长 | 大小 | 说明 |
|------|------|------|------|
| [question.wav](audio/question.wav) | 2.23s | 70 KB | 发送的中文问题（edge-tts 生成） |
| [response.wav](audio/response.wav) | 21.68s | 2.0 MB | Agent 语音回复（Nova Sonic matthew，48kHz mono） |

!!! note "TURN Forbidden IP 警告"
    测试中 `aioice` 库报出 `STUN transaction failed (403 - Forbidden IP)` 警告，这是 CHANNEL_BIND 请求被 KVS TURN 服务器拒绝（某些 peer IP 不被允许直接绑定）。**不影响功能** — 连接通过 Send Indication 方式仍然成功建立。


## 结论与建议

AgentCore Runtime WebRTC 双向流是一个值得关注的新能力，但目前仍有较高的基础设施门槛。以下是我们通过端到端实测总结的关键判断。

### WebRTC 的价值

AgentCore Runtime 新增 WebRTC 支持，为**浏览器和移动端的实时语音 Agent** 提供了原生级体验：

- **低延迟**：UDP 传输，适合实时对话
- **浏览器原生**：无需额外 SDK，`getUserMedia()` + `RTCPeerConnection` 即可
- **防火墙友好**：TURN over TLS/443 可穿透大部分企业防火墙

### 与 WebSocket 的选择

| 场景 | 推荐协议 |
|------|---------|
| 浏览器实时语音 Agent | **WebRTC** |
| 移动端实时语音 Agent | **WebRTC** |
| 服务端文本 + 音频流 | WebSocket |
| 不想管 VPC/TURN 基础设施 | WebSocket |
| 需要最低运维复杂度 | WebSocket |

### 生产环境建议

1. **至少 2 个 AZ**：为 AgentCore 配置多 AZ 私有子网，提高可用性
2. **TURN Credential 刷新**：TTL 仅 5 分钟，客户端需要定期刷新
3. **监控冷启动**：VPC 模式冷启动 ~8.7s，可通过保持活跃 session 缓解
4. **安全组最小化**：虽然本文使用了默认 outbound all，生产环境建议限制到 TURN server IP 范围

## 参考链接

- [Bidirectional streaming with WebRTC — 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-webrtc.html)
- [Tutorial: WebRTC with KVS TURN — 官方教程](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-webrtc-get-started-kvs.html)
- [Configure AgentCore for VPC — 官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)
- [完整示例代码 — GitHub](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming-webrtc)
- [KVS GetIceServerConfig API](https://docs.aws.amazon.com/kinesisvideostreams/latest/dg/API_signaling_GetIceServerConfig.html)
