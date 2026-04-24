# MAX → Telegram Bridge — Полная техническая документация

## Обзор

MAX→Telegram Bridge решает конкретную задачу: пользователь не хочет устанавливать MAX, но обязан читать сообщения из школьных, спортивных, семейных групп и личных переписок.

**Проблема:** MAX — обязательный мессенджер в школьных чатах России. Официального Telegram-клиента нет. Установка MAX на основной телефон создаёт cognitive overhead и privacy риски.

**Решение:** Unofficial userbot + supervisor-managed Python async worker + Telegram Forum Supergroup с Topics. Каждый MAX-чат = отдельный изолированный топик. Reply в топике = ответ в MAX.

**Ключевые требования:**
- Один пользователь, один аккаунт MAX — не SaaS
- Никаких третьих лиц с доступом к семейным данным (дети, школа)
- Self-hosted: локально на Mac или на Hetzner Cloud
- Восстановление после перезапуска без потери маппингов

**Текущее состояние на 2 апреля 2026:** bridge уже работает в production на Hetzner Cloud, в Docker Compose, с локальным SQLite state и ограниченным SSH-доступом по IP.

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
│  │  │  (pymax)       │─►│  (router)    │─►│  (aiogram)     │ │   │
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

### Три компонента

**MAX Adapter** (`src/adapters/max_adapter.py`)  
Обёртка над `pymax.SocketMaxClient`. Управляет WebSocket-соединением с MAX, парсит входящие события в `MaxMessage` dataclass, скачивает медиа во временную директорию, отправляет исходящие сообщения. Реализует собственный reconnect-цикл (fresh client на каждый reconnect) обходя OOM-баг библиотеки.

Библиотечная база адаптера:
- GitHub: `https://github.com/MaxApiTeam/PyMax`
- PyPI пакет: `maxapi-python`
- Импорт в коде: `pymax`
- Основной класс: `SocketMaxClient`

На 2 апреля 2026 репозиторий `PyMax` на GitHub помечен как archived/read-only, поэтому проект использует pinned dependency в `requirements.txt` и не рассчитывает на быстрые upstream-фиксы.

**Bridge Core** (`src/bridge/core.py`)  
Вся бизнес-логика без зависимости от транспорта. Принимает события от адаптеров, принимает решения о роутинге, создаёт/переименовывает топики, проверяет режим чата, обеспечивает идемпотентность через SQLite.

**Telegram Adapter** (`src/adapters/tg_adapter.py`)  
Обёртка над `aiogram`. Управляет Forum Supergroup Topics: создание, переименование, отправка сообщений (текст/фото/видео/аудио/voice/документ). Принимает сообщения из нужной форум-группы, пропускает обычные сообщения от участников группы и ограничивает команды `/status`, `/chats` и `/reauth` только владельцем.

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
        ├─ [command /status, /chats, /reauth] → handle_command()
        │     └─ [not owner_id] → DROP
        ├─ [no message_thread_id] → DROP (не в топике)
        └─► BridgeCore._on_tg_reply(topic_id, text, reply_to_tg_id, sender_name)
              ├─ get_binding_by_topic(topic_id) → max_chat_id
              ├─ [not found] → send_notification("⚠️ Не найден MAX чат")
              ├─ [mode=readonly] → send_text("🚫 readonly")
              ├─ [mode=disabled] → return
              ├─ [reply_to_tg_id] → get_max_msg_id_by_tg() → reply_to_max_id
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
- **Видео:** bridge запрашивает `VIDEO_PLAY`, предпочитает реальные `MP4_*` URL вместо `EXTERNAL` HTML-плеера, подбирает `User-Agent` по `srcAg`, затем отправляет через `send_video`
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
    self._started = False
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

В проекте есть базовый regression-набор на `pytest`:

- `tests/test_max_adapter.py` — системные MAX события, supported attachments, channel/forward unwrap, unknown diagnostics, echo/ack исходящих
- `tests/test_bridge_core.py` — пересылка media и rendered text в Telegram
- `tests/test_tg_adapter.py` — приём сообщений от участников группы и command filtering
- `tests/test_main.py` — startup notification с runtime/location/masked IP и статусом startup `pytest`
- `tests/test_repository.py` — upsert `message_map` и сохранность MAX ↔ TG маппинга

Запуск:
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q
```

Автоматизация:

- локально: `PYTHONPATH=. .venv/bin/pytest -q`
- в CI: GitHub Actions workflow `tests.yml`
- в production: startup self-check запускается после первого успешного `MAX connected`

### Секреты и приватные данные

Реальные секреты и приватные данные должны жить только в ignored-файлах:

- `.env.secrets` — `TG_BOT_TOKEN`, `TG_OWNER_ID`, `TG_FORUM_GROUP_ID`, `MAX_PHONE`
- `.env` — только не-секретные локальные env (`DATA_DIR`, optional `CONFIG_LOCAL_PATH`)
- `config.local.yaml` — реальные `max_chat_id`, названия частных чатов и локальные override
- `deploy/server.local.md` — server IP / локальные заметки
- `data/` — SQLite, MAX session, runtime health artifacts

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
  session_filename: "max_bridge_session.db"

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
  forward_voice: false
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
| MAX сессия | `data/max_bridge_session` | До re-auth |

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
| `/reauth` | Инструкция по переавторизации MAX |

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

### Phase 2: Stabilization 🔄

| Задача | Приоритет |
|--------|-----------|
| Retry + backoff для Telegram API | High |
| Alert при потере MAX сессии (>3 ретрая) | High |
| `/status` команда с uptime и статистикой | Medium |
| 7 дней без ручного вмешательства | Goal |

### Phase 3: Cloud ✅

| Задача | Приоритет |
|--------|-----------|
| Hetzner production deploy | Done |
| Docker Compose production | Done |
| Базовый server hardening | Done |

### Phase 4: Hardening ⏳

| Задача | Приоритет |
|--------|-----------|
| Per-chat управление из Telegram | Medium |
| Длинные сообщения (>4096 символов) | Medium |
| Файлы >50 MB — уведомление | Low |
| Unit тесты bridge/core.py | Medium |

---

## Известные ограничения

| Ограничение | Причина | Решение |
|-------------|---------|---------|
| Потеря сообщений за время downtime | pymax не имеет history replay | Минимизировать downtime |
| Команды ограничены владельцем | `/status`, `/chats`, `/reauth` только для owner | — |
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
