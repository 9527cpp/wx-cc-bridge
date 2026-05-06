"""iLink channel: 封装现有的ilink/client.py为AbstractChannel接口。"""
from __future__ import annotations

import asyncio
import json
import os
import base64
import struct
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import AbstractChannel, Message


BASE_URL_DEFAULT = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"
LONGPOLL_TIMEOUT = 40.0


def _uin_header() -> str:
    n = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(n).encode()).decode()


def _extract_text(msg: dict) -> str | None:
    body = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
    for item in body.get("item_list") or []:
        if item.get("type") == 1:
            return (item.get("text_item") or {}).get("text")
    return None


def _extract_meta(msg: dict) -> tuple[str | None, str | None]:
    body = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
    return body.get("from_user_id"), body.get("context_token")


class ILinkClient:
    def __init__(
        self,
        bot_token: str | None = None,
        base_url: str = BASE_URL_DEFAULT,
    ):
        self.bot_token = bot_token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=LONGPOLL_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _uin_header(),
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    async def _get(self, path: str, params: dict | None = None) -> dict:
        r = await self._client.get(
            f"{self.base_url}/{path}", params=params, headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        r = await self._client.post(
            f"{self.base_url}/{path}", json=body, headers=self._headers()
        )
        r.raise_for_status()
        return r.json()

    async def get_qrcode(self, bot_type: int = 3) -> dict:
        return await self._get("ilink/bot/get_bot_qrcode", {"bot_type": bot_type})

    async def get_qrcode_status(self, qrcode: str) -> dict:
        return await self._get("ilink/bot/get_qrcode_status", {"qrcode": qrcode})

    async def getupdates(self, cursor: str = "") -> dict:
        return await self._post(
            "ilink/bot/getupdates",
            {
                "get_updates_buf": cursor,
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )

    async def send_text(self, to_user_id: str, context_token: str, text: str) -> dict:
        return await self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": f"wx-cc-bridge-{uuid.uuid4()}",
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
        )

    async def get_config(self, ilink_user_id: str, context_token: str | None = None) -> dict:
        body: dict = {"ilink_user_id": ilink_user_id}
        if context_token:
            body["context_token"] = context_token
        return await self._post("ilink/bot/getconfig", body)

    async def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> dict:
        return await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
        )


async def _ilink_login(client: ILinkClient, token_path: Path) -> None:
    if token_path.exists():
        data = json.loads(token_path.read_text())
        client.bot_token = data["bot_token"]
        if data.get("baseurl"):
            client.base_url = data["baseurl"].rstrip("/")
        print(f"[ilink] reuse token from {token_path}")
        return

    qr = await client.get_qrcode()
    qrcode_id = qr.get("qrcode") or qr.get("qrcode_str")
    login_url = qr.get("qrcode_img_content") or qr.get("qrcode_img")
    if not qrcode_id:
        raise RuntimeError(f"no qrcode in response: {qr}")
    qr_content = login_url or qrcode_id

    import qrcode as qrlib
    q = qrlib.QRCode(border=1)
    q.add_data(qr_content)
    q.make()
    q.print_ascii(invert=True)

    print(f"[ilink] 请用微信扫上方二维码登录 ClawBot")
    print(f"[ilink] (URL: {qr_content})")

    last_dump = None
    while True:
        status = await client.get_qrcode_status(qrcode_id)
        st = status.get("status")
        dump = json.dumps(status, ensure_ascii=False)
        if dump != last_dump:
            print(f"[ilink] status → {dump}")
            last_dump = dump
        if st == "confirmed" and status.get("bot_token"):
            client.bot_token = status["bot_token"]
            if status.get("baseurl"):
                client.base_url = status["baseurl"].rstrip("/")
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(
                json.dumps(
                    {"bot_token": client.bot_token, "baseurl": client.base_url},
                    ensure_ascii=False,
                )
            )
            print(f"[ilink] 登录成功，token 已保存到 {token_path}")
            return
        await asyncio.sleep(2)


class ILinkChannel(AbstractChannel):
    name = "ilink"

    def __init__(
        self,
        token_path: Path | None = None,
        cursor_path: Path | None = None,
    ):
        from pathlib import Path as _Path
        import os as _os

        state_dir = _Path(_os.environ.get("WX_CC_STATE", _Path.home() / ".wx-cc-bridge"))
        self.token_path = token_path or (state_dir / "token.json")
        self.cursor_path = cursor_path or (state_dir / "cursor.txt")
        self._client: ILinkClient | None = None
        self._cursor: str = ""

    async def login(self) -> None:
        self._client = ILinkClient()
        await _ilink_login(self._client, self.token_path)
        self._cursor = self._load_cursor()
        print(f"[ilink] start, cursor={self._cursor!r}")

    def _load_cursor(self) -> str:
        return self.cursor_path.read_text().strip() if self.cursor_path.exists() else ""

    def _save_cursor(self, c: str) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(c)

    async def recv_messages(self) -> AsyncIterator[Message]:
        client = self._client
        assert client is not None

        while True:
            try:
                data = await client.getupdates(self._cursor)
            except Exception as e:
                print(f"[ilink] poll error: {e!r}; retry in 2s")
                await asyncio.sleep(2)
                continue

            if "errcode" in data or "errmsg" in data:
                print(f"[ilink] server error: {data}; retry in 2s")
                await asyncio.sleep(2)
                continue

            new_cursor = data.get("get_updates_buf")
            if new_cursor and new_cursor != self._cursor:
                self._cursor = new_cursor
                self._save_cursor(self._cursor)

            for raw in data.get("msgs") or []:
                sender, ctx_token = _extract_meta(raw)
                text = _extract_text(raw)
                if not (sender and ctx_token and text):
                    print(f"[ilink] skipped: {json.dumps(raw, ensure_ascii=False)[:400]}")
                    continue
                yield Message(
                    from_user_id=sender,
                    to_user_id="",
                    content=text,
                    msg_id=None,
                    context_token=ctx_token,
                    msg_type="text",
                    raw=raw,
                )

    async def send_text(
        self,
        to_user_id: str,
        context_token: str | None,
        text: str,
    ) -> None:
        client = self._client
        assert client is not None
        ctx = context_token or ""
        resp = await client.send_text(to_user_id, ctx, text)
        print(f"[ilink] sent resp={resp}")

    async def send_typing(self, to_user_id: str, status: int) -> None:
        client = self._client
        assert client is not None
        # typing 需要先 get_config 获取 ticket
        cfg = await client.get_config(to_user_id)
        ticket = cfg.get("typing_ticket")
        if not ticket:
            print(f"[ilink] send_typing: no ticket for {to_user_id}")
            return
        resp = await client.send_typing(to_user_id, ticket, status)
        print(f"[ilink] send_typing status={status} resp={resp}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
