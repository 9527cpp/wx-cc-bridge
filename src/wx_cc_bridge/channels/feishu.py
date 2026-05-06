"""飞书 Bot Channel（HTTP 轮询拉取消息 + REST API 发送）。

飞书 SDK(larksuite-oapi) 不包含 WebSocket 长连接，仅支持 HTTP webhook 回调。
本实现采用 HTTP 轮询模式：对每个会话轮询最新消息，通过 message_id 哈希集合去重。
对标 WeComChannel 的 recv_messages/send_text 接口。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .base import AbstractChannel, Message


FEISHU_API_ROOT = "https://open.feishu.cn"
LARK_API_ROOT = "https://open.larksuite.com"

# 轮询间隔（秒）
_POLL_INTERVAL_SEC = 1.0
# 重连参数
_MAX_RECONNECT_ATTEMPTS = 5
_RECONNECT_BASE_DELAY = 1.0


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str = "feishu"

    @property
    def api_root(self) -> str:
        return FEISHU_API_ROOT if self.domain == "feishu" else LARK_API_ROOT

    @classmethod
    def load(cls, config_path: Path) -> "FeishuConfig":
        data = json.loads(config_path.read_text())
        app_id = data.get("app_id")
        app_secret = data.get("app_secret")
        if not app_id:
            raise ValueError(f"feishu_config.json missing required field 'app_id' at {config_path}")
        if not app_secret:
            raise ValueError(f"feishu_config.json missing required field 'app_secret' at {config_path}")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            domain=data.get("domain", "feishu"),
        )

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "domain": self.domain,
        }, ensure_ascii=False))


class FeishuChannel(AbstractChannel):
    name = "feishu"

    def __init__(self, config: FeishuConfig | None = None, config_path: Path | None = None):
        import os as _os

        state_dir = Path(_os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))
        self._config_path = config_path or (state_dir / "feishu_config.json")
        if config is not None:
            self.config = config
        elif self._config_path.exists():
            self.config = FeishuConfig.load(self._config_path)
        else:
            raise RuntimeError(f"飞书配置不存在: {self._config_path}")

        self._tenant_access_token: str | None = None
        self._abort_event: asyncio.Event | None = None
        self._http_client: httpx.AsyncClient | None = None
        # 记录已见过的 message_id，防止重复处理
        self._seen_msg_ids: set[str] = set()

    async def login(self) -> None:
        print(f"[feishu] 配置加载 app_id={self.config.app_id}")
        print(f"[feishu] domain={self.config.domain} api_root={self.config.api_root}")
        self._http_client = httpx.AsyncClient(timeout=30.0)
        await self._ensure_token()

    async def _ensure_token(self) -> None:
        """获取 tenant_access_token。"""
        url = f"{self.config.api_root}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}
        resp = await self._http_client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[feishu] 获取 token 失败: {data}")
        self._tenant_access_token = data.get("tenant_access_token", "")
        print("[feishu] token 获取成功")

    async def _api_request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """发起到飞书开放 API 的请求，自动处理 token 刷新。"""
        if not self._tenant_access_token:
            await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._tenant_access_token}"
        url = f"{self.config.api_root}{path}"
        resp = await self._http_client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            await self._ensure_token()
            headers["Authorization"] = f"Bearer {self._tenant_access_token}"
            resp = await self._http_client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"[feishu] API 错误 {path}: code={data.get('code')} msg={data.get('msg')}")
        return data

    def _parse_message(self, raw: dict[str, Any]) -> Message | None:
        """从飞书消息对象解析出统一 Message。"""
        try:
            sender = raw.get("sender", {})
            sender_id = sender.get("id", "")
            sender_type = sender.get("sender_type", "")
            # 过滤掉机器人自己的消息
            if sender_type == "bot":
                return None

            msg_type = raw.get("msg_type", "")
            body_content = raw.get("body", {}).get("content", "{}")
            try:
                body = json.loads(body_content)
            except Exception:
                body = {"text": body_content}

            if msg_type == "text":
                text = body.get("text", "").strip()
            elif msg_type == "post":
                text = body.get("text", "").strip()
            else:
                print(f"[feishu] 跳过非文本消息 type={msg_type}")
                return None

            if not text:
                return None

            return Message(
                from_user_id=sender_id,
                to_user_id=self.config.app_id,
                content=text,
                msg_id=raw.get("message_id"),
                context_token=raw.get("message_id"),
                msg_type=msg_type,
                raw=raw,
            )
        except Exception as e:
            print(f"[feishu] 解析消息异常: {e!r}")
            return None

    async def _fetch_messages_page(self, page_token: str | None = None) -> tuple[list[Message], str | None, bool]:
        """拉取一页消息，返回 (消息列表, 下一页token, 是否有更多)。"""
        params: dict[str, Any] = {
            "container_id_type": "p2p",
            "page_size": 50,
            "sort_type": "ByCreateTimeDesc",
        }
        if page_token:
            params["page_token"] = page_token

        data = await self._api_request(
            "GET",
            "/open-apis/im/v1/messages",
            params=params,
        )
        items: list[dict[str, Any]] = data.get("data", {}).get("items", [])
        has_more = data.get("data", {}).get("has_more", False)
        next_page_token = data.get("data", {}).get("page_token")

        messages = []
        for item in items:
            msg = self._parse_message(item)
            if msg and msg.msg_id and msg.msg_id not in self._seen_msg_ids:
                self._seen_msg_ids.add(msg.msg_id)
                messages.append(msg)

        return messages, next_page_token if has_more else None, has_more

    async def recv_messages(self) -> AsyncIterator[Message]:
        """轮询接收消息，对标 WeComChannel。"""
        self._abort_event = asyncio.Event()
        page_token: str | None = None

        while not self._abort_event.is_set():
            try:
                msgs, next_token, has_more = await self._fetch_messages_page(page_token)
                page_token = next_token

                for msg in msgs:
                    print(f"[feishu] 收到消息 from={msg.from_user_id} content={msg.content[:30]!r}")
                    yield msg

                # 如果没有更多消息，sleep 等待下次轮询
                if not has_more:
                    await asyncio.sleep(_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[feishu] recv_messages 异常: {e!r}")
                await asyncio.sleep(_RECONNECT_BASE_DELAY)

    async def send_text(self, to_user_id: str, context_token: str | None, text: str) -> None:
        """通过 REST API 发送文本消息。"""
        body = {
            "receive_id": to_user_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        await self._api_request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json=body,
        )
        print(f"[feishu] 已发送回复 to={to_user_id} len={len(text)}")

    async def close(self) -> None:
        if self._abort_event:
            self._abort_event.set()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        print("[feishu] 已关闭")
