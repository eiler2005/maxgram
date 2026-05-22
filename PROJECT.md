# MAX → Telegram Bridge — Полная техническая документация

## Обзор

MAX→Telegram Bridge решает конкретную задачу: пользователь не хочет устанавливать MAX, но обязан читать сообщения из школьных, спортивных, семейных групп и личных переписок.

**Проблема:** MAX — обязательный мессенджер в школьных чатах России. Официального Telegram-клиента нет. Установка MAX на основной телефон создаёт cognitive overhead и privacy риски.

**Решение:** Unofficial userbot + supervisor-managed Python async worker + Telegram Forum Supergroup с Topics. Каждый MAX-чат = отдельный изолированный топик. Reply в топике = ответ в MAX.

**Ключевые требования:**
- Один пользователь, один активный MAX account — не SaaS
- Никаких третьих лиц с доступом к семейным данным (дети, школа)
- Self-hosted: локально на Mac или на Hetzner Cloud
- Восстановление после перезапуска без потери маппингов
- Восстановление route-контекста при новом телефоне / новом MAX account без хранения текстов сообщений

**Текущее состояние на 22 мая 2026:** bridge работает в production на Hetzner Cloud, в Docker Compose, с локальным SQLite state, session snapshots, recovery registry и ограниченным SSH-доступом по IP.

### Что уже сделано в production-сессии

- чувствительные данные вынесены из git в `.env.secrets`, `config.local.yaml`, `.env.host`
- добавлен отдельный production compose для Hetzner
- код и состояние (`bridge.db`, MAX session) перенесены на Hetzner VM
- настроен `deploy`-пользователь, отключены root-login и password auth
- включены `UFW`, `fail2ban`, `unattended-upgrades`
- устранён конфликт нескольких Telegram poller-инстансов
- разрешены сообщения в Telegram topic от участников группы, а не только owner
- добавлен префикс автора для Telegram → MAX: `[Имя Фамилия]`
- добавлено startup-уведомление с runtime, host, location и masked IP
- добавлен smoke-report по SQLite для быстрых ручных проверок
- добавлен supervisor/runtime health слой с persisted snapshot, event history, outbox и Docker heartbeat
- основной служебный канал зафиксирован как owner DM; forum-topic fanout стал опциональным
- добавлен MAX account migration recovery registry: account generations, weekly chat snapshots, owner-only `/recovery ...`, JSON export и remap topic → new MAX chat

---

## Архитектура

### Компонентная диаграмма

```
┌─────────────────────────────────────────────────────────────────┐
│                        Bridge Service                            │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ Bridge Supervisor                                         │  │
│  │  • heartbeat                                              │  │
│  │  • restart worker                                         │  │
│  │  • persist health state                                   │  │
│  └───────────────┬───────────────────────────────────────────┘  │
│                  │                                               │
│  ┌───────────────▼───────────────────────────────────────────┐   │
│  │  Bridge Worker                                            │   │
│  │  ┌────────────────┐  ┌──────────────┐  ┌────────────────┐ │   │
│  │  │  MAX Adapter   │  │ Bridge Core  │  │  TG Adapter    │ │   │
│  │  │  (facade)      │─►│  (router)    │─►│  (aiogram)     │ │   │
│  │  │                │◄─│              │◄─│                │ │   │
│  │  └────────────────┘  └──────┬───────┘  └────────────────┘ │   │
│  └──────────────────────────────┼─────────────────────────────┘   │
│                                 │                                  │
│                      ┌──────────▼──────────┐                       │
│                      │ SQLite + Health     │                       │
│                      │ bridge.db           │                       │
│                      │ health_state.json   │                       │
│                      │ health_events.jsonl │                       │
│                      │ alert_outbox.jsonl  │                       │
│                      └─────────────────────┘                       │
└─────────────────────────────────────────────────────────────────┘
         │                                             │
   MAX WebSocket                               Telegram Bot API
   api.oneme.ru:443                            HTTPS long-polling
   (personal account)                          (forum group members)
```

### Основные компоненты

**Entrypoint + composition** (`src/main.py`, `src/startup/composition.py`)
`main.py` остаётся тонкой точкой входа: logging, config load, `RuntimeHealthStore`, `BridgeSupervisor`. Runtime wiring вынесен в `startup/composition.py`: создание `Repository`, `MaxAdapter`, `TelegramAdapter`, `BridgeCore`, ops notifier и startup notification flow.

