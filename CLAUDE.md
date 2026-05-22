# CLAUDE.md — MAX→Telegram Bridge

Контекст проекта для Claude Code. Читается автоматически при каждой сессии.

## Что это

Личный bridge-сервис: сообщения из российского мессенджера **MAX** → **Telegram Forum Supergroup**.
Один пользователь, один активный MAX account, один Telegram бот. Не SaaS, не multi-tenant.

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
| `src/main.py` | Тонкий entry point: logging, config load, health store, supervisor |
| `src/startup/composition.py` | Runtime composition root: Repository, adapters, BridgeCore, startup notifications |
| `src/adapters/max/adapter.py` (`src/adapters/max_adapter.py`) | pymax userbot facade: connect, recv, send, reconnect; старый import path сохранён |
| `src/adapters/max/{payload,users,errors,media/*}.py` | Pymax-free MAX helper leaves: payload parsing, names, error classification, CDN UA/download |
| `src/adapters/tg/adapter.py` (`src/adapters/tg_adapter.py`) | aiogram бот: топики, send, recv reply; старый import path сохранён |
| `src/adapters/tg/notifier.py` | Owner DM / ops topic notifications and alert outbox flush |
| `src/bridge/contracts.py` | Транспортно-нейтральные dataclass-модели и Protocol-порты между core и adapters |
| `src/bridge/core.py` + `src/bridge/{forwarding,replies,topics,...}.py` | Роутинг, dedup, topic auto-create, commands, recovery registry; core координирует leaf modules |
| `src/runtime/health/` | Health snapshot, health events, alert outbox, heartbeat; package-level imports сохранены |
| `src/runtime/supervisor.py` | Worker restart loop, heartbeat, crash alerts |
| `src/config/loader.py` | YAML конфиг + env переменные |
| `src/db/models.py` | SQLite схема: routing, delivery, retry, users, recovery registry |
| `src/db/repository.py` + `src/db/repos/*.py` | Repository facade + subdomain repos over one `aiosqlite.Connection` |
| `infra/ansible/` | Ansible playbooks: deploy, backup, recover, bootstrap, hardening (см. `infra/ansible/README.md`) |

## База данных (SQLite)

**Никакого контента сообщений:**

- `chat_bindings` — `max_chat_id ↔ tg_topic_id`, режим чата
- `message_map` — `max_msg_id ↔ tg_msg_id` (дедупликация + reply routing)
- `tg_reply_map` — дополнительные TG message ids для reply routing поздно досланных медиа
- `delivery_log` — статусы доставки (meta only)
- `pending_media_downloads` — durable retry meta для MAX media
- `known_users` — справочник имён MAX для `/dm`
- `max_account_generations` — поколения MAX account; новый телефон = новый account
- `chat_recovery_registry` — recovery metadata для переноса Telegram topics на новый MAX account (`last_scan_at`, invite/admin/DM metadata, manual notes)
- `dm_contact_recovery_registry` — DM-only recovery contacts из реальных `client.dialogs`/привязанных DM topics; не `client.contacts` и не весь `known_users`
- `chat_recovery_events` — append-only audit recovery lifecycle без message text/raw payload

## Recovery registry / новый телефон

- MAX phone migration трактуется как новый MAX account.
- Bridge сохраняет Telegram continuity, но не клонирует MAX account и не возвращает закрытые чаты без invite/admin approval.
- DM contacts для восстановления — только люди из реальных личных MAX dialogs; полная address book и group writers из `known_users` не копируются.
- Safe recovery scan запускается после MAX connect/reconnect, раз в неделю и event-driven при `new_binding`, `title_changed`, MAX `CONTROL`.
- Event-driven scans ставятся через `asyncio.create_task`, debounce/cooldown и не должны задерживать forwarding, topic creation или rename.
- Auto recovery scans не шлют отдельный Telegram-spam по обычным дельтам (`unmapped`, `needs_invite`, DM contact changes): агрегированная выжимка попадает в 4-часовой `/status`. Срочный owner/ops alert остаётся только для смены MAX account / migration-required. Без invite links, notes, phones, contact names, message text, titles или raw MAX fields.
- Команды владельца: `/recovery scan`, `/recovery report`, `/recovery export`, `/recovery set <topic_id> key=value ...`, `/recovery remap <topic_id> <new_max_chat_id>`.
- `/recovery export` уходит только owner DM и может содержать invite links/admin notes.
- После remap reply на старое TG сообщение отправляется без `reply_to_msg_id`, если mapped MAX message принадлежит старому `max_chat_id`.

