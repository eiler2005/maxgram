# Roadmap — Maxgram

## Статус: v1.3.0 в production ✅

Дата запуска MVP: 2026-04-02  
Дата v1.1.0: 2026-04-03  
Дата v1.2.0: 2026-04-08
Дата v1.3.0: 2026-05-22
Дата full MAX client ports refactor: 2026-05-23

---

## Что сделано (Phase 0 + Phase 1 + Phase 2 + Phase 3)

### Phase 0: Spike / Discovery ✅
- [x] Исследование pymax (PyPI: `maxapi-python`)
- [x] Авторизация через phone+OTP
- [x] Получение входящих сообщений через WebSocket
- [x] Создание Telegram форум-суперчата с Topics
- [x] Проверка aiogram: create_topic, send_message, reply routing
- [x] **Go/No-Go: GO** — обе системы работают

Итоги spike: `spike/SPIKE_RESULTS.md`

### Phase 1: MVP ✅
- [x] Структура проекта, конфиг YAML, SQLite схема
- [x] MAX Adapter: connect, auth, message parsing, reconnect
- [x] TG Adapter: create/rename topics, send text/photo/video/audio/document, receive reply
- [x] Bridge Core: routing, dedup, auto-create topics, name resolution для DM
- [x] Reply routing: Telegram reply → MAX send (с retry при reconnect)
- [x] Media forwarding MAX→TG: фото, видео, аудио, документы
- [x] Own-message filtering (не форвардим свои сообщения)
- [x] Fallback title rename: "Чат 123456789" → "Имя контакта"
- [x] Запуск локально (nohup + bridge.log)

### Критические баги Phase 1 ✅
- [x] **pymax OOM**: `reconnect=True` → exponential growth chats/dialogs → fix: outer reconnect loop
- [x] **Telemetry disabled**: PyMax 2 client создаётся с `ExtraConfig(telemetry=False)`; старый pymax 1 `send_fake_telemetry=True` SSL storm не возвращаем
- [x] **sender_name=None**: `message.sender` это `int`, не User-объект → fix: `get_cached_user(int(sender_id))`
- [x] **send_message fails on reconnect**: "Socket is not connected" → fix: retry 3×5s
- [x] **5 startup notifications**: `on_start` fires on every reconnect → fix: `_started_once` flag

### Phase 2: Stabilization ✅
- [x] **Telegram retry/backoff** — 3 попытки, задержки 1s/2s, `TelegramRetryAfter` respected
- [x] **`/status` команда** — uptime, сообщения за 4ч (текст/медиа), ошибки, топ-10 активных чатов
- [x] **Уведомление при потере MAX** — watchdog: alert если MAX недоступен > 60 секунд
- [x] **Периодический статус-отчёт** — автоматически каждые 4 часа без команды
- [x] **Расширенный startup** — runtime, hostname, datacenter location, masked IP, кол-во чатов
- [x] **Startup self-check в production** — после `MAX connected` запускается `pytest`, а итог добавляется в startup-уведомление бота
- [x] **Media forwarding TG→MAX** — фото, видео, аудио, голосовые, документы через pymax attachment API
- [x] **sender_name из live API** — `get_cached_user` + live `get_users()` fallback; имена в группах работают
- [x] **Own-message echo dedup** — реальный `max_msg_id` сохраняется перед отправкой; эхо подавляется
- [x] **`/status` в личном чате** — команды принимаются от владельца и в форум-группе, и в DM с ботом
- [x] **152 regression-теста** — все проходят; описание: `docs/tests.md`

### Phase 3: Cloud Migration ✅
- [x] Dockerfile + docker-compose.prod.yml
- [x] Production runtime: Python 3.13 + `restart: always`
- [x] Hetzner Cloud (CX23, hel1): UFW, fail2ban, non-root контейнер, `cap_drop: ALL`
- [x] SSH-only доступ по ключу, restricted by IP
- [x] `unattended-upgrades`, backups enabled
- [x] MAX сессия перенесена на сервер без re-auth
- [x] `/reauth` команда — инструкция для ручного переподключения

Детали деплоя: `docs/runbooks/hetzner-production.md`

---

## Phase 4: Hardening ✅

**Цель:** удобство управления и устойчивость к edge cases.

