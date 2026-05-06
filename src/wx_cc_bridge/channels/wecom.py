"""企业微信 Bot WebSocket channel。

协议参考: @wecom/aibot-node-sdk
连接地址: wss://openws.work.weixin.qq.com
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

from .base import AbstractChannel, Message


DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
BASE_URL_DEFAULT = "https://qyapi.weixin.qq.com"

# WebSocket 帧类型
_WS_CMD_SUBSCRIBE = "aibot_subscribe"
_WS_CMD_CALLBACK = "aibot_msg_callback"
_WS_CMD_EVENT_CALLBACK = "aibot_event_callback"
_WS_CMD_RESP = "aibot_respond_msg"
_WS_CMD_HEARTBEAT = "ping"

# 重连参数
_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0

# 心跳参数
_HEARTBEAT_INTERVAL = 30.0  # 秒
_MAX_MISSED_PONG = 2


def _generate_req_id(cmd: str) -> str:
    return f"{cmd}:{uuid.uuid4()}"


def _content_to_text(body: dict) -> str | None:
    """从企业微信消息 body 中提取文本。

    SDK 消息格式（body）：
    - text: { "msgtype": "text", "text": { "content": "..." } }
    - image: { "msgtype": "image", "image": { ... } }
    """
    msgtype = body.get("msgtype", "")
    if msgtype == "text":
        text_block = body.get("text")
        if isinstance(text_block, dict):
            return text_block.get("content")
    return None


class WeComConfig:
    def __init__(
        self,
        bot_id: str,
        secret: str,
        websocket_url: str = DEFAULT_WS_URL,
        base_url: str = BASE_URL_DEFAULT,
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.websocket_url = websocket_url
        self.base_url = base_url

    @classmethod
    def load(cls, config_path: Path) -> "WeComConfig":
        data = json.loads(config_path.read_text())
        return cls(
            bot_id=data["bot_id"],
            secret=data["secret"],
            websocket_url=data.get("websocket_url", DEFAULT_WS_URL),
            base_url=data.get("base_url", BASE_URL_DEFAULT),
        )

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "bot_id": self.bot_id,
            "secret": self.secret,
            "websocket_url": self.websocket_url,
            "base_url": self.base_url,
        }, ensure_ascii=False))


class WeComChannel(AbstractChannel):
    name = "wecom"

    def __init__(
        self,
        config: WeComConfig | None = None,
        config_path: Path | None = None,
    ):
        from pathlib import Path as _Path
        import os as _os

        state_dir = _Path(_os.environ.get("WX_CC_STATE", _Path.home() / ".wx-cc-bridge"))
        self.config_path = config_path or (state_dir / "wecom_config.json")

        if config is not None:
            self.config = config
        elif self.config_path.exists():
            self.config = WeComConfig.load(self.config_path)
        else:
            raise RuntimeError(
                f"企业微信配置不存在: {self.config_path}，"
                "请先配置 bot_id 和 secret"
            )

        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._abort_event: asyncio.Event | None = None
        self._msg_queue: asyncio.Queue[Message] | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._missed_pong_count: int = 0

    async def login(self) -> None:
        # 配置加载后直接尝试建立 WebSocket 连接
        print(f"[wecom] 配置加载 bot_id={self.config.bot_id}")
        print(f"[wecom] 开始连接 WebSocket: {self.config.websocket_url}")

    async def _connect_ws(self) -> websockets.asyncio.client.ClientConnection:
        """建立 WebSocket 连接并完成认证。"""
        ws_url = self.config.websocket_url
        print(f"[wecom] 连接 WebSocket: {ws_url}", flush=True)
        ws = await ws_connect(ws_url, ping_interval=None)
        print(f"[wecom] WebSocket 连接已建立，本地地址={ws.local_address}", flush=True)

        # 发送认证帧
        auth_frame = {
            "cmd": _WS_CMD_SUBSCRIBE,
            "headers": {"req_id": _generate_req_id(_WS_CMD_SUBSCRIBE)},
            "body": {
                "bot_id": self.config.bot_id,
                "secret": self.config.secret,
            },
        }
        await ws.send(json.dumps(auth_frame, ensure_ascii=False))
        print(f"[wecom] 认证帧已发送，等待认证结果...")

        # 等待认证响应
        while True:
            raw = await ws.recv()
            frame = json.loads(raw)
            cmd = frame.get("cmd", "")
            errcode = frame.get("errcode", 0)
            errmsg = frame.get("errmsg", "")

            if cmd == _WS_CMD_SUBSCRIBE or (frame.get("headers", {}).get("req_id", "") or "").startswith(_WS_CMD_SUBSCRIBE):
                if errcode != 0:
                    raise RuntimeError(f"[wecom] 认证失败 errcode={errcode} errmsg={errmsg}")
                print(f"[wecom] 认证成功 bot_id={self.config.bot_id}")
                await self._start_heartbeat()
                break

            # 忽略其他帧（理论上认证阶段不应该收到其他帧）
            print(f"[wecom] 收到非认证帧 cmd={cmd}，忽略")

        return ws

    async def _reconnect(self) -> None:
        """指数退避重连。"""
        self._stop_heartbeat()
        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_MAX_DELAY)
            print(f"[wecom] 重连尝试 {attempt}/{_MAX_RECONNECT_ATTEMPTS}，{delay:.1f}s 后...")
            await asyncio.sleep(delay)

            try:
                self._ws = await self._connect_ws()
                print(f"[wecom] 重连成功")
                return
            except Exception as e:
                print(f"[wecom] 重连失败: {e!r}")
                if self._abort_event and self._abort_event.is_set():
                    return

        print(f"[wecom] 重连次数用尽，停止")

    async def _start_heartbeat(self) -> None:
        """启动心跳定时器。"""
        self._stop_heartbeat()
        self._missed_pong_count = 0
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """心跳循环，每 _HEARTBEAT_INTERVAL 秒发送一次 ping。"""
        while not (self._abort_event and self._abort_event.is_set()):
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            if self._abort_event and self._abort_event.is_set():
                break
            if self._missed_pong_count >= _MAX_MISSED_PONG:
                print(f"[wecom] 连续{self._missed_pong_count}次未收到 pong，连接已死，强制重连")
                self._stop_heartbeat()
                if self._ws:
                    await self._ws.close()
                return
            self._missed_pong_count += 1
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        """发送心跳帧。"""
        ws = self._ws
        if not ws:
            return
        try:
            frame = {
                "cmd": _WS_CMD_HEARTBEAT,
                "headers": {"req_id": _generate_req_id(_WS_CMD_HEARTBEAT)},
            }
            await ws.send(json.dumps(frame, ensure_ascii=False))
            print(f"[wecom] 发送心跳帧, missed_pong={self._missed_pong_count}")
        except websockets.exceptions.ConnectionClosed:
            print(f"[wecom] 心跳发送失败: 连接已关闭")
        except Exception as e:
            print(f"[wecom] 发送心跳失败: {e!r}")

    def _stop_heartbeat(self) -> None:
        """停止心跳定时器。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _ws_recv_loop(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        """WebSocket 接收循环，收到消息放入队列。"""
        queue = self._msg_queue
        assert queue is not None

        print(f"[wecom] WebSocket 连接就绪，开始接收消息...", flush=True)
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except Exception as e:
                print(f"[wecom] 解析帧失败: {e!r}")
                continue

            cmd = frame.get("cmd", "")
            body = frame.get("body") or {}
            headers = frame.get("headers") or {}

            if cmd == _WS_CMD_CALLBACK:
                print(f"[wecom] 消息帧 body={body}", flush=True)
                # 消息内容在 body.text.content（text消息）或其他消息类型的嵌套结构中
                text = None
                if isinstance(body, dict):
                    # text 消息: body.text.content
                    text_block = body.get("text")
                    if isinstance(text_block, dict):
                        text = text_block.get("content")
                    # 或者 content 字段直接就是内容
                    if not text:
                        text = body.get("content")
                text = _content_to_text(body) or text
                if not text:
                    print(f"[wecom] 跳过非文本消息 msgtype={body.get('msgtype')}")
                    continue

                msg = Message(
                    from_user_id=body.get("from", {}).get("userid", "") or body.get("from_user_id", ""),
                    to_user_id=body.get("to_user_id", ""),
                    content=text,
                    msg_id=body.get("msgid"),
                    context_token=headers.get("req_id"),
                    msg_type=body.get("msgtype", "text"),
                    raw=frame,
                )
                print(f"[wecom] 收到消息 from={msg.from_user_id} content={text[:50]!r}")
                await queue.put(msg)

            elif cmd == _WS_CMD_EVENT_CALLBACK:
                event = body.get("event", {})
                event_type = event.get("eventtype", "")
                print(f"[wecom] 收到事件: {event_type}")
                if event_type == "disconnected_event":
                    print(f"[wecom] 被新连接踢下线，不自动重连")
                    await queue.put(None)
                    return

            elif cmd == _WS_CMD_HEARTBEAT or (cmd == "" and (frame.get("headers", {}).get("req_id", "") or "").startswith(_WS_CMD_HEARTBEAT)):
                # pong 响应帧（cmd 为空，req_id 以 ping 开头）
                self._missed_pong_count = 0
                print(f"[wecom] 收到 pong 响应")

            else:
                print(f"[wecom] 收到其他帧 cmd={cmd}")

    async def recv_messages(self) -> AsyncIterator[Message]:
        self._msg_queue = asyncio.Queue[Message | None]()
        self._abort_event = asyncio.Event()
        queue = self._msg_queue

        # 建立初始连接
        try:
            self._ws = await self._connect_ws()
        except Exception as e:
            print(f"[wecom] 初始连接失败: {e!r}")
            self._abort_event.set()
            return

        # 启动接收循环
        recv_task = asyncio.create_task(self._ws_recv_loop(self._ws))
        self._recv_task = recv_task

        while True:
            msg = await queue.get()
            if msg is None:
                # 断连事件
                break
            yield msg

            # 检查接收任务是否异常退出
            if recv_task.done():
                exc = recv_task.exception()
                if exc:
                    print(f"[wecom] 接收循环异常: {exc!r}")
                # 尝试重连
                if not self._abort_event.is_set():
                    await self._reconnect()
                    if self._ws and not self._abort_event.is_set():
                        recv_task = asyncio.create_task(self._ws_recv_loop(self._ws))
                        self._recv_task = recv_task
                    else:
                        break
                else:
                    break

    async def send_text(
        self,
        to_user_id: str,
        context_token: str | None,
        text: str,
    ) -> None:
        ws = self._ws
        if not ws:
            raise RuntimeError("[wecom] WebSocket 未连接")

        req_id = context_token or _generate_req_id(_WS_CMD_RESP)
        resp_frame = {
            "cmd": _WS_CMD_RESP,
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "markdown",
                "markdown": {"content": text},
            },
        }
        print(f"[wecom] 发送回复帧: {resp_frame}", flush=True)
        await ws.send(json.dumps(resp_frame, ensure_ascii=False))
        print(f"[wecom] 已发送回复 to={to_user_id} len={len(text)}")

    async def close(self) -> None:
        if self._abort_event:
            self._abort_event.set()
        self._stop_heartbeat()
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        print(f"[wecom] 已关闭")
