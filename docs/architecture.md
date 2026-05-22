# Архитектура MAX→Telegram Bridge

## Архитектура после рефакторинга (вид сверху)

### Поток данных

```text
                       ┌──────────────────────────────────────────────┐
                       │                СУПЕРВАЙЗЕР                  │
                       │  runtime/supervisor.py — restart worker      │
                       │  runtime/health/* — state/events/outbox      │
                       └──────────────────────────────────────────────┘
                                          │ управляет
                                          ▼
   ┌─────────────┐     события     ┌──────────────────┐    события     ┌──────────────┐
   │             │ ──────────────► │                  │ ──────────────►│              │
   │   MAX       │                 │   BRIDGE CORE    │                │  TELEGRAM    │
   │  pymax      │                 │   coordinator    │                │  aiogram     │
   │             │ ◄────────────── │                  │ ◄──────────────│              │
   └─────────────┘     send        └──────────────────┘     reply      └──────────────┘
         ▲                                  │                                  ▲
         │                                  │ читает/пишет                     │
         │                                  ▼                                  │
         │                          ┌──────────────────┐                       │
         │                          │  SQLite + files  │                       │
         │                          │ bindings/maps/   │                       │
         │                          │ delivery/queue/  │                       │
         │                          │ recovery/health  │                       │
         │                          └──────────────────┘                       │
         │                                                                     │
         └──── pymax bounded modules ───────────── aiogram adapter/notifier ───┘
```

`BridgeCore` не импортирует `pymax`, `aiogram` или concrete adapters. Он зависит от
`src/bridge/contracts.py`: dataclass-моделей (`MaxMessage`, `MaxAttachment`,
recovery snapshots) и Protocol-портов (`MaxBridgePort`, `TelegramBridgePort`,
`OpsNotifierPort`). Runtime wiring живёт в `src/startup/composition.py`.

### Карта модулей

```text
src/
│
├── main.py                  entrypoint: logging, config, health store, supervisor
├── startup/
│   └── composition.py        composition root: Repository + adapters + BridgeCore
│
├── config/
│   └── loader.py             config.yaml + config.local.yaml + env/secrets
│
├── bridge/                  business logic; no pymax/aiogram imports
│   ├── contracts.py          transport-neutral dataclasses + Protocol ports
│   ├── core.py               coordinator: wires callbacks, services, background jobs
│   ├── status.py             /status, /chats and /help rendering
│   ├── mapping.py            message_map / tg_reply_map idempotency helpers
│   ├── topics.py             topic create/bind/rename decisions
│   ├── forwarding.py         MAX -> TG text/media delivery
│   ├── replies.py            TG replies -> MAX outbound messages
│   ├── media_retry.py        durable MAX media retry enqueue/process/worker
│   ├── delivery.py           delivery_log status helpers
│   ├── background.py         status, watchdog, sweeps, cleanup, weekly recovery
│   ├── commands/
│   │   ├── dispatcher.py     registers command handlers on Telegram port
│   │   ├── dm.py             /dm <user> <text>
│   │   └── recovery.py       /recovery scan|report|export|set|remap
│   └── recovery/
│       ├── scheduler.py      scan task state, debounce/cooldown, notifications
│       ├── orchestrator.py   pure safe scan orchestration and snapshot upsert
│       └── reporter.py       report/status-summary/critical migration alert
│
├── db/                      SQLite facade + subdomain repositories
│   ├── models.py             schema and migrations
│   ├── types.py              repository dataclasses
│   ├── repository.py         public Repository facade
│   └── repos/
│       ├── bindings.py       chat_bindings
│       ├── messages.py       message_map, tg_reply_map
│       ├── delivery.py       delivery_log and activity counters
│       ├── pending_media.py  durable media retry queue
│       ├── users.py          known_users for /dm
│       ├── generations.py    max_account_generations
│       └── recovery.py       chat/dm recovery registry + audit events
│
├── adapters/
│   ├── tg/
│   │   ├── adapter.py        aiogram bot, topic ops, send/receive callbacks
│   │   └── notifier.py       owner DM, ops topic fanout, alert outbox flush
│   ├── tg_adapter.py         compatibility import path
│   │
│   ├── max/
│   │   ├── adapter.py        public MaxAdapter facade over operation services
│   │   ├── state.py          connection/outbound/raw-history/recovery state
│   │   ├── deps.py           explicit service dependency objects
│   │   ├── lifecycle.py      start/reconnect/readiness lifecycle service
│   │   ├── events.py         backend events -> MaxMessage normalization
│   │   ├── raw_payload.py    raw payload hooks/history fetch service
│   │   ├── send.py           outbound send with reconnect wait/ack service
│   │   ├── media/
│   │   │   ├── attachments.py attachment extraction/download service
│   │   │   ├── downloader.py HTTP Range/.part downloader
│   │   │   └── ua.py         MAX CDN srcAg -> User-Agent mapping
│   │   ├── backends/
│   │   │   ├── base.py       internal MaxBackend protocol
│   │   │   └── pymax/        PymaxBackend; only place with pymax imports
│   │   ├── payload.py        plain payload helpers
│   │   ├── users.py          names and DM partner helpers
│   │   ├── errors.py         outbound error classification
│   │   ├── recovery.py       recovery snapshot collection helpers
│   │   ├── resolve.py        chat/user title resolution helpers
│   │   ├── runtime_state.py  last issue / readiness metadata
│   │   ├── voice_recovery.py empty voice/raw history recovery helpers
│   │   ├── context.py        adapter context dataclass
│   │   ├── constants.py      MAX adapter constants
│   │   └── types.py          adapter-local types
│   ├── max_adapter.py        compatibility import path
│   └── max_session_store.py  persistent MAX session blob storage
│
└── runtime/
    ├── supervisor.py         worker restart loop and lifecycle boundaries
    ├── healthcheck.py        Docker healthcheck endpoint
    └── health/
        ├── state.py          HealthSnapshot, HealthIssue, Severity
        ├── store.py          public RuntimeHealthStore facade
        ├── writer.py         atomic persisted writes
        ├── events.py         health event log
        ├── outbox.py         durable alert outbox
        ├── heartbeat.py      heartbeat file writer
        └── rendering.py      operator-facing health messages
```