**MAX Adapter** (`src/adapters/max/`, compatibility `src/adapters/max_adapter.py`)
Публичный `MaxAdapter` — facade над operation services (`send`, `events`, `media`, `recovery`, `resolve`, `voice_recovery`, `lifecycle`) и internal `MaxBackend`. Текущая реализация backend-а — `PymaxBackend` в `src/adapters/max/backends/pymax/`; это единственное место, где разрешены imports `pymax`. Для тестов и будущей замены backend можно подменить через internal injection point, не трогая `BridgeCore`. Старый import path `src.adapters.max_adapter` сохранён.

Библиотечная база адаптера:
- GitHub: `https://github.com/MaxApiTeam/PyMax`
- PyPI пакет: `maxapi-python`
- Импорт в коде: `pymax`
- Runtime backend: `PymaxBackend` поверх `SocketMaxClient`

На 2 апреля 2026 репозиторий `PyMax` на GitHub помечен как archived/read-only, поэтому проект использует pinned dependency в `requirements.txt` и не рассчитывает на быстрые upstream-фиксы.

**Bridge Core** (`src/bridge/contracts.py`, `src/bridge/core.py`, `src/bridge/*`)
Вся бизнес-логика без зависимости от транспорта. `BridgeCore` зависит от transport-neutral contracts, а не от concrete adapters. Сам `core.py` — runtime coordinator: wiring callbacks, stats, service references и background entrypoints. Leaf modules (`forwarding.py`, `replies.py`, `topics.py`, `status.py`, `media_retry.py`, `commands/`, `recovery/`, `background.py`) держат routing, topic, status, command, recovery and background behavior с явными зависимостями.

**Telegram Adapter** (`src/adapters/tg/`, compatibility `src/adapters/tg_adapter.py`)
Обёртка над `aiogram`. `tg/adapter.py` управляет Forum Supergroup Topics, send/receive и command dispatch. `tg/notifier.py` отвечает за owner DM, ops topic fanout and alert outbox flush. Старый import path `src.adapters.tg_adapter` сохранён.

**Repository + runtime health** (`src/db/repository.py`, `src/db/repos/*.py`, `src/runtime/health/`)
`Repository` — публичный фасад над subdomain repos, все используют один `aiosqlite.Connection`. Runtime health package сохраняет прежние форматы `health_state.json`, `health_events.jsonl`, `alert_outbox.jsonl`, `health_heartbeat.json`.

---

## Потоки данных

### MAX → Telegram (входящее)

```
MAX WebSocket event
  └─► MaxAdapter._handle_raw_message(message)
        ├─ msg_id  = str(message.id)
        ├─ chat_id = str(message.chat_id)   # int, >0 = DM, <0 = group
        ├─ sender_id = str(message.sender)  # int — НЕ User-объект!
        ├─ sender_name ← client.get_cached_user(int(sender_id))
        ├─ chat_title ← client.chats (для групп)
        ├─ is_dm = chat_id_int > 0
        ├─ is_own = sender_id == own_id
        ├─ скачивает attaches → data/tmp/
        └─► BridgeCore._on_max_message(msg)
              ├─ [is_own] → только проверяет rename, skip forward
              ├─ [duplicate?] → is_duplicate(msg_id, chat_id) → DROP
              ├─ save_message(idempotency key, tg_msg_id=None)
              ├─► _get_or_create_topic(msg)
              │     ├─ get_binding(chat_id)
              │     │   └─ [fallback title?] → resolve + rename_topic()
              │     └─ [no binding] → resolve_title → create_topic → save_binding
              ├─ [mode=disabled] → return
              └─► _forward_to_telegram(msg, topic_id)
                    ├─ [photo]    → tg.send_photo(topic_id, path, caption)
                    ├─ [video]    → tg.send_video(topic_id, path, caption)
                    ├─ [audio]    → tg.send_audio(topic_id, path, caption)
                    ├─ [voice]    → tg.send_voice(topic_id, path, caption)
                    ├─ [document] → tg.send_document(topic_id, path, caption)
                    ├─ [text]     → tg.send_text(topic_id, "[Name] text")
                    └─ [unknown]  → tg.send_text(topic_id, placeholder)
                    └─ unlink(tmp_file)
```

### Telegram → MAX (исходящее)

