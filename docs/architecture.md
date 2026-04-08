# Архитектура MAX→Telegram Bridge

## Обзор

```
┌─────────────────────────────────────────────────────────────┐
│                        Bridge Service                        │
│                                                             │
│  ┌──────────────┐    ┌─────────────┐    ┌───────────────┐  │
│  │  MAX Adapter │    │ Bridge Core │    │  TG Adapter   │  │
│  │  (userbot)   │───►│  (router)   │───►│  (aiogram)    │  │
│  │              │◄───│             │◄───│               │  │
│  └──────────────┘    └──────┬──────┘    └───────────────┘  │
│                             │                               │
│                       ┌─────▼──────┐                        │
│                       │  SQLite DB │                        │
│                       │ (bindings, │                        │
│                       │  messages) │                        │
│                       └────────────┘                        │
└─────────────────────────────────────────────────────────────┘
         │                                         │
   MAX WebSocket                          Telegram Bot API
   (personal account)                    (HTTPS long-polling)
```

## Потоки данных

### MAX → Telegram (входящее)

```
MAX WebSocket event
  └─► MAX Adapter._handle_raw_message()
        ├─ парсит поля (msg_id, chat_id, sender_id, text, attaches)
        ├─ определяет is_dm (chat_id > 0) и is_own (sender == own_id)
        ├─ скачивает медиа в data/tmp/ (если есть)
        └─► Bridge Core._on_max_message()
              ├─ пропускает is_own (но проверяет rename fallback-топика)
              ├─ дедупликация по max_msg_id в message_map
              ├─ сохраняет MessageRecord (idempotency key)
              ├─► _get_or_create_topic() — возвращает tg_topic_id
              │     ├─ если binding есть и title="Чат XXXXX" → пробует rename
              │     └─ если нет → _resolve_chat_title() → tg.create_topic()
              ├─ проверяет binding.mode (disabled → skip)
              └─► _forward_to_telegram()
                    ├─ фото → tg.send_photo()
                    ├─ видео → tg.send_video()
                    ├─ аудио → tg.send_audio()
                    ├─ voice → tg.send_voice()
                    ├─ документ → tg.send_document()
                    ├─ текст → tg.send_text() с prefix "[Имя]" для групп
                    └─ удаляет tmp-файл
```

### Telegram → MAX (исходящее)

```
Telegram Update (reply в топике форум-группы)
  └─► TG Adapter.handle_message()
        ├─ проверяет chat.id == forum_group_id
        ├─ игнорирует сообщения от ботов
        ├─ команды (`/status`, `/chats`, `/reauth`) принимает только от owner_id
        ├─ обычные сообщения в топиках принимает от участников группы
        ├─ извлекает topic_id (message_thread_id)
        └─► Bridge Core._on_tg_reply()
              ├─ ищет ChatBinding по tg_topic_id
              ├─ проверяет mode (readonly → уведомление, disabled → skip)
              ├─ если reply_to_tg_msg_id → ищет max_msg_id (reply routing)
              ├─ добавляет префикс автора `[Имя Фамилия]`
              └─► MAX Adapter.send_message()
                    ├─ ждёт до 15s если reconnect (self._started=False)
                    └─ client.send_message(chat_id, text, reply_to=...)
```

## Компоненты

### MAX Adapter (`src/adapters/max_adapter.py`)

Обёртка над `pymax.SocketMaxClient`. Управляет:
- Соединением и аутентификацией (сессия в `data/max_bridge_session`)
- Reconnect-циклом (fresh client на каждый reconnect — обход pymax OOM-бага)
- Парсингом входящих сообщений → `MaxMessage` dataclass
- Скачиванием медиавложений в `data/tmp/`
- Отправкой сообщений с retry при reconnect
- `get_dm_partner_id(chat_id)` — поиск реального собеседника в DM через `client.dialogs`, фильтруя собственный `own_id`
- `find_user_by_name(name)` — поиск user_id по имени в `contacts`, `dialogs` и `_users` кеше pymax

