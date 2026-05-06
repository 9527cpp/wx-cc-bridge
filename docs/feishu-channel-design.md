# 飞书 Bot 长连接 Channel 设计文档

## 1. 背景与目标

### 现状
wx-cc-bridge 已支持 iLink（个人微信）和 WeCom Bot（企业微信）两个通道：

```
个人微信 iLink  → channels/ilink.py  ─┐
企业微信 WeCom  → channels/wecom.py  ─┤→ bridge.py → commands.py / claude_runner.py
飞书 Feishu     → channels/feishu.py ─┘  (本文新增)
```

### 目标
新增飞书 Bot WebSocket 长连接通道，三通道共用 bridge 核心逻辑：

```
飞书 Bot → channels/feishu.py (WebSocket 长连接) ─┐
                                                    ├─→ bridge.py → commands.py / claude_runner.py
企业微信/个人微信 → channels/[ilink|wecom].py  ────┘
```

## 2. 飞书机器人 SDK

### SDK 选择
- **包**：`larksuite-oapi`（PyPI: `larksuite-oapi`）
- **文档**：https://open.feishu.cn/document/
- **长连接**：SDK 封装了 WebSocket 客户端（`ws_client`），无需自实现重连/心跳

### 关键概念
| 企微 | 飞书 |
|------|------|
| `bot_id` + `secret` | `app_id` + `app_secret` |
| `wss://openws.work.weixin.qq.com` | `ws://open.feishu.cn`（SDK 自动连接） |
| `aibot_subscribe` | SDK 自动完成 token 换取 + 连接 |
| `aibot_msg_callback` | `im.message.receive_v1`（事件名） |
| `aibot_resp` | `im.message.create_v1`（主动发送） |

## 3. 架构设计

### 3.1 目录结构

```
src/wx_cc_bridge/
├── channels/
│   ├── __init__.py       # 不变
│   ├── base.py           # AbstractChannel, Message dataclass（已有，不改）
│   ├── ilink.py          # 不变
│   ├── wecom.py          # 不变
│   └── feishu.py         # 新增：飞书 Channel
├── bridge.py             # 改动：_build_channels() 加飞书检测
├── wecom_onboard.py      # 不变
└── feishu_onboard.py    # 新增：飞书接入引导
```

### 3.2 FeishuChannel 对标 WeComChannel

飞书 SDK 的 `ws_client` 与企微 WebSocket 在概念上完全对应：

| WeComChannel 行为 | FeishuChannel 对应 |
|------------------|-------------------|
| `login()` 建立 WS 连接并认证 | `login()` 初始化 SDK WSClient，SDK 自动连接 |
| `recv_messages()` yield 消息帧 | SDK 注册 `im.message.receive_v1` 事件回调，yield Message |
| `send_text()` 发 resp 帧 | 调用 `im.message.create_v1` API 发送消息 |
| 重连逻辑自实现 | SDK 内置自动重连 |
| 心跳自实现 ping/pong | SDK 内置 |
| 断连事件 `disconnected_event` | SDK 自动重连（无需手动处理） |

### 3.3 Message 统一抽象

复用 `channels/base.py` 的 `Message` dataclass，无需改动。

```python
@dataclass
class Message:
    from_user_id: str       # 飞书用户的 open_id
    to_user_id: str        # 机器人的 app_id
    content: str            # 消息文本
    msg_id: str | None     # 飞书消息 ID
    context_token: str | None  # 企微叫 req_id，飞书叫 message_id，统一放这里
    msg_type: str = "text"
    raw: dict = field(default_factory=dict)  # 原始事件体
```

## 4. FeishuChannel 实现

### 4.1 SDK 初始化

```python
from larksuiteoapi import Config, APP_TYPE_CUSTOM, DOMAIN_FEISHU
from larksuiteoapi.api import Request
from larksuiteoapi.service.im.v1 import CreateMessageReq

# app_settings = Config.new_internal_app_settings_from_env()  # 从环境变量
# 或显式传入：
app_settings = Config(
    app_id="cli_xxx",
    app_secret="xxx",
    app_type=APP_TYPE_CUSTOM,
    domain=DOMAIN_FEISHU,  # 国内飞书
)
```

### 4.2 长连接客户端