### Границы зависимостей

- `src/bridge/*`, `src/db/*`, `src/runtime/*`, `src/main.py` не импортируют `pymax` или `aiogram`.
- `src/adapters/tg/*` — единственная aiogram boundary; старый путь `src.adapters.tg_adapter` оставлен для совместимости.
- `src/adapters/max/*` — MAX boundary. `pymax` imports разрешены только в `src/adapters/max/backends/pymax/*`, защищено `tests/test_bridge_contracts.py`.
- `src/startup/composition.py` — composition root: здесь допустимо соединять concrete adapters с `BridgeCore`.
- Recovery auto-scan дельты не спамят Telegram: они попадают агрегатами в 4-часовой `/status`; отдельный alert остаётся только для `account_migration_required`.

## Обзор

```
┌─────────────────────────────────────────────────────────────┐
│                   Bridge Service (Supervisor)                │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                   Bridge Worker                       │  │
│  │                                                       │  │
│  │  ┌──────────────┐  ┌─────────────┐  ┌───────────────┐ │  │
│  │  │  MAX Adapter │  │ Bridge Core │  │  TG Adapter   │ │  │
│  │  │  (userbot)   │─►│  (router)   │─►│  (aiogram)    │ │  │
│  │  │              │◄─│             │◄─│               │ │  │
│  │  └──────────────┘  └──────┬──────┘  └───────────────┘ │  │
│  └───────────────────────────┼───────────────────────────┘  │
│                              │                               │
│    ┌─────────────────────────▼──────────────────────────┐    │
│    │ SQLite + Runtime Health Files                      │    │
│    │ bridge.db · health_state.json · health_events.jsonl│    │
│    │ alert_outbox.jsonl · health_heartbeat.json         │    │
│    └────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
         │                                         │
   MAX WebSocket                          Telegram Bot API
   (personal account)                    (HTTPS long-polling)
```

