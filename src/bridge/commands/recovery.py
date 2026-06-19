"""`/recovery` command handler."""

import json
import shlex
import time
from dataclasses import asdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from ...config.loader import AppConfig
from ...db.repository import Repository
from ..contracts import MaxBridgePort, MaxRecoveryContactSnapshot, TelegramBridgePort


def _format_contacts_status(status: dict[str, object]) -> str:
    exists = "yes" if status.get("exists") else "no"
    key = "yes" if status.get("key_configured") else "no"
    decryptable = status.get("decryptable")
    decryptable_text = "unknown" if decryptable is None else ("yes" if decryptable else "no")
    lines = [
        "📇 Recovery contacts snapshot status",
        f"exists: {exists}",
        f"key configured: {key}",
        f"decryptable: {decryptable_text}",
    ]
    if status.get("created_at"):
        lines.append(f"created_at: {status['created_at']}")
    if status.get("contact_count") is not None:
        lines.append(f"contacts_with_phone: {status['contact_count']}")
    if status.get("total_seen") is not None:
        lines.append(f"total_seen: {status['total_seen']}")
    if status.get("skipped_without_phone") is not None:
        lines.append(f"skipped_without_phone: {status['skipped_without_phone']}")
    if status.get("source_account_hash"):
        lines.append(f"source_account_hash: {status['source_account_hash']}")
    if status.get("mode"):
        lines.append(f"file_mode: {status['mode']}")
    if status.get("error"):
        lines.append(f"error: {status['error']}")
    return "\n".join(lines)


def _format_contacts_snapshot_result(result: dict[str, object]) -> str:
    return (
        "✅ Recovery contacts snapshot saved\n"
        f"contacts_with_phone: {result.get('contact_count', 0)}\n"
        f"total_seen: {result.get('total_seen', 0)}\n"
        f"skipped_without_phone: {result.get('skipped_without_phone', 0)}\n"
        f"file: {result.get('path', 'recovery_contacts.enc.json')}"
    )


def _format_contacts_import_result(
    result: dict[str, object],
    *,
    dry_run: bool,
    db_result: dict[str, int] | None = None,
) -> str:
    if dry_run:
        return (
            "✅ Recovery contacts import dry-run\n"
            f"snapshot_contacts: {result.get('snapshot_contact_count', 0)}\n"
            "pymax_call: no\n"
            "db_write: no"
        )
    db_result = db_result or {}
    return (
        "✅ Recovery contacts import applied\n"
        f"snapshot_contacts: {result.get('snapshot_contact_count', 0)}\n"
        f"pymax_imported: {result.get('imported_count', 0)}\n"
        f"registry_scanned: {db_result.get('scanned', 0)}\n"
        f"registry_inserted: {db_result.get('inserted', 0)}\n"
        f"registry_status_changed: {db_result.get('status_changed', 0)}"
    )


async def _enrich_imported_contacts_for_registry(
    repo: Repository,
    contacts: list[MaxRecoveryContactSnapshot],
) -> list[dict[str, object]]:
    existing_by_user = {
        entry.max_user_id: entry
        for entry in await repo.list_dm_contact_recovery_entries()
    }
    topics_by_chat = {
        binding.max_chat_id: binding.tg_topic_id
        for binding in await repo.list_bindings()
    }
    enriched: list[dict[str, object]] = []
    for contact in contacts:
        existing = existing_by_user.get(contact.max_user_id)
        topic_id = contact.tg_topic_id
        old_chat_id = contact.old_dm_chat_id
        current_chat_id = contact.current_dm_chat_id
        status = contact.recovery_status
        if current_chat_id and current_chat_id in topics_by_chat:
            topic_id = topics_by_chat[current_chat_id]
            status = "visible"
        if existing is not None:
            topic_id = topic_id or existing.tg_topic_id
            old_chat_id = old_chat_id or existing.old_dm_chat_id or existing.current_dm_chat_id
            if (
                topic_id is not None
                and current_chat_id
                and existing.current_dm_chat_id
                and current_chat_id != existing.current_dm_chat_id
            ):
                status = "needs_remap"
        if not current_chat_id:
            status = "needs_contact"
        enriched.append(
            asdict(
                MaxRecoveryContactSnapshot(
                    max_user_id=contact.max_user_id,
                    display_name=contact.display_name,
                    old_dm_chat_id=old_chat_id,
                    current_dm_chat_id=current_chat_id,
                    tg_topic_id=topic_id,
                    source=contact.source,
                    recovery_status=status,
                )
            )
        )
    return enriched


