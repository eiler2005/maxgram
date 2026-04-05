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
- Ограничением команд `/status`, `/chats` и `/reauth` только владельцем

### Bridge Core (`src/bridge/core.py`)

Центральная логика без зависимости от транспорта:
- Роутинг MAX↔TG
- Дедупликация (idempotency key в SQLite до отправки)
- Auto-create + auto-rename топиков
- Проверка режима чата (active/readonly/disabled)
- Определение имени топика (`config → chat_title → MAX API → fallback`)

### Repository (`src/db/repository.py`)

Все обращения к SQLite. Принципы:
- Только простые запросы, никаких JOIN-монстров
- Никакого контента сообщений
- Все методы async (aiosqlite)

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
