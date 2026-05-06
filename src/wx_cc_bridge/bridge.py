"""M2 bridge: WeChat ↔ Claude Code CLI.

收到消息 → /命令走 commands.handle → 否则 subprocess claude -p，带 session_id。
每个 chat_id 串行化；不同 chat 并发。

支持多 channel：iLink（个人微信）、WeCom Bot（企业微信）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import subprocess
import time as time_module
from pathlib import Path

import httpx

from . import claude_runner, commands
from .channels import AbstractChannel, ILinkChannel, WeComChannel
from .channels.wecom import WeComConfig
from .session_store import SessionStore
from . import wechat_onboard, wecom_onboard

TYPING_HEARTBEAT_SEC = 3.0
SOFT_NOTICE_SEC = 90.0

STATE_DIR = Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))
SESSIONS_PATH = STATE_DIR / "sessions.json"
DEFAULT_WS_ROOT = Path(
    os.environ.get("WX_CC_WS_ROOT", Path.home() / "cc-wx-sessions")
)

TOKEN_PATH = STATE_DIR / "token.json"
WECOM_CONFIG_PATH = STATE_DIR / "wecom_config.json"


def _restart_service() -> None:
    """扫码成功后自动重启后台服务。"""
    print("[onboard] 扫码成功，正在自动执行: make restart-service")
    try:
        subprocess.run(["make", "restart-service"], check=True)
        print("[onboard] 后台服务重启完成")
    except Exception as e:
        print(f"[onboard] 自动重启服务失败: {e}")
        print("[onboard] 请手动执行: make restart-service")


def _render_side_by_side(left_lines: list[str], right_lines: list[str], left_label: str, right_label: str) -> None:
    """并排渲染两组行，带标签。"""
    max_len = max(len(line) for line in left_lines + right_lines)
    sep = "  "
    header_left = f"┌─ {left_label} ─┐"
    header_right = f"┌─ {right_label} ─┐"
    header = f"{header_left}{sep}{header_right}"
    print(f"\n{'':─<{len(header)}}")
    print(header)
    print(f"{'':─<{len(header)}}")
    for l, r in zip(left_lines, right_lines):
        print(f"{l:<{max_len}}{sep}{r}")
    print(f"{'':─<{len(header)}}")
    print("")


async def _wecom_fetch_qr() -> tuple[str, str]:
    """获取企微二维码，返回 (scode, auth_url)。"""
    plat_map = {"darwin": 1, "windows": 2, "linux": 3}
    plat = plat_map.get(platform.system().lower(), 0)
    url = f"https://work.weixin.qq.com/ai/qc/generate?source=wecom-cli&plat={plat}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        resp = r.json()
        data = resp.get("data") or {}
        scode = str(data.get("scode") or "").strip()
        auth_url = str(data.get("auth_url") or "").strip()
        if not scode or not auth_url:
            raise RuntimeError(f"获取企微二维码失败: {resp}")
        return scode, auth_url


async def run_onboarding() -> bool:
    """并排显示两个二维码，任一扫码成功后退出。"""
    # 按用户期望：无论当前是否已有 token/config，都展示双码供重绑。
    need_wechat = os.environ.get("WX_CC_ENABLE_ILINK", "1") != "0"
    need_wecom = os.environ.get("WX_CC_ENABLE_WECOM", "1") != "0"

    print("[onboard] 检测到未配置，开始扫码引导...")

    # 并行获取两个 QR
    wechat_task = asyncio.create_task(wechat_onboard.fetch_qr_only()) if need_wechat else None
    wecom_task = asyncio.create_task(_wecom_fetch_qr()) if need_wecom else None

    async def poll_wechat(qrcode_id: str, qrcode_content: str, timeout: float, poll_interval: float) -> bool:
        base_url = os.environ.get("WX_CC_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
        async with httpx.AsyncClient(timeout=40.0) as client:
            start = time_module.time()
            while time_module.time() - start < timeout:
                r = await client.get(f"{base_url}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode_id})
                r.raise_for_status()
                resp = r.json()
                if str(resp.get("status") or "") == "confirmed":
                    bot_token = resp.get("bot_token") or ""
                    base_url_out = resp.get("baseurl") or base_url
                    if bot_token:
                        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
                        TOKEN_PATH.write_text(json.dumps({"bot_token": bot_token, "baseurl": base_url_out.rstrip("/")}))
                        print(f"[onboard] 个人微信扫码成功，已保存到 {TOKEN_PATH}")
                        return True
                await asyncio.sleep(poll_interval)
        return False

    async def poll_wecom(scode: str, timeout: float, poll_interval: float) -> bool:
        query_url = "https://work.weixin.qq.com/ai/qc/query_result"
        async with httpx.AsyncClient(timeout=15.0) as client:
            start = time_module.time()
            while time_module.time() - start < timeout:
                r = await client.get(query_url, params={"scode": scode})
                r.raise_for_status()
                resp = r.json()
                data = resp.get("data") or {}
                if str(data.get("status") or "") == "success":
                    bot_info = data.get("bot_info") or {}
                    bot_id = str(bot_info.get("botid") or "").strip()
                    secret = str(bot_info.get("secret") or "").strip()
                    if bot_id and secret:
                        WECOM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                        cfg = WeComConfig(bot_id=bot_id, secret=secret)
                        cfg.save(WECOM_CONFIG_PATH)
                        print(f"[onboard] 企业微信扫码成功，已保存到 {WECOM_CONFIG_PATH}")
                        return True
                await asyncio.sleep(poll_interval)
        return False

    timeout = 300.0
    poll_interval = 2.0

    while True:
        # 获取 QR
        if wecom_task and not wecom_task.done():
            await wecom_task
        if wechat_task and not wechat_task.done():
            await wechat_task

        wechat_lines: list[str] = []
        wecom_lines: list[str] = []
        wechat_id, wechat_content = "", ""
        wecom_scode, wecom_auth_url = "", ""

        if need_wechat and wechat_task:
            try:
                wechat_id, wechat_content = wechat_task.result()
                wechat_lines = wechat_onboard.render_qr_to_lines(wechat_content)
            except Exception as e:
                print(f"[onboard] 获取个人微信二维码失败: {e}")
                need_wechat = False

        if need_wecom and wecom_task:
            try:
                wecom_scode, wecom_auth_url = wecom_task.result()
                wecom_lines = wecom_onboard._render_qr_to_lines(wecom_auth_url)
            except Exception as e:
                print(f"[onboard] 获取企业微信二维码失败: {e}")
                need_wecom = False

        if not wechat_lines and not wecom_lines:
            print("[onboard] 两个二维码都获取失败，退出")
            return False

        _render_side_by_side(wechat_lines, wecom_lines, "个人微信", "企业微信")
        if need_wechat:
            print(f"[onboard] 个人微信替代链接: {wechat_content}")
        if need_wecom:
            print(f"[onboard] 企业微信替代链接: https://work.weixin.qq.com/ai/qc/gen?source=wecom-cli&scode={wecom_scode}")

        # 创建轮询任务
        poll_wechat_task = asyncio.create_task(poll_wechat(wechat_id, wechat_content, timeout, poll_interval)) if need_wechat and wechat_id else None
        poll_wecom_task = asyncio.create_task(poll_wecom(wecom_scode, timeout, poll_interval)) if need_wecom and wecom_scode else None

        done, pending = await asyncio.wait(
            [t for t in [poll_wechat_task, poll_wecom_task] if t],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        for t in done:
            if t.result():
                _restart_service()
                return True

        # 两个都超时了，重新获取二维码
        print("[onboard] 扫码超时，重新获取二维码...")
        wechat_task = asyncio.create_task(wechat_onboard.fetch_qr_only()) if need_wechat else None
        wecom_task = asyncio.create_task(_wecom_fetch_qr()) if need_wecom else None


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


async def main(onboard_only: bool = False) -> None:
    # onboard_only=True: 只显示二维码，扫码成功后重启 service 并退出
    # onboard_only=False: 正常启动 bridge（service 后台运行走这里）
    if onboard_only:
        ok = await run_onboarding()
        if ok:
            return  # _restart_service() 已经 sys.exit 了
        print("[bridge] onboard 超时退出")
        return

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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--onboard", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main(onboard_only=args.onboard))
    except KeyboardInterrupt:
        print("\n[exit]")


if __name__ == "__main__":
    run()
