# HANDOFF — MAX→Telegram Bridge

## Цель

Личный bridge: сообщения из MAX (российский мессенджер) → Telegram Forum Supergroup.
Каждый MAX-чат = отдельный топик. Reply в топике = ответ в MAX.
Один пользователь, self-hosted, никаких третьих лиц.

---

## Что работает (подтверждено в production)

- MAX подключается и получает входящие сообщения
- Новые чаты → автоматически создаётся топик в Telegram
- DM-топики переименовываются из "Чат XXXXX" в реальное имя контакта
- Фото, видео, аудио и документы пересылаются в Telegram
- Системные MAX-события (`leave`, `add`, contact/sticker, edited/removed) рендерятся как текст
- Reply из Telegram → сообщение уходит в MAX
- В Telegram topic могут писать участники группы, а не только owner
- В MAX исходящее из Telegram приходит с префиксом автора: `[Имя Фамилия]`
- Reconnect работает без OOM и без SSL-шторма
- Дедупликация: при reconnect сообщения не дублируются
- Bridge работает в production на Hetzner Cloud

---

## Что сломано / не доделано

1. **Telegram API без retry** — если TG API временно недоступен, сообщение может потеряться без повторной попытки.

2. **Нет алерта при длительной потере MAX** — bridge молча reconnect-ится. Если MAX недоступен > 1 минуты, пользователь не знает.

3. **`/status` пока слишком простой** — отвечает "✅ Bridge работает", но без uptime, количества чатов и последней активности.

4. **SSH-доступ завязан на домашний IP** — если IP сменится, нужно обновить правило и в Hetzner Firewall, и в `UFW`.

---

## Как воспроизвести текущее состояние

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

---

## Ключевые файлы

| Файл | Зачем |
|------|-------|
| `src/adapters/max_adapter.py` | pymax userbot, reconnect, парсинг сообщений |
| `src/adapters/tg_adapter.py` | aiogram, топики, send/recv |
| `src/bridge/core.py` | вся логика роутинга |
| `src/db/repository.py` | SQLite: bindings, dedup, delivery log |
| `config.yaml` | чаты, режимы, параметры |
| `scripts/smoke_check.py` | быстрый smoke-report по SQLite |
| `deploy/docker-compose.prod.yml` | production-compose для Hetzner |
| `docs/runbooks/operations.md` | ежедневная эксплуатация |
| `docs/runbooks/hetzner-production.md` | secure-runbook по серверу |
| `data/bridge.db` | состояние (не в git) |

---

## Подтверждённые факты о pymax (добытые через debugging)

- `message.sender` — `int`, **не** User-объект
- `message.chat_id` — `int`; положительный = DM, отрицательный = группа
- Имя пользователя: `user.names[0].first_name` (не `user.first_name`)
- Кеш: `client.get_cached_user(int(user_id))` — синхронный, работает
- `client.me` — атрибут (не метод `get_me()`)
- `client.chats` — список групп (populated после sync)
- `SocketMaxClient(reconnect=True)` → OOM-баг: `chats`/`dialogs` растут без сброса
- `send_fake_telemetry=True` (default) → SSL TLSV1_ALERT_RECORD_OVERFLOW на каждом connect
- Правильный запуск: `reconnect=False, send_fake_telemetry=False` + outer `while True` loop

---

## Неудачные гипотезы

- ~~`fix_fallback_titles()` при старте~~ — вызов сразу после connect нестабилен, убрали; теперь rename происходит при первом входящем сообщении
- ~~`client.get_me()`~~ — нет такого метода, есть `client.me`
- ~~`send_message` с параметром `reply_to`~~ — правильное имя параметра именно `reply_to` (не `reply_to_message_id`); проблема была в том, что сокет ещё не был готов

---

## Следующий лучший шаг

**Добавить retry/backoff для Telegram API** (`src/adapters/tg_adapter.py`).

Это сейчас самый полезный следующий шаг для production-качества.
