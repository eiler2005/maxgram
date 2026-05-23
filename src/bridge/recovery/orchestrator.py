"""Recovery snapshot orchestration helpers."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Optional

from ...db.repository import ChatBinding, Repository
from ...logging_utils import log_event
from ..contracts import MaxBridgePort, MaxMessage
from . import reporter

logger = logging.getLogger("src.bridge.core")


def message_has_control_event(msg: MaxMessage) -> bool:
    values = [
        *(msg.attachment_types or []),
        msg.message_type,
    ]
    return any(str(value or "").upper() == "CONTROL" for value in values)


async def run_after_connect(
    *,
    safe_scan: Callable[..., Awaitable[dict[str, object]]],
    log_scan_failure: Callable[..., None],
):
    try:
        await safe_scan(reason="max_connect", notify=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_scan_failure(reason="max_connect", error=e)


def schedule_event_scan(
    *,
    collect_snapshot: object,
    reason: str,
    cooldowns: dict[str, int],
    delays: dict[str, int],
    last_scan_at: dict[str, float],
    scan_reasons: set[str],
    current_task: Optional[asyncio.Task],
    current_scan_at: Optional[float],
    run_scheduled_scan: Callable[[float], Awaitable[None]],
) -> tuple[Optional[asyncio.Task], Optional[float]]:
    if not callable(collect_snapshot):
        return current_task, current_scan_at

    now = time.monotonic()
    cooldown = float(cooldowns.get(reason, 0))
    previous_scan_at = last_scan_at.get(reason)
    if previous_scan_at is not None and cooldown > 0 and now - previous_scan_at < cooldown:
        log_event(
            logger,
            logging.INFO,
            "bridge.recovery.scan_scheduled",
            stage="recovery",
            outcome="skipped",
            reason=reason,
            skip_reason="cooldown",
            cooldown_seconds=int(cooldown),
        )
        return current_task, current_scan_at

    delay = max(0.0, float(delays.get(reason, 60)))
    target_at = now + delay
    scan_reasons.add(reason)
    if current_task is not None and not current_task.done():
        if current_scan_at is not None and target_at >= current_scan_at:
            log_event(
                logger,
                logging.INFO,
                "bridge.recovery.scan_scheduled",
                stage="recovery",
                outcome="coalesced",
                reason=reason,
                scheduled_in_seconds=max(0, int(current_scan_at - now)),
            )
            return current_task, current_scan_at
        current_task.cancel()

    task = asyncio.create_task(
        run_scheduled_scan(delay),
        name="recovery_snapshot_event",
    )
    log_event(
        logger,
        logging.INFO,
        "bridge.recovery.scan_scheduled",
        stage="recovery",
        outcome="scheduled",
        reason=reason,
        scheduled_in_seconds=int(delay),
    )
    return task, target_at


async def run_scheduled_event_scan(
    *,
    delay_seconds: float,
    scan_reasons: set[str],
    last_scan_at: dict[str, float],
    clear_scan_at: Callable[[], None],
    safe_scan: Callable[..., Awaitable[dict[str, object]]],
    log_scan_failure: Callable[..., None],
):
    reason_text = "event"
    try:
        await asyncio.sleep(delay_seconds)
        reasons = sorted(scan_reasons)
        scan_reasons.clear()
        clear_scan_at()
        reason_text = ",".join(reasons) if reasons else "event"
        now = time.monotonic()
        for reason in reasons:
            last_scan_at[reason] = now
        await safe_scan(reason=reason_text, notify=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_scan_failure(reason=reason_text, error=e)


def _status_for_missing_recovery_chat(existing, *, migration_required: bool) -> str:
    if existing and getattr(existing, "invite_link", None):
        return "joinable_by_link"
    if existing and (
        getattr(existing, "owner_user_id", None) or reporter.entry_admin_contacts(existing)
    ):
        return "manual_admin_required"
    if migration_required:
        return "account_migration_required"
    return "needs_invite"


def _snapshot_entry_from_chat(
    chat,
    *,
    registry_key: str,
    tg_topic_id: Optional[int],
    binding: Optional[ChatBinding] = None,
    status: str = "visible",
) -> dict[str, object]:
    data = asdict(chat)
    return {
        "registry_key": registry_key,
        "tg_topic_id": tg_topic_id,
        "title": data.get("title") or (binding.title if binding else registry_key),
        "old_max_chat_id": binding.max_chat_id if binding else data.get("max_chat_id"),
        "current_max_chat_id": binding.max_chat_id if binding else data.get("max_chat_id"),
        "chat_kind": data.get("chat_kind") or "unknown",
        "mode": binding.mode if binding else "active",
        "access_type": data.get("access_type"),
        "invite_link": data.get("invite_link"),
        "owner_user_id": data.get("owner_user_id"),
        "owner_name": data.get("owner_name"),
        "admin_contacts": data.get("admin_contacts") or [],
        "dm_partner_user_id": data.get("dm_partner_user_id"),
        "dm_partner_name": data.get("dm_partner_name"),
        "participant_count": data.get("participant_count"),
        "recovery_status": status,
    }


def _contact_as_dict(contact) -> dict[str, object]:
    if hasattr(contact, "__dataclass_fields__"):
        return asdict(contact)
    return dict(getattr(contact, "__dict__", {}))


def _status_for_missing_dm_contact(*, migration_required: bool) -> str:
    return "account_migration_required" if migration_required else "needs_contact"


def _snapshot_entry_from_contact(
    contact,
    *,
    binding: Optional[ChatBinding],
    existing=None,
) -> dict[str, object]:
    data = _contact_as_dict(contact)
    max_user_id = str(data.get("max_user_id") or "").strip()
    current_dm_chat_id = data.get("current_dm_chat_id") or data.get("old_dm_chat_id")
    current_dm_chat_id = str(current_dm_chat_id) if current_dm_chat_id is not None else None
    old_dm_chat_id = getattr(existing, "old_dm_chat_id", None) if existing else data.get("old_dm_chat_id")
    tg_topic_id = binding.tg_topic_id if binding else data.get("tg_topic_id")
    if tg_topic_id is None and existing is not None:
        tg_topic_id = getattr(existing, "tg_topic_id", None)

    recovery_status = str(data.get("recovery_status") or "visible")
    existing_current = getattr(existing, "current_dm_chat_id", None) if existing else None
    if existing_current and current_dm_chat_id and existing_current != current_dm_chat_id and tg_topic_id is not None:
        recovery_status = "needs_remap"
        old_dm_chat_id = old_dm_chat_id or existing_current

    return {
        "max_user_id": max_user_id,
        "display_name": data.get("display_name") or max_user_id,
        "old_dm_chat_id": old_dm_chat_id or current_dm_chat_id,
        "current_dm_chat_id": current_dm_chat_id,
        "tg_topic_id": tg_topic_id,
        "source": data.get("source") or "dialog",
        "recovery_status": recovery_status,
    }


def _missing_dm_contact_entry(existing, *, migration_required: bool) -> dict[str, object]:
    return {
        "max_user_id": existing.max_user_id,
        "display_name": existing.display_name,
        "old_dm_chat_id": existing.old_dm_chat_id or existing.current_dm_chat_id,
        "current_dm_chat_id": existing.current_dm_chat_id,
        "tg_topic_id": existing.tg_topic_id,
        "source": existing.source,
        "recovery_status": _status_for_missing_dm_contact(
            migration_required=migration_required,
        ),
    }


async def safe_scan(
    *,
    max_adapter: MaxBridgePort,
    repo: Repository,
    scan_lock: asyncio.Lock,
    reason: str = "manual",
    notify: bool = False,
    maybe_notify: Callable[..., Awaitable[None]],
) -> dict[str, object]:
    async with scan_lock:
        snapshot = await max_adapter.collect_recovery_snapshot()
        account_result = {"migration_required": False, "max_user_id": snapshot.max_user_id}
        if snapshot.max_user_id:
            account_result = await repo.upsert_max_account_generation(
                max_user_id=snapshot.max_user_id,
                masked_phone=snapshot.masked_phone,
                session_fingerprint_hash=snapshot.session_fingerprint_hash,
            )

        migration_required = bool(account_result.get("migration_required"))
        snapshot_by_chat = {str(chat.max_chat_id): chat for chat in snapshot.chats}
        matched_chat_ids: set[str] = set()

        existing_by_topic = {
            entry.tg_topic_id: entry
            for entry in await repo.list_recovery_entries()
            if entry.tg_topic_id is not None
        }

        entries: list[dict[str, object]] = []
        bindings = await repo.list_bindings()
        bindings_by_chat = {str(binding.max_chat_id): binding for binding in bindings}
        for binding in bindings:
            chat = snapshot_by_chat.get(str(binding.max_chat_id))
            existing = existing_by_topic.get(binding.tg_topic_id)
            if chat:
                matched_chat_ids.add(chat.max_chat_id)
                entries.append(
                    _snapshot_entry_from_chat(
                        chat,
                        registry_key=f"tg_topic:{binding.tg_topic_id}",
                        tg_topic_id=binding.tg_topic_id,
                        binding=binding,
                        status="visible",
                    )
                )
                continue

            entries.append(
                {
                    "registry_key": f"tg_topic:{binding.tg_topic_id}",
                    "tg_topic_id": binding.tg_topic_id,
                    "title": binding.title,
                    "old_max_chat_id": getattr(existing, "old_max_chat_id", None) or binding.max_chat_id,
                    "current_max_chat_id": binding.max_chat_id,
                    "chat_kind": getattr(existing, "chat_kind", "unknown") if existing else "unknown",
                    "mode": binding.mode,
                    "recovery_status": _status_for_missing_recovery_chat(
                        existing,
                        migration_required=migration_required,
                    ),
                }
            )

        for chat in snapshot.chats:
            if chat.max_chat_id in matched_chat_ids:
                continue
            entries.append(
                _snapshot_entry_from_chat(
                    chat,
                    registry_key=f"max_chat:{chat.max_chat_id}",
                    tg_topic_id=None,
                    status="unmapped",
                )
            )

        existing_contacts_by_user = {
            entry.max_user_id: entry
            for entry in await repo.list_dm_contact_recovery_entries()
        }
        contact_entries: list[dict[str, object]] = []
        seen_contact_ids: set[str] = set()
        for contact in getattr(snapshot, "contacts", []) or []:
            data = _contact_as_dict(contact)
            max_user_id = str(data.get("max_user_id") or "").strip()
            if not max_user_id:
                continue
            current_dm_chat_id = data.get("current_dm_chat_id") or data.get("old_dm_chat_id")
            binding = bindings_by_chat.get(str(current_dm_chat_id)) if current_dm_chat_id else None
            existing = existing_contacts_by_user.get(max_user_id)
            contact_entries.append(
                _snapshot_entry_from_contact(
                    contact,
                    binding=binding,
                    existing=existing,
                )
            )
            seen_contact_ids.add(max_user_id)
        for max_user_id, existing in existing_contacts_by_user.items():
            if max_user_id in seen_contact_ids:
                continue
            contact_entries.append(
                _missing_dm_contact_entry(
                    existing,
                    migration_required=migration_required,
                )
            )

        result = await repo.upsert_recovery_snapshot(entries, reason=reason)
        contact_result = await repo.upsert_dm_contact_recovery_snapshot(
            contact_entries,
            reason=reason,
        )
        result = {
            **result,
            "topics": len(bindings),
            "visible": len(matched_chat_ids),
            "migration_required": migration_required,
            "dm_contacts_scanned": contact_result.get("scanned", 0),
            "dm_contacts_inserted": contact_result.get("inserted", 0),
            "dm_contacts_status_changed": contact_result.get("status_changed", 0),
        }
        log_event(
            logger,
            logging.INFO,
            "bridge.recovery.scan_completed",
            stage="recovery",
            outcome="completed",
            reason=reason,
            scanned=result.get("scanned", 0),
            inserted=result.get("inserted", 0),
            status_changed=result.get("status_changed", 0),
            unmapped=result.get("unmapped", 0),
            needs_invite=result.get("needs_invite", 0),
            manual_admin_required=result.get("manual_admin_required", 0),
            dm_contacts_scanned=result.get("dm_contacts_scanned", 0),
            dm_contacts_inserted=result.get("dm_contacts_inserted", 0),
            dm_contacts_status_changed=result.get("dm_contacts_status_changed", 0),
            topics=len(bindings),
            visible=len(matched_chat_ids),
            migration_required=migration_required,
        )
    if notify:
        await maybe_notify(reason=reason, result=result)
    return result