```python
from larksuiteoapi.service.im.v1 import MessageEvent

class FeishuChannel(AbstractChannel):
    name = "feishu"

    def __init__(self, config: FeishuConfig | None = None, config_path: Path | None = None):
        # 加载 feishu_config.json，同 WeComConfig 模式
        ...

    async def login(self) -> None:
        # 初始化 WSClient，SDK 自动建立并维持长连接
        self.ws_client = WsClient(app_settings=self.app_settings)
        self.ws_client.start()

        # 注册消息事件监听
        self.ws_client.im.message.register_event_handler(
            MessageEvent,
            self._on_message,
        )
        print(f"[feishu] 长连接已启动，等待消息...")

    def _on_message(self, data: MessageEvent) -> None:
        # SDK 回调，在单独的线程中触发
        # 需要线程安全地放到 asyncio.Queue 中
        msg = self._parse_message(data)
        self._queue.put_nowait(msg)

    async def recv_messages(self) -> AsyncIterator[Message]:
        # 从队列中 yield 消息，完全对标 WeComChannel 的 recv_messages
        ...
```

### 4.3 消息解析

飞书消息事件 `im.message.receive_v1` 的 `MessageEvent` body 结构：

```python
# 文本消息
{
    "header": {
        "event_id": "...",
        "create_time": "...",
        "event_type": "im.message.receive_v1",
    },
    "event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_xxx"}},
        "message": {
            "message_id": "om_xxx",
            "create_time": "...",
            "chat_id": "oc_xxx",
            "chat_type": "p2p",
            "message_type": "text",
            "content": "{\"text\":\"用户发的文字\"}",  # JSON 字符串
        }
    }
}
```

文本提取：`json.loads(event["message"]["content"])["text"]`

### 4.4 发送消息

飞书发消息调用 REST API，不是 WebSocket 帧：

```python
def send_text(self, to_user_id: str, context_token: str | None, text: str) -> None:
    # to_user_id = open_id（来自消息的 sender.sender_id.open_id）
    # context_token = message_id（用于避免重复？）
    body = {
        "receive_id": to_user_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    req = CreateMessageReq(
        path={"receive_id_type": "open_id"},
        body=body,
    )
    # SDK 自动带 app_access_token
    resp = Request().request(req)
```

### 4.5 错误处理

- **SDK 连接失败**：抛出异常，`login()` 捕获并打印错误
- **消息解析失败**：`skip non-text messages`，同 WeComChannel
- **发送失败**：打印错误，不阻塞主循环

## 5. 配置

### 5.1 FeishuConfig

```python
@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str = "feishu"  # "feishu" | "lark"（海外）
```

### 5.2 配置文件

```
~/.wx-cc-bridge/feishu_config.json
{
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "domain": "feishu"
}
```

### 5.3 环境变量

| 环境变量 | 作用 |
|---------|------|
| `WX_CC_ENABLE_FEISHU` | `0` 禁用飞书通道（默认 `1` 启用） |
| `WX_CC_FEISHU_APP_ID` | 直接传入 app_id（绕过配置文件） |
| `WX_CC_FEISHU_APP_SECRET` | 直接传入 app_secret |

## 6. Bridge.py 改动

在 `_build_channels()` 中新增：

```python
# Feishu Bot channel
if os.environ.get("WX_CC_ENABLE_FEISHU", "1") != "0":
    feishu_config_path = STATE_DIR / "feishu_config.json"
    if feishu_config_path.exists():
        channels.append(FeishuChannel(config_path=feishu_config_path))
    else:
        print(f"[bridge] Feishu config not found ({feishu_config_path}), skipping Feishu channel")
```

日志前缀统一用 `[feishu]`。

## 7. Feishu Onboarding

流程对标 `wecom_onboard.py`：

```
1) 引导用户去 open.feishu.cn 创建企业自建应用
2) 开启「机器人」能力
3) 开启「长连接机器人」权限
4) 复制 App ID + App Secret
5) 运行 feishu_onboard，粘贴凭证
6) 保存到 feishu_config.json
```

### feishu_onboard.py CLI

```bash
python -m wx_cc_bridge.feishu_onboard
# 或
wx-feishu-setup
```

```bash
$ python -m wx_cc_bridge.feishu_onboard
[feishu-setup] 飞书机器人接入引导
请先去 open.feishu.cn 创建企业自建应用，开启「长连接机器人」权限
然后复制 App ID 和 App Secret 粘贴于此

App ID: cli_xxx
App Secret: xxx
[feishu-setup] 正在验证凭证...
[feishu-setup] 验证成功
[feishu-setup] 配置已保存到 ~/.wx-cc-bridge/feishu_config.json
```

## 8. Typing 指示器

飞书**不支持**机器人主动发送"正在输入"状态，同企微。一期不实现。

## 9. 依赖

新增：
- `larksuite-oapi>=1.0.33`（飞书官方 Python SDK）

现有依赖不变。

## 10. 已确认

1. **长连接**：SDK 内置自动重连，无需手动实现
2. **多 Bot**：一期先做单 bot
3. **日志前缀**：`[feishu]`
4. **与企微对比**：飞书 SDK 封装程度更高，重连/心跳 SDK 自己搞定
5. **消息类型**：一期只处理 text，跳过其他类型（image/file 等）
