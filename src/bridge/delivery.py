"""Bridge delivery-log helpers."""

import time
from typing import Optional

from ..db.repository import Repository


def build_failed_outbound_id(topic_id: int, tg_msg_id: Optional[int]) -> str:
    suffix = tg_msg_id if tg_msg_id is not None else int(time.time())
    return f"out_fail:{topic_id}:{suffix}"


async def log_outbound_failure(
    repo: Repository,
    *,
    topic_id: int,
    tg_msg_id: Optional[int],
    max_chat_id: str,
    error: str,
    attempts: int = 1,
):
    await repo.log_delivery(
        build_failed_outbound_id(topic_id, tg_msg_id),
        max_chat_id,
        "outbound",
        "failed",
        error,
        attempts=max(attempts, 1),
    )
