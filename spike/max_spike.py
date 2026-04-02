#!/usr/bin/env python3
"""
Phase 0 Spike: Проверка MAX userbot (pymax / maxapi-python)

Что проверяем:
  1. Авторизация по номеру телефона (SMS код)
  2. Список чатов (chat_id для config.local.yaml)
  3. Live listener — входящие сообщения
  4. Структура Message объекта

Как запустить:
  MAX_PHONE=+7XXXXXXXXXX .venv/bin/python spike/max_spike.py

  Или в Docker (с TTY для ввода кода):
  docker-compose run --rm -e MAX_PHONE=+7XXXXXXXXXX app python spike/max_spike.py
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Логирование (без PII — только статусы)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHONE = os.environ.get("MAX_PHONE", "")


def msg_to_dict(message) -> dict:
    """Безопасно конвертируем Message в dict для отображения (без PII в логах)"""
    try:
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
        elif hasattr(message, "__dict__"):
            return {k: str(v) for k, v in message.__dict__.items() if not k.startswith("_")}
    except Exception:
        pass
    return {"repr": repr(message)}


async def run_spike(phone: str):
    from pymax import MaxClient

    print(f"\n[MAX Spike] Инициализация клиента для {phone}")
    print(f"[MAX Spike] Сессия сохраняется в: {DATA_DIR}/")
    print("[MAX Spike] При первом запуске придёт SMS с кодом — введи его\n")

    client = MaxClient(
        phone=phone,
        work_dir=str(DATA_DIR),
        session_name="max_bridge_session",
        reconnect=True,
    )

    # ── on_start: список чатов ────────────────────────────────────────────────
    @client.on_start()
    async def on_connected():
        print("\n" + "=" * 60)
        print("[MAX Spike] ✅ Подключился к MAX!")
        print("=" * 60)

        me = await client.get_me()
        print(f"[MAX Spike] Аккаунт: {me}")

        print("\n[MAX Spike] Загружаю список чатов...")
        try:
            chats = await client.fetch_chats()
            print(f"\n[MAX Spike] Найдено чатов: {len(chats)}")
            print("─" * 60)
            print(f"{'ID':<20} {'Тип':<12} {'Название'}")
            print("─" * 60)
            for chat in chats:
                chat_id = getattr(chat, "id", "?")
                chat_type = getattr(chat, "type", "?")
                title = getattr(chat, "title", None) or getattr(chat, "name", None) or "—"
                print(f"{str(chat_id):<20} {str(chat_type):<12} {title}")
            print("─" * 60)
            print("\n→ Скопируй нужные chat_id в config.local.yaml (секция chats:)\n")
        except Exception as e:
            print(f"[MAX Spike] ⚠️  Не удалось получить чаты: {e}")

        print("[MAX Spike] Слушаю входящие сообщения...")
        print("[MAX Spike] Напиши себе что-нибудь в MAX с другого устройства")
        print("[MAX Spike] Нажми Ctrl+C для остановки\n")

    # ── on_message: показываем структуру ────────────────────────────────────
    @client.on_message()
    async def on_message(message):
        print("\n" + "=" * 60)
        print("[MAX Spike] 📨 Новое сообщение!")
        print("=" * 60)

        # Поля нужные для bridge
        chat_id   = getattr(message, "chat_id", "?")
        msg_id    = getattr(message, "message_id", None) or getattr(message, "id", "?")
        text      = getattr(message, "text", None)
        sender    = getattr(message, "sender", None) or getattr(message, "user", None)
        sender_id = getattr(sender, "id", "?") if sender else "?"
        sender_name = (
            getattr(sender, "name", None)
            or getattr(sender, "first_name", None)
            or str(sender_id)
        ) if sender else "?"

        attachments = getattr(message, "attachments", None) or getattr(message, "attaches", None)

        print(f"  chat_id:     {chat_id}")
        print(f"  msg_id:      {msg_id}")
        print(f"  sender_id:   {sender_id}")
        print(f"  sender_name: {sender_name}")
        print(f"  text:        {text!r}")
        print(f"  attachments: {attachments}")

        # Полный дамп для SPIKE_RESULTS.md
        data = msg_to_dict(message)
        print(f"\n  Полный dump (для SPIKE_RESULTS.md):")
        try:
            print("  " + json.dumps(data, ensure_ascii=False, indent=2, default=str)[:3000])
        except Exception:
            print(f"  {data}")
        print()

    # ── Запуск ───────────────────────────────────────────────────────────────
    try:
        await client.run()
    except KeyboardInterrupt:
        print("\n[MAX Spike] Остановлено")
    except Exception as e:
        print(f"\n[MAX Spike] ❌ Ошибка: {e}")
        raise


def main():
    phone = MAX_PHONE

    if not phone:
        print("Введи номер телефона MAX аккаунта (формат +7XXXXXXXXXX):")
        phone = input("→ ").strip()

    if not phone.startswith("+"):
        print("❌ Номер должен быть в формате +7XXXXXXXXXX")
        sys.exit(1)

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              MAX Spike — Phase 0                             ║
╠══════════════════════════════════════════════════════════════╣
║  1. Подключаемся к MAX через pymax (WebSocket)               ║
║  2. При первом запуске: SMS с кодом на {phone:<20} ║
║  3. Введи 6-значный код когда попросит                       ║
║  4. Сессия сохраняется — повторный запуск без кода           ║
║                                                              ║
║  Для остановки: Ctrl+C                                       ║
╚══════════════════════════════════════════════════════════════╝
""")

    asyncio.run(run_spike(phone))


if __name__ == "__main__":
    main()
