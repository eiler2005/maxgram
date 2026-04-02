# Spike Results — Phase 0

**Дата:** 2026-04-02  
**Решение: GO ✅** — обе системы работают, MVP запущен.

---

## MAX Userbot

### Библиотека

- [x] `maxapi-python` (PyPI, `pip install maxapi-python==1.2.5`) — установилась
- [x] Авторизация через phone + OTP-код (SMS)
- [x] Сессия сохраняется в файл, повторная авторизация не нужна
- [x] Входящие сообщения приходят через WebSocket listener
- [x] Медиа (фото, документы) доступны через вложения

**Используемая библиотека:** `maxapi-python` (PyPI) — `SocketMaxClient`  
**Метод авторизации:** phone + SMS OTP

### Критические открытия

```python
# message.sender — это int (user_id), НЕ User-объект
sender_id = str(getattr(message, "sender", None))   # int → str

# chat_id: положительный = DM, отрицательный = группа
is_dm = int(chat_id) > 0

# Имя пользователя — через кеш клиента
user = client.get_cached_user(int(user_id))
name = user.names[0].first_name  # НЕ user.first_name напрямую

# Название группы — из client.chats (populated после sync)
chat_obj = next((c for c in client.chats if c.id == chat_id_int), None)
chat_title = chat_obj.title if chat_obj else None

# ОБЯЗАТЕЛЬНЫЕ флаги при создании клиента:
SocketMaxClient(reconnect=False, send_fake_telemetry=False)
# reconnect=True → OOM (lists grow without reset)
# send_fake_telemetry=True → SSL TLSV1_ALERT_RECORD_OVERFLOW storm
```

### Структура события сообщения (реальная)

```python
# pymax Message fields:
message.id        # int — уникальный в рамках чата (не глобально)
message.chat_id   # int — положительный (DM) или отрицательный (группа)
message.sender    # int — user_id отправителя (НЕ User объект)
message.text      # str | None
message.attaches  # list — вложения (фото, файлы)

# Attach fields:
attach.type       # str — "PHOTO_ATTACH", "FILE_ATTACH" и т.п.
attach.file_id    # str | None
attach.id         # str | None (fallback)
attach.filename   # str | None
```

### Список чатов (пример структуры результата spike)

| Название чата | max_chat_id | Тип | Режим |
|--------------|-------------|-----|-------|
| Школьный чат | -70000000000001 | Group | active |
| Семейная группа | -70000000000002 | Group | active |
| Спортивная секция | -70000000000003 | Group | active |
| Личный контакт | 123456789 | DM | active |

DM чаты: `chat_id == user_id` собеседника. Имя резолвится через `resolve_user_name(chat_id)`.

---

## Telegram Topics

### Настройка

- [x] Бот создан через @BotFather
- [x] Токен добавлен в `.env`
- [x] Форум-супергруппа создана
- [x] Topics включены в настройках группы
- [x] Бот добавлен в группу как администратор (с правом управления топиками)

### Структура Update при reply в топике

```python
# aiogram Message при reply в топике форум-группы:
message.message_thread_id  # int — ID топика (topic_id)
message.reply_to_message   # Message | None — оригинальное сообщение
message.reply_to_message.message_id  # int — для reply routing
message.from_user.id       # int — кто написал
message.chat.id            # int — ID группы
message.text               # str | None
message.caption            # str | None (для медиа с подписью)
```

---

## Blockers / Issues обнаруженные в процессе

| Issue | Статус | Решение |
|-------|--------|---------|
| `reconnect=True` → OOM | ✅ Fixed | Outer reconnect loop, fresh client |
| `send_fake_telemetry=True` → SSL storm | ✅ Fixed | `send_fake_telemetry=False` |
| `message.sender` — int, не User | ✅ Fixed | `get_cached_user(int(sender_id))` |
| `client.me` — атрибут, не метод | ✅ Fixed | `self._client.me` (не `get_me()`) |
| `send_message` fails on reconnect window | ✅ Fixed | Retry 3×5s |
| DM title = "Чат XXXXX" | ✅ Fixed | `resolve_user_name` + auto-rename |