- [ ] **Per-chat управление из Telegram** — `/mode -70000000000001 readonly`
- [x] **`/chats` команда** — список активных чатов с топиками, режимом и счётчиками сообщений
- [ ] **Длинные сообщения** — разбивать >4096 символов на части
- [x] **Нативные voice note bubbles** — голосовые (`VOICE`) отправляются в Telegram как `send_voice`
- [x] **Missed messages gap** — при восстановлении после offline bridge шлёт уведомление о возможном пропуске сообщений
- [x] **Унификация типов MAX-вложений** — единый normalizer для alias-типов (`IMAGE`, `VOICE`, `DOCUMENT`, `DOC` и т.п.) используется и в верхнем dispatch, и в download pipeline
- [x] **Пост-валидация скачанных вложений** — после download проверяются `Content-Type` и сигнатура файла, HTML/player fallback отбрасывается
- [x] **Расширение тест-сьюта** — retry-логика `_tg_retry`, тест логирования outbound forward

---

## Phase 5: UX-улучшения (следующая итерация)

**Цель:** управление чатами и новые контакты без открытия MAX.

- [x] **`/dm Имя Фамилия текст`** — инициировать новый DM в MAX прямо из Telegram; поиск по имени в БД и кеше pymax; команда доступна всем участникам группы в топике General
- [x] **`/help` команда** — статическая справка со всеми командами
- [x] **Стартовое сообщение** — перечисляет все доступные команды (`/status · /chats · /dm · /help`)
- [x] **`known_users` таблица** — SQLite-справочник пользователей MAX; пополняется из входящих сообщений; основа для /dm
- [x] **DM topic title fix** — правильное имя топика при DM, инициированном с нашей стороны; `get_dm_partner_id()` фильтрует `own_id` из typed dialog snapshots
- [ ] **Длинные сообщения** — разбивать >4096 символов на части
- [ ] **Per-chat управление из Telegram** — `/mode -70000000000001 readonly`

---

## Phase 6: Ops automation ✅

**Цель:** превратить ручной runbook деплоя/восстановления в исполняемый код.

- [x] **Ansible playbooks** — `infra/ansible/{deploy,backup,recover,bootstrap,hardening}.yml` кодифицируют ручной workflow; в `--check` deploy работает как безопасный preflight verify текущего состояния без rollout
- [x] **Idempotent regular deploy** — rsync релиз-бандла, `docker compose build/up -d` без `down`, polling Docker healthcheck до `healthy`, smoke check по `bridge.db`
- [x] **Backup + recover** — `tar.gz` envs+state на локальную машину, развёртывание на новый VM с защитой от перезаписи живого prod
- [x] **Bootstrap + hardening для новых VM** — `deploy` user, Docker, sshd template, UFW, fail2ban, unattended-upgrades; не применяются к существующему prod
- [x] **Inventory с реальным IP — в `.gitignore`** — production identifiers никогда не коммитятся

Детали: [infra/ansible/README.md](../infra/ansible/README.md)

---

## Phase 7: MAX account migration recovery ✅

**Цель:** если старый телефон/MAX account потерян, сохранить Telegram continuity и иметь полный registry для ручного восстановления доступа к MAX чатам.

- [x] **Account generations** — `max_account_generations` хранит `max_user_id`, masked phone, session fingerprint hash, статус `active|retired|lost`, first/last seen
- [x] **Chat recovery registry** — `chat_recovery_registry` хранит stable key `tg_topic:<topic_id>`, old/current `max_chat_id`, chat kind, mode, access, invite link, owner/admin contacts, DM partner metadata, participant count, manual note, recovery status
- [x] **DM contact recovery registry** — `dm_contact_recovery_registry` хранит личных собеседников только из реальных MAX dialogs/DM topics, не всю address book и не group writers из `known_users`
- [x] **Snapshot freshness** — у каждой registry row есть `last_scan_at`; `/recovery report` показывает возраст последнего snapshot
- [x] **Append-only recovery events** — scan/set/remap/account-change пишутся в `chat_recovery_events`, без message text/raw payload
- [x] **MAX snapshot collector** — `MaxAdapter.collect_recovery_snapshot()` собирает typed chat/channel/dialog snapshots, enrich через `get_chat()`, плюс DM contact snapshot из dialogs only
- [x] **Owner-only recovery commands** — `/recovery scan`, `/recovery report`, `/recovery export`, `/recovery set`, `/recovery remap`
- [x] **Hybrid snapshot triggers** — safe scan после MAX connect/reconnect, weekly safety-net и event-driven scans на `new_binding`, `title_changed`, MAX `CONTROL`
- [x] **Async debounced scheduler** — event-driven scans выполняются background task'ом, схлопывают повторные события и не задерживают forwarding/topic creation
- [x] **Quiet recovery status summary** — обычные auto-scan дельты попадают в 4-часовой `/status`, отдельный alert остаётся для migration-required; invite links, notes, phones, message text и raw payload не попадают в статус/уведомления/логи
- [x] **Remap safety** — Telegram topic сохраняется; stale reply mapping после remap не отправляет `reply_to` на старый `max_chat_id`
- [x] **Privacy tests** — report/logs не раскрывают invite links, notes, phone numbers, message text или raw MAX payloads