## Критические особенности pymax

> Это знание получено через debugging — **не терять**.

- `BridgeCore` не импортирует `pymax`/`aiogram` и не зависит от concrete adapters: общие модели (`MaxMessage`, `MaxAttachment`, recovery snapshot) и Protocol-порты живут в `src/bridge/contracts.py`. Pymax-грабли и protocol hooks остаются внутри `src/adapters/max/` (`src/adapters/max_adapter.py` — compatibility import).
- `pymax` imports разрешены только внутри bounded MAX adapter modules: `client_factory.py`, `events.py`, `lifecycle.py`, `raw_payload.py`, `send.py`, `media/attachments.py`. Проверка: `tests/test_bridge_contracts.py::test_pymax_imports_stay_inside_max_adapter_boundary`.
- `message.sender` — это `int` (user_id), **не** User-объект
- `message.chat_id` — `int`; положительный = DM, отрицательный = группа
- `User.names: list[Names]` — имя через `names[0].first_name / last_name / name`
- `client.chats` — список групп (populated после sync)
- `client.dialogs` — список DM-диалогов (populated после sync); каждый Dialog имеет `.id` и `.participants: dict[str, int]`
- `client.get_cached_user(id)` — синхронный кеш пользователей
- **DM chat_id ≠ всегда ID собеседника**: когда наш аккаунт инициирует DM, MAX-echo может вернуть `chat_id == own_id`. Либо `resolve_user_name(chat_id)` фейлится для нового контакта и код откатывается к `sender_id == own_id`. В обоих случаях имя собеседника нужно искать через `client.dialogs` → `Dialog.participants` (фильтруя `own_id`). Реализовано в `MaxAdapter.get_dm_partner_id()`.
- Signed OK CDN URL для MAX-видео чувствительны к `User-Agent`: выбирать клиент по `srcAg` (`CHROME`, `CHROME_ANDROID`, `CHROME_IPHONE`, Safari/iPhone fallback). При обрыве скачивания использовать `*.part` + `Range`; если CDN не поддержал докачку, перекачивать с нуля.
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

## Документация

- Поддерживать bilingual public docs: при изменении user-facing/architecture/runbook информации обновлять обе версии, если есть пара (`README.md` ↔ `README-ru.md`).
- Русский остаётся основным языком operator/runbook/context docs (`CLAUDE.md`, `PROJECT.md`, `docs/runbooks/*`), но public README должен иметь актуальную English и Russian версию без смыслового drift.
- Архитектурные изменения отражать минимум в `docs/architecture.md`, `docs/tests.md` и, если меняется boundary/decision, в `docs/decisions/*`; навигационные/summary изменения дублировать в README/README-ru.
- Документация должна описывать фактическое состояние кода после рефакторинга: реальные имена файлов, compatibility import paths, dependency boundaries и текущие operational правила.

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
- Исключение: `/dm` в General может быть public для участников группы; `/recovery ...` всегда owner-only
- Если MAX-вложение после всех retry не скачалось, Telegram должен получить текстовый fallback, а `delivery_log.status` — `partial`, не ложный `delivered`

## Дальнейшее развитие

Подробный план: `docs/roadmap.md`
Архитектурные решения: `docs/decisions/`
Операционные процедуры: `docs/runbooks/`