```
Telegram Update (message в форум-группе)
  └─► TGAdapter.handle_message(message)
        ├─ [not forum_group_id] → DROP
        ├─ [from_user.is_bot] → DROP
        ├─ [owner command /status, /chats, /reauth, /recovery ...] → handle_command()
        │     └─ [not owner_id] → DROP
        ├─ [public /dm in General] → handle_command()
        ├─ [no message_thread_id] → DROP (не в топике)
        └─► BridgeCore._on_tg_reply(topic_id, text, reply_to_tg_id, sender_name)
              ├─ get_binding_by_topic(topic_id) → max_chat_id
              ├─ [not found] → send_notification("⚠️ Не найден MAX чат")
              ├─ [mode=readonly] → send_text("🚫 readonly")
              ├─ [mode=disabled] → return
              ├─ [reply_to_tg_id] → get_tg_reply_mapping() → reply_to_max_id
              │     └─ [mapped max_chat_id != current binding] → no reply_to after remap
              ├─ compose outbound text = "[Name]\ntext"
              └─► MaxAdapter.send_message(chat_id, text, reply_to=max_id)
                    ├─ [not _started] → wait 3×5s for reconnect
                    └─ client.send_message(chat_id=int, text=str, reply_to=int)
```

### Name Resolution (DM топики)

```
_resolve_chat_title(msg):
  1. config.get_chat_title(chat_id)  → из config.yaml / config.local.yaml (если задано)
  2. msg.chat_title                  → из client.chats (для групп)
  3. [is_dm] → resolve_user_name(chat_id)
       ├─ client.get_cached_user(int) → _extract_user_name()
       │     └─ user.names[0].first_name + last_name
       └─ [cache miss] → client.get_users([int]) → live request
  4. fallback: "Чат {chat_id}"

Fallback rename:
  При получении нового сообщения в чат с title="Чат XXXXX":
  → resolve реальное имя → tg.rename_topic() → repo.update_title()
```

---

## Реализованные функции

### Роутинг и дедупликация

- Каждое входящее событие проверяется по `(max_msg_id, max_chat_id)` в `message_map`
- `max_msg_id` сохраняется в DB **до** отправки в Telegram (idempotency key)
- При reconnect MAX может повторно доставить события — дедупликация блокирует дубли

### Auto-create топиков

- `forward_all: true` — Bridge создаёт топик для любого нового чата
- Топик именуется по приоритету: `config title → group name → DM contact name → fallback`
- Fallback `"Чат XXXXX"` автоматически переименовывается при следующем сообщении

### Режимы чатов

| Режим | MAX → TG | TG → MAX |
|-------|----------|----------|
| `active` | ✅ | ✅ |
| `readonly` | ✅ | ❌ (уведомление) |
| `disabled` | ❌ | ❌ (тихо) |

### Медиа

- **Фото:** скачивается во временный файл, отправляется через `send_photo`, удаляется
- **Видео:** bridge запрашивает `VIDEO_PLAY`, предпочитает реальные `MP4_*` URL вместо `EXTERNAL` HTML-плеера, подбирает `User-Agent` по `srcAg` (`CHROME`, `CHROME_ANDROID`, `CHROME_IPHONE`, Safari/iPhone fallback), затем отправляет через `send_video`
- **Аудио:** `AUDIO` отправляется через `send_audio`, `VOICE` — через `send_voice` (native voice bubble)
- **Документы:** скачиваются через `get_file_by_id(...)`, отправляются через `send_document`
- **Post-validation:** после скачивания проверяются `Content-Type` и magic bytes; HTML/text fallback отклоняется для ожидаемого медиа
- **Лимит размера:** файлы больше `max_file_size_mb` получают явное уведомление о превышении лимита (и для MAX→TG, и для TG→MAX)
- **Неподдерживаемые типы:** placeholder `[Неподдерживаемый тип: {type}]`
- TTL файлов: 1 час (auto-cleanup каждые 30 минут)

### Системные сообщения

