"""Channel基类：定义所有channel必须实现的统一接口。"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class Message:
    """统一消息格式。所有channel收到的消息都转成这个格式。"""
    from_user_id: str
    to_user_id: str
    content: str                    # 统一成字符串（文本消息）
    msg_id: str | None = None
    context_token: str | None = None
    msg_type: str = "text"         # text | image | voice | file | video | mixed
    raw: dict = field(default_factory=dict)   # 原始消息体，供特定channel使用


class AbstractChannel(ABC):
    """Channel抽象基类。每个channel实现login、recv_messages、send_text。"""

    name: str  # e.g. "ilink" or "wecom"

    @abstractmethod
    async def login(self) -> None:
        """登录或加载已保存的认证凭证。"""

    @abstractmethod
    async def recv_messages(self) -> AsyncIterator[Message]:
        """无限循环接收消息，yield Message。"""

    @abstractmethod
    async def send_text(
        self,
        to_user_id: str,
        context_token: str | None,
        text: str,
    ) -> None:
        """发送文本消息给指定用户。"""

    async def send_typing(self, to_user_id: str, status: int) -> None:
        """发送 typing 指示器（status=1=正在输入, 2=取消）。不支持则忽略。"""

    async def close(self) -> None:
        """关闭channel，释放资源。子类可选override。"""
