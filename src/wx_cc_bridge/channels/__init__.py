"""Channel抽象层：统一iLink和企业微信Bot的消息接口。"""
from __future__ import annotations

from .base import AbstractChannel, Message
from .feishu import FeishuChannel
from .ilink import ILinkChannel
from .wecom import WeComChannel

__all__ = ["AbstractChannel", "Message", "FeishuChannel", "ILinkChannel", "WeComChannel"]
