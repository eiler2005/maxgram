# Roadmap — Maxgram

## Статус: v1.1.0 в production ✅

Дата запуска MVP: 2026-04-02  
Дата v1.1.0: 2026-04-03

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
- [x] **SSL storm**: `send_fake_telemetry=True` (default) → TLSV1_ALERT_RECORD_OVERFLOW → fix: `send_fake_telemetry=False`
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
- [x] **32 regression-теста** — все проходят; описание: `docs/tests.md`

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

## Phase 4: Hardening (следующая итерация)

**Цель:** удобство управления и устойчивость к edge cases.

- [ ] **Per-chat управление из Telegram** — `/mode -70000000000001 readonly`
- [ ] **`/chats` команда** — список активных чатов с топиками и числом сообщений
- [ ] **Длинные сообщения** — разбивать >4096 символов на части
- [ ] **Нативные voice note bubbles** — голосовые как `send_voice` вместо обычного аудио
- [ ] **Missed messages gap** — уведомление о пропущенных сообщениях за время downtime
- [x] **Унификация типов MAX-вложений** — единый normalizer для alias-типов (`IMAGE`, `VOICE`, `DOCUMENT`, `DOC` и т.п.) используется и в верхнем dispatch, и в download pipeline
- [ ] **Пост-валидация скачанных вложений** — после download проверять `Content-Type` и сигнатуру файла, чтобы HTML/player fallback не уходил в Telegram как медиафайл
- [ ] **Расширение тест-сьюта** — retry-логика, stat counters, `_build_status_message`

---

## Известные ограничения (won't fix)

| Ограничение | Причина |
|-------------|---------|
| Сообщения за время downtime теряются | pymax не имеет history replay API |
| Команды бота доступны только владельцу | Намеренно — личный инструмент |
| Нет поддержки реакций, опросов, пинов | Out of scope |
| Нет полной истории при старте | Слишком сложно, не нужно |

---

## Технический долг

| Задача | Приоритет |
|--------|-----------|
| Тесты для retry-логики `_tg_retry` | Medium |
| Тесты для `_build_status_message` | Medium |
| Валидация скачанного файла (`Content-Type` / magic bytes) перед отправкой в Telegram | Medium |
| Per-chat управление из TG | Medium |
| `/chats` команда | Low |
| Обработка файлов >50MB (явное уведомление) | Low |
