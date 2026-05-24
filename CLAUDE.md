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
| `src/adapters/max/adapter.py` (`src/adapters/max_adapter.py`) | MAX facade: operation services with explicit deps over internal backend boundary; старый import path сохранён |
| `src/adapters/max/network/` | MAX-only egress abstraction: direct socket, authenticated HTTP CONNECT, aiohttp proxy options; не используется TG/core |
| `src/adapters/max/backends/pymax/` | Единственная текущая pymax implementation boundary (`Client + ExtraConfig`, PyMax 2 transport/events/raw gateway/models/media helpers) |
| `src/adapters/max/{payload,users,errors,media/*}.py` | Pymax-free MAX helper leaves: payload parsing, names, error classification, CDN UA/download |
| `src/adapters/tg/adapter.py` (`src/adapters/tg_adapter.py`) | aiogram бот: топики, send, recv reply; старый import path сохранён |
| `src/adapters/tg/notifier.py` | Owner DM / ops topic notifications and alert outbox flush |
| `src/bridge/contracts.py` | Транспортно-нейтральные dataclass-модели и Protocol-порты между core и adapters |
| `src/bridge/core.py` + `src/bridge/{forwarding,replies,topics,status,media_retry,...}.py` | Core — runtime coordinator; forwarding/replies/topics/status/media-retry/commands/recovery живут в leaf modules |
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
- `dm_contact_recovery_registry` — DM-only recovery contacts из typed dialog snapshots/привязанных DM topics; не `client.contacts` и не весь `known_users`
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
- `pymax` imports и форма pymax-клиента разрешены только внутри `src/adapters/max/backends/pymax/*`. `MaxAdapter` — facade over operation services with explicit deps and typed MAX client ports; замена pymax в будущем означает новый `MaxBackend`/client port adapter, а не изменение `BridgeCore` или operation services. Проверки: `tests/test_bridge_contracts.py::test_pymax_imports_stay_inside_max_adapter_boundary`, `test_max_adapter_uses_composition_not_mixins`, `test_max_services_use_explicit_dependencies`, `test_max_operation_services_do_not_use_pymax_client_shape_directly`, `tests/test_max_service_ports.py`, `tests/test_max_adapter_leaves.py::test_max_adapter_can_be_composed_with_fake_backend`.
- `message.sender` — это `int` (user_id), **не** User-объект
- `message.chat_id` — `int`; положительный = DM, отрицательный = группа
- `User.names: list[Names]` — имя через `names[0].first_name / last_name / name`
- PyMax 2 `client.chats` может содержать dialogs/groups/channels; backend фильтрует их по `Chat.type`
- `dialogs_snapshot()` — typed DTO поверх `client.chats` с `Chat.type == DIALOG`; старого public `client.dialogs` в PyMax 2 нет
- `client.get_cached_user(id)` — синхронный кеш пользователей
- **DM chat_id ≠ всегда ID собеседника**: когда наш аккаунт инициирует DM, MAX-echo может вернуть `chat_id == own_id`. Либо `resolve_user_name(chat_id)` фейлится для нового контакта и код откатывается к `sender_id == own_id`. В обоих случаях имя собеседника нужно искать через typed `dialogs_snapshot()` → `participants` (фильтруя `own_id`). Реализовано в `MaxAdapter.get_dm_partner_id()`.
- Signed OK CDN URL для MAX-видео чувствительны к `User-Agent`: выбирать клиент по `srcAg` (`CHROME`, `CHROME_ANDROID`, `CHROME_IPHONE`, Safari/iPhone fallback). При обрыве скачивания использовать `*.part` + `Range`; если CDN не поддержал докачку, перекачивать с нуля.
- `Client(..., extra_config=ExtraConfig(reconnect=False, telemetry=False))` — **обязательно оба флага**
  - `reconnect=True` → не использовать: bridge держит outer reconnect loop со свежим client
  - `telemetry=True` → не использовать: сохраняем минимальный telemetry/privacy posture