- **ControlAttach:** `add`, `leave`, `remove`, `new` и похожие события рендерятся в человекочитаемый текст
- **ControlAttach join-by-link:** `joinbylink` рендерится как `Присоединились по ссылке: ...`
- **ContactAttach:** пересылается как текст `Контакт: ...`
- **StickerAttach:** пересылается текстом (`[Стикер]` / `[Аудиостикер]`)
- **EDITED/REMOVED:** приходят как отдельные уведомления, чтобы bridge не терял эти статусы
- **CHANNEL/forward:** bridge разворачивает `link.message` и raw `CHANNEL`-обёртки до исходного текста/медиа; файлы скачиваются по исходным `chat_id/message_id`, а pymax-дубликат обёртки подавляется.
- **Неизвестные типы:** вместо короткой заглушки bridge отправляет диагностический блок `[Неизвестное сообщение MAX]` с `type`, `status`, `link_*`, количеством текста/вложений и списком полей объекта.

### Группы: имя отправителя

Для групповых чатов (не DM) к тексту добавляется префикс `[Имя Отправителя]`:
```
[Участник чата] Завтра собрание в 18:00
```

### Telegram → MAX: имя автора

Для сообщений, отправленных из Telegram в MAX, bridge добавляет имя Telegram-пользователя:

```text
[Мария Иванова]
Проверка связи
```

### MAX account migration recovery

MAX не даёт штатно поменять телефон в профиле. Новый телефон практически означает новый MAX account, а private/admin-only чаты требуют нового invite link или ручного приглашения админа. Bridge сохраняет не историю сообщений, а recovery context для сохранения Telegram continuity:

- `max_account_generations` фиксирует поколения аккаунта: `max_user_id`, masked phone, hash fingerprint сессии, статус `active|retired|lost`, first/last seen
- `chat_recovery_registry` хранит stable key `tg_topic:<topic_id>`, старый/текущий `max_chat_id`, title, mode, priority, access type, invite link, owner/admin contacts, DM partner metadata, participant count, manual note, recovery status
- `last_scan_at` показывает свежесть snapshot для каждой registry row; `/recovery report` показывает возраст последнего snapshot
- `chat_recovery_events` хранит append-only audit scan/set/remap/account-change без message text/raw MAX payload
- `MaxAdapter.collect_recovery_snapshot()` читает `client.chats`, `client.channels`, `client.dialogs`, enrich через `get_chat()`
- `bridge/recovery/scheduler.py` запускает safe scan после MAX connect/reconnect, затем раз в неделю, а также event-driven при `new_binding`, `title_changed` и MAX `CONTROL`
- Event-driven scans работают асинхронно: background task с debounce/cooldown не задерживает обычную пересылку, создание topics или rename
- Обычные recovery auto-scan дельты попадают агрегатами в 4-часовой `/status`; отдельный owner/ops alert остаётся только для `account_migration_required`. Invite links, notes, phone numbers, message text и raw MAX payload не выводятся в status/notification/log/health

Owner-only команды:

```text
/recovery scan
/recovery report
/recovery export
/recovery set <topic_id> key=value ...
/recovery remap <topic_id> <new_max_chat_id>
```

Remap сохраняет существующий Telegram topic и меняет routing на новый MAX chat. Старый `message_map` остаётся для истории; если пользователь отвечает на старое TG сообщение после remap, bridge не отправляет `reply_to_msg_id`, если mapped MAX message принадлежит старому `max_chat_id`.

### Reconnect стратегия

Pymax `reconnect=True` имеет OOM-баг (см. [ADR-004](docs/decisions/ADR-004-pymax-reconnect-strategy.md)):

```python
# Правильная стратегия:
# MAX Adapter держит собственный reconnect loop,
# а внешний supervisor поднимает весь worker при crash task-group.
while True:
    client = SocketMaxClient(
        reconnect=False,           # управляем сами
        send_fake_telemetry=False, # без SSL storm
    )
    await client.start()           # блокирует до disconnect
    state.connection.started = False
    await asyncio.sleep(5)         # пауза перед reconnect
```

### Outbound retry при reconnect

`send_message` ждёт до 15 секунд если MAX переподключается:
```python
for _ in range(3):
    if self._started: break
    await asyncio.sleep(5)
```

### Тесты

В проекте есть regression-набор на `pytest` (**177 тестов**):

