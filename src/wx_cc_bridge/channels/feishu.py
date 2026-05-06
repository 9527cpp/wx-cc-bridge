"""飞书 Bot 长连接 Channel。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DOMAIN_FEISHU = "feishu"
DEFAULT_DOMAIN_LARK = "lark"


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str = DEFAULT_DOMAIN_FEISHU

    @classmethod
    def load(cls, config_path: Path) -> "FeishuConfig":
        data = json.loads(config_path.read_text())
        return cls(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            domain=data.get("domain", DEFAULT_DOMAIN_FEISHU),
        )

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "domain": self.domain,
        }, ensure_ascii=False))


# FeishuChannel placeholder — 后续 Task 实现完整逻辑
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