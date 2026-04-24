# CLAUDE.md — MAX→Telegram Bridge

Контекст проекта для Claude Code. Читается автоматически при каждой сессии.

## Что это

Личный bridge-сервис: сообщения из российского мессенджера **MAX** → **Telegram Forum Supergroup**.
Один пользователь, один аккаунт MAX, один Telegram бот. Не SaaS, не multi-tenant.

**Цель:** не устанавливать MAX, получать все сообщения в Telegram и отвечать обратно.

## Архитектура (одна строчка)

```
Supervisor ──► Worker(MAX Adapter ──► Bridge Core ──► TG Adapter) ──► Telegram Topics
                     │
          SQLite DB + persisted runtime health files
```

Каждый MAX-чат = отдельный топик в Telegram Forum Supergroup. Reply в топике = ответ в MAX.

## Компоненты

| Файл | Ответственность |
|------|----------------|
| `src/main.py` | Supervisor entry point + bootstrap worker |
| `src/adapters/max_adapter.py` | pymax userbot: connect, recv, send, reconnect |
| `src/adapters/tg_adapter.py` | aiogram бот: топики, send, recv reply, ops notifications |
| `src/bridge/core.py` | Роутинг: MAX→TG, TG→MAX, dedup, topic auto-create, health-aware status |
| `src/runtime/health.py` | Health snapshot, health events, alert outbox, heartbeat |
| `src/runtime/supervisor.py` | Worker restart loop, heartbeat, crash alerts |
| `src/config/loader.py` | YAML конфиг + env переменные |
| `src/db/models.py` | SQLite схема (3 таблицы) |
| `src/db/repository.py` | Data access layer |
| `infra/ansible/` | Ansible playbooks: deploy, backup, recover, bootstrap, hardening (см. `infra/ansible/README.md`) |

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
- `client.chats` — список групп (populated после sync)
- `client.dialogs` — список DM-диалогов (populated после sync); каждый Dialog имеет `.id` и `.participants: dict[str, int]`
- `client.get_cached_user(id)` — синхронный кеш пользователей
- **DM chat_id ≠ всегда ID собеседника**: когда наш аккаунт инициирует DM, MAX-echo может вернуть `chat_id == own_id`. Либо `resolve_user_name(chat_id)` фейлится для нового контакта и код откатывается к `sender_id == own_id`. В обоих случаях имя собеседника нужно искать через `client.dialogs` → `Dialog.participants` (фильтруя `own_id`). Реализовано в `MaxAdapter.get_dm_partner_id()`.
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
6. **Git push только по явной просьбе** — после изменений не делать push автоматически, спросить сначала
7. **Деплой на Hetzner только по явной просьбе** — после изменений не обновлять сервер автоматически, спросить сначала

## Запуск

```bash
# Локально
source .venv/bin/activate
python -m src.main

# Фоновый (с логом)
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &

# Проверить процесс
ps aux | grep 'python -m src.main' | grep -v grep

# Остановить
kill $(ps aux | grep 'python -m src.main' | grep -v grep | awk '{print $2}')
```

## Конфиг и секреты

Секреты — только в `.env.secrets` (не в git):
```
TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE
```

`.env` должен содержать только не-секретные локальные env (`DATA_DIR`, optional `CONFIG_LOCAL_PATH`).
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