- `tests/test_max_adapter.py` — системные MAX события, supported attachments, channel/forward unwrap, unknown diagnostics, echo/ack исходящих, recovery snapshot collector
- `tests/test_max_adapter_leaves.py` — pymax-free helper leaves и `SocketMaxClient` flags
- `tests/test_bridge_contracts.py` — contracts/composition/pymax-boundary architecture regressions
- `tests/test_bridge_core.py` — пересылка media/rendered text, `/dm`, `/recovery`, async event-driven recovery scans, remap stale-reply safety
- `tests/test_tg_adapter.py` — приём сообщений от участников группы, public `/dm` allowlist, owner-only `/recovery`
- `tests/test_main.py` — startup notification с runtime/location/masked IP и статусом startup `pytest`
- `tests/test_repository.py` — upsert `message_map`, MAX ↔ TG маппинг, recovery migrations/idempotency/deltas/report/export/remap

Запуск:
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q
```

Автоматизация:

- локально: `PYTHONPATH=. .venv/bin/pytest -q`, `compileall`, `ruff check .`, scoped bridge `ruff`, scoped MAX/bridge `mypy`
- в CI: GitHub Actions workflow `tests.yml` запускает тот же test/lint/typecheck gate
- в production: startup self-check запускается после первого успешного `MAX connected`

### Секреты и приватные данные

Реальные секреты и приватные данные должны жить только в ignored-файлах:

- `.env.secrets` — `TG_BOT_TOKEN`, `TG_OWNER_ID`, `TG_FORUM_GROUP_ID`, `MAX_PHONE`
- `.env` — только не-секретные локальные env (`DATA_DIR`, optional `CONFIG_LOCAL_PATH`)
- `config.local.yaml` — реальные `max_chat_id`, названия частных чатов и локальные override
- `deploy/server.local.md` — server IP / локальные заметки
- `data/` — SQLite, MAX session, session snapshots, recovery registry, runtime health artifacts

---

## Модель данных

### `chat_bindings` — маппинг чатов

```sql
max_chat_id  TEXT PRIMARY KEY  -- ID чата в MAX (str, может быть отрицательным)
tg_topic_id  INTEGER           -- message_thread_id в Telegram
title        TEXT              -- название топика
mode         TEXT              -- active | readonly | disabled
created_at   INTEGER           -- unix timestamp
```

### `message_map` — маппинг сообщений

```sql
max_msg_id   TEXT    -- ID сообщения в MAX (уникален в рамках чата)
max_chat_id  TEXT    -- ID чата (для уникальности пары)
tg_msg_id    INTEGER -- message_id в Telegram (NULL до отправки)
tg_topic_id  INTEGER
direction    TEXT    -- inbound | outbound
created_at   INTEGER
UNIQUE(max_msg_id, max_chat_id)
```

Используется для:
1. **Дедупликации** — проверка перед обработкой
2. **Reply routing** — найти `max_msg_id` по `tg_msg_id` для ответа на конкретное сообщение

### `tg_reply_map` — дополнительные reply mappings

```sql
tg_msg_id    INTEGER PRIMARY KEY
max_chat_id  TEXT
max_msg_id   TEXT
tg_topic_id  INTEGER
source       TEXT    -- message_map | pending_media | ...
created_at   INTEGER
```

Нужен для поздно досланных MAX-медиа: несколько Telegram messages могут отвечать одному исходному MAX message. После recovery remap bridge сверяет `max_chat_id` mapping-а с текущим binding и не отправляет stale `reply_to_msg_id`.

### `delivery_log` — лог доставки

```sql
max_msg_id      TEXT
max_chat_id     TEXT
direction       TEXT    -- inbound | outbound
status          TEXT    -- pending | delivered | failed
error           TEXT    -- причина ошибки (NULL если успех)
attempts        INTEGER
created_at      INTEGER
last_attempt_at INTEGER
```

**Принцип:** только metadata. Текст сообщений и медиа в DB не хранятся.

### `max_account_generations` — поколения MAX аккаунта

```sql
generation_id            INTEGER PRIMARY KEY
max_user_id              TEXT UNIQUE
masked_phone             TEXT
session_fingerprint_hash TEXT
status                   TEXT    -- active | retired | lost
first_seen_at            INTEGER
last_seen_at             INTEGER
```

Если после reauth `max_user_id` отличается от предыдущего active generation, bridge помечает старый account как retired и считает, что нужен migration flow.

### `chat_recovery_registry` — registry восстановления

```sql
registry_key             TEXT PRIMARY KEY  -- tg_topic:<id> | max_chat:<id>
tg_topic_id              INTEGER UNIQUE
title                    TEXT
old_max_chat_id          TEXT
current_max_chat_id      TEXT
chat_kind                TEXT              -- dm | group | channel | unknown
mode                     TEXT
priority                 INTEGER
access_type              TEXT
invite_link              TEXT
owner_user_id            TEXT
owner_name               TEXT
admin_contacts_json      TEXT
dm_partner_user_id       TEXT
dm_partner_name          TEXT
participant_count        INTEGER
manual_note              TEXT
recovery_status          TEXT
first_seen_at            INTEGER
last_seen_at             INTEGER
last_scan_at             INTEGER
```

Хранит всё, что нужно для ручного восстановления доступа и remap, кроме содержимого сообщений: invite links, owner/admin contacts, DM partner metadata и manual notes.

### `chat_recovery_events` — audit recovery lifecycle

```sql
registry_key TEXT
tg_topic_id  INTEGER
event_type   TEXT    -- scan | manual_update | remap | account_seen | ...
details_json TEXT
created_at   INTEGER
```

Append-only audit без message text, media URLs, signed tokens и raw MAX payload.

---

## Конфигурация

### config.yaml (в git)

```yaml
telegram:
  bot_token: "${TG_BOT_TOKEN}"
  owner_id: "${TG_OWNER_ID}"
  forum_group_id: "${TG_FORUM_GROUP_ID}"

