# Maxgram — Полная документация

Личный bridge-сервис: сообщения из российского мессенджера **MAX** зеркалируются в **Telegram Forum Supergroup** как отдельные топики. Ответ из Telegram уходит обратно в MAX.

**Цель:** перестать устанавливать MAX, читать и отвечать на сообщения прямо из Telegram.

---

## Как это работает

```
MAX (личный аккаунт)        Telegram Forum Supergroup
┌──────────────────┐         ┌──────────────────────────┐
│ DM: Контакт      │────────►│  📁 Личный диалог         │
│ Группа: Школа    │────────►│  📁 Школьный чат          │
│ Группа: Кружок   │────────►│  📁 Группа кружка         │
│ ...              │         │  📁 ...                   │
└──────────────────┘         └──────────────────────────┘
        ▲                               │
        └───────── Reply в топике ───────┘
```

Каждый MAX-чат (DM или группа) = отдельный топик. Топик создаётся автоматически при первом сообщении. Reply в топике = ответ в MAX.

---

## Основные возможности

- **Автоматическое зеркалирование** — все чаты MAX появляются как топики без ручной настройки
- **Двусторонняя связь** — reply из Telegram уходит в MAX, включая reply на конкретное сообщение
- **Имя отправителя в группах** — `[Имя Фамилия] текст сообщения`
- **Собственные сообщения** — если написал в MAX напрямую, появится в Telegram с пометкой `[Вы]`
- **Медиа в обе стороны** — фото, видео, аудио, голосовые, документы (MAX→TG и TG→MAX)
- **Имена контактов** — DM топики именуются по имени собеседника из профиля MAX
- **Режимы per-chat** — `active` / `readonly` / `disabled`
- **Дедупликация** — сообщения не дублируются при переподключении
- **Устойчивый reconnect** — без OOM и SSL-ошибок
- **Команда `/status`** — аптайм, статистика сообщений, топ активных чатов; работает в группе и в личном чате с ботом
- **Команда `/chats`** — список подключённых чатов с topic id, режимом и счётчиками сообщений
- **Автоматический статус-отчёт** — каждые 4 часа бот присылает сводку без команды
- **Watchdog MAX** — уведомление, если MAX недоступен более 60 секунд
- **Gap-уведомление после reconnect** — после восстановления бот предупреждает о возможном пропуске сообщений за время простоя
- **Retry Telegram API** — 3 попытки с экспоненциальным backoff, поддержка `Retry-After`
- **Startup self-check** — после старта в production бот пишет результат встроенного `pytest`-прогона
- **Устойчивое скачивание MAX-видео** — bridge предпочитает реальные `MP4_*` потоки вместо `EXTERNAL` HTML-плеера и подбирает `User-Agent` по `srcAg`
- **Post-validation загрузок** — после скачивания проверяются `Content-Type` и сигнатура файла, HTML/player fallback не уходит как медиа
- **Реальная пересылка MAX channel/forward** — `CHANNEL`/forward-обёртки разворачиваются до исходного текста и медиа вместо служебной заглушки
- **Диагностика неизвестных MAX-сообщений** — новый формат MAX уходит в Telegram как подробный блок с `type`, `link_*`, счётчиками и списком полей
- **Нативные voice bubbles** — MAX `VOICE` пересылается в Telegram через `send_voice`

---

## Архитектура

```
MAX WebSocket ──► MAX Adapter ──► Bridge Core ──► TG Adapter ──► Telegram
                  (pymax)         (роутинг)        (aiogram)      (Topics)
                                      │
                                  SQLite DB
                              (bindings, dedup,
                               delivery log)
```

Один Python async процесс. Никаких внешних зависимостей. SQLite как единственное хранилище состояния.

Подробнее: [docs/architecture.md](docs/architecture.md)

---

## Технологии

