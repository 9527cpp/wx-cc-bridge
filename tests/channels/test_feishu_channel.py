import pytest
from pathlib import Path
import tempfile


def test_feishu_channel_loads_config():
    from wx_cc_bridge.channels.feishu import FeishuChannel, FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        cfg = FeishuConfig(app_id="cli_test", app_secret="sec_test")
        cfg.save(path)

        ch = FeishuChannel(config_path=path)
        assert ch.config.app_id == "cli_test"
        assert ch.name == "feishu"


def test_feishu_channel_raises_if_no_config():
    from wx_cc_bridge.channels.feishu import FeishuChannel

    with pytest.raises(RuntimeError, match="飞书配置不存在"):
        FeishuChannel(config_path=Path("/nonexistent/path.json"))


def test_feishu_config_load_preserves_domain():
    from wx_cc_bridge.channels.feishu import FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        FeishuConfig(app_id="a", app_secret="b", domain="lark").save(path)
        loaded = FeishuConfig.load(path)
        assert loaded.domain == "lark"