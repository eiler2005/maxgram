"""
Доставка алертов в Telegram напрямую через Bot API.

Сознательно без aiogram: наблюдатель не должен зависеть от библиотек наблюдаемого.
Вызов идёт с хоста-наблюдателя, поэтому сетевой путь до api.telegram.org независим от
production-хоста — именно это делает наблюдаемым класс отказа F6.
"""

from __future__ import annotations

import html
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .config import WatchdogConfig
from .rules import CRIT, Finding, Recovery, humanize_duration, rule_title
from .state import WatchdogState

logger = logging.getLogger("watchdog.notify")

TELEGRAM_TIMEOUT_SECONDS = 15
MAX_MESSAGE_CHARS = 3800

SEVERITY_BADGE = {"crit": "🔴", "warn": "🟡", "info": "🔵"}


def clean(text: str, limit: int = 300) -> str:
    """Схлопывает пробелы и режет длину.

    Тексты ошибок приходят из stderr чужих утилит: там бывают табы, переводы
    строк и куски usage. В сообщении оператору это выглядит как мусор, поэтому
    нормализуем перед вставкой.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return html.escape(collapsed)


def render_alert(finding: Finding, cfg: WatchdogConfig) -> str:
    badge = SEVERITY_BADGE.get(finding.severity, "🟡")
    lines = [
        f"{badge} <b>[EXT] {clean(finding.title, 120)}</b>",
        f"Хост: {clean(cfg.target_name, 60)} · проверка с внешнего VPS",
        f"Класс отказа: {clean(finding.failure_class, 20)} · "
        f"<code>{clean(finding.rule, 40)}</code>",
        "",
        f"<b>Что произошло:</b> {clean(finding.detail, 400)}",
        f"<b>Что делать:</b> {clean(finding.hint, 400)}",
    ]
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


def render_recovery(recovery: Recovery, cfg: WatchdogConfig) -> str:
    lines = [
        f"✅ <b>[EXT] Норма: {clean(rule_title(recovery.rule), 120)}</b>",
        f"Хост: {clean(cfg.target_name, 60)} · <code>{clean(recovery.rule, 40)}</code>",
        "",
    ]
    if recovery.duration_seconds:
        lines.append(
            f"Проверка снова проходит. Проблема длилась "
            f"{clean(humanize_duration(recovery.duration_seconds), 40)}."
        )
    else:
        lines.append("Проверка снова проходит.")
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


def render_daily_summary(cfg: WatchdogConfig, active: list[str]) -> str:
    lines = [
        "🔵 <b>[EXT] Внешний watchdog на связи</b>",
        f"Хост под наблюдением: {clean(cfg.target_name, 60)}",
        "",
    ]
    if active:
        lines.append("<b>Открытые проблемы:</b>")
        lines += [f"• {clean(rule_title(rule), 120)} (<code>{clean(rule, 40)}</code>)" for rule in active]
    else:
        lines.append("Открытых проблем нет, все проверки проходят.")
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


def send_telegram(cfg: WatchdogConfig, text: str, *, silent: bool = False) -> bool:
    """Шлёт во все настроенные цели. True, если доставлено хотя бы в одну."""
    if not cfg.telegram_configured:
        logger.error("telegram not configured (TG_BOT_TOKEN / TG_OWNER_ID missing)")
        return False

    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    delivered = False
    for target in cfg.targets:
        payload: dict[str, object] = {
            "chat_id": target.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        if target.topic_id:
            payload["message_thread_id"] = target.topic_id
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
                response.read()
            delivered = True
        except urllib.error.HTTPError as e:
            logger.error("telegram %s failed: HTTP %s %s", target.label, e.code, e.read()[:200])
        except Exception as e:  # noqa: BLE001 — сеть, важно не уронить цикл
            logger.error("telegram %s failed: %s", target.label, e)
    return delivered


def dispatch(
    cfg: WatchdogConfig,
    state: WatchdogState,
    *,
    alerts: list[Finding],
    recoveries: list[Recovery],
    now: Optional[int] = None,
) -> int:
    """Отправляет алерты с dedup и recovery без dedup. Возвращает число сообщений."""
    moment = int(now if now is not None else time.time())
    sent = 0

    for recovery in recoveries:
        # Recovery отправляется без dedup, но не дважды подряд: повтор в пределах
        # одного цикла опроса означает гонку двух процессов, а не второе событие.
        key = f"recovery:{recovery.rule}"
        if moment - state.last_sent_at(key) < max(30, cfg.poll_interval_seconds):
            logger.info("recovery %s уже отправлен только что — пропускаю", recovery.rule)
            continue
        if send_telegram(cfg, render_recovery(recovery, cfg), silent=True):
            state.mark_sent(key, moment)
            sent += 1

    for finding in alerts:
        key = f"alert:{finding.rule}"
        age = moment - state.last_sent_at(key)
        if state.last_sent_at(key) and age < cfg.dedup_ttl_seconds:
            logger.info("dedup skip %s (sent %ss ago)", finding.rule, age)
            continue
        if send_telegram(cfg, render_alert(finding, cfg), silent=finding.severity != CRIT):
            state.mark_sent(key, moment)
            sent += 1

    return sent
