# CLAUDE.md — MAX→Telegram Bridge

Контекст проекта для Claude Code. Читается автоматически при каждой сессии.

## Что это

Личный bridge-сервис: сообщения из российского мессенджера **MAX** → **Telegram Forum Supergroup**.
Один пользователь, один аккаунт MAX, один Telegram бот. Не SaaS, не multi-tenant.

**Цель:** не устанавливать MAX, получать все сообщения в Telegram и отвечать обратно.

## Архитектура (одна строчка)

```
MAX WebSocket ──► MAX Adapter ──► Bridge Core ──► TG Adapter ──► Telegram Topics
                                       │
                                   SQLite DB
```

Каждый MAX-чат = отдельный топик в Telegram Forum Supergroup. Reply в топике = ответ в MAX.

## Компоненты

| Файл | Ответственность |
|------|----------------|
| `src/main.py` | Точка входа, TaskGroup (MAX + TG + cleanup) |
| `src/adapters/max_adapter.py` | pymax userbot: connect, recv, send, reconnect |
| `src/adapters/tg_adapter.py` | aiogram бот: топики, send, recv reply |
| `src/bridge/core.py` | Роутинг: MAX→TG, TG→MAX, dedup, topic auto-create |
| `src/config/loader.py` | YAML конфиг + env переменные |
| `src/db/models.py` | SQLite схема (3 таблицы) |
| `src/db/repository.py` | Data access layer |

## База данных (SQLite)

**3 таблицы, никакого контента сообщений:**

- `chat_bindings` — `max_chat_id ↔ tg_topic_id`, режим чата
- `message_map` — `max_msg_id ↔ tg_msg_id` (дедупликация + reply routing)
- `delivery_log` — статусы доставки (meta only)

## Критические особенности pymax

> Это знание получено через debugging — **не терять**.

- `message.sender` — это `int` (user_id), **не** User-объект
- `message.chat_id` — `int`; положительный = DM, отрицательный = группа
- `User.names: list[Names]` — имя через `names[0].first_name / last_name / name`
- `client.chats` — список групп (populated после sync); для DM кеша нет
- `client.get_cached_user(id)` — синхронный кеш пользователей
- `SocketMaxClient(reconnect=False, send_fake_telemetry=False)` — **обязательно оба флага**
  - `reconnect=True` → OOM (chats/dialogs растут без очистки при каждом reconnect)
  - `send_fake_telemetry=True` (default) → SSL RECORD_OVERFLOW storm
- Reconnect реализован вручную: `while True: client = make_client(); await client.start()`
- `send_message` ждёт до 15 сек если `_started=False` (reconnect window)

## Принципы (не нарушать)

1. **Privacy first** — текст сообщений и медиа не логируются, не хранятся в DB
2. **Whitelist by default** — `forward_all: true` в конфиге; режим per-chat переопределяет
3. **Idempotency** — `max_msg_id` сохраняется до отправки в TG (idempotency key)
4. **No third parties** — всё self-hosted, никаких GREEN-API и подобных
5. **Единственный процесс** — не запускать два экземпляра (двойной userbot = проблемы)

## Запуск

```bash
# Локально
source .venv/bin/activate
python src/main.py

# Фоновый (с логом)
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &

# Проверить процесс
ps aux | grep main.py | grep -v grep

# Остановить
kill $(ps aux | grep 'python src/main.py' | grep -v grep | awk '{print $2}')
```

## Конфиг и секреты

Секреты — только в `.env` (не в git):
```
TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE
```

Базовая конфигурация — в `config.yaml` (в git, без секретов).
Локальные chat bindings и реальные названия чатов — в `config.local.yaml` (не в git).

## Ключевые ограничения

- pymax не воспроизводит историю при reconnect → сообщения во время downtime теряются
- Telegram Bot API: max 30 msg/sec, файлы до 50 MB
- Topics в Telegram: названия до 128 символов
- Бот принимает команды **только от `TG_OWNER_ID`**

## Дальнейшее развитие

Подробный план: `docs/roadmap.md`
Архитектурные решения: `docs/decisions/`
Операционные процедуры: `docs/runbooks/`
