"""飞书 Bot 长连接 Channel。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DOMAIN_FEISHU = "feishu"
DEFAULT_DOMAIN_LARK = "lark"


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str = DEFAULT_DOMAIN_FEISHU

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
            domain=data.get("domain", DEFAULT_DOMAIN_FEISHU),
        )

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "domain": self.domain,
        }, ensure_ascii=False))


from typing import AsyncIterator
import asyncio


class FeishuChannel:
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

        self._ws_client = None
        self._msg_queue: asyncio.Queue | None = None
        self._abort_event: asyncio.Event | None = None

    async def login(self) -> None:
        print(f"[feishu] 配置加载 app_id={self.config.app_id}")
        print(f"[feishu] domain={self.config.domain}")

    async def recv_messages(self) -> AsyncIterator:
        # 空实现，仅供 bridge 编译通过，后续 Step 实现完整逻辑
        if False:
            yield

    async def send_text(self, to_user_id: str, context_token: str | None, text: str) -> None:
        raise NotImplementedError("TODO")

    async def close(self) -> None:
        print("[feishu] 已关闭")