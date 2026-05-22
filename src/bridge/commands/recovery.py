"""`/recovery` command handler."""

import json
import shlex
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from ...config.loader import AppConfig
from ...db.repository import Repository
from ..contracts import TelegramBridgePort


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
        freshness = format_freshness(report["stats"].get("last_scan_at"))
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