- PyMax 2 session store должен подхватывать legacy PyMax 1 table `auth(token, device_id)` через `src/adapters/max/backends/pymax/session_store.py`. Иначе PyMax 2 считает, что сессии нет, начинает SMS-auth (`AUTH_REQUEST`) и уходит в rate-limit.
- Existing PyMax 1 `SocketMaxClient` sessions были DESKTOP sessions. PyMax 2 factory использует v1-compatible `DeviceType.DESKTOP` user-agent и sync overrides `0`; default Android profile может дать `LOGIN login.cred / FAIL_WRONG_PASSWORD` на старом token.
- После включения/изменения MAX password/SMS сервер может инвалидировать старый token (`FAIL_LOGIN_TOKEN`, `requires_reauth=true`). Штатный путь: остановить bridge, запустить `python scripts/max_reauth.py` в compose one-shot, ввести SMS/2FA в интерактивном терминале, затем снова поднять bridge. Reauth path должен отключать legacy PyMax 1 `auth` import, иначе старый token импортируется обратно.
- Не делать профилактический/автоматический MAX reauth. Обычный offline/reboot/week-away сценарий должен использовать существующий persisted device token как обычный клиент; reauth разрешён только при явном `requires_reauth=true`/invalid token или осознанной ручной проверке с остановленным bridge. Перед reauth сохранять snapshot `session.db`; не запускать второй MAX-клиент рядом с работающим bridge.
- `Opcode.SESSIONS_CLOSE` нельзя использовать как точечное закрытие одной старой сессии: live-проверка показала, что вызов с `{"time": ...}` привёл к `FAIL_LOGOUT_ALL` и инвалидировал desktop tokens. Старые MAX sessions закрывать только вручную в телефоне, пока не будет подтверждённого безопасного payload/API.
- MAX initial sync может вернуть `lastMessage.attaches.type=UNSUPPORTED`; upstream PyMax 2 `LoginResponse` strict union падает. `src/adapters/max/backends/pymax/login.py` ставит backend-local `BridgeAuthService`, который удаляет только unknown attachments до validation.
- PyMax 2 может отдать `message.text` как `bytes`; для SHARE/сложных payload это может быть msgpack-like binary, а не plain UTF-8. `MaxEventsService` сначала пробует извлечь `text` через msgpack, затем strict UTF-8, и не форвардит binary garbage с `�`.
- PyMax 2 TCP msgpack decoder может падать на raw `CHAT_HISTORY`, если MAX отдаёт map с array-like key (`TypeError: unhashable type: 'list'`). `src/adapters/max/backends/pymax/transport.py` ставит backend-local `BridgeMsgpackPayloadCodec`, который конвертирует такие keys в hashable форму до нормализации payload.
- PyMax 2 TCP `seq` в upstream растёт до `0xFFFFFFFF`, но TCP framer пакует его в one-byte поле; после `255` возникает `struct.error: 'B' format requires 0 <= number <= 255` и ломаются `CHAT_HISTORY`/TG→MAX sends. `src/adapters/max/backends/pymax/transport.py` ставит `BridgeConnectionManager`/sequence guard с wrap `% 0x100`; regression marker: `pymax_tcp_sequence_overflow`.
- PyMax 2 native `on_raw()` используется вместо старого private `_handle_message_notifications` patch; raw requests изолированы в backend `raw_gateway.py` через `client._app.invoke(...)`.
- MAX egress выбирается только внутри `src/adapters/max/`: `home_ru_proxy` использует authenticated HTTP CONNECT к VPS-local reverse Channel M listener, который держится исходящим SSH remote-forward с домашнего РФ роутера; `hetzner_direct` оставляет старый direct egress с VPS. Автоматического fallback нет: при падении proxy MAX деградирует с issue `max_egress_unavailable`, но сам не переключается на Hetzner direct.
- Reconnect реализован вручную: `while True: client = make_client(); await client.start()`
- `MaxAdapter.is_ready()` должен учитывать реальный `client.is_connected`, а не только `_started`: после роутерного flap PyMax 2 может закрыть TCP transport, но не вернуть управление из `start()`.
- `send_message` ждёт до 15 сек если `_started=False` (reconnect window)
- DM history sweep должен ждать `max_adapter.is_ready()`: запуск `CHAT_HISTORY` до `MAX connected` даёт пачку `Not connected`/pending future шумов и может мешать диагностике reauth/reconnect.
- DM history sweep должен быть бережным к MAX API: balanced config живёт в `health.dm_history_sweep` (`120s` warmup после старта/reconnect, затем `900s` steady, jitter и per-chat delay). `replay_recent_history` получает pre-dedup callback через существующий `message_map`, чтобы не нормализовать/качать повторно уже доставленные history messages; pending empty recovery не пропускать.

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
TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE,
MAX_EGRESS_PROXY_URL
MAX_EGRESS_PROXY_HOST, MAX_EGRESS_PROXY_GATEWAY   # .env.host для docker compose extra_hosts reverse Channel M
```

`.env` должен содержать только не-секретные локальные env (`DATA_DIR`, optional `CONFIG_LOCAL_PATH`).
Базовая конфигурация — в `config.yaml` (в git, без секретов).
Локальные chat bindings и реальные названия чатов — в `config.local.yaml` (не в git).
Production `config.local.yaml` должен явно ставить:

```yaml
max:
  egress:
    active: "home_ru_proxy"
```

`hetzner_direct` — только ручной аварийный режим через изменение конфига оператором; в `/status` он помечается warning-ом `MAX uses non-RU direct egress`.

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
