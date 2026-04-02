# Roadmap — MAX→Telegram Bridge

## Статус: MVP запущен ✅

Дата запуска MVP: 2026-04-02

---

## Что сделано (Phase 0 + Phase 1)

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
- [x] Media forwarding: фото, видео, аудио, документы
- [x] Own-message filtering (не форвардим свои сообщения)
- [x] Fallback title rename: "Чат 123456789" → "Имя контакта"
- [x] Запуск локально (nohup + bridge.log)

### Критические баги устранены ✅
- [x] **pymax OOM**: `reconnect=True` → exponential growth chats/dialogs → fix: outer reconnect loop
- [x] **SSL storm**: `send_fake_telemetry=True` (default) → TLSV1_ALERT_RECORD_OVERFLOW → fix: `send_fake_telemetry=False`
- [x] **sender_name=None**: `message.sender` это `int`, не User-объект → fix: `get_cached_user(int(sender_id))`
- [x] **send_message fails on reconnect**: "Socket is not connected" → fix: retry 3×5s
- [x] **5 startup notifications**: `on_start` fires on every reconnect → fix: `_started_once` flag

---

## Phase 2: Stabilization (следующие 1–2 недели)

**Цель:** Сервис работает 7 дней без ручного вмешательства.

- [ ] **Telegram retry/backoff** — сейчас нет retry для TG API ошибок (rate limit, network)
- [ ] **Missed messages при downtime** — pymax не replay историю; нужно хотя бы уведомление о gap
- [ ] **`/status` команда** — показать: uptime, количество чатов, последнее сообщение
- [ ] **Уведомление при потере MAX сессии** — сейчас молча reconnect; нужен alert если >3 ретраев
- [ ] **sender_name из live API** — `get_cached_user` может не знать новых юзеров; live fallback реализован, нужно протестировать
- [ ] **Тест дедупликации при reconnect** — убедиться что дубли не идут в TG после reconnect
- [ ] **Ручное тестирование 7 дней** — валидация стабильности

---

## Phase 3: Cloud Migration (после стабилизации)

**Цель:** Переехать с Mac на Fly.io, чтобы bridge работал 24/7.

- [ ] Dockerfile протестировать локально
- [ ] Fly.io: создать app, volume (1GB), secrets
- [ ] Перенести MAX сессию (`data/max_bridge_session`) на volume
- [ ] Проверить что после смены IP MAX не требует re-auth
- [ ] Написать `/reauth` команду (если MAX потребует re-login с нового IP)
- [ ] `fly logs -f` → убедиться стабильно

Детали деплоя: `docs/runbooks/deployment.md`

---

## Phase 4: Hardening (по желанию)

**Цель:** Удобство управления и устойчивость к edge cases.

- [ ] **Per-chat управление из Telegram** — `/mode -70000000000001 readonly`
- [ ] **Длинные сообщения** — разбивать >4096 символов на части
- [ ] **Файлы >50MB** — уведомление с именем файла вместо попытки отправить
- [ ] **Нативные voice note bubbles** — сейчас audio пересылается как обычное audio-вложение; можно улучшить UX
- [ ] **`/chats` команда** — список активных чатов с топиками
- [ ] **Unit тесты** — core логика (routing, dedup, name resolution)
- [ ] **Форматирование** — если MAX поддерживает markdown

---

## Известные ограничения (won't fix)

| Ограничение | Причина |
|-------------|---------|
| Сообщения за время downtime теряются | pymax не имеет history replay API |
| Команды бота доступны только владельцу | `/status` и `/reauth` ограничены owner |
| Нет поддержки реакций, опросов, пинов | Out of scope MVP |
| Нет полной истории при старте | Слишком сложно, не нужно |

---

## Технический долг

| Задача | Приоритет |
|--------|-----------|
| Retry + backoff для TG API | High |
| Alert при длительной потере MAX | High |
| `/status` команда | Medium |
| Unit тесты bridge/core.py | Medium |
| Обработка файлов >50MB | Low |
