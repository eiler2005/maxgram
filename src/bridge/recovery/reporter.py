"""Recovery report and notification formatting."""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional

from ...db.repository import Repository
from ...logging_utils import log_event
from ...runtime.health import format_timestamp

logger = logging.getLogger("src.bridge.core")

RECOVERY_NOTIFICATION_DEDUP_SECONDS = 24 * 60 * 60


def entry_admin_contacts(entry) -> list[dict[str, str]]:
    if not entry:
        return []
    raw = getattr(entry, "admin_contacts_json", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def format_freshness(
    last_scan_at: Optional[int],
    *,
    format_duration_compact: Callable[[int], str],
) -> str:
    if not last_scan_at:
        return "snapshot ещё не собирался"
    age = format_duration_compact(max(0, int(time.time()) - int(last_scan_at)))
    return f"{format_timestamp(int(last_scan_at))} ({age} назад)"


def status_label(status: str) -> str:
    labels = {
        "visible": "виден",
        "remapped": "remap готов",
        "joinable_by_link": "есть invite link",
        "manual_admin_required": "нужен админ",
        "account_migration_required": "нужен перенос",
        "needs_invite": "нужен invite",
        "needs_contact": "нужен контакт",
        "needs_remap": "нужен remap",
        "unmapped": "виден, не привязан",
        "lost": "потерян",
    }
    return labels.get(status, status)


async def build_report_message(
    *,
    repo: Repository,
    format_freshness_fn: Callable[[int | None], str],
) -> str:
    report = await repo.get_recovery_report()
    stats = report["stats"]
    entries = report["entries"]
    lines = [
        "🧭 MAX Recovery Registry",
        f"Свежесть snapshot: {format_freshness_fn(stats.get('last_scan_at'))}",
        f"Всего записей: {stats['total']} · TG topics: {stats['topics']} · unmapped MAX: {stats['unmapped']}",
        (
            f"Готово: {stats['restored']} · по ссылке: {stats['joinable_by_link']} · "
            f"нужен invite: {stats['needs_invite']} · админ/manual: {stats['manual_admin_required']}"
        ),
        (
            f"DM contacts: {stats['dm_contacts']} · linked topics: {stats['dm_contacts_linked']} · "
            f"needs contact/remap: {stats['dm_contacts_needs_remap']} · "
            f"свежесть: {format_freshness_fn(stats.get('dm_contacts_last_scan_at'))}"
        ),
    ]

    attention = [
        entry for entry in entries
        if entry.recovery_status != "remapped"
        and (
            entry.tg_topic_id is None
            or entry.recovery_status not in {"visible", "tracked"}
        )
    ]
    if attention:
        lines.append("")
        lines.append("Что требует внимания:")
        for entry in attention[:20]:
            topic = f"#{entry.tg_topic_id}" if entry.tg_topic_id is not None else "unmapped"
            title = (entry.title or entry.registry_key)[:44]
            lines.append(f"  {topic} · {status_label(entry.recovery_status)} · {title}")
        if len(attention) > 20:
            lines.append(f"  ... и ещё {len(attention) - 20}")
    return "\n".join(lines)


def parse_set_fields(entry, tokens: list[str]) -> dict[str, object]:
    updates: dict[str, object] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "priority":
            updates["priority"] = int(value)
        elif key in {"status", "recovery_status"}:
            allowed_statuses = {
                "visible",
                "remapped",
                "joinable_by_link",
                "manual_admin_required",
                "account_migration_required",
                "needs_invite",
                "unmapped",
                "lost",
                "tracked",
            }
            if value not in allowed_statuses:
                raise ValueError(f"unknown status: {value}")
            updates["recovery_status"] = value
        elif key in {"note", "manual_note"}:
            updates["manual_note"] = value
        elif key in {"link", "invite_link"}:
            updates["invite_link"] = value
        elif key == "access":
            updates["access_type"] = value
        elif key == "owner":
            if ":" in value:
                owner_name, owner_id = value.rsplit(":", 1)
                updates["owner_name"] = owner_name.strip()
                updates["owner_user_id"] = owner_id.strip()
            else:
                updates["owner_name"] = value
        elif key == "admin":
            contacts = entry_admin_contacts(entry)
            admin_name = value
            admin_id = ""
            if ":" in value:
                admin_name, admin_id = value.rsplit(":", 1)
            contact = {"user_id": admin_id.strip(), "name": admin_name.strip()}
            dedupe_key = contact["user_id"] or contact["name"]
            contacts = [
                item for item in contacts
                if (item.get("user_id") or item.get("name")) != dedupe_key
            ]
            contacts.append(contact)
            updates["admin_contacts_json"] = json.dumps(
                contacts,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
    return updates


def changes_are_important(result: dict[str, object]) -> bool:
    if bool(result.get("migration_required")):
        return True
    if int(result.get("dm_contacts_status_changed") or 0) > 0:
        return True
    for key in ("inserted", "unmapped", "needs_invite", "manual_admin_required"):
        if int(result.get(key) or 0) > 0:
            return True
    return False


def notification_digest(result: dict[str, object]) -> str:
    payload = {
        "inserted": int(result.get("inserted") or 0),
        "unmapped": int(result.get("unmapped") or 0),
        "needs_invite": int(result.get("needs_invite") or 0),
        "manual_admin_required": int(result.get("manual_admin_required") or 0),
        "dm_contacts_status_changed": int(result.get("dm_contacts_status_changed") or 0),
        "migration_required": bool(result.get("migration_required")),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def maybe_notify_changes(
    *,
    reason: str,
    result: dict[str, object],
    last_digest: str | None,
    last_notified_at: float,
    send_ops_notification: Callable[[str], Awaitable[None]],
) -> tuple[str, float] | None:
    if not changes_are_important(result):
        return None

    now = time.monotonic()
    digest = notification_digest(result)
    if (
        digest == last_digest
        and now - last_notified_at < RECOVERY_NOTIFICATION_DEDUP_SECONDS
    ):
        log_event(
            logger,
            logging.INFO,
            "bridge.recovery.notification",
            stage="recovery",
            outcome="skipped",
            reason=reason,
            skip_reason="dedup",
        )
        return None

    parts = [
        f"new: {int(result.get('inserted') or 0)}",
        f"unmapped: {int(result.get('unmapped') or 0)}",
        f"needs invite: {int(result.get('needs_invite') or 0)}",
        f"admin/manual: {int(result.get('manual_admin_required') or 0)}",
        f"DM contacts changed: {int(result.get('dm_contacts_status_changed') or 0)}",
    ]
    if result.get("migration_required"):
        parts.append("account migration: required")
    text = (
        "🧭 MAX recovery snapshot изменился\n"
        f"Триггер: {reason}\n"
        f"{' · '.join(parts)}\n"
        "Открой /recovery report для деталей."
    )
    await send_ops_notification(text)
    log_event(
        logger,
        logging.INFO,
        "bridge.recovery.notification",
        stage="recovery",
        outcome="sent",
        reason=reason,
        inserted=int(result.get("inserted") or 0),
        unmapped=int(result.get("unmapped") or 0),
        needs_invite=int(result.get("needs_invite") or 0),
        manual_admin_required=int(result.get("manual_admin_required") or 0),
        migration_required=bool(result.get("migration_required")),
    )
    return digest, now
