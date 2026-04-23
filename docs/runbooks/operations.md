# Runbook: Операционные процедуры

## Production: Hetzner quick checklist

Текущий production-сценарий:

- сервер на Hetzner Cloud
- bridge работает в Docker Compose
- SSH доступ только по ключу
- SSH разрешён только с твоего текущего домашнего IP
- на VM включены `UFW`, `fail2ban`, `unattended-upgrades`

### Как заходить на сервер

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
```

После входа:

```bash
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=50
```

### Как обновлять bridge

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
git pull
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=80
```

### Как восстановиться, если домашний IP сменился

Сейчас SSH разрешён только с твоего текущего IP.

Нужно обновить доступ в двух местах:

1. В панели Hetzner Cloud Firewall:
   - не должно быть широких правил `Any IPv4` / `Any IPv6`
   - должно быть только `22/tcp` с нового IP в формате `x.x.x.x/32`
2. На самом сервере в `UFW`:

```bash
# зайти через Hetzner Console / LISH или временно открыть доступ в Cloud Firewall
sudo ufw delete allow from <OLD_IP> to any port 22 proto tcp
sudo ufw allow from <NEW_IP> to any port 22 proto tcp comment 'SSH from home IP'
sudo ufw status numbered
```

### Как проверить, что bridge жив

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker inspect --format '{{json .State.Health}}' deploy-bridge-1 | jq
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Ожидаемое состояние:

- контейнер `bridge` в статусе `Up`
- Docker healthcheck в статусе `healthy`
- в логах есть `MAX connected`
- в свежем startup-логе есть `Running startup tests` и затем `Startup tests passed: ...`
- нет непрерывных ошибок `TelegramConflictError`
- в `message_map` и `delivery_log` появляются свежие записи
- если видео из MAX внезапно пришло в Telegram как `.html`, сначала проверить `docker logs` на `VIDEO_PLAY`: bridge должен выбирать `MP4_*` URL, а не `EXTERNAL`, и signed CDN URL должен открываться с `User-Agent`, соответствующим `srcAg`

Куда идут служебные сообщения:

- основной канал: личный чат владельца с ботом (`TG_OWNER_ID`)
- дополнительный канал: forum topic внутри `TG_FORUM_GROUP_ID`, только если задан `telegram.ops_topic_id`
- если `ops_topic_id` не задан, все startup/status/error alert сообщения идут только в owner DM

### Что важно помнить

- Доступ к серверу теперь только с твоего текущего IP.
- Если домашний IP изменится, нужно будет обновить правило и в Hetzner Firewall, и в `UFW`.
- Cloud Firewall я не менял через API, потому что для этого нужен отдельный Hetzner API token.
- Поэтому в панели Hetzner отдельно проверь, что там нет широких правил `Any IPv4` / `Any IPv6`, а только `22/tcp` с твоего IP.
- В production у контейнера `restart: always`, поэтому после обычного `reboot` VM bridge должен подняться сам.
- Если сделать `docker compose down`, контейнер будет удалён; после reboot VM он уже не восстановится сам, пока не выполнить `docker compose ... up -d`.
- Теперь PID1 внутри контейнера — supervisor. Даже если MAX/TG интеграция деградирует, контейнер должен оставаться `Up`, а проблема должна отражаться в `data/health_state.json` и в ops-алертах.

## Запуск / Остановка

```bash
# Запуск (фоновый, с логом)
cd /path/to/maxtg_bridge
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &

# Проверить что работает
ps aux | grep 'python -m src.main' | grep -v grep
tail -20 data/bridge.log

# Остановить
kill $(ps aux | grep 'python -m src.main' | grep -v grep | awk '{print $2}')

# Перезапуск
kill $(ps aux | grep 'python -m src.main' | grep -v grep | awk '{print $2}')
sleep 2
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &
```

**ВАЖНО:** Никогда не запускать два экземпляра одновременно — два userbot = проблемы с MAX.

## Проверка здоровья

```bash
# Последние события
tail -50 data/bridge.log

# Статистика доставки
.venv/bin/python -c "
import asyncio, aiosqlite

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    db.row_factory = aiosqlite.Row
    print('=== Bindings ===')
    async with db.execute('SELECT * FROM chat_bindings') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    print('=== Recent deliveries ===')
    async with db.execute('SELECT * FROM delivery_log ORDER BY created_at DESC LIMIT 10') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    await db.close()

asyncio.run(main())
"
```

Для production на Hetzner удобнее использовать:

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Дополнительные runtime-артефакты:

