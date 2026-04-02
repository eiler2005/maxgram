#!/usr/bin/env python3
"""
Phase 0 Spike: Проверка Telegram Forum + Topics через aiogram

Что проверяем:
  1. Бот запускается и отвечает
  2. Создание топика в форум-группе программно
  3. Отправка сообщения в конкретный топик
  4. Получение reply из топика (проверяем что есть message_thread_id)
  5. Получение структуры Update — что именно приходит при reply

Предварительно:
  1. Создай бота через @BotFather → получи TG_BOT_TOKEN
  2. Создай Telegram супергруппу
  3. Включи Topics: Настройки группы → Темы → включить
  4. Добавь бота в группу как администратора (права: управление темами + отправка сообщений)
  5. Узнай ID группы: добавь @userinfobot в группу или см. tg_spike.py --get-id
  6. Заполни .env: TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID

Как запустить:
  python spike/tg_spike.py

  Или в Docker:
  docker-compose run --rm app python spike/tg_spike.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_OWNER_ID = int(os.environ.get("TG_OWNER_ID", "0"))
TG_FORUM_GROUP_ID = int(os.environ.get("TG_FORUM_GROUP_ID", "0"))


def check_env():
    missing = []
    if not TG_BOT_TOKEN:
        missing.append("TG_BOT_TOKEN")
    if not TG_OWNER_ID:
        missing.append("TG_OWNER_ID")
    if not TG_FORUM_GROUP_ID:
        missing.append("TG_FORUM_GROUP_ID")
    if missing:
        print(f"❌ Не заполнены переменные окружения: {', '.join(missing)}")
        print("  Скопируй .env.example → .env и заполни значения")
        sys.exit(1)


async def test_create_topic(bot, group_id: int, title: str) -> int:
    """Создаём топик и возвращаем его ID"""
    print(f"\n[TG Spike] Создаю топик: '{title}'")
    try:
        result = await bot.create_forum_topic(
            chat_id=group_id,
            name=title,
        )
        topic_id = result.message_thread_id
        print(f"[TG Spike] ✅ Топик создан. message_thread_id = {topic_id}")
        print(f"[TG Spike] Полный результат: {result}")
        return topic_id
    except Exception as e:
        print(f"[TG Spike] ❌ Ошибка создания топика: {e}")
        print("[TG Spike] Убедись что:")
        print("  - Topics включены в группе (Настройки → Темы)")
        print("  - Бот добавлен как администратор с правом управления темами")
        return 0


async def test_send_to_topic(bot, group_id: int, topic_id: int):
    """Отправляем тестовое сообщение в топик"""
    print(f"\n[TG Spike] Отправляю сообщение в топик {topic_id}...")
    try:
        msg = await bot.send_message(
            chat_id=group_id,
            text="[TEST] Привет из MAX bridge spike! 🧪",
            message_thread_id=topic_id,
        )
        print(f"[TG Spike] ✅ Сообщение отправлено. message_id = {msg.message_id}")
        return msg.message_id
    except Exception as e:
        print(f"[TG Spike] ❌ Ошибка отправки: {e}")
        return 0


async def listen_for_replies(dp, bot):
    """
    Слушаем входящие сообщения.
    Когда приходит reply в топик — печатаем полную структуру.
    """
    from aiogram.types import Message

    print(f"\n[TG Spike] Слушаю сообщения...")
    print(f"[TG Spike] Отправь reply на тестовое сообщение в топике '🧪 MAX Spike Test'")
    print(f"[TG Spike] Нажми Ctrl+C для остановки\n")

    @dp.message()
    async def handle_any_message(message: Message):
        print("\n" + "=" * 60)
        print("[TG Spike] Получено сообщение!")
        print("=" * 60)

        # Основные поля которые нам нужны для bridge routing
        print(f"  message_id:        {message.message_id}")
        print(f"  chat.id:           {message.chat.id}")
        print(f"  chat.type:         {message.chat.type}")
        print(f"  from.id:           {message.from_user.id if message.from_user else None}")
        print(f"  text:              {message.text!r}")
        print(f"  message_thread_id: {message.message_thread_id}")   # ← ID топика!
        print(f"  is_topic_message:  {message.is_topic_message}")
        print(f"  reply_to_message:  {message.reply_to_message}")

        if message.reply_to_message:
            print(f"  reply_to.message_id: {message.reply_to_message.message_id}")
            print(f"  reply_to.text:       {message.reply_to_message.text!r}")

        # Полный дамп для записи в SPIKE_RESULTS.md
        data = message.model_dump(exclude_none=True)
        print(f"\n  Полный dump (для SPIKE_RESULTS.md):")
        print(f"  {json.dumps(data, ensure_ascii=False, indent=2, default=str)[:2000]}")

        # Проверяем что сообщение от владельца
        if message.from_user and message.from_user.id == TG_OWNER_ID:
            await message.reply("✅ [SPIKE] Reply получен! message_thread_id = "
                                f"{message.message_thread_id}")


async def main():
    check_env()

    try:
        from aiogram import Bot, Dispatcher
        from aiogram.enums import ParseMode
    except ImportError:
        print("❌ aiogram не установлен. Запусти: pip install aiogram==3.17.0")
        sys.exit(1)

    print("=" * 60)
    print("Telegram Forum + Topics Spike")
    print("=" * 60)
    print(f"  Bot token:    {TG_BOT_TOKEN[:10]}...")
    print(f"  Owner ID:     {TG_OWNER_ID}")
    print(f"  Forum group:  {TG_FORUM_GROUP_ID}")

    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher()

    # Проверка что бот работает
    try:
        me = await bot.get_me()
        print(f"\n[TG Spike] ✅ Бот подключён: @{me.username} (id={me.id})")
    except Exception as e:
        print(f"\n[TG Spike] ❌ Ошибка подключения бота: {e}")
        print("  Проверь TG_BOT_TOKEN в .env")
        await bot.session.close()
        sys.exit(1)

    # Проверка что группа доступна
    try:
        chat = await bot.get_chat(TG_FORUM_GROUP_ID)
        print(f"[TG Spike] ✅ Группа: {chat.title!r} (type={chat.type})")
        if not getattr(chat, 'is_forum', False):
            print("[TG Spike] ⚠️  ВНИМАНИЕ: is_forum=False")
            print("  Включи Topics в настройках группы: Управление группой → Темы")
    except Exception as e:
        print(f"[TG Spike] ❌ Не могу получить группу: {e}")
        print("  Убедись что бот добавлен в группу и TG_FORUM_GROUP_ID верный")
        await bot.session.close()
        sys.exit(1)

    # Создаём тестовый топик
    topic_id = await test_create_topic(bot, TG_FORUM_GROUP_ID, "🧪 MAX Spike Test")

    if topic_id:
        # Отправляем сообщение в топик
        await test_send_to_topic(bot, TG_FORUM_GROUP_ID, topic_id)
        print(f"\n[TG Spike] Теперь зайди в Telegram, найди топик '🧪 MAX Spike Test'")
        print(f"[TG Spike] и сделай REPLY на тестовое сообщение.")
        print(f"[TG Spike] Результат появится здесь.\n")

    # Запускаем listener (регистрируем handlers)
    await listen_for_replies(dp, bot)

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("\n[TG Spike] Остановлено")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
