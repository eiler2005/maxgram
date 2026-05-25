"""Durable MAX -> Telegram text retry helpers."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from .contracts import MaxMessage, TelegramBridgePort
from .retry_policy import (
    TEXT_RETRY_LEASE_SECONDS,
    TEXT_RETRY_POLL_SECONDS,
    TEXT_RETRY_TTL_SECONDS,
    exponential_backoff_seconds,
    is_expired,
)
from ..db.repository import MessageRecord, PendingInboundMessage, Repository
from ..logging_utils import build_max_flow_id, log_event

logger = logging.getLogger("src.bridge.core")
OpsSender = Callable[[str], Awaitable[None]]
TgErrorGetter = Callable[[], Optional[str]]


def compose_text_only_inbound_payload(msg: MaxMessage) -> str:
    sender_prefix = ""
    if msg.is_own:
        sender_prefix = "[Вы] "
    elif not msg.is_dm and msg.sender_name:
        sender_prefix = f"[{msg.sender_name}] "

    body_text = f"{sender_prefix}{msg.text}".strip() if msg.text else ""
    extra_text = "\n".join(part for part in msg.rendered_texts if part).strip()
    return "\n".join(part.strip() for part in (body_text, extra_text) if part and part.strip())


def is_text_only_inbound_retry_candidate(msg: MaxMessage) -> bool:
    if msg.attachments or msg.attachment_failures:
        return False
    return bool(compose_text_only_inbound_payload(msg))


def is_retryable_tg_delivery_error(error: Optional[str]) -> bool:
    if not error:
        return True
    lowered = error.lower()
    permanent_markers = (
        "chat not found",
        "bot was blocked",
        "bot was kicked",
        "message thread not found",
        "topic not found",
        "not enough rights",
        "have no rights",
        "message is too long",
        "can't parse entities",
        "unsupported url protocol",
        "file is too big",
    )
    if any(marker in lowered for marker in permanent_markers):
        return False
    retry_markers = (
        "retry_after",
        "retry after",
        "too many requests",
        "timeout",
        "timed out",
        "connection",
        "network",
        "server disconnected",
        "bad gateway",
        "gateway timeout",
        "internal server error",
        "502",
        "503",
        "504",
    )
    return any(marker in lowered for marker in retry_markers) or "bad request" not in lowered


def pending_inbound_retry_delay(attempts_after_failure: int) -> int:
    return exponential_backoff_seconds(
        attempts_after_failure,
        base_seconds=30,
        cap_seconds=3600,
        max_exponent=7,
    )


async def enqueue_text_inbound_retry(
    *,
    repo: Repository,
    msg: MaxMessage,
    topic_id: int,
    error: str,
    attempts: int = 0,
) -> int:
    now = int(time.time())
    return await repo.enqueue_pending_inbound(
        PendingInboundMessage(
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=topic_id,
            text=compose_text_only_inbound_payload(msg),
            status="pending",
            attempts=max(0, attempts),
            next_attempt_at=now,
            last_error=error,
            created_at=now,
        )
    )


def _get_last_tg_error(tg: TelegramBridgePort) -> str:
    getter = getattr(tg, "get_last_send_error", None)
    if callable(getter):
        return getter() or "tg_send_failed"
    return "tg_send_failed"


async def process_pending_inbound_message(
    *,
    repo: Repository,
    tg: TelegramBridgePort,
    stats: dict[str, int | float],
    job: PendingInboundMessage,
    send_ops_notification: OpsSender | None = None,
    ttl_seconds: int = TEXT_RETRY_TTL_SECONDS,
):
    if not job.id:
        return
    flow_id = build_max_flow_id(job.max_chat_id, job.max_msg_id) or "mx:pending-inbound"
    now = int(time.time())
    if is_expired(job.created_at, ttl_seconds, now=now):
        await repo.mark_pending_inbound_failed(job.id, error="expired", now=now)
        if send_ops_notification is not None:
            await send_ops_notification(
                "⚠️ Отложенное MAX→TG сообщение не доставлено за 48 часов; "
                f"текст удалён из очереди. max_chat_id={job.max_chat_id} "
                f"max_msg_id={job.max_msg_id} topic={job.tg_topic_id}"
            )
        log_event(
            logger,
            logging.ERROR,
            "bridge.inbound_retry.failed",
            flow_id=flow_id,
            direction="inbound",
            stage="inbound_retry",
            outcome="failed",
            reason="expired",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            pending_inbound_id=job.id,
        )
        return

    if not job.text:
        await repo.mark_pending_inbound_failed(job.id, error="missing_text", now=now)
        return

    log_event(
        logger,
        logging.INFO,
        "bridge.inbound_retry.attempt_started",
        flow_id=flow_id,
        direction="inbound",
        stage="inbound_retry",
        outcome="started",
        max_chat_id=job.max_chat_id,
        max_msg_id=job.max_msg_id,
        tg_topic_id=job.tg_topic_id,
        pending_inbound_id=job.id,
        attempts=int(job.attempts or 0) + 1,
    )
    tg_msg_id = await tg.send_text(job.tg_topic_id, job.text, flow_id=flow_id)
    if tg_msg_id:
        await repo.save_message(
            MessageRecord(
                max_msg_id=job.max_msg_id,
                max_chat_id=job.max_chat_id,
                tg_msg_id=tg_msg_id,
                tg_topic_id=job.tg_topic_id,
                direction="inbound",
                created_at=int(time.time()),
            )
        )
        await repo.log_delivery(
            job.max_msg_id,
            job.max_chat_id,
            "inbound",
            "delivered",
            attempts=int(job.attempts or 0) + 1,
        )
        await repo.mark_pending_inbound_delivered(job.id, tg_msg_id=tg_msg_id)
        stats["inbound_text"] += 1
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound_retry.delivered",
            flow_id=flow_id,
            direction="inbound",
            stage="inbound_retry",
            outcome="delivered",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            tg_msg_id=tg_msg_id,
            pending_inbound_id=job.id,
        )
        return

    error = _get_last_tg_error(tg)
    attempts_after_failure = int(job.attempts or 0) + 1
    if is_retryable_tg_delivery_error(error):
        delay = pending_inbound_retry_delay(attempts_after_failure)
        await repo.mark_pending_inbound_retry(
            job.id,
            error=error,
            next_attempt_at=int(time.time()) + delay,
        )
        log_event(
            logger,
            logging.WARNING,
            "bridge.inbound_retry.retry_scheduled",
            flow_id=flow_id,
            direction="inbound",
            stage="inbound_retry",
            outcome="retry",
            reason="tg_delivery_error",
            max_chat_id=job.max_chat_id,
            max_msg_id=job.max_msg_id,
            tg_topic_id=job.tg_topic_id,
            pending_inbound_id=job.id,
            attempts=attempts_after_failure,
            retry_in_seconds=delay,
            error=error,
        )
        return

    await repo.mark_pending_inbound_failed(job.id, error=error)
    if send_ops_notification is not None:
        await send_ops_notification(
            "⚠️ Отложенное MAX→TG сообщение не удалось доставить автоматически; "
            f"текст удалён из очереди. max_chat_id={job.max_chat_id} "
            f"max_msg_id={job.max_msg_id} topic={job.tg_topic_id}"
        )
    log_event(
        logger,
        logging.ERROR,
        "bridge.inbound_retry.failed",
        flow_id=flow_id,
        direction="inbound",
        stage="inbound_retry",
        outcome="failed",
        reason="non_retryable_tg_failure",
        max_chat_id=job.max_chat_id,
        max_msg_id=job.max_msg_id,
        tg_topic_id=job.tg_topic_id,
        pending_inbound_id=job.id,
        error=error,
    )


async def run_pending_inbound_messages(
    *,
    repo: Repository,
    tg: TelegramBridgePort,
    stats: dict[str, int | float],
    send_ops_notification: OpsSender | None = None,
    poll_interval: int = TEXT_RETRY_POLL_SECONDS,
    lease_seconds: int = TEXT_RETRY_LEASE_SECONDS,
    ttl_seconds: int = TEXT_RETRY_TTL_SECONDS,
    limit: int = 5,
):
    while True:
        now = int(time.time())
        jobs = await repo.get_due_pending_inbound(now=now, limit=limit)
        for job in jobs:
            if not job.id:
                continue
            leased = await repo.lease_pending_inbound(
                job.id,
                lease_until=int(time.time()) + lease_seconds,
            )
            if not leased:
                continue
            await process_pending_inbound_message(
                repo=repo,
                tg=tg,
                stats=stats,
                job=job,
                send_ops_notification=send_ops_notification,
                ttl_seconds=ttl_seconds,
            )
        await asyncio.sleep(poll_interval)
