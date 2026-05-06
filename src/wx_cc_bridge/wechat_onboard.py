"""Native iLink (个人微信) QR onboarding for wx-cc-bridge.

Flow:
1) Request QR code payload from iLink endpoint
2) Render QR in terminal
3) Poll scan result until bot_token returned
4) Save to wx-cc-bridge token.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import qrcode

STATE_DIR = Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))
LONGPOLL_TIMEOUT = 40.0


def _state_dir() -> Path:
    return Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))


def _token_path() -> Path:
    return _state_dir() / "token.json"


def render_qr_to_lines(url: str) -> list[str]:
    """返回紧凑 QR 码的行列表（两行像素合并为一行半高字符）。"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    def row_pair_to_text(top: list[bool], bottom: list[bool]) -> str:
        chars: list[str] = []
        for t, b in zip(top, bottom):
            if t and b:
                chars.append("█")
            elif t and not b:
                chars.append("▀")
            elif (not t) and b:
                chars.append("▄")
            else:
                chars.append(" ")
        return "".join(chars)

    lines: list[str] = []
    if len(matrix) % 2 == 1:
        matrix = [*matrix, [False] * len(matrix[0])]
    for i in range(0, len(matrix), 2):
        lines.append(row_pair_to_text(matrix[i], matrix[i + 1]))
    return lines


def render_qr(url: str) -> None:
    """在终端渲染紧凑 QR 码（两行像素合并为一行半高字符）。"""
    for line in render_qr_to_lines(url):
        print(line)


async def fetch_qr_only() -> tuple[str, str]:
    """只获取 QR 码，不渲染，返回 (qrcode_id, qrcode_content)。"""
    base_url = os.environ.get("WX_CC_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
    async with httpx.AsyncClient(timeout=LONGPOLL_TIMEOUT) as client:
        r = await client.get(f"{base_url}/ilink/bot/get_bot_qrcode", params={"bot_type": 3})
        r.raise_for_status()
        resp = r.json()
        qrcode_id = resp.get("qrcode") or resp.get("qrcode_str") or ""
        qrcode_content = resp.get("qrcode_img_content") or resp.get("qrcode_img") or qrcode_id
        if not qrcode_id:
            raise RuntimeError(f"获取二维码失败，响应异常: {resp}")
        return str(qrcode_id), str(qrcode_content)


async def poll_until_scanned(timeout_sec: int = 300, poll_interval_sec: float = 2.0) -> dict:
    """轮询扫码结果，成功则返回写入 token.json 的字典。"""
    base_url = os.environ.get("WX_CC_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
    async with httpx.AsyncClient(timeout=LONGPOLL_TIMEOUT) as client:
        # 先获取 QR
        qrcode_id, _ = await fetch_qr_only()
        print("[wechat-onboard] 等待个人微信扫码...")

        bot_token, base_url_out = await _poll_result_async(client, base_url, qrcode_id, timeout_sec, poll_interval_sec)
        print("[wechat-onboard] 扫码成功！")

        return {"bot_token": bot_token, "baseurl": base_url_out}


async def _poll_result_async(client: httpx.AsyncClient, base_url: str, qrcode_id: str, timeout_sec: int, poll_interval_sec: float) -> tuple[str, str]:
    """异步轮询扫码结果。"""
    start = time.time()
    while time.time() - start < timeout_sec:
        r = await client.get(f"{base_url}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode_id})
        r.raise_for_status()
        resp = r.json()
        status = str(resp.get("status") or "")
        if status == "confirmed":
            bot_token = resp.get("bot_token") or ""
            base_url_out = resp.get("baseurl") or base_url
            if bot_token:
                return str(bot_token), str(base_url_out).rstrip("/")
        await asyncio.sleep(poll_interval_sec)
    raise TimeoutError(f"扫码超时（{timeout_sec}s）")


async def interactive_login(timeout_sec: int = 300, poll_interval_sec: float = 2.0) -> dict:
    """交互式扫码登录，返回写入 token.json 的字典。"""
    base_url = os.environ.get("WX_CC_ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")
    async with httpx.AsyncClient(timeout=LONGPOLL_TIMEOUT) as client:
        print("[wechat-onboard] 正在获取个人微信登录二维码...")
        qrcode_id, qrcode_content = await fetch_qr_only()
        print("[wechat-onboard] 请使用微信扫码登录 ClawBot：")
        render_qr(qrcode_content)
        print(f"[wechat-onboard] 替代链接: {qrcode_content}")
        print("[wechat-onboard] 等待扫码结果...")

        bot_token, base_url_out = await _poll_result_async(client, base_url, qrcode_id, timeout_sec, poll_interval_sec)
        print("[wechat-onboard] 扫码成功！")

        return {"bot_token": bot_token, "baseurl": base_url_out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wx-wechat-setup",
        description="原生个人微信扫码接入：获取 bot_token 并写入 wx-cc-bridge 配置",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="扫码轮询超时秒数（默认 300）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="轮询间隔秒数（默认 2）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_token_path(),
        help="wx-cc-bridge token.json 输出路径",
    )
    args = parser.parse_args(argv)

    try:
        data = asyncio.run(interactive_login(args.timeout, args.poll_interval))
        token_path = args.output
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        print(f"[wechat-onboard] 失败: {e}", file=sys.stderr)
        return 1

    print(f"[wechat-onboard] 登录成功，已保存到 {args.output}")
    print("[wechat-onboard] 下一步可运行: PYTHONPATH=src python3 -m wx_cc_bridge.bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