max:
  phone: "${MAX_PHONE}"
  session_filename: "session.db"

storage:
  db_filename: "bridge.db"
  tmp_dirname: "tmp"

bridge:
  forward_all: true        # автосоздание топиков для новых чатов
  default_mode: "active"
  file_retention_hours: 1
  message_retention_days: 30
  log_retention_days: 7

content:
  forward_photos: true
  forward_documents: true
  forward_voice: true
  placeholder_unsupported: "[Неподдерживаемый тип: {type}]"

chats: []
```

### config.local.yaml (не в git)

```yaml
chats:
  - max_chat_id: "-70000000000001"
    title: "Школьный чат"
    mode: "active"
  # ... локальные чаты владельца
```

### .env (не в git)

```
DATA_DIR=./data
CONFIG_LOCAL_PATH=config.local.yaml  # optional
```

### .env.secrets (не в git)

```
TG_BOT_TOKEN=...
TG_OWNER_ID=...        # Telegram user_id владельца
TG_FORUM_GROUP_ID=...  # ID форум-супергруппы (отрицательное число)
MAX_PHONE=+79...
```

---

## Хранилище и политика retention

| Тип данных | Где хранится | TTL |
|------------|-------------|-----|
| Текст сообщений | ❌ Нигде | — |
| Медиафайлы | `data/tmp/` | 1 час |
| `message_map` | SQLite | 30 дней |
| `delivery_log` | SQLite | 7 дней |
| `chat_bindings` | SQLite | Бессрочно |
| `chat_recovery_registry` | SQLite | Бессрочно |
| `chat_recovery_events` | SQLite | Бессрочно / operator audit |
| Recovery export JSON | `data/tmp/` | Удаляется после отправки owner DM |
| MAX сессия | `data/session.db` | До re-auth |

Автоочистка `BridgeCore.run_cleanup()` — каждые 30 минут.

---

## Критические особенности pymax

Эти знания получены через debugging production — **обязательно учитывать**.

| Факт | Правильно | Неправильно |
|------|-----------|------------|
| `message.sender` | `int` (user_id) | ~~User-объект~~ |
| Имя пользователя | `user.names[0].first_name` | ~~`user.first_name`~~ |
| Кеш пользователей | `client.get_cached_user(int(id))` | ~~прямой доступ~~ |
| Reconnect | `reconnect=False` + outer loop | ~~`reconnect=True`~~ (OOM) |
| Telemetry | `send_fake_telemetry=False` | ~~default True~~ (SSL storm) |
| Собственный ID | `client.me` (атрибут) | ~~`client.get_me()`~~ (нет метода) |
| Название чата | `client.chats` (после sync) | ~~`message.chat_title`~~ (нет поля) |

---

## Telegram Bot API

### Необходимые права бота в форум-группе

- Отправка сообщений
- Управление топиками (create, edit)
- Отправка медиа (фото, видео, аудио, документы)

### Ограничения

- Бот принимает команды **только от `TG_OWNER_ID`**
- Исключение: `/dm` в General доступна участникам группы через explicit allowlist
- `/recovery ...` всегда owner-only, потому что export/report связаны с invite/admin metadata
- Бот принимает сообщения **только из `TG_FORUM_GROUP_ID`**
- Обычные сообщения внутри topic могут отправлять участники группы
- Максимальный размер файла: 50 MB (Bot API limit)
- Максимальная длина названия топика: 128 символов
- Rate limit: 30 сообщений/сек на группу

### Команды бота

| Команда | Ответ |
|---------|-------|
| `/status` | `✅ Bridge работает` |
| `/chats` | Список чатов с topic_id, режимом и активностью |
| `/help` | Справка по командам |
| `/dm Имя Фамилия текст` | Public в General: начать новый DM в MAX |
| `/reauth` | Инструкция по переавторизации MAX |
| `/recovery scan` | Owner-only: обновить recovery snapshot сейчас |
| `/recovery report` | Owner-only: totals, статусы и свежесть snapshot |
| `/recovery export` | Owner-only: JSON registry в owner DM |
| `/recovery set <topic_id> key=value ...` | Owner-only: note/link/admin/status/priority |
| `/recovery remap <topic_id> <new_max_chat_id>` | Owner-only: сохранить TG topic и сменить MAX routing |

---

## Запуск и операции

### Локальный запуск

```bash
# Установка
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Для разработки и тестов
pip install -r requirements-dev.txt