| Компонент | Технология |
|-----------|-----------|
| MAX userbot | [`pymax`](https://github.com/MaxApiTeam/PyMax) / `maxapi-python` |
| Telegram бот | `aiogram` 3.x |
| База данных | SQLite + `aiosqlite` |
| Конфиг | YAML + `python-dotenv` |
| Runtime | Python 3.13+, `asyncio` |
| Деплой | Docker Compose / Hetzner Cloud |

---

## Production

Bridge работает в production на **Hetzner Cloud**.

- Runtime: Docker Compose (non-root контейнер, `cap_drop: ALL`, `restart: always`)
- State: SQLite + MAX сессия в bind-mounted `data/`
- Доступ: только SSH-ключ, ограничен по IP через UFW
- Security: `fail2ban`, `unattended-upgrades`, публичных HTTP-портов нет
- Бот после старта присылает startup-уведомление с runtime/host и итогом встроенного `pytest`

---

## Быстрый старт

**Требования:** Python 3.13+, Telegram бот ([@BotFather](https://t.me/BotFather)), форум-супергруппа с Topics, аккаунт MAX.

```bash
# 1. Зависимости
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Секреты
cp .env.example .env
# Заполни: TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE

# 3. (опционально) Локальные привязки чатов
cp config.local.yaml.example config.local.yaml

# 4. Первый запуск — авторизация MAX по SMS
python src/main.py

# 5. Фоновый запуск
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &
```

Через Docker:
```bash
docker-compose -f deploy/docker-compose.yml up -d
```

Production (Hetzner):
```bash
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
```

---

## Переменные окружения

| Переменная | Описание | Где взять |
|-----------|----------|-----------|
| `TG_BOT_TOKEN` | Токен Telegram бота | @BotFather |
| `TG_OWNER_ID` | Твой Telegram user_id | @userinfobot |
| `TG_FORUM_GROUP_ID` | ID форум-супергруппы | Из URL или @userinfobot |
| `MAX_PHONE` | Номер телефона MAX | Твой номер `+79...` |

---

## Конфигурация

### config.yaml (в git)

```yaml
bridge:
  forward_all: true      # авто-создание топиков для новых чатов
  default_mode: "active"

content:
  forward_photos: true
  forward_documents: true
  forward_voice: false
```

### config.local.yaml (не в git)

```yaml
chats:
  - max_chat_id: "-70000000000001"
    title: "Школьный чат"
    mode: "active"
```

---

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Regression-набор покрывает: routing, дедупликацию, системные MAX-события, MAX channel/forward unwrap, media forwarding, reply routing, Telegram topic filtering.

Смоук-проверка:
```bash
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

---

## Структура проекта

```
maxgram/
├── src/
│   ├── main.py                ← точка входа, asyncio.TaskGroup
│   ├── adapters/
│   │   ├── max_adapter.py     ← MAX userbot: connect, recv, send, reconnect
│   │   └── tg_adapter.py     ← Telegram бот: topics, send, receive
│   ├── bridge/
│   │   └── core.py           ← вся бизнес-логика роутинга
│   ├── config/loader.py       ← YAML + .env
│   └── db/
│       ├── models.py          ← SQLite схема (3 таблицы)
│       └── repository.py     ← data access layer
│
├── docs/
│   ├── architecture.md        ← диаграммы и потоки данных
│   ├── roadmap.md            ← статус и планы
│   ├── decisions/            ← ADR-001…004
│   └── runbooks/             ← операции, деплой, Hetzner
│
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-compose.prod.yml
│
├── tests/                    ← pytest regression suite
├── scripts/smoke_check.py    ← ручная проверка по SQLite
│
├── config.yaml               ← базовая конфигурация (в git)
├── config.local.yaml.example ← шаблон локальных chat bindings
└── .env.example              ← шаблон секретов
```

---

## Daily Ops Cheat Sheet

```bash
# Подключиться к серверу
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge

# Статус и логи
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m

# Смоук-проверка
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15

# Обновить и перезапустить
git pull
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
```

---

## Документация

| Файл | Содержание |
|------|-----------|
| [docs/architecture.md](docs/architecture.md) | Архитектура, потоки данных, схема DB |
| [docs/roadmap.md](docs/roadmap.md) | Статус фаз и планы |
| [docs/decisions/](docs/decisions/) | ADR-001…004: ключевые решения |
| [docs/runbooks/operations.md](docs/runbooks/operations.md) | Операционные процедуры |
| [docs/runbooks/deployment.md](docs/runbooks/deployment.md) | Деплой: локально, Docker, Hetzner, Fly.io |
| [docs/runbooks/hetzner-production.md](docs/runbooks/hetzner-production.md) | Безопасный production-деплой |
| [docs/tests.md](docs/tests.md) | Описание regression-набора |
| [PROJECT.md](PROJECT.md) | Полная техническая документация |
| [CHANGELOG.md](CHANGELOG.md) | История изменений |

---

## Статус

| Фаза | Статус | Описание |
|------|--------|----------|
| Phase 0: Spike | ✅ | pymax работает, Telegram Topics работают |
| Phase 1: MVP | ✅ | Bridge запущен, все основные функции |
| Phase 2: Stabilization | ✅ | Retry TG API, /status, watchdog, медиа TG→MAX |
| Phase 3: Cloud | ✅ | Hetzner production, Docker Compose, hardening |
| Phase 4: Hardening | ⏳ | Per-chat управление из TG, больше тестов |

Подробный роадмап: [docs/roadmap.md](docs/roadmap.md)

---

## Критические особенности pymax

> Знания получены через debugging production — не забывать.

| Факт | Правильно | Неправильно |
|------|-----------|------------|
| `message.sender` | `int` (user_id) | ~~User-объект~~ |
| Имя пользователя | `user.names[0].first_name` | ~~`user.first_name`~~ |
| Кеш пользователей | `client.get_cached_user(int(id))` | — |
| Reconnect | `reconnect=False` + outer loop | ~~`reconnect=True`~~ (OOM) |
| Telemetry | `send_fake_telemetry=False` | ~~default True~~ (SSL storm) |
| Собственный ID | `client.me` (атрибут) | ~~`client.get_me()`~~ |

---

## Известные ограничения

- Сообщения за время downtime **теряются** — pymax не имеет history replay
- Неофициальный userbot — возможное нарушение ToS MAX
- Команды бота (`/status`, `/chats`, `/reauth`) ограничены владельцем

---

## License

MIT — see [LICENSE](LICENSE)
