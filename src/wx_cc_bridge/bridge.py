"""M2 bridge: WeChat ↔ Claude Code CLI.

收到消息 → /命令走 commands.handle → 否则 subprocess claude -p，带 session_id。
每个 chat_id 串行化；不同 chat 并发。

支持多 channel：iLink（个人微信）、WeCom Bot（企业微信）。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from . import claude_runner, commands
from .channels import AbstractChannel, ILinkChannel, WeComChannel
from .channels.wecom import WeComConfig
from .session_store import SessionStore

TYPING_HEARTBEAT_SEC = 3.0
SOFT_NOTICE_SEC = 90.0

STATE_DIR = Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))
SESSIONS_PATH = STATE_DIR / "sessions.json"
DEFAULT_WS_ROOT = Path(
    os.environ.get("WX_CC_WS_ROOT", Path.home() / "cc-wx-sessions")
)


def default_cwd_for(chat_id: str) -> str:
    safe = chat_id.replace("@", "_at_").replace("/", "_").replace(" ", "_")
    return str(DEFAULT_WS_ROOT / safe)


def _build_channels() -> list[AbstractChannel]:
    """根据配置构建所有启用的 channel。"""
    channels: list[AbstractChannel] = []

    # iLink channel：检测 token.json 是否存在
    if os.environ.get("WX_CC_ENABLE_ILINK", "1") != "0":
        token_path = STATE_DIR / "token.json"
        if token_path.exists():
            channels.append(ILinkChannel(token_path=token_path))
        else:
            print(f"[bridge] iLink token not found ({token_path}), skipping iLink channel")

    # WeCom Bot channel：检测 wecom_config.json 是否存在
    if os.environ.get("WX_CC_ENABLE_WECOM", "1") != "0":
        wecom_config_path = STATE_DIR / "wecom_config.json"
        if wecom_config_path.exists():
            channels.append(WeComChannel(config_path=wecom_config_path))
        else:
            print(f"[bridge] WeCom config not found ({wecom_config_path}), skipping WeCom channel")
        # 也支持直接传入配置（方便测试）
        wecom_bot_id = os.environ.get("WX_CC_WECOM_BOT_ID", "")
        wecom_secret = os.environ.get("WX_CC_WECOM_SECRET", "")
        if wecom_bot_id and wecom_secret and channels and not isinstance(channels[-1], WeComChannel):
            channels.append(WeComChannel(config=WeComConfig(bot_id=wecom_bot_id, secret=wecom_secret)))

    return channels


@contextlib.asynccontextmanager
async def typing_indicator(
    channel: AbstractChannel,
    chat_id: str,
    max_duration: float | None = None,
):
    """Show "正在输入" in WeChat while the body executes.

    Best-effort: any typing API failure is logged and ignored so it can't
    block the real reply flow.
    """
    try:
        await channel.send_typing(chat_id, status=1)
    except Exception as e:
        print(f"[typing] error: {e!r}")
        # 如果 channel 不支持 typing，继续执行
        yield
        return

    stop = asyncio.Event()

    async def loop() -> None:
        elapsed = 0.0
        while not stop.is_set():
            if max_duration is not None and elapsed >= max_duration:
                print(f"[typing] soft cutoff hit ({max_duration}s), stop heartbeat")
                return
            try:
                await channel.send_typing(chat_id, status=1)
            except Exception as e:
                print(f"[typing] keepalive error: {e!r}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_HEARTBEAT_SEC)
                return
            except asyncio.TimeoutError:
                elapsed += TYPING_HEARTBEAT_SEC
                continue

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await task
        try:
            await channel.send_typing(chat_id, status=2)
        except Exception as e:
            print(f"[typing] cancel error: {e!r}")


async def handle_message(
    msg,  # Message from channels.base
    channel: AbstractChannel,
    store: SessionStore,
    locks: dict[str, asyncio.Lock],
) -> None:
    """处理一条消息，分发到 commands 或 claude_runner。"""
    chat_id = msg.from_user_id
    ctx_token = msg.context_token
    text = msg.content
    channel_name = channel.name

    cmd_reply = await commands.handle(text, chat_id, store, default_cwd_for)
    if cmd_reply is not None:
        try:
            await channel.send_text(chat_id, ctx_token, cmd_reply)
        except Exception as e:
            print(f"[{channel_name}] [send cmd-reply] error: {e!r}")
        return

    lock = locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        state = store.get(chat_id)
        cwd = Path(state.get("cwd") or default_cwd_for(chat_id))
        session_id = state.get("session_id")

        print(f"[{channel_name}] [claude→] {chat_id} cwd={cwd} sid={session_id}")
        t0 = asyncio.get_event_loop().time()

        async def _soft_notice() -> None:
            try:
                await asyncio.sleep(SOFT_NOTICE_SEC)
                await channel.send_text(chat_id, ctx_token, "(还在思考中，请稍等…)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{channel_name}] [soft-notice] error: {e!r}")

        notice_task = asyncio.create_task(_soft_notice())
        try:
            async with typing_indicator(channel, chat_id, max_duration=SOFT_NOTICE_SEC):
                result = await claude_runner.ask(text, cwd=cwd, session_id=session_id)
        finally:
            notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await notice_task
        dt = asyncio.get_event_loop().time() - t0
        print(
            f"[{channel_name}] [claude←] {dt:.1f}s "
            f"sid={result.session_id} err={bool(result.error)} "
            f"text_len={len(result.text)}"
        )

        if result.error:
            reply = f"[Claude 出错] {result.error[:800]}"
            print(f"[{channel_name}] [claude ERR] {result.error[:500]}")
        else:
            reply = result.text or "(Claude 回了空)"
            if result.session_id and result.session_id != session_id:
                store.set_session(chat_id, result.session_id)

        try:
            resp = await channel.send_text(chat_id, ctx_token, reply)
            print(f"[{channel_name}] [send←] resp={resp} ({len(reply)} chars sent)")
        except Exception as e:
            print(f"[{channel_name}] [send EXC] {e!r}")


async def channel_loop(
    channel: AbstractChannel,
    store: SessionStore,
    locks: dict[str, asyncio.Lock],
) -> None:
    """单个 channel 的消息接收循环。"""
    try:
        async for msg in channel.recv_messages():
            asyncio.create_task(handle_message(msg, channel, store, locks))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[{channel.name}] channel loop error: {e!r}")
    finally:
        await channel.close()


async def main() -> None:
    channels = _build_channels()
    if not channels:
        print("[bridge] no channels enabled, exiting")
        return

    store = SessionStore(SESSIONS_PATH)
    locks: dict[str, asyncio.Lock] = {}

    print(f"[bridge] start, channels={[ch.name for ch in channels]}, ws_root={DEFAULT_WS_ROOT}")

    # 登录所有 channel
    for ch in channels:
        try:
            await ch.login()
        except Exception as e:
            print(f"[{ch.name}] login failed: {e!r}, removing channel")
            await ch.close()
            channels.remove(ch)

    if not channels:
        print("[bridge] no channels available after login, exiting")
        return

    # 并行运行所有 channel 的消息循环
    tasks = [asyncio.create_task(channel_loop(ch, store, locks)) for ch in channels]
    await asyncio.gather(*tasks)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[exit]")


if __name__ == "__main__":
    run()