Используемая библиотека:
- GitHub: `https://github.com/MaxApiTeam/PyMax`
- PyPI: `maxapi-python`
- Импорт: `pymax`

**Флаги SocketMaxClient (обязательно):**
```python
SocketMaxClient(reconnect=False, send_fake_telemetry=False)
```

### Telegram Adapter (`src/adapters/tg_adapter.py`)

Обёртка над `aiogram`. Управляет:
- Ботом и long-polling dispatcher
- Созданием/переименованием топиков в форум-группе
- Отправкой текста, фото, видео, аудио, voice и документов в топики
- Получением reply от участников форум-группы → callback в Bridge Core
- Ограничением команд только владельцем

Поддерживает два типа команд:
- `on_command(cmd, handler)` — без аргументов (`/status`, `/chats`, `/help`) — только владелец
- `on_arg_command(cmd, handler)` — с произвольным текстом (`/dm Имя текст`)

**Политика доступа к командам:**
- Все команды: только от `TG_OWNER_ID` (владелец) — в любом топике или личном чате с ботом
- `/dm` (и любая `on_arg_command`) в топике **General** (без `message_thread_id`): доступна всем участникам форум-группы

### Bridge Core (`src/bridge/core.py`)

Центральная логика без зависимости от транспорта:
- Роутинг MAX↔TG
- Дедупликация (idempotency key в SQLite до отправки)
- Auto-create + auto-rename топиков
- Проверка режима чата (active/readonly/disabled)
- Определение имени топика (`config → chat_title → MAX API → fallback`)
- Персистирование отправителей входящих сообщений в `known_users`
- Команды: `/status`, `/chats`, `/help`, `/dm Имя текст`

**`/dm` — инициация нового DM в MAX из Telegram:**
Алгоритм longest-prefix matching (до 4 слов для имени, минимум 1 слово сообщение).
Поиск пользователя: DB `known_users` → pymax in-memory кеш (`contacts`, `dialogs`, `_users`).

### Repository (`src/db/repository.py`)

Все обращения к SQLite. Принципы:
- Только простые запросы, никаких JOIN-монстров
- Никакого контента сообщений
- Все методы async (aiosqlite)

## Архитектура логирования

### Цели

Система логирования строится как event trail по каждому сообщению, а не как набор несвязанных строк.
Главная задача: ответить на вопросы "что пришло", "как это классифицировали", "что решили сделать", "что реально отправили" и "почему пропустили или уронили".

### Слои

```text
src/logging_utils.py
  ├─ sanitize_preview()    → safe preview текста только для DEBUG
  ├─ sanitize_url()        → убирает query string и чувствительные части URL
  ├─ sanitize_path()       → оставляет только basename файла
  ├─ build_max_flow_id()   → mx:<chat_id>:<msg_id>
  ├─ build_tg_flow_id()    → tg:<topic_id>:<tg_msg_id>
  ├─ log_event()           → единая точка записи event-полей
  └─ EventFormatter        → text/json/mixed форматирование
```

### Форматы

Поддерживаются env-переключатели:

- `LOG_LEVEL`
- `LOG_FORMAT=text|json|mixed`
- `LOG_PREVIEW_CHARS`
- `LOG_LIBRARIES_DEBUG=0|1`

`mixed` — основной operational-режим:

- строка остаётся читаемой человеком
- обязательные поля идут как `key=value`
- сложные поля сериализуются как компактный JSON

`json` — для машинного разбора и внешних log-pipeline.

### Корреляция событий

Каждая трасса маршрута получает `flow_id`:

- `mx:<chat_id>:<msg_id>` для MAX -> Telegram
- `tg:<topic_id>:<tg_msg_id>` для Telegram -> MAX

Это позволяет grep-ить полный путь одного сообщения через:

- MAX adapter
- Bridge core
- Telegram adapter

