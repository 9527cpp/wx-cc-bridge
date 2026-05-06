"""飞书 Bot 接入引导：提示用户创建应用并输入凭证。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from .channels.feishu import FeishuConfig


def _state_dir() -> Path:
    return Path(os.environ.get("WX_CC_STATE", Path.home() / ".wx-cc-bridge"))


def _feishu_config_path() -> Path:
    return _state_dir() / "feishu_config.json"


def _verify_credentials(app_id: str, app_secret: str, domain: str) -> bool:
    """验证 app_id/app_secret 是否有效（尝试获取 token）。"""
    api_root = "https://open.feishu.cn" if domain == "feishu" else "https://open.larksuite.com"
    url = f"{api_root}/open-apis/auth/v3/tenant_access_token/internal"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(url, json={"app_id": app_id, "app_secret": app_secret})
            r.raise_for_status()
            data = r.json()
            return data.get("code") == 0 and bool(data.get("tenant_access_token"))
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wx-feishu-setup",
        description="飞书机器人接入引导：验证 app_id/app_secret 并写入配置",
    )
    parser.add_argument(
        "--app-id",
        type=str,
        default=None,
        help="飞书应用的 App ID",
    )
    parser.add_argument(
        "--app-secret",
        type=str,
        default=None,
        help="飞书应用的 App Secret",
    )
    parser.add_argument(
        "--domain",
        type=str,
        default="feishu",
        choices=["feishu", "lark"],
        help="飞书域名（默认 feishu，海外用 lark）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_feishu_config_path(),
        help="飞书配置输出路径",
    )
    args = parser.parse_args(argv)

    app_id = args.app_id
    app_secret = args.app_secret

    # 交互式输入
    if not app_id:
        print("请先去 open.feishu.cn 创建企业自建应用，开启「机器人」能力")
        print("然后复制 App ID 和 App Secret 粘贴于此")
        print()
        app_id = input("App ID: ").strip()
        if not app_id:
            print("[feishu-setup] App ID 不能为空", file=sys.stderr)
            return 1

    if not app_secret:
        app_secret = input("App Secret: ").strip()
        if not app_secret:
            print("[feishu-setup] App Secret 不能为空", file=sys.stderr)
            return 1

    print(f"[feishu-setup] 正在验证凭证 (domain={args.domain})...")
    if not _verify_credentials(app_id, app_secret, args.domain):
        print("[feishu-setup] 凭证验证失败，请检查 App ID 和 App Secret 是否正确", file=sys.stderr)
        return 1

    cfg = FeishuConfig(app_id=app_id, app_secret=app_secret, domain=args.domain)
    cfg.save(args.output)
    print(f"[feishu-setup] 验证成功，配置已保存到 {args.output}")
    print("[feishu-setup] 下一步可运行: PYTHONPATH=src python3 -m wx_cc_bridge.bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