- `data/health_state.json` — текущий persisted health snapshot supervisor-а
- `data/health_events.jsonl` — история переходов `healthy/degraded/recovering/recovered`
- `data/alert_outbox.jsonl` — системные алерты, которые не удалось отправить в Telegram сразу
- `data/health_heartbeat.json` — heartbeat для Docker healthcheck

Быстрая диагностика:

```bash
cd /opt/maxtg-bridge
jq . data/health_state.json
tail -50 data/health_events.jsonl
wc -l data/alert_outbox.jsonl
jq . data/health_heartbeat.json
```

Как интерпретировать:

- контейнер `Up` + healthcheck `healthy` + `health_state.json` с `overall_status=degraded` означает: supervisor жив, но одна из подсистем сломана
- если `alert_outbox.jsonl` не пустой, Telegram ops-уведомления не ушли сразу и ждут автоматической досылки
- если проблема в MAX session, `/status` и `health_state.json` должны явно показывать `requires_reauth` / подсказку про SMS reauth
- по умолчанию outbox относится к owner DM; forum-topic fanout участвует только если настроен `ops_topic_id`

## Режимы логирования

Поддерживаются env-переключатели:

```bash
LOG_LEVEL=INFO|DEBUG
LOG_FORMAT=text|json|mixed
LOG_PREVIEW_CHARS=120
LOG_LIBRARIES_DEBUG=0|1
```

Рекомендуемый production-режим:

```bash
LOG_LEVEL=INFO
LOG_FORMAT=mixed
LOG_LIBRARIES_DEBUG=0
```

Для детальной диагностики конкретного кейса:

```bash
LOG_LEVEL=DEBUG
LOG_FORMAT=mixed
LOG_PREVIEW_CHARS=120
```

Что важно:

- на `INFO` логируются route/outcome/meta без полного текста сообщений
- на `DEBUG` появляются safe preview текста
- `LOG_LIBRARIES_DEBUG=1` поднимает `pymax`/`aiogram`, использовать только временно

## Как искать трассу сообщения

Основные поля в логах:

- `event=...`
- `flow_id=mx:<chat_id>:<msg_id>` для MAX -> Telegram
- `flow_id=tg:<topic_id>:<tg_msg_id>` для Telegram -> MAX

Быстрые команды:

```bash
# все шаги по конкретному MAX-сообщению
rg 'flow_id=mx:-70000000000003:4242' data/bridge.log

# все шаги по конкретному Telegram-сообщению
rg 'flow_id=tg:99:777' data/bridge.log

# только завершения маршрута
rg 'event=bridge\.(inbound|outbound)\.forward_finished' data/bridge.log

# только retry/fail отправки в Telegram
rg 'event=tg\.outbound\.(retry|failed|sent)' data/bridge.log

# retry/fail отправки из Telegram в MAX
rg 'event=max\.outbound\.(retry|failed|sent)' data/bridge.log

# последние неуспешные TG -> MAX доставки из SQLite
sqlite3 -header -column data/bridge.db \
  "SELECT max_msg_id, max_chat_id, error, attempts, datetime(created_at, 'unixepoch', 'localtime') AS created_local \
   FROM delivery_log \
   WHERE direction='outbound' AND status='failed' \
   ORDER BY created_at DESC LIMIT 20"
```

Полезные `event`-группы:

- `max.inbound.*` — что пришло из MAX и как нормализовали
- `bridge.inbound.*` — routing/dedup/topic resolution для MAX -> TG
- `tg.outbound.*` — отправка в Telegram, retry и fail
- `tg.inbound.*` — что пришло из Telegram и скачивание медиа
- `bridge.outbound.*` — routing/reply resolution и доставка TG -> MAX

## Reason codes

Нормальные/ожидаемые:

- `duplicate`
- `empty_event`
- `readonly`
- `disabled`

Требуют внимания:

- `no_topic`
- `too_large`
- `tg_send_failed`
- `max_send_failed`
- `download_rejected`
- `download_failed`
- `ack_timeout`

## Базовая живая smoke-проверка на тестовых чатах

Этот сценарий нужен после деплоя или после правок в routing.

### Подготовка

- выдели 1 тестовый MAX-чат или DM
- дождись, чтобы для него уже существовал Telegram topic
- не используй прод-критичный чат для первой проверки

### Проверка 1: MAX -> Telegram

1. Отправь в тестовый MAX-чат короткий текст, например: `SMOKE MAX -> TG`.
2. Убедись, что он появился в соответствующем Telegram topic.
3. Если это группа, проверь что в Telegram есть префикс отправителя `[Имя]`.

### Проверка 2: Telegram -> MAX