# Первая авторизация MAX (интерактивно, вводить SMS)
python -m src.main

# Фоновый запуск
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &

# Проверка
ps aux | grep 'python -m src.main' | grep -v grep
tail -f data/bridge.log
```

### Проверка здоровья

```bash
# Последние события
tail -50 data/bridge.log

# Статус в DB
.venv/bin/python -c "
import asyncio, aiosqlite

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    db.row_factory = aiosqlite.Row
    async with db.execute('SELECT * FROM chat_bindings') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    async with db.execute('SELECT * FROM delivery_log ORDER BY created_at DESC LIMIT 5') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    await db.close()
asyncio.run(main())
"
```

### Docker

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f
```

### Ansible (рекомендуемый способ деплоя и операций)

С версии 1.1.7 регулярный деплой, бэкап и подготовка новых VM кодифицированы как Ansible playbooks в `infra/ansible/`:

```bash
cd infra/ansible
ansible-playbook deploy.yml --check --diff   # сначала preflight verify без rollout
ansible-playbook deploy.yml                   # затем реально
ansible-playbook backup.yml                   # снять снимок state на ноут
```

`bootstrap.yml` + `hardening.yml` запускаются только для новых VM, к текущему prod не применяются.
Inventory с реальным IP — в `.gitignore`. Quickstart: [infra/ansible/README.md](infra/ansible/README.md).

### Hetzner Cloud (ручной fallback)

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Production baseline:

- один Ubuntu 24.04 VM
- Docker Compose
- `UFW` + `fail2ban` + `unattended-upgrades`
- SSH только по ключу и только с доверенного IP
- публичные HTTP-порты не используются

### Fly.io (исторический/альтернативный вариант)

```bash
fly apps create maxtg-bridge
fly volumes create bridge_data --size 1 --region ams
fly secrets set TG_BOT_TOKEN=... TG_OWNER_ID=... TG_FORUM_GROUP_ID=... MAX_PHONE=...
fly deploy -c deploy/fly.toml
fly logs -f
```

Подробнее: [docs/runbooks/deployment.md](docs/runbooks/deployment.md)

---

## Статус роадмапа

### Phase 0: Spike ✅ (2026-04-02)

| Задача | Статус |
|--------|--------|
| pymax авторизация | ✅ |
| Получение входящих сообщений | ✅ |
| Telegram Topics создание | ✅ |
| Reply routing в aiogram | ✅ |
| Go/No-Go: **GO** | ✅ |

Результаты: [spike/SPIKE_RESULTS.md](spike/SPIKE_RESULTS.md)

### Phase 1: MVP ✅ (2026-04-02)

| Задача | Статус |
|--------|--------|
| Структура проекта, SQLite схема | ✅ |
| MAX Adapter с reconnect | ✅ |
| TG Adapter с управлением топиками | ✅ |
| Bridge Core: routing, dedup, auto-create | ✅ |
| Reply Telegram → MAX | ✅ |
| Медиа: фото, видео, аудио, документы | ✅ |
| Name resolution для DM | ✅ |
| Fallback title auto-rename | ✅ |
| pymax OOM fix | ✅ |
| SSL storm fix (`send_fake_telemetry=False`) | ✅ |
| Outbound retry при reconnect (3×5s) | ✅ |
| Запуск локально | ✅ |

