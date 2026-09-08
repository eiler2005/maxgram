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
from .rules import CRIT, Finding
from .state import WatchdogState

logger = logging.getLogger("watchdog.notify")

TELEGRAM_TIMEOUT_SECONDS = 15
MAX_MESSAGE_CHARS = 3800

SEVERITY_BADGE = {"crit": "🔴", "warn": "🟡", "info": "🔵"}


def render_alert(finding: Finding, cfg: WatchdogConfig) -> str:
    badge = SEVERITY_BADGE.get(finding.severity, "🟡")
    lines = [
        f"{badge} <b>[EXT] {html.escape(finding.title)}</b>",
        f"Наблюдаемый хост: {html.escape(cfg.target_name)} (проверка с внешнего VPS)",
        f"Класс отказа: {html.escape(finding.failure_class)} · правило "
        f"<code>{html.escape(finding.rule)}</code>",
        "",
        f"<b>Что сломано:</b> {html.escape(finding.detail)}",
        f"<b>Что делать:</b> {html.escape(finding.hint)}",
    ]
    return "\n".join(lines)[:MAX_MESSAGE_CHARS]


def render_recovery(rule: str, cfg: WatchdogConfig) -> str:
    return "\n".join(
        [
            f"✅ <b>[EXT] Восстановлено: {html.escape(rule)}</b>",
            f"Наблюдаемый хост: {html.escape(cfg.target_name)}",
            "Проверка снова проходит.",
        ]
    )[:MAX_MESSAGE_CHARS]


def render_daily_summary(cfg: WatchdogConfig, active: list[str]) -> str:
    if active:
        body = "Открытые проблемы: " + ", ".join(html.escape(rule) for rule in active) + "."
    else:
        body = "Открытых проблем нет."
    return "\n".join(
        [
            "🔵 <b>[EXT] Внешний watchdog жив</b>",
            f"Наблюдаемый хост: {html.escape(cfg.target_name)}",
            body,
        ]
    )


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
    recoveries: list[str],
    now: Optional[int] = None,
) -> int:
    """Отправляет алерты с dedup и recovery без dedup. Возвращает число сообщений."""
    moment = int(now if now is not None else time.time())
    sent = 0

    for rule in recoveries:
        if send_telegram(cfg, render_recovery(rule, cfg), silent=True):
            state.mark_sent(f"recovery:{rule}", moment)
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
