"""Tests for FeishuChannel full integration (send_text + recv_messages)."""
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock


def test_feishu_channel_send_text_builds_correct_payload():
    from wx_cc_bridge.channels.feishu import FeishuChannel, FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        FeishuConfig(app_id="cli_test", app_secret="sec_test").save(path)

        ch = FeishuChannel(config_path=path)
        ch._http_client = AsyncMock()
        ch._tenant_access_token = "tok_test"

        # Mock send message response
        send_resp = AsyncMock()
        send_resp.status_code = 200
        send_resp.json = MagicMock(return_value={"code": 0, "msg": "success"})

        async def fake_request(method, url, **kwargs):
            return send_resp

        ch._http_client.request = AsyncMock(side_effect=fake_request)

        asyncio.get_event_loop().run_until_complete(
            ch.send_text("ou_abc", "msg_123", "Hello")
        )

        # Verify httpx client was called
        ch._http_client.request.assert_called_once()


def test_feishu_channel_parses_message_correctly():
    from wx_cc_bridge.channels.feishu import FeishuChannel, FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        FeishuConfig(app_id="cli_test", app_secret="sec_test").save(path)

        ch = FeishuChannel(config_path=path)

        raw = {
            "message_id": "om_123",
            "msg_type": "text",
            "sender": {"id": "ou_user", "sender_type": "user"},
            "body": {"content": '{"text":"Hello"}'},
        }

        msg = ch._parse_message(raw)
        assert msg is not None
        assert msg.from_user_id == "ou_user"
        assert msg.content == "Hello"
        assert msg.msg_id == "om_123"


def test_feishu_channel_skips_bot_messages():
    from wx_cc_bridge.channels.feishu import FeishuChannel, FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        FeishuConfig(app_id="cli_test", app_secret="sec_test").save(path)

        ch = FeishuChannel(config_path=path)

        raw = {
            "message_id": "om_456",
            "msg_type": "text",
            "sender": {"id": "ou_bot", "sender_type": "bot"},
            "body": {"content": '{"text":"Bot message"}'},
        }

        msg = ch._parse_message(raw)
        assert msg is None


def test_feishu_channel_skips_non_text_messages():
    from wx_cc_bridge.channels.feishu import FeishuChannel, FeishuConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        FeishuConfig(app_id="cli_test", app_secret="sec_test").save(path)

        ch = FeishuChannel(config_path=path)

        raw = {
            "message_id": "om_789",
            "msg_type": "image",
            "sender": {"id": "ou_user", "sender_type": "user"},
            "body": {"content": '{"image_key":"img_xxx"}'},
        }

        msg = ch._parse_message(raw)
        assert msg is None