### Phase 2: Stabilization ✅

| Задача | Статус |
|--------|--------|
| Retry + backoff для Telegram API | ✅ |
| Watchdog/alert при потере MAX | ✅ |
| `/status` команда с uptime и статистикой | ✅ |
| 7 дней без ручного вмешательства | ✅ |

### Phase 3: Cloud ✅

| Задача | Приоритет |
|--------|-----------|
| Hetzner production deploy | Done |
| Docker Compose production | Done |
| Базовый server hardening | Done |

### Phase 4: Hardening ✅

| Задача | Статус |
|--------|--------|
| `/chats` команда | ✅ |
| Нативные voice note bubbles | ✅ |
| Missed messages gap notice | ✅ |
| Файлы >50 MB — уведомление | ✅ |
| Расширение unit/regression tests | ✅ |

### Phase 5-6: UX + Ops automation ✅

| Задача | Статус |
|--------|--------|
| `/dm Имя Фамилия текст` из General | ✅ |
| `/help` команда | ✅ |
| `known_users` lookup | ✅ |
| Ansible deploy/backup/recover/bootstrap/hardening | ✅ |

### Phase 7: MAX account migration recovery ✅ (2026-05-22)

| Задача | Статус |
|--------|--------|
| Account generations + session fingerprint hash | ✅ |
| Chat recovery registry + `last_scan_at` freshness | ✅ |
| Hybrid recovery snapshots: connect/reconnect + weekly + event-driven | ✅ |
| Async debounce/cooldown scheduler без задержки forwarding | ✅ |
| Quiet recovery status summary + migration alert | ✅ |
| Owner-only `/recovery scan/report/export/set/remap` | ✅ |
| Remap safety для stale reply mapping | ✅ |
| Privacy tests для report/logs/export path | ✅ |

### Phase 8: Architecture boundary refactor ✅ (2026-05-22)

| Задача | Статус |
|--------|--------|
| Bridge contracts boundary: core без concrete adapters | ✅ |
| Repository subdomain repos with compatibility facade | ✅ |
| Bridge leaf modules for forwarding/replies/topics/recovery/background | ✅ |
| Telegram adapter package + notifier split | ✅ |
| Runtime health package split with stable file formats | ✅ |
| MAX adapter package split, pymax bounded modules, pymax-free helper leaves | ✅ |
| Startup composition root; `main.py` as thin entry point | ✅ |
| Architecture regression tests for contracts, composition and pymax boundary | ✅ |

---

## Известные ограничения

| Ограничение | Причина | Решение |
|-------------|---------|---------|
| Потеря сообщений за время downtime | pymax не имеет history replay | Минимизировать downtime |
| Новый телефон/MAX account не восстанавливает закрытые чаты автоматически | MAX требует новый invite/link/admin approval | Recovery registry + `/recovery remap` после ручного доступа |
| Команды ограничены владельцем | `/status`, `/chats`, `/reauth`, `/recovery ...` только для owner; `/dm` public только в General | — |
| Неофициальный userbot | Нет официального Python SDK для личных аккаунтов MAX | Мониторить pymax обновления |
| Нет истории при старте | Out of scope MVP | Phase 4+ |

---

## Архитектурные решения

Документированы в [docs/decisions/](docs/decisions/):

| ADR | Решение |
|-----|---------|
| [ADR-001](docs/decisions/ADR-001-unofficial-userbot.md) | Unofficial userbot вместо Bot API |
| [ADR-002](docs/decisions/ADR-002-telegram-forum-topics.md) | Forum Supergroup + Topics как UI |
| [ADR-003](docs/decisions/ADR-003-python-monolith-sqlite.md) | Python async монолит + SQLite |
| [ADR-004](docs/decisions/ADR-004-pymax-reconnect-strategy.md) | Fresh client на каждый reconnect |
| [ADR-005](docs/decisions/ADR-005-max-account-recovery-registry.md) | MAX account migration recovery registry |
| [ADR-006](docs/decisions/ADR-006-bridge-contracts-boundary.md) | Bridge contracts boundary для transport adapters |