Supervisor never exits on MAX/TG integration failures. It restarts the worker, persists health transitions, and keeps Docker `HEALTHCHECK` green as long as the runtime loop itself is alive.

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
        ├─ owner-only команды (`/status`, `/chats`, `/reauth`, `/recovery ...`)
        ├─ `/dm` в General принимает от участников группы
        ├─ обычные сообщения в топиках принимает от участников группы
        ├─ извлекает topic_id (message_thread_id)
        └─► Bridge Core._on_tg_reply()
              ├─ ищет ChatBinding по tg_topic_id
              ├─ проверяет mode (readonly → уведомление, disabled → skip)
              ├─ если reply_to_tg_msg_id → ищет max_msg_id + mapped max_chat_id
              ├─ после recovery remap stale reply_to из старого max_chat_id отбрасывается
              ├─ добавляет префикс автора `[Имя Фамилия]`
              └─► MAX Adapter.send_message()
                    ├─ ждёт до 15s если reconnect (self._started=False)
                    └─ client.send_message(chat_id, text, reply_to=...)
```

## Компоненты

### Supervisor / Runtime Health (`src/runtime/`)

Runtime слой разделён на supervisor и health package:

- `supervisor.py` — PID1-процесс, который запускает bridge worker, ловит его аварийный выход и перезапускает с backoff
- `health/state.py` — `HealthSnapshot / SubsystemState / HealthIssue / Severity`
- `health/store.py` — публичный `RuntimeHealthStore`
- `health/writer.py`, `health/events.py`, `health/outbox.py`, `health/heartbeat.py`, `health/rendering.py` — atomic writes, event log, alert outbox, heartbeat and operator message rendering
- persisted артефакты:
  - `data/health_state.json`
  - `data/health_events.jsonl`
  - `data/alert_outbox.jsonl`
  - `data/health_heartbeat.json`
- `healthcheck.py` — Docker healthcheck по freshness heartbeat, а не по доступности MAX/TG API
- `BridgeCore.run_weekly_recovery_snapshot()` и event-driven scheduler — meta-only snapshots для восстановления после нового телефона / нового MAX account

Подсистемы health-model:

- `runtime`
- `max_link`
- `tg_link`
- `storage`
- `scheduler`
- `alerting`

### Bridge Contracts (`src/bridge/contracts.py`)

Транспортно-нейтральная граница между routing-core и внешними библиотеками:
- dataclass-модели `MaxMessage`, `MaxAttachment`, `MaxAttachmentFailure`, `MaxIssue`, `MaxRecoverySnapshot`
- Protocol-порты `MaxBridgePort`, `TelegramBridgePort`, `OpsNotifierPort`
- helper-политики, которые нужны core и adapter-слою одинаково (`is_probable_client_cid`, DM history sweep window)

`src/bridge/contracts.py` не импортирует `pymax`, `aiogram` или concrete adapters. Канонический импорт общих моделей:

```python
from src.bridge.contracts import MaxMessage, MaxAttachment, MaxBridgePort, TelegramBridgePort
```

### Composition Root (`src/main.py`, `src/startup/composition.py`)

`src/main.py` — тонкая точка входа: logging, config load, `RuntimeHealthStore`, `BridgeSupervisor`.

`src/startup/composition.py` — единственное место runtime wiring: создаёт `Repository`, `MaxAdapter`, `TelegramAdapter`, `BridgeCore`, ops notifier и startup notification flow. `src.main` не импортирует concrete adapters или `BridgeCore` напрямую; эту границу защищает architectural regression test.

### MAX Adapter (`src/adapters/max/`, compatibility `src/adapters/max_adapter.py`)

Публичный класс `MaxAdapter` живёт в `src/adapters/max/adapter.py`; старый import path `src.adapters.max_adapter` сохранён как compatibility alias. `MaxAdapter` больше не наследует набор mixin-ов: это facade, который собирает operation services поверх internal backend boundary. По умолчанию он lazily создаёт `PymaxBackend`, но tests/future backend replacement могут передать fake/alternate backend через internal injection point без изменения `BridgeCore`.

Внутренний разрез MAX adapter после explicit-deps рефакторинга:

```text
src.adapters.max_adapter compatibility alias
  └─► MaxAdapter facade
        ├─ public MaxBridgePort methods
        │    send_message(), collect_recovery_snapshot(),
        │    download_*_reference(), resolve_*(), get_last_issue()
        │
        ├─ operation services
        │    lifecycle.py       start/reconnect/readiness
        │    events.py          backend event/raw payload -> MaxMessage
        │    send.py            outbound text + reconnect wait + ack tracking
        │    media/attachments.py media refs -> local files / MaxAttachment
        │    recovery.py        chats/dialogs/users -> MaxRecoverySnapshot
        │    resolve.py         user/chat title and DM partner lookup
        │    voice_recovery.py  empty voice/raw history recovery
        │
        ├─ deps.py              RuntimeDeps/SendDeps/EventsDeps/...
        ├─ state.py             connection/outbound/raw-history/empty-recovery
        └─ MaxBackend
              └─► PymaxBackend  only pymax imports, SocketMaxClient, payloads/files