Детали: [docs/runbooks/operations.md#max-account-recovery-registry](runbooks/operations.md#max-account-recovery-registry)

---

## Phase 8: MAX adapter architecture ✅

**Цель:** сделать MAX adapter заменяемым по backend и явным по внутренним зависимостям.

- [x] **Backend boundary** — `pymax` isolated in `src/adapters/max/backends/pymax/`; replacing the library means implementing another `MaxBackend`.
- [x] **Facade + operation services** — public `MaxAdapter` wires lifecycle/events/send/media/recovery/resolve/voice services without mixin inheritance.
- [x] **Explicit service dependencies** — service registry / service `__getattr__` removed; services receive explicit deps/state slices/callables.
- [x] **Test harness over private adapter hooks** — adapter tests no longer subclass real `MaxAdapter` for private overrides; fake service deps cover media/send/event paths.

---

## Phase 9: Bridge core coordinator split ✅

**Цель:** оставить `BridgeCore` runtime coordinator-ом, а смысловые операции держать в leaf modules.

- [x] **Status/report rendering split** — `/status`, `/chats`, `/help` живут в `bridge/status.py`.
- [x] **Recovery scheduler split** — debounce/cooldown scan state и notification digest живут в `bridge/recovery/scheduler.py`.
- [x] **Media retry ownership** — enqueue/find/process/worker logic живёт в `bridge/media_retry.py`.
- [x] **Command dispatcher** — Telegram command registration живёт в `bridge/commands/dispatcher.py`.
- [x] **Scoped quality gates** — CI проверяет bridge boundary через scoped ruff и strict-ish mypy для touched bridge modules.

---

## Phase 10: Full MAX client ports ✅

**Дата:** 2026-05-23  
**Цель:** полностью отделить MAX operation services от формы `pymax`-клиента, оставив библиотечную и object-shape зависимость только внутри backend boundary.

- [x] **Typed MAX client port + DTO** — `src/adapters/max/ports.py` задаёт внутренний порт клиента и DTO для messages, attachments, users, chats, dialogs, outbound send result и raw interceptor result.
- [x] **PymaxClientAdapter** — `src/adapters/max/backends/pymax/client_adapter.py` тонкий facade над PyMax 2 backend modules (`client_factory`, `transport`, `events`, `raw_gateway`, `models`, `media`) и конвертирует pymax objects в наши DTO.
- [x] **Services больше не знают форму pymax-клиента** — `send`, `resolve`, `recovery`, `media`, `raw/history`, `voice_recovery`, `events` и `lifecycle` работают через typed port methods/snapshots вместо `_send_and_wait`, `_handle_message_notifications`, `fetch_history`, `get_file_by_id`, `contacts/dialogs/chats/channels/_users/me`.
- [x] **Compatibility preserved** — public `MaxAdapter`, `BridgeCore`, compatibility imports и `client_factory.create_socket_client()` сохранены.
- [x] **Regression coverage** — добавлен `tests/test_max_service_ports.py`, расширен `tests/test_bridge_contracts.py`, существующий `tests/test_max_adapter.py` покрывает raw voice recovery, channel unwrap/dedupe, media retry, outbound echo ack и lifecycle edge cases.
- [x] **Verified and deployed** — локальный gate зелёный (`183 passed`), production deploy и smoke check прошли успешно; изменение merged to `master` и deployed в commit `ec03f27` (`Decouple MAX services from pymax client shape`).

---

## Известные ограничения (won't fix)

| Ограничение | Причина |
|-------------|---------|
| Сообщения за время downtime теряются | pymax не имеет history replay API |
| Новый телефон/MAX account не восстанавливает закрытые чаты автоматически | MAX требует новый invite/link/admin approval; bridge хранит registry и remap-команды, но не делает auto-join |
| Команды бота доступны только владельцу | Намеренно — личный инструмент |
| Нет поддержки реакций, опросов, пинов | Out of scope |
| Нет полной истории при старте | Слишком сложно, не нужно |

---

## Технический долг

| Задача | Приоритет |
|--------|-----------|
| Per-chat управление из TG | Medium |
| Более удобный guided UI поверх `/recovery report` для массового remap | Medium |
