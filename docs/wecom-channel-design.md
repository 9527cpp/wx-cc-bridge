# 企业微信 Bot 长连接 Channel 设计文档

## 1. 背景与目标

### 现状
wx-cc-bridge 通过个人微信 iLink 协议接入 Claude Code，架构如下：
```
微信 ClawBot → bridge.py (ilink client) → claude -p
```

### 目标
新增企业微信 Bot WebSocket 长连接通道，两通道共用 bridge 核心逻辑：
```
企业微信 Bot → bridge.py (wecom channel) ─┐
                                             ├─→ commands.py / claude_runner.py → claude -p
微信 iLink        → bridge.py (ilink channel) ─┘
```

## 2. 架构设计

### 2.1 多 Channel 抽象

现有 `bridge.py` 的 `main()` 是单 channel 的，需要改造为支持多 channel 并行：

```python
# 抽象接口
class Channel(ABC):
    @abstractmethod
    async def login(self) -> None: ...

    @abstractmethod
    async def recv_messages(self) -> AsyncIterator[Message]: ...

    @abstractmethod
    async def send_text(self, to_user_id: str, context_token: str, text: str) -> None: ...

    @abstractmethod
    async def send_typing(self, to_user_id: str, status: int) -> None: ...
```

每个 channel 实现自己的登录、消息接收、发送逻辑，对 bridge.py 暴露统一接口。

### 2.2 目录结构

```
src/wx_cc_bridge/
├── channels/
│   ├── __init__.py       # Channel 基类 + 工厂函数
│   ├── base.py            # AbstractChannel, Message dataclass
│   ├── ilink.py           # 现有 iLink 实现（重构自 ilink/client.py）
│   └── wecom.py           # 企业微信 Bot WebSocket 实现
├── bridge.py              # 主循环，改造成多 channel 轮询
├── commands.py            # 不变
├── claude_runner.py       # 不变
└── session_store.py       # 不变
```

### 2.3 Message 统一抽象

```python
@dataclass
class Message:
    from_user_id: str
    to_user_id: str
    content: str                    # 统一成字符串（文本消息）
    msg_id: str | None = None
    context_token: str | None = None
    msg_type: str = "text"         # text | image | voice | file | video
    raw: dict | None = None        # 原始消息体，供特定 channel 使用
```

## 3. 企业微信 Channel 实现

### 3.1 WebSocket 连接流程

```
connect wss://openws.work.weixin.qq.com
    ↓
WebSocket open → 立即发送认证帧
    ↓
认证成功 (errcode=0)
    ↓
进入消息循环:
    - on message (aibot_msg_callback) → yield Message
    - 心跳 ping/pong 自动处理
    - 断连 → 指数退避重连 (最多10次)
```

### 3.2 认证帧

```python
{
    "cmd": "aibot_subscribe",
    "headers": {"req_id": "<uuid>"},
    "body": {
        "bot_id": "<配置的bot_id>",
        "secret": "<配置的secret>"
    }
}
```

### 3.3 收到消息帧 (server → client)

```json
{
    "cmd": "aibot_msg_callback",
    "headers": {"req_id": "..."},
    "body": {
        "msg_id": "xxx",
        "from_user_id": "user_id",
        "to_user_id": "bot_id",
        "content": {
            "type": "text",
            "text": {"content": "用户发的文字"}
        }
    }
}
```

### 3.4 回复帧 (client → server)

```json
{
    "cmd": "aibot_resp",
    "headers": {"req_id": "<消息帧的req_id>"},
    "body": {
        "msg_id": "<消息帧的msg_id>",
        "content": {
            "type": "text",
            "text": {"content": "回复文字"}
        }
    }
}
```

### 3.5 断连事件处理

收到 `aibot_event_callback` + `eventtype == "disconnected_event"` 时：
- 不自动重连（新连接建立导致旧连接被踢）
- 通知用户，等待手动处理

### 3.6 配置

```python
# ~/.wx-cc-bridge/wecom_config.json
{
    "bot_id": "<企业微信机器人BotID>",
    "secret": "<企业微信机器人Secret>",
    "websocket_url": "wss://openws.work.weixin.qq.com"  # 可选，有默认值
}
```

配置路径独立于 iLink 的 `token.json`。

## 4. Bridge 主循环改造

### 现有逻辑（单 iLink）

```python
async def main():
    client = ILinkClient()
    await login(client, TOKEN_PATH)
    while True:
        data = await client.getupdates(cursor)
        for msg in data.get("msgs"):
            asyncio.create_task(handle_message(...))
```

### 改造后（多 Channel）

```python
async def main():
    channels = [
        ILinkChannel(),        # 从配置决定是否启用
        WeComChannel(),
    ]

    await asyncio.gather(*[ch.login() for ch in channels])

    # 每个 channel 独立接收消息，统一 dispatch
    async def channel_loop(ch: Channel):
        async for msg in ch.recv_messages():
            asyncio.create_task(handle_message(msg, ch))

    await asyncio.gather(*[channel_loop(ch) for ch in channels])
```

## 5. Typing 指示器

iLink 有 `send_typing` API，企业微信 Bot WebSocket 协议**没有**直接的 typing 推送机制。

两种方案：

| 方案 | 做法 | 优缺点 |
|------|------|--------|
| A | 不实现 typing | 最简单，但用户体验稍差（不知道在处理） |
| B | 通过企业微信 Agent HTTP API 发送 typing（需要额外配置 Agent 模式） | 体验好，但需要额外配置 |

**推荐方案 A**，保持 channel 独立性。一期先不做。

## 6. 实现优先级

| 阶段 | 内容 |
|------|------|
| P0 | `channels/base.py` 抽象层 + `Message` dataclass |
| P0 | `channels/wecom.py` WebSocket client + 基本消息收发 |
| P1 | `bridge.py` 多 channel 改造 |
| P1 | 配置分离 (`wecom_config.json`) |
| P2 | 多 channel 同时运行 + per-chat lock |

## 7. 依赖

新增 Python 依赖：
- `websockets>=12.0`（异步 WebSocket 客户端）

现有依赖不变：`httpx`, `qrcode`。

## 8. 已确认

1. **断连重连**：OK，自动指数退避重连（最多10次）
2. **多 bot**：一期先做单 bot
3. **日志前缀**：用 `[wecom]`