async def handle_recovery(
    *,
    args: str,
    cfg: AppConfig,
    repo: Repository,
    tg: TelegramBridgePort,
    safe_scan: Callable[..., Awaitable[dict[str, object]]],
    log_scan_failure: Callable[..., None],
    build_report: Callable[[], Awaitable[str]],
    format_freshness: Callable[[int | None], str],
    parse_set_fields: Callable[[object, list[str]], dict[str, object]],
    max_adapter: MaxBridgePort | None = None,
) -> str:
    try:
        tokens = shlex.split(args or "")
    except ValueError as e:
        return f"⚠️ Не понял аргументы: {e}"
    if not tokens or tokens[0] in {"help", "?"}:
        return (
            "🧭 Recovery команды:\n"
            "  /recovery scan — обновить snapshot сейчас\n"
            "  /recovery report — краткий отчёт\n"
            "  /recovery export — JSON владельцу в DM\n"
            "  /recovery contacts status\n"
            "  /recovery contacts snapshot [--force]\n"
            "  /recovery contacts import dry-run|apply\n"
            "  /recovery set <topic_id> key=value ...\n"
            "  /recovery remap <topic_id> <new_max_chat_id>"
        )

    action = tokens[0].lower()
    if action == "scan":
        try:
            result = await safe_scan(reason="manual")
        except Exception as e:
            log_scan_failure(reason="manual", error=e)
            return f"❌ Recovery snapshot не обновлён: {type(e).__name__}"
        report = await repo.get_recovery_report()
        stats = report.get("stats") if isinstance(report, dict) else {}
        if not isinstance(stats, dict):
            stats = {}
        freshness = format_freshness(stats.get("last_scan_at"))
        return (
            "✅ Recovery snapshot обновлён\n"
            f"Скан: {result.get('scanned', 0)} записей · visible topics: {result.get('visible', 0)}"
            f" · DM contacts: {result.get('dm_contacts_scanned', 0)}"
            + ("\n⚠️ Обнаружен новый MAX account: нужен migration flow" if result.get("migration_required") else "")
            + f"\nСвежесть: {freshness}"
        )

    if action == "report":
        return await build_report()

    if action == "export":
        export = await repo.export_recovery_registry()
        tmp_dir = Path(getattr(getattr(cfg, "storage", None), "tmp_dir", "/tmp"))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        filename = f"max_recovery_registry-{int(time.time())}.json"
        path = tmp_dir / filename
        path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            send_owner_document = getattr(tg, "send_owner_document", None)
            if not callable(send_owner_document):
                return "⚠️ Telegram adapter не умеет отправлять owner document"
            sent = await send_owner_document(
                str(path),
                caption="MAX recovery registry export",
                filename=filename,
            )
            return "✅ Export отправлен владельцу в DM" if sent else "❌ Не удалось отправить export"
        finally:
            with suppress(Exception):
                path.unlink(missing_ok=True)

    if action == "contacts":
        if max_adapter is None:
            return "⚠️ MAX adapter недоступен для contacts recovery"
        if len(tokens) < 2:
            return "⚠️ Формат: /recovery contacts status|snapshot|import"
        contacts_action = tokens[1].lower()
        if contacts_action == "status":
            return _format_contacts_status(max_adapter.recovery_contacts_snapshot_status())
        if contacts_action == "snapshot":
            force = "--force" in tokens[2:]
            try:
                result = await max_adapter.create_recovery_contacts_snapshot(force=force)
            except Exception as e:
                return f"❌ Contacts snapshot не сохранён: {type(e).__name__}"
            return _format_contacts_snapshot_result(result)
        if contacts_action == "import":
            if len(tokens) != 3 or tokens[2].lower() not in {"dry-run", "apply"}:
                return "⚠️ Формат: /recovery contacts import dry-run|apply"
            dry_run = tokens[2].lower() == "dry-run"
            try:
                result = await max_adapter.import_recovery_contacts_snapshot(dry_run=dry_run)
            except Exception as e:
                return f"❌ Contacts import не выполнен: {type(e).__name__}"
            if dry_run:
                return _format_contacts_import_result(result, dry_run=True)
            contacts = result.get("contacts") or []
            if not isinstance(contacts, list):
                return "❌ Contacts import вернул некорректный результат"
            registry_contacts = await _enrich_imported_contacts_for_registry(repo, contacts)
            db_result = await repo.upsert_dm_contact_recovery_snapshot(
                registry_contacts,
                reason="contacts_import",
            )
            return _format_contacts_import_result(result, dry_run=False, db_result=db_result)
        return "⚠️ Формат: /recovery contacts status|snapshot|import"

    if action == "set":
        if len(tokens) < 3:
            return "⚠️ Формат: /recovery set <topic_id> key=value ..."
        try:
            topic_id = int(tokens[1])
        except ValueError:
            return "⚠️ topic_id должен быть числом"
        entry = await repo.get_recovery_entry_by_topic(topic_id)
        if entry is None:
            return "⚠️ Запись не найдена. Сначала выполни /recovery scan"
        try:
            updates = parse_set_fields(entry, tokens[2:])
        except (TypeError, ValueError) as e:
            return f"⚠️ Не удалось обновить: {e}"
        if not updates:
            return "⚠️ Нет поддержанных полей для обновления"
        updated = await repo.update_recovery_entry(topic_id, updates)
        if updated is None:
            return "⚠️ Запись не найдена"
        return f"✅ Recovery запись #{topic_id} обновлена: {', '.join(sorted(updates))}"

    if action == "remap":
        if len(tokens) != 3:
            return "⚠️ Формат: /recovery remap <topic_id> <new_max_chat_id>"
        try:
            topic_id = int(tokens[1])
        except ValueError:
            return "⚠️ topic_id должен быть числом"
        try:
            binding = await repo.remap_recovery_topic(topic_id, tokens[2])
        except ValueError as e:
            return f"⚠️ Remap невозможен: {e}"
        if binding is None:
            return "⚠️ Binding для topic_id не найден"
        return f"✅ Топик #{topic_id} теперь отправляет в MAX chat {binding.max_chat_id}"

    return "⚠️ Неизвестная recovery команда. Используй /recovery help"
