# Runbook: Деплой

## Вариант 1: Локально (Mac/Linux) — текущий

```bash
cd /path/to/maxtg_bridge
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Создать .env из шаблона
cp .env.example .env
# Заполнить TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE

# При необходимости локально описать конкретные MAX-чаты
cp config.local.yaml.example config.local.yaml

# Важно: .env, config.local.yaml и data/ не должны попадать в git
# и исключены из Docker build context через .dockerignore

# Авторизоваться в MAX (один раз, интерактивно)
python src/main.py
# Введи SMS-код при запросе, сессия сохранится в data/max_bridge_session

# Запустить в фоне
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &
```

Для локальной разработки и регрессионных проверок:
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

## Вариант 2: Docker (локально)

```bash
# Создать .env (см. выше)
docker-compose -f deploy/docker-compose.yml up -d

# Логи
docker-compose -f deploy/docker-compose.yml logs -f

# Остановить
docker-compose -f deploy/docker-compose.yml down
```

## Вариант 2b: Docker Compose (production / Hetzner)

Для production-окружения использовать отдельный compose-файл:

```bash
cp deploy/hetzner.env.example .env.host
# выставить APP_UID / APP_GID под пользователя на сервере

docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs -f
```

Особенности production-compose:

- нет проброса портов
- конфиги монтируются read-only
- контейнер запускается non-root
- включены `no-new-privileges` и `cap_drop: [ALL]`

После ручной проверки MAX <-> Telegram можно быстро посмотреть последние служебные метаданные:

```bash
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Подробный secure-runbook для Hetzner: `docs/runbooks/hetzner-production.md`

## Вариант 3: Fly.io (рекомендуется для 24/7)

> ⚠️ Перед переносом на Fly.io убедиться что есть `/reauth` команда —
> MAX может потребовать re-auth при смене IP.

### Первоначальный деплой

```bash
# Установить flyctl
brew install flyctl

# Авторизоваться
fly auth login

# Создать приложение
fly apps create maxtg-bridge

# Создать persistent volume для данных (сессия, БД)
fly volumes create bridge_data --size 1 --region ams

# Установить секреты
fly secrets set \
  TG_BOT_TOKEN=xxx \
  TG_OWNER_ID=xxx \
  TG_FORUM_GROUP_ID=xxx \
  MAX_PHONE=xxx

# Деплой
fly deploy -c deploy/fly.toml
```

### Перенести MAX сессию

```bash
# После первого деплоя перенести сессию
fly ssh console
cp /tmp/max_bridge_session /app/data/max_bridge_session

# Или через sftp
fly sftp get data/max_bridge_session ./max_bridge_session_backup
fly sftp put ./max_bridge_session_backup /app/data/max_bridge_session
```

### Мониторинг

```bash
fly logs -f
fly status
fly ssh console
```

### Обновление кода

```bash
fly deploy -c deploy/fly.toml
```

## Переменные окружения

| Переменная | Описание | Где взять |
|-----------|----------|-----------|
| `TG_BOT_TOKEN` | Токен Telegram бота | @BotFather в Telegram |
| `TG_OWNER_ID` | Твой Telegram user_id | @userinfobot в Telegram |
| `TG_FORUM_GROUP_ID` | ID форум-супергруппы | Из URL или @userinfobot |
| `MAX_PHONE` | Номер телефона MAX | Твой номер в формате +79xxxxxxxxx |

## Структура данных (не в git)

```
data/
├── bridge.db              # SQLite (состояние bridge)
├── max_bridge_session     # Сессия MAX (не терять!)
└── tmp/                   # Временные медиафайлы (auto-cleanup)
```

**Бэкап `data/`** перед любыми изменениями инфраструктуры.

## Локальные приватные файлы

Следующие файлы/директории должны оставаться только локально:

- `.env`
- `config.local.yaml`
- `data/`
- `.claude/settings.local.json`

Они исключены из git через `.gitignore`, а Docker build context очищается через `.dockerignore`.
