"""Tests for FeishuConfig."""
from pathlib import Path
import json, tempfile

import pytest


def test_feishu_config_save_load():
    from wx_cc_bridge.channels.feishu import FeishuConfig

    cfg = FeishuConfig(app_id="cli_abc123", app_secret="secret_xyz")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        cfg.save(path)
        loaded = FeishuConfig.load(path)
        assert loaded.app_id == "cli_abc123"
        assert loaded.app_secret == "secret_xyz"
        assert loaded.domain == "feishu"  # default


def test_feishu_config_save_load_lark_domain():
    from wx_cc_bridge.channels.feishu import FeishuConfig

    cfg = FeishuConfig(app_id="cli_abc", app_secret="sec", domain="lark")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        cfg.save(path)
        loaded = FeishuConfig.load(path)
        assert loaded.domain == "lark"