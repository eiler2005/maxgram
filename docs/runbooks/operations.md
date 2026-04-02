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
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Ожидаемое состояние:

- контейнер `bridge` в статусе `Up`
- в логах есть `MAX connected`
- нет непрерывных ошибок `TelegramConflictError`
- в `message_map` и `delivery_log` появляются свежие записи

### Что важно помнить

- Доступ к серверу теперь только с твоего текущего IP.
- Если домашний IP изменится, нужно будет обновить правило и в Hetzner Firewall, и в `UFW`.
- Cloud Firewall я не менял через API, потому что для этого нужен отдельный Hetzner API token.
- Поэтому в панели Hetzner отдельно проверь, что там нет широких правил `Any IPv4` / `Any IPv6`, а только `22/tcp` с твоего IP.

## Запуск / Остановка

```bash
# Запуск (фоновый, с логом)
cd /path/to/maxtg_bridge
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &

# Проверить что работает
ps aux | grep main.py | grep -v grep
tail -20 data/bridge.log

# Остановить
kill $(ps aux | grep 'python src/main.py' | grep -v grep | awk '{print $2}')

# Перезапуск
kill $(ps aux | grep 'python src/main.py' | grep -v grep | awk '{print $2}')
sleep 2
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &
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

1. Найти `max_chat_id` из лога: `grep "New topic created" data/bridge.log`
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

1. Проверить что bridge запущен: `ps aux | grep main.py`
2. Проверить лог: `tail -100 data/bridge.log`
3. Проверить delivery_log в DB (см. выше)
4. Если `SSL: TLSV1_ALERT_RECORD_OVERFLOW` → убедиться что используется `send_fake_telemetry=False`
5. Если `dialogs=NNN` растёт (>12 при каждом reconnect) → убедиться что `reconnect=False`

## Проблема: "❌ Не удалось отправить сообщение в MAX"

Происходит если MAX reconnect > 15 сек. Bridge ждёт 3×5 сек и возвращает ошибку.
Решение: подождать несколько секунд и отправить ещё раз.

## Проблема: сообщение не появилось (потеряно)

pymax **не воспроизводит историю** после reconnect. Сообщения отправленные во время downtime теряются.
Это известное ограничение — не баг.

## Обновление зависимостей

```bash
source .venv/bin/activate
pip install --upgrade maxapi-python aiogram aiosqlite
# Проверить что всё работает
python src/main.py
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
