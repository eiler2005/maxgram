"""
Команда /watchdog: рассказать про внешний наблюдатель и запросить проверку.

Важная граница: сам bridge — наблюдаемая система, а не наблюдатель. Эта команда
лишь удобство: она просит наблюдателя прислать свежую сводку. Если bridge мёртв,
команда не сработает — и это ровно тот случай, ради которого наблюдатель живёт
на другом хосте и пишет сам.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

from ..contracts import TelegramInlineButton

logger = logging.getLogger(__name__)

CHECK_ACTION = "watchdog_check"
SIGNATURE_HEADER = "X-Watchdog-Signature"
REQUEST_TIMEOUT_SECONDS = 20


def _push_url() -> str:
    return (os.environ.get("WATCHDOG_PUSH_URL") or "").strip()


def _secret() -> str:
    return (os.environ.get("WATCHDOG_PUSH_SECRET") or "").strip()


def is_configured() -> bool:
    return bool(_push_url() and _secret())


def build_message() -> str:
    """Описание сервиса. Постоянный текст — поэтому живёт в команде, а не в алертах."""
    lines = [
        "🛰 <b>Внешний watchdog</b>",
        "",
        "Наблюдает за этим bridge с <b>отдельного VPS</b> — другой провайдер, "
        "другая сеть. Смысл в отдельности: если этот хост умрёт целиком, "
        "сообщить об этом сможет только тот, кто снаружи.",
        "",
        "<b>Слои наблюдения</b>",
        "L0 · внутри контейнера — supervisor, MAX watchdog, HEALTHCHECK",
        "L1 · ВНЕШНИЙ наблюдатель ──HTTP──► ВНУТРЕННИЙ status API",
        "L2 · ВНЕШНИЙ наблюдатель ──SSH──► production-хост",
        "L3 · production-хост ──HMAC POST──► ВНЕШНИЙ наблюдатель",
        "L4 · host-мониторинг ──► контейнер наблюдателя",
        "",
        "<b>Что он ловит</b>",
        "Остановленный контейнер, мёртвый хост и сломанную доставку алертов — "
        "то, о чём bridge не может сообщить сам.",
        "",
        "<b>Когда пишет</b>",
        "Сводка 4 раза в сутки (09:00, 13:00, 17:00, 21:00 МСК) и алерты при "
        "проблемах. Проверки идут каждые 60 секунд, но молча.",
        "",
        "<b>Что он не делает</b>",
        "Ничего не чинит: его ключ привязан к read-only пробе. "
        "Поднимает контейнер человек.",
        "",
        "Подробно: docs/runbooks/watchdog.md",
    ]
    if not is_configured():
        lines += ["", "⚠️ Кнопка недоступна: не заданы WATCHDOG_PUSH_URL / WATCHDOG_PUSH_SECRET."]
    return "\n".join(lines)


def build_buttons() -> list[TelegramInlineButton]:
    if not is_configured():
        return []
    # nonce делает callback_data уникальным: Telegram кеширует одинаковые нажатия.
    return [
        TelegramInlineButton(
            text="🔄 Проверить сейчас",
            callback_data=f"{CHECK_ACTION}:{int(time.time())}",
        )
    ]


def request_check() -> str:
    """Просит наблюдателя прислать свежую сводку. Возвращает текст для владельца."""
    url, secret = _push_url(), _secret()
    if not (url and secret):
        return "Не настроено: нет WATCHDOG_PUSH_URL / WATCHDOG_PUSH_SECRET"

    # Тот же подписанный канал, что и push: отдельный секрет заводить незачем.
    check_url = url.rsplit("/push", 1)[0] + "/check" if url.endswith("/push") else url
    body = json.dumps({"ts": int(time.time()), "reason": "manual"}).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        check_url,
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
        return "Наблюдатель проверяет — сводка сейчас придёт"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return "Наблюдатель уже выполняет проверку, подожди минуту"
        logger.warning("watchdog check rejected: HTTP %s", e.code)
        return f"Наблюдатель отклонил запрос (HTTP {e.code})"
    except Exception as e:  # noqa: BLE001 — сеть между хостами
        logger.warning("watchdog check failed: %s", type(e).__name__)
        return "Наблюдатель недоступен — проверь сеть и контейнер на его хосте"
