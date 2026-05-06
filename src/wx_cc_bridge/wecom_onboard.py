"""Native WeCom QR onboarding for wx-cc-bridge.

Flow:
1) Request QR code payload from WeCom endpoint
2) Render QR in terminal
3) Poll scan result until botId/secret returned
4) Save to wx-cc-bridge wecom_config.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

import httpx
import qrcode

from .channels.wecom import WeComConfig

QR_CODE_PAGE = "https://work.weixin.qq.com/ai/qc/gen?source=wecom-cli&scode="
QR_GENERATE_URL_TEMPLATE = "https://work.weixin.qq.com/ai/qc/generate?source=wecom-cli&plat={plat}"
QR_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"


def _state_dir() -> Path:
    return Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))


def _wecom_config_path() -> Path:
    return _state_dir() / "wecom_config.json"


def _plat_code() -> int:
    name = platform.system().lower()
    if name == "darwin":
        return 1
    if name == "windows":
        return 2
    if name == "linux":
        return 3
    return 0


def _render_qr_terminal(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # 用半高字符压缩终端二维码高度：
    # 两行像素合并为一行字符（▀/▄/█/空格），在保持可扫性的同时减少占屏。
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

    print("")
    if len(matrix) % 2 == 1:
        matrix = [*matrix, [False] * len(matrix[0])]
    for i in range(0, len(matrix), 2):
        print(row_pair_to_text(matrix[i], matrix[i + 1]))
    print("")


def _fetch_qr(client: httpx.Client) -> tuple[str, str]:
    url = QR_GENERATE_URL_TEMPLATE.format(plat=_plat_code())
    r = client.get(url)
    r.raise_for_status()
    resp = r.json()
    data = resp.get("data") or {}
    scode = str(data.get("scode") or "").strip()
    auth_url = str(data.get("auth_url") or "").strip()
    if not scode or not auth_url:
        raise RuntimeError(f"获取二维码失败，响应异常: {resp}")
    return scode, auth_url


def _poll_result(client: httpx.Client, scode: str, timeout_sec: int, poll_interval_sec: float) -> tuple[str, str]:
    start = time.time()
    while time.time() - start < timeout_sec:
        r = client.get(QR_QUERY_URL, params={"scode": scode})
        r.raise_for_status()
        resp = r.json()
        data = resp.get("data") or {}
        status = str(data.get("status") or "")
        if status == "success":
            bot_info = data.get("bot_info") or {}
            bot_id = str(bot_info.get("botid") or "").strip()
            secret = str(bot_info.get("secret") or "").strip()
            if bot_id and secret:
                return bot_id, secret
            raise RuntimeError("扫码成功但未拿到 botid/secret")
        time.sleep(poll_interval_sec)
    raise TimeoutError(f"扫码超时（{timeout_sec}s）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wx-wecom-setup",
        description="原生企业微信扫码接入：获取 botId/secret 并写入 wx-cc-bridge 配置",
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
        default=3.0,
        help="轮询间隔秒数（默认 3）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_wecom_config_path(),
        help="wx-cc-bridge wecom 配置输出路径",
    )
    args = parser.parse_args(argv)

    try:
        with httpx.Client(timeout=15.0) as client:
            print("[wecom-setup] 正在获取扫码二维码...")
            scode, auth_url = _fetch_qr(client)
            print("[wecom-setup] 请使用企业微信扫码：")
            _render_qr_terminal(auth_url)
            print(f"[wecom-setup] 也可打开链接扫码: {QR_CODE_PAGE}{scode}")
            print("[wecom-setup] 等待扫码结果...")
            bot_id, secret = _poll_result(client, scode, args.timeout, args.poll_interval)
            print("[wecom-setup] 扫码成功，已获取 botId/secret")

        cfg = WeComConfig(bot_id=bot_id, secret=secret)
        cfg.save(args.output)
    except Exception as e:
        print(f"[wecom-setup] 失败: {e}", file=sys.stderr)
        return 1

    print(f"[wecom-setup] 导入成功: {args.output}")
    print("[wecom-setup] 下一步可运行: PYTHONPATH=src python3 -m wx_cc_bridge.bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
