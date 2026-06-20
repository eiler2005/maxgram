"""MAX message actions rendered as Telegram buttons and callbacks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .contracts import (
    MaxBridgePort,
    MaxMessage,
    TelegramCallbackAction,
    TelegramInlineButton,
)
from ..db.repository import Repository
from ..db.types import _json_loads
from ..logging_utils import log_event

logger = logging.getLogger("src.bridge.core")

MAX_TELEGRAM_ACTION_BUTTONS = 6
MAX_CALLBACK_DATA_BYTES = 64


@dataclass
class PreparedTelegramButtons:
    buttons: list[TelegramInlineButton] = field(default_factory=list)
    callback_action_ids: list[str] = field(default_factory=list)


def _safe_button_label(value: str | None, *, fallback: str) -> str:
    label = (value or "").strip()
    if not label:
        return fallback
    label = " ".join(label.split())
    if len(label) > 40:
        label = f"{label[:37].rstrip()}..."
    return label


async def prepare_telegram_buttons(
    *,
    repo: Repository,
    msg: MaxMessage,
    topic_id: int,
) -> PreparedTelegramButtons:
    prepared = PreparedTelegramButtons()
    seen: set[tuple[str, str]] = set()

    for action in msg.actions:
        if len(prepared.buttons) >= MAX_TELEGRAM_ACTION_BUTTONS:
            break
        url = (action.url or "").strip()
        if not url:
            continue
        key = (action.kind, url)
        if key in seen:
            continue
        seen.add(key)

        if action.kind == "open_url":
            prepared.buttons.append(
                TelegramInlineButton(
                    text=_safe_button_label(action.label, fallback="Открыть сайт"),
                    url=url,
                )
            )
            continue

        if action.kind == "max_join":
            action_id = await repo.create_callback_action(
                action_type="max_join",
                max_chat_id=msg.chat_id,
                max_msg_id=msg.msg_id,
                tg_topic_id=topic_id,
                source_type=action.source_type,
                payload={"url": url},
            )
            callback_data = f"max_join:{action_id}"
            if len(callback_data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
                continue
            prepared.buttons.append(
                TelegramInlineButton(
                    text=_safe_button_label(action.label, fallback="Вступить в MAX"),
                    callback_data=callback_data,
                )
            )
            prepared.callback_action_ids.append(action_id)

    return prepared


async def attach_callback_actions_to_message(
    *,
    repo: Repository,
    action_ids: list[str],
    tg_msg_id: int | None,
) -> None:
    if tg_msg_id is None:
        return
    for action_id in action_ids:
        await repo.attach_callback_action_message(action_id, tg_msg_id=tg_msg_id)


async def handle_telegram_callback_action(
    *,
    repo: Repository,
    max_adapter: MaxBridgePort,
    callback: TelegramCallbackAction,
    schedule_recovery_event_scan: Callable[[str], None],
) -> str:
    if callback.action != "max_join":
        return "Неизвестное действие"

    record = await repo.get_callback_action(callback.action_id)
    if record is None or record.action_type != "max_join":
        return "Действие не найдено"
    if record.status != "pending":
        return "Эта кнопка уже обработана"

    payload = _json_loads(record.payload_json, {})
    link = str(payload.get("url") or "").strip() if isinstance(payload, dict) else ""
    if not link:
        await repo.mark_callback_action_used(callback.action_id, error="missing_invite_link")
        return "MAX invite link не найден"

    log_event(
        logger,
        logging.INFO,
        "bridge.callback.max_join",
        direction="callback",
        stage="join",
        outcome="started",
        max_chat_id=record.max_chat_id,
        max_msg_id=record.max_msg_id,
        tg_topic_id=callback.topic_id or record.tg_topic_id,
        tg_msg_id=callback.tg_msg_id or record.tg_msg_id,
    )
    try:
        joined = await max_adapter.join_chat_by_link(link)
    except Exception as exc:
        safe_error = f"{type(exc).__name__}: join_failed"
        await repo.mark_callback_action_used(callback.action_id, error=safe_error)
        log_event(
            logger,
            logging.WARNING,
            "bridge.callback.max_join",
            direction="callback",
            stage="join",
            outcome="failed",
            reason="max_join_failed",
            max_chat_id=record.max_chat_id,
            max_msg_id=record.max_msg_id,
            tg_topic_id=callback.topic_id or record.tg_topic_id,
            error_type=type(exc).__name__,
        )
        return "Не удалось вступить в MAX чат"

    await repo.mark_callback_action_used(callback.action_id)
    schedule_recovery_event_scan("max_join_button")
    title = joined.title or joined.chat_id or "чат"
    log_event(
        logger,
        logging.INFO,
        "bridge.callback.max_join",
        direction="callback",
        stage="join",
        outcome="joined",
        max_chat_id=joined.chat_id or record.max_chat_id,
        max_msg_id=record.max_msg_id,
        tg_topic_id=callback.topic_id or record.tg_topic_id,
        chat_kind=joined.chat_kind,
    )
    return f"✅ Вступили в MAX: {title}"