1. В том же Telegram topic отправь сообщение, например: `SMOKE TG -> MAX`.
2. Убедись, что оно появилось в MAX.
3. Проверь, что в MAX сообщение пришло с префиксом автора Telegram:

```text
[Имя Фамилия]
SMOKE TG -> MAX
```

### Проверка 3: метаданные bridge

Сразу после ручной проверки выполни:

```bash
cd /opt/maxtg-bridge
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Ожидаемый результат:

- есть свежая запись `direction='inbound'` для MAX -> Telegram
- есть свежая запись `direction='outbound'` для Telegram -> MAX
- нет свежих `failed` записей по тестовому окну

### Когда применять

- после деплоя на новый сервер
- после изменений в `src/adapters/tg_adapter.py`
- после изменений в `src/adapters/max_adapter.py`
- после изменений в `src/bridge/core.py`

## Добавить новый чат

1. Найти `max_chat_id` из лога: `rg 'event=bridge.inbound.topic_resolved .*outcome=created' data/bridge.log`
2. Добавить в `config.local.yaml` раздел `chats:` (опционально — если нужен специфичный режим)
3. При `forward_all: true` чат добавится автоматически при первом сообщении

## Изменить режим чата

**Через DB напрямую:**
```bash
.venv/bin/python -c "
import asyncio, aiosqlite

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    await db.execute(\"UPDATE chat_bindings SET mode=? WHERE max_chat_id=?\",
                     ('readonly', '-70000000000001'))
    await db.commit()
    await db.close()
    print('done')

asyncio.run(main())
"
```

Режимы: `active` | `readonly` | `disabled`

## Переименовать топик вручную

Топик переименовывается автоматически когда:
1. Приходит новое сообщение и текущее название — fallback ("Чат XXXXXX")
2. Bridge корректно определяет имя из профиля MAX

Если нужно вручную — переименовать прямо в Telegram.

## Проблема: bridge не форвардит сообщения

1. Проверить что bridge запущен: `ps aux | grep 'python -m src.main'`
2. Проверить лог: `tail -100 data/bridge.log`
3. Проверить persisted health snapshot: `jq . data/health_state.json`
4. Проверить outbox алертов: `wc -l data/alert_outbox.jsonl`
5. Проверить delivery_log в DB (см. выше)
6. Если `SSL: TLSV1_ALERT_RECORD_OVERFLOW` → убедиться что используется `send_fake_telemetry=False`
7. Если `dialogs=NNN` растёт (>12 при каждом reconnect) → убедиться что `reconnect=False`
8. Если после reboot VM кажется, что bridge "не поднялся" → проверить `docker compose ps`, Docker healthcheck и startup-лог; в production сначала должны появиться `MAX connected`, потом `Running startup tests`, потом `Startup tests passed: ...`

## Проблема: "❌ Не удалось отправить сообщение в MAX"

Теперь bridge сначала делает до 3 попыток отправки при временных транспортных ошибках MAX:

- `Socket is not connected`
- `Must be ONLINE session`
- timeout / broken pipe / connection reset

Если после retry сообщение всё равно не ушло:

- в Telegram появится `❌ Не удалось отправить сообщение в MAX`
- в `delivery_log` появится запись `direction='outbound'`, `status='failed'`
- в `error` будет последняя причина, а в `attempts` — число попыток

Быстрая проверка:

```bash
sqlite3 -header -column data/bridge.db \
  "SELECT max_msg_id, error, attempts, datetime(created_at, 'unixepoch', 'localtime') AS created_local \
   FROM delivery_log \
   WHERE direction='outbound' AND status='failed' \
   ORDER BY created_at DESC LIMIT 10"
```

И в логах:

```bash
rg 'event=max\.outbound\.(retry|failed|sent)' data/bridge.log
```

## Проблема: сообщение не появилось (потеряно)

pymax **не воспроизводит историю** после reconnect. Сообщения отправленные во время downtime теряются.
Это известное ограничение — не баг.

## Обновление зависимостей

```bash
source .venv/bin/activate
pip install --upgrade maxapi-python aiogram aiosqlite
# Проверить что всё работает
python -m src.main
```

## Очистка старых данных вручную

```bash
.venv/bin/python -c "
import asyncio, aiosqlite, time

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    cutoff = int(time.time()) - 30 * 86400
    await db.execute('DELETE FROM message_map WHERE created_at < ?', (cutoff,))
    await db.execute('DELETE FROM delivery_log WHERE created_at < ?', (cutoff,))
    await db.commit()
    await db.close()
    print('cleanup done')

asyncio.run(main())
"
```

Автоочистка запускается каждые 30 минут автоматически.
