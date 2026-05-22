"""Internal MAX adapter state dataclasses."""

import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class PendingOutboundAck:
    """Ожидаем подтверждение исходящего сообщения по эху из MAX."""
    chat_id: str
    text: str
    reply_to_msg_id: Optional[str]
    created_monotonic: float
    future: asyncio.Future

@dataclass
class ForwardedPayload:
    """Развёрнутое содержимое forward/channel сообщения MAX."""
    message: object
    chat_id: Optional[str]
    msg_id: Optional[str]
    link_type: Optional[str]

@dataclass
class OutboundFailureState:
    """Последняя ошибка исходящей отправки в MAX."""
    error: Optional[str]
    attempts: int = 0
