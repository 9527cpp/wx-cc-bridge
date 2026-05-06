"""Channel抽象层：统一iLink和企业微信Bot的消息接口。"""
from __future__ import annotations

from .base import AbstractChannel, Message
from .ilink import ILinkChannel
from .wecom import WeComChannel

__all__ = ["AbstractChannel", "Message", "ILinkChannel", "WeComChannel"]