### События по стадиям

Основные группы:

- `max.inbound.*` — сырой приём, нормализация, skip и download вложений
- `bridge.inbound.*` — dedup, topic resolution, forward MAX -> TG
- `tg.outbound.*` — отправка в Telegram, retry, sent, failed
- `tg.inbound.*` — входящее из Telegram и скачивание медиа
- `bridge.outbound.*` — reply resolution и доставка TG -> MAX
- `max.outbound.*` — отправка в MAX и echo/ack result
- `bridge.watchdog.*`, `bridge.cleanup.*`, `app.startup.*` — эксплуатационные фоновые события

Общие поля событий:

- `event`
- `flow_id`
- `direction`
- `stage`
- `outcome`
- `reason`
- `max_chat_id`
- `max_msg_id`
- `tg_topic_id`
- `tg_msg_id`

### Политика приватности

В постоянных логах не хранятся:

- полный текст сообщений на `INFO`
- телефон, токены и query-параметры URL
- абсолютные temp-path
- бинарные payload и сырые dumps библиотек

На `DEBUG` допускается только `safe preview`:

- переносы заменяются на `\n`
- control chars удаляются
- длинные цифровые последовательности маскируются
- строка ограничивается `LOG_PREVIEW_CHARS`

### Отношение к SQLite

SQLite остаётся источником состояния и delivery metadata:

- `message_map` — дедупликация и reply routing
- `delivery_log` — high-level статус доставки

Детальный event trail в v1 хранится только в application logs, без отдельной audit-таблицы.

## Схема базы данных

```sql
-- Связь чатов
chat_bindings (
    max_chat_id TEXT PK,
    tg_topic_id INTEGER,
    title       TEXT,
    mode        TEXT,   -- active | readonly | disabled
    created_at  INTEGER
)

-- Маппинг сообщений (дедупликация + reply routing)
message_map (
    max_msg_id  TEXT,
    max_chat_id TEXT,
    tg_msg_id   INTEGER,
    tg_topic_id INTEGER,
    direction   TEXT,   -- inbound | outbound
    created_at  INTEGER,
    UNIQUE(max_msg_id, max_chat_id)
)

-- Лог доставки (meta only, без текста)
delivery_log (
    max_msg_id      TEXT,
    max_chat_id     TEXT,
    direction       TEXT,
    status          TEXT,  -- pending | delivered | failed
    error           TEXT,
    attempts        INTEGER,
    created_at      INTEGER,
    last_attempt_at INTEGER
)

-- Справочник пользователей MAX (для /dm поиска по имени)
known_users (
    max_user_id  TEXT PK,
    display_name TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
)
-- Заполняется при каждом входящем сообщении от не-собственного отправителя.
-- Поиск по имени — Python-level case-insensitive (SQLite NOCASE не покрывает кириллицу).
```

## Конфигурационная модель

```
config.yaml                 ← в git (базовая конфигурация)
config.local.yaml           ← НЕ в git (локальные chat bindings / titles)
.env                        ← НЕ в git (секреты)
  TG_BOT_TOKEN
  TG_OWNER_ID
  TG_FORUM_GROUP_ID
  MAX_PHONE
```

## Политика хранения данных

| Данные | Хранится | TTL |
|--------|----------|-----|
| Текст сообщений | Нет | — |
| Медиафайлы (tmp) | Временно | 1 час |
| message_map | Да | 30 дней |
| delivery_log | Да | 7 дней |
| chat_bindings | Да | Бессрочно |

Фоновая очистка запускается каждые 30 минут (`Bridge.run_cleanup()`).

## Деплой

Основной production-вариант сейчас — Hetzner Cloud VM + Docker Compose.

Локально:

```bash
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &
```

Production:
```
deploy/Dockerfile
deploy/docker-compose.prod.yml
deploy/hetzner.env.example
```

Подробнее: `docs/runbooks/deployment.md`
