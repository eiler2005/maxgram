"""Operator-facing bridge status and help rendering."""

import time

from . import forwarding as bridge_forwarding
from .contracts import MaxBridgePort
from ..db.repository import ChatBinding, Repository
from ..runtime.health import RuntimeHealthStore, format_timestamp, render_health_summary


class BridgeStatusReporter:
    def __init__(
        self,
        *,
        repo: Repository,
        max_adapter: MaxBridgePort,
        stats: dict[str, int | float],
        health: RuntimeHealthStore | None,
        build_recovery_status_summary,
    ):
        self._repo = repo
        self._max = max_adapter
        self._stats = stats
        self._health = health
        self._build_recovery_status_summary = build_recovery_status_summary

    async def build_status_message(self, period_hours: int = 4) -> str:
        """Сформировать текстовый статусный отчёт за period_hours часов."""
        since = int(time.time()) - period_hours * 3600

        msgs = await self._repo.count_messages_since(since)
        chat_activity = await self._repo.get_chat_activity_since(since, limit=10)
        all_bindings = await self._repo.list_bindings()

        uptime_sec = int(time.time() - self._stats["start_time"])
        h, m = divmod(uptime_sec // 60, 60)
        uptime_str = f"{h}ч {m}м" if h else f"{m}м"

        max_ok = "✅" if self._max.is_ready() else "❌"
        tg_ok = "✅"
        get_last_issue = getattr(self._max, "get_last_issue", None)
        max_issue = get_last_issue() if callable(get_last_issue) else None
        get_last_connected_at = getattr(self._max, "get_last_connected_at", None)
        last_connected_at = get_last_connected_at() if callable(get_last_connected_at) else None

        inbound_total = msgs.get("inbound", 0)
        outbound_total = msgs.get("outbound", 0)
        inbound_media = self._stats["inbound_media"]
        outbound_media = self._stats["outbound_media"]
        failed_in = self._stats["failed_inbound"]
        failed_out = self._stats["failed_outbound"]
        errors_total = failed_in + failed_out

        total_chats = len(all_bindings)
        active_chats = sum(1 for b in all_bindings if b.mode == "active")

        lines = [
            f"📊 Bridge Status  ·  uptime: {uptime_str}",
            f"Период: последние {period_hours}ч",
            "",
            "🔗 Соединение",
            f"  MAX → Telegram  {max_ok}",
            f"  Telegram → MAX  {tg_ok}",
            "",
            f"📨 Сообщения (за {period_hours}ч)",
            f"  Входящих  (MAX→TG): {inbound_total}"
            + (f"  (медиа: {inbound_media})" if inbound_media else ""),
            f"  Исходящих (TG→MAX): {outbound_total}"
            + (f"  (медиа: {outbound_media})" if outbound_media else ""),
        ]
        if errors_total:
            lines.append(
                f"  ⚠️ Ошибок доставки: {errors_total}  (↓{failed_in} ↑{failed_out})"
            )
        else:
            lines.append("  Ошибок: 0")

        media_stats = await self._repo.count_pending_media()
        pending_media = int(media_stats.get("pending_count") or 0)
        media_retry_line = f"  ⏳ Медиа retry: {pending_media}"
        if pending_media:
            oldest = media_stats.get("oldest_created_at")
            if oldest:
                media_retry_line += (
                    f", старейшее "
                    f"{bridge_forwarding.format_duration_compact(int(time.time()) - int(oldest))}"
                )
        lines.append(media_retry_line)

        empty_stats_getter = getattr(self._max, "get_pending_empty_recovery_stats", None)
        if callable(empty_stats_getter):
            empty_stats = empty_stats_getter()
            pending_empty = int(empty_stats.get("pending_count") or 0)
            empty_line = f"  ⏳ Empty/voice recovery: {pending_empty}"
            oldest_empty = empty_stats.get("oldest_created_at")
            if pending_empty and oldest_empty:
                empty_line += (
                    f", старейшее "
                    f"{bridge_forwarding.format_duration_compact(int(time.time()) - int(oldest_empty))}"
                )
            lines.append(empty_line)

        if chat_activity:
            lines += ["", "💬 Активные чаты"]
            for c in chat_activity:
                title = (c["title"] or "—")[:30]
                lines.append(f"  {title:<32} ↓{c['inbound']}  ↑{c['outbound']}")

        lines += [
            "",
            f"🗂 Всего чатов: {total_chats}  (активных: {active_chats})",
        ]

        recovery_status_lines = await self._build_recovery_status_summary()
        if recovery_status_lines:
            lines += ["", *recovery_status_lines]

        if self._health is not None:
            snapshot = await self._health.get_snapshot()
            lines += ["", *render_health_summary(snapshot)]
        elif not self._max.is_ready() and max_issue is not None:
            lines += [
                "",
                "⚠️ Проблема MAX",
                f"  {max_issue.summary}",
            ]
            if getattr(max_issue, "requires_reauth", False):
                lines.append("  Требуется: reauth по SMS")

        if last_connected_at:
            lines.append(
                f"Последний успешный MAX connect: {format_timestamp(last_connected_at)}"
            )

        return "\n".join(lines)

    async def build_chats_message(self, period_hours: int = 24) -> str:
        """Список чатов с topic_id, режимом и активностью за period_hours часов."""
        bindings = await self._repo.list_bindings()
        if not bindings:
            return "🗂 Чаты: 0"

        since = int(time.time()) - period_hours * 3600
        activity = await self._repo.get_chat_activity_map_since(since)

        mode_badge = {
            "active": "✅",
            "readonly": "🔒",
            "disabled": "⏸",
        }

        def sort_key(binding: ChatBinding) -> tuple[int, str]:
            stats = activity.get(binding.max_chat_id, {})
            return (int(stats.get("total", 0)), binding.title.lower())

        ordered = sorted(bindings, key=sort_key, reverse=True)
        total_chats = len(bindings)
        active_chats = sum(1 for b in bindings if b.mode == "active")

        lines = [
            f"🗂 Чаты: {total_chats} (активных: {active_chats})",
            f"Активность за {period_hours}ч:",
        ]

        max_rows = 40
        for index, binding in enumerate(ordered):
            if index >= max_rows:
                lines.append(f"... и ещё {total_chats - max_rows}")
                break
            stats = activity.get(binding.max_chat_id, {})
            inbound = int(stats.get("inbound", 0))
            outbound = int(stats.get("outbound", 0))
            badge = mode_badge.get(binding.mode, "•")
            title = (binding.title or f"Чат {binding.max_chat_id}").strip()
            lines.append(
                f"{badge} #{binding.tg_topic_id} {title[:42]} · ↓{inbound} ↑{outbound}"
            )

        return "\n".join(lines)

    async def build_help_message(self) -> str:
        """Справка по командам bridge."""
        return (
            "ℹ️ MAX Bridge — пересылка чатов MAX ↔ Telegram\n"
            "\n"
            "Каждый MAX-чат = отдельный топик в этой группе.\n"
            "Reply в топике = ответ обратно в MAX.\n"
            "\n"
            "📋 Команды (только для владельца):\n"
            "  /status — состояние bridge, статистика за 4ч\n"
            "  /chats  — список чатов с активностью за 24ч\n"
            "  /help   — эта справка\n"
            "\n"
            "📩 Команда для всех участников группы (в топике General):\n"
            "  /dm Имя Фамилия текст — начать новый DM в MAX\n"
            "\n"
            "💡 Пример /dm (пишите в General):\n"
            "  /dm Татьяна Геннадиевна Ладина Добрый день!"
        )
