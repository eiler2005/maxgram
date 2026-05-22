"""Bridge message mapping helpers."""

import time
from typing import Optional

from .contracts import MaxMessage
from ..db.repository import MessageRecord, Repository


async def save_inbound_idempotency_key(repo: Repository, msg: MaxMessage):
    """Persist inbound MAX id before Telegram send."""
    await repo.save_message(MessageRecord(
        max_msg_id=msg.msg_id,
        max_chat_id=msg.chat_id,
        tg_msg_id=None,
        tg_topic_id=None,
        direction="inbound",
        created_at=int(time.time()),
    ))


async def save_inbound_delivery_mapping(
    repo: Repository,
    msg: MaxMessage,
    *,
    tg_msg_id: Optional[int],
    tg_topic_id: int,
):
    await repo.save_message(MessageRecord(
        max_msg_id=msg.msg_id,
        max_chat_id=msg.chat_id,
        tg_msg_id=tg_msg_id,
        tg_topic_id=tg_topic_id,
        direction="inbound",
        created_at=int(time.time()),
    ))


async def save_outbound_mapping(
    repo: Repository,
    *,
    max_msg_id: str,
    max_chat_id: str,
    tg_topic_id: int,
):
    await repo.save_message(MessageRecord(
        max_msg_id=max_msg_id,
        max_chat_id=max_chat_id,
        tg_msg_id=None,
        tg_topic_id=tg_topic_id,
        direction="outbound",
        created_at=int(time.time()),
    ))


async def save_tg_reply_mapping(
    repo: Repository,
    *,
    tg_msg_id: int,
    max_chat_id: str,
    max_msg_id: str,
    tg_topic_id: Optional[int],
    source: str,
):
    await repo.save_tg_reply_mapping(
        tg_msg_id,
        max_chat_id,
        max_msg_id,
        tg_topic_id,
        source=source,
    )