```

- `backends/base.py` — internal `MaxBackend` protocol: create client, make attachments/messages, opcodes, history/media payloads.
- `backends/pymax/` — `PymaxBackend`; единственное место с `pymax` imports и `SocketMaxClient(reconnect=False, send_fake_telemetry=False)`.
- `state.py` — явный mutable state по доменам: connection, outbound, raw history, empty recovery.
- `deps.py` — explicit dependency objects for operation services; старый service registry / dynamic `__getattr__` не используется.
- `lifecycle.py`, `events.py`, `send.py`, `media/attachments.py`, `recovery.py`, `resolve.py`, `voice_recovery.py` — operation services. Они не импортируют `pymax`, не принимают полный `MaxAdapter`; pymax-specific objects приходят через backend.
- `media/downloader.py`, `media/ua.py`, `payload.py`, `users.py`, `errors.py` — pymax-free helper leaves.

Pymax imports are allowed only inside `src/adapters/max/backends/pymax/*`. Replacing `pymax` later means implementing another `MaxBackend`, not changing `BridgeCore`.

The adapter facade manages:
- Соединением и аутентификацией (сессия в `data/session.db`)
- Reconnect-циклом (fresh client на каждый reconnect — обход pymax OOM-бага)
- Парсингом входящих сообщений → `MaxMessage` dataclass из `src.bridge.contracts`
- Скачиванием медиавложений в `data/tmp/`
- Отправкой сообщений с retry при reconnect
- `collect_recovery_snapshot()` — сбор meta-only recovery snapshot из `client.chats`, `client.channels`, `client.dialogs` + `get_chat()`: chat kind, invite link, owner/admin, DM partner, participant count, session fingerprint hash, DM contact snapshot из реальных dialogs only
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

### Telegram Adapter (`src/adapters/tg/`, compatibility `src/adapters/tg_adapter.py`)

`src/adapters/tg/adapter.py` содержит aiogram bot/dispatcher, topic operations and message send/receive. `src/adapters/tg/notifier.py` отвечает за owner DM, ops topic fanout and alert outbox flush/reporting. Старый import path `src.adapters.tg_adapter` сохранён.

Adapter управляет:
- Ботом и long-polling dispatcher
- Созданием/переименованием топиков в форум-группе
- Отправкой текста, фото, видео, аудио, voice и документов в топики
- Получением reply от участников форум-группы → callback в Bridge Core
- Ограничением команд только владельцем
- Системными ops-уведомлениями:
  - основной канал: owner DM (`TG_OWNER_ID`)
  - опциональный fanout: `telegram.ops_topic_id` внутри forum group
  - outbox/retry, если Telegram временно не принимает alert

Поддерживает два типа команд:
- `on_command(cmd, handler)` — без аргументов (`/status`, `/chats`, `/help`) — только владелец
- `on_arg_command(cmd, handler)` — с произвольным текстом (`/dm Имя текст`)

**Политика доступа к командам:**
- Все команды: только от `TG_OWNER_ID` (владелец) — в любом топике или личном чате с ботом
- `/dm` в топике **General** (без `message_thread_id`): доступна всем участникам форум-группы
- `/recovery ...` и остальные arg-команды: owner-only даже в General

### Bridge Core (`src/bridge/core.py`)

Центральная логика без зависимости от concrete transports: зависит от `src.bridge.contracts`, а не от `pymax`, `aiogram`, `MaxAdapter` или `TelegramAdapter`.

`core.py` — тонкий runtime coordinator: хранит зависимости, stats и ссылки на leaf services, регистрирует MAX/TG callbacks, запускает background entrypoints. Форматтеры статуса, recovery scheduler state, media retry business logic и command wiring живут вне `core.py`; это защищено architectural regression test.

Leaf modules:
- `mapping.py`, `delivery.py`, `topics.py`
- `forwarding.py`, `replies.py`, `media_retry.py`, `status.py`
- `commands/dispatcher.py`, `commands/dm.py`, `commands/recovery.py`
- `recovery/scheduler.py`, `recovery/orchestrator.py`, `recovery/reporter.py`
- `background.py`

Ключевые инварианты прежние: `message_map` пишется до отправки в Telegram; event-driven recovery scans выполняются background task и не блокируют forwarding/topic creation/rename; `/recovery export` остаётся owner-only DM.

**`/dm` — инициация нового DM в MAX из Telegram:**
Алгоритм longest-prefix matching (до 4 слов для имени, минимум 1 слово сообщение).
Поиск пользователя: DB `known_users` → pymax in-memory кеш (`contacts`, `dialogs`, `_users`).

**`/recovery` — миграция MAX аккаунта / нового телефона:**
- `/recovery scan` обновляет registry из текущего MAX аккаунта
- `/recovery report` показывает totals, DM contact aggregates и свежесть snapshot
- `/recovery export` отправляет owner DM JSON с invite/admin/manual metadata и DM contact recovery list
- `/recovery set <topic_id> key=value ...` сохраняет ручные notes/link/admin/status
- `/recovery remap <topic_id> <new_max_chat_id>` сохраняет Telegram topic и меняет routing на новый MAX chat

**Event-driven recovery snapshots:**
- `new_binding` — после создания нового `ChatBinding` bridge ставит high-priority scan примерно через 60 секунд; обычный cooldown не мешает этому событию.
- `title_changed` — после fallback-title rename или явного обновления title ставится короткий debounced scan.
- `control_event` — MAX `CONTROL` в `attachment_types` или `message_type` ставит lower-priority scan с cooldown, чтобы не спамить MAX API.
- `max_connect` и `weekly` остаются safety net: scan после успешного connect/reconnect и weekly background scan.
- `manual` (`/recovery scan`) выполняется сразу и возвращает видимый оператору результат со свежестью.

Планировщик в `bridge/recovery/scheduler.py` использует `asyncio.create_task`: message forwarding, topic creation и rename не ждут snapshot. Несколько событий схлопываются в один scan task; snapshot errors логируются безопасно, без raw payload и без invite links. Этот же scan обновляет `dm_contact_recovery_registry` из `MaxRecoverySnapshot.contacts`; источником являются только `client.dialogs` и уже привязанные DM topics, не `client.contacts` и не `known_users`.

Routine recovery deltas from auto scans are quiet: новые registry rows, unmapped MAX chats, `needs_invite`, `manual_admin_required` и DM contact status changes попадают агрегатами в 4-часовой `/status`, где `/recovery report` остаётся detail view. Immediate owner/ops alert сохраняется только для `account_migration_required`. Любой recovery status/notification текст содержит только counts/statuses; invite links, manual notes, phone numbers, message text, titles, DM contact names и raw MAX fields не попадают в notification/log/health.

### Repository (`src/db/repository.py`, `src/db/repos/*.py`)

`Repository` остался публичным фасадом с прежними методами; SQL разнесён по subdomain repos:

- `bindings.py` — chat bindings and topic mappings
- `messages.py` — `message_map`, `tg_reply_map`
- `delivery.py` — delivery log and activity counters
- `pending_media.py` — durable media retry queue
- `users.py` — `known_users`
- `generations.py` — MAX account generations
- `recovery.py` — recovery registry/events and DM contact registry

Все subrepo используют одно `aiosqlite.Connection`, чтобы commit/transaction behavior оставался прежним. Принципы:
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
- `bridge.media_retry.*` — durable retry MAX-видео из `pending_media_downloads`
- `bridge.recovery.*` — meta-only recovery snapshot scheduling/scan/report/remap/notification events
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
- invite links и manual recovery notes в обычных логах; допустимы только агрегаты вроде `has_invite_link=true`

На `DEBUG` допускается только `safe preview`:

- переносы заменяются на `\n`
- control chars удаляются
- длинные цифровые последовательности маскируются
- строка ограничивается `LOG_PREVIEW_CHARS`

### Отношение к SQLite

SQLite остаётся источником состояния и delivery metadata:

- `message_map` — дедупликация и reply routing
- `tg_reply_map` — дополнительные TG message ids для reply routing поздно досланных медиа
- `delivery_log` — high-level статус доставки
- `chat_recovery_registry` — meta-only registry для восстановления topic routing после нового MAX account
- `chat_recovery_events` — append-only audit только по recovery lifecycle, без message text/raw payload

Детальный event trail по сообщениям в v1 хранится только в application logs, без отдельной message audit-таблицы.

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

-- Поколения MAX аккаунта: новый телефон = новый MAX account
max_account_generations (
    generation_id            INTEGER PK,
    max_user_id              TEXT UNIQUE,
    masked_phone             TEXT,
    session_fingerprint_hash TEXT,
    status                   TEXT, -- active | retired | lost
    first_seen_at            INTEGER,
    last_seen_at             INTEGER
)

-- Recovery registry для переноса существующих Telegram topics на новый MAX account
chat_recovery_registry (
    registry_key             TEXT PK, -- tg_topic:<id> | max_chat:<id>
    tg_topic_id              INTEGER UNIQUE,
    title                    TEXT,
    old_max_chat_id          TEXT,
    current_max_chat_id      TEXT,
    chat_kind                TEXT, -- dm | group | channel | unknown
    mode                     TEXT,
    priority                 INTEGER,
    access_type              TEXT,
    invite_link              TEXT,
    owner_user_id            TEXT,
    owner_name               TEXT,
    admin_contacts_json      TEXT,
    dm_partner_user_id       TEXT,
    dm_partner_name          TEXT,
    participant_count        INTEGER,
    manual_note              TEXT,
    recovery_status          TEXT,
    first_seen_at            INTEGER,
    last_seen_at             INTEGER,
    last_scan_at             INTEGER
)

-- DM-only контакты для восстановления после нового телефона.
-- Источник: реальные client.dialogs и уже привязанные DM topics, не полная address book.
dm_contact_recovery_registry (
    max_user_id              TEXT PK,
    display_name             TEXT,
    old_dm_chat_id           TEXT,
    current_dm_chat_id       TEXT,
    tg_topic_id              INTEGER,
    source                   TEXT, -- dialog | dm_topic
    recovery_status          TEXT, -- visible | needs_contact | needs_remap | account_migration_required | remapped
    first_seen_at            INTEGER,
    last_seen_at             INTEGER,
    last_scan_at             INTEGER
)

-- Append-only audit по recovery lifecycle; details_json без текста сообщений/raw payload
chat_recovery_events (
    registry_key             TEXT,
    tg_topic_id              INTEGER,
    event_type               TEXT,
    details_json             TEXT,
    created_at               INTEGER
)
```

## Конфигурационная модель

```
config.yaml                 ← в git (базовая конфигурация)
config.local.yaml           ← НЕ в git (локальные chat bindings / titles)
.env                        ← НЕ в git (не-секретные локальные env)
  DATA_DIR
  CONFIG_LOCAL_PATH (optional)
.env.secrets                ← НЕ в git (секреты)
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
| recovery registry | Да | Бессрочно, в `data/bridge.db` |
| DM contact recovery registry | Да, только реальные DM dialogs | Бессрочно, в `data/bridge.db` |
| recovery export JSON | Временно | удаляется после отправки owner DM |

Фоновая очистка запускается каждые 30 минут (`Bridge.run_cleanup()`).

## Деплой

Основной production-вариант сейчас — Hetzner Cloud VM + Docker Compose.

Локально:

```bash
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &
```

Production:
```
deploy/Dockerfile
deploy/docker-compose.prod.yml
deploy/hetzner.env.example
```

Подробнее: `docs/runbooks/deployment.md`
