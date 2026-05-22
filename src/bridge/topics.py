"""Telegram topic binding and fallback-title helpers."""

import logging
import time
from collections.abc import Callable
from typing import Optional

from .contracts import MaxBridgePort, MaxMessage, TelegramBridgePort
from ..config.loader import AppConfig
from ..db.repository import ChatBinding, Repository
from ..logging_utils import log_event

logger = logging.getLogger("src.bridge.core")


async def get_or_create_topic(
    *,
    cfg: AppConfig,
    repo: Repository,
    tg: TelegramBridgePort,
    max_adapter: MaxBridgePort,
    msg: MaxMessage,
    schedule_recovery_scan: Callable[[str], None],
    flow_id: Optional[str] = None,
) -> Optional[int]:
    """Вернуть существующий topic_id или создать новый."""
    binding = await repo.get_binding(msg.chat_id)
    if binding:
        if binding.title.startswith("Чат "):
            real_title = await resolve_chat_title(cfg=cfg, max_adapter=max_adapter, msg=msg)
            if not real_title.startswith("Чат "):
                await tg.rename_topic(binding.tg_topic_id, real_title, flow_id=flow_id)
                await repo.update_title(msg.chat_id, real_title)
                schedule_recovery_scan("title_changed")
                log_event(
                    logger,
                    logging.INFO,
                    "bridge.inbound.topic_resolved",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="routing",
                    outcome="renamed",
                    max_chat_id=msg.chat_id,
                    max_msg_id=msg.msg_id,
                    tg_topic_id=binding.tg_topic_id,
                    title=real_title,
                )
                return binding.tg_topic_id
        log_event(
            logger,
            logging.INFO,
            "bridge.inbound.topic_resolved",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="existing",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            tg_topic_id=binding.tg_topic_id,
            title=binding.title,
        )
        return binding.tg_topic_id

    title = await resolve_chat_title(cfg=cfg, max_adapter=max_adapter, msg=msg)

    try:
        topic_id = await tg.create_topic(title, flow_id=flow_id)
    except Exception as e:
        log_event(
            logger,
            logging.ERROR,
            "bridge.inbound.topic_resolved",
            flow_id=flow_id,
            direction="inbound",
            stage="routing",
            outcome="failed",
            reason="topic_create_failed",
            max_chat_id=msg.chat_id,
            max_msg_id=msg.msg_id,
            title=title,
            error=str(e),
        )
        return None

    mode = cfg.get_chat_mode(msg.chat_id)
    await repo.save_binding(ChatBinding(
        max_chat_id=msg.chat_id,
        tg_topic_id=topic_id,
        title=title,
        mode=mode,
        created_at=int(time.time()),
    ))
    schedule_recovery_scan("new_binding")
    log_event(
        logger,
        logging.INFO,
        "bridge.inbound.topic_resolved",
        flow_id=flow_id,
        direction="inbound",
        stage="routing",
        outcome="created",
        max_chat_id=msg.chat_id,
        max_msg_id=msg.msg_id,
        tg_topic_id=topic_id,
        title=title,
        mode=mode,
    )
    return topic_id


async def resolve_chat_title(
    *,
    cfg: AppConfig,
    max_adapter: MaxBridgePort,
    msg: MaxMessage,
) -> str:
    """Определить название для топика."""
    config_title = cfg.get_chat_title(msg.chat_id)
    if config_title:
        return config_title

    if msg.chat_title:
        return msg.chat_title

    if not msg.is_dm:
        title = await max_adapter.resolve_chat_title(msg.chat_id)
        if title:
            return title

    if msg.is_dm:
        own_id = max_adapter.get_own_id()
        if msg.sender_name and msg.sender_id and msg.sender_id != own_id:
            return msg.sender_name

        dm_partner_id = max_adapter.get_dm_partner_id(msg.chat_id)
        chat_id_candidate = msg.chat_id if msg.chat_id != own_id else None
        sender_candidate = (
            msg.sender_id
            if (
                msg.sender_id
                and msg.sender_id != own_id
                and msg.sender_id != dm_partner_id
                and msg.sender_id != msg.chat_id
            )
            else None
        )
        candidates = [dm_partner_id, sender_candidate, chat_id_candidate]
        seen: set[str] = set()
        for uid in candidates:
            if not uid or uid in seen:
                continue
            seen.add(uid)
            name = await max_adapter.resolve_user_name(uid)
            if name:
                return name

    return f"Чат {msg.chat_id}"


async def fix_fallback_titles(
    *,
    repo: Repository,
    tg: TelegramBridgePort,
    max_adapter: MaxBridgePort,
    schedule_recovery_scan: Callable[[str], None],
):
    """При старте переименовать все топики с fallback-названием 'Чат XXXXX'."""
    bindings = await repo.list_bindings()
    for binding in bindings:
        if not binding.title.startswith("Чат "):
            continue
        own_id = max_adapter.get_own_id()
        candidate_id = (
            max_adapter.get_dm_partner_id(binding.max_chat_id)
            or (binding.max_chat_id if binding.max_chat_id != own_id else None)
        )
        if not candidate_id:
            continue
        name = await max_adapter.resolve_user_name(candidate_id)
        if name:
            await tg.rename_topic(binding.tg_topic_id, name)
            await repo.update_title(binding.max_chat_id, name)
            schedule_recovery_scan("title_changed")
            log_event(
                logger,
                logging.INFO,
                "bridge.maintenance.fallback_title_fixed",
                stage="maintenance",
                outcome="renamed",
                max_chat_id=binding.max_chat_id,
                tg_topic_id=binding.tg_topic_id,
                title=name,
            )
        else:
            log_event(
                logger,
                logging.DEBUG,
                "bridge.maintenance.fallback_title_skipped",
                stage="maintenance",
                outcome="skipped",
                reason="name_unresolved",
                max_chat_id=binding.max_chat_id,
                tg_topic_id=binding.tg_topic_id,
            )
