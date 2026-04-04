# Тест-сьют Maxgram

Запуск:

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

Всего: **19 тестов**, все асинхронные через `pytest-asyncio`. Внешних зависимостей нет — SQLite в памяти (`tmp_path`), MAX и Telegram заменены stub-классами.

---

## test_config_loader.py — конфигурация (1 тест)

| Тест | Что проверяет |
|------|--------------|
| `test_load_config_merges_optional_local_override` | `config.local.yaml` перекрывает `config.yaml`: `bridge.default_mode`, список `chats` с `max_chat_id`, `title`, `mode`. Переменные окружения (`TG_BOT_TOKEN` и др.) подставляются в YAML через env-interpolation. |

---

## test_repository.py — работа с SQLite (1 тест)

| Тест | Что проверяет |
|------|--------------|
| `test_save_message_upserts_tg_fields` | При двойном `save_message` с одним `max_msg_id` второй вызов дополняет запись: `tg_msg_id` и `tg_topic_id` обновляются через `ON CONFLICT DO UPDATE SET ... = COALESCE(excluded, existing)`. Проверяет что `get_max_msg_id_by_tg` находит запись по `tg_msg_id`. |

---

## test_max_adapter.py — парсинг сырых сообщений MAX (7 тестов)

### Системные события (CONTROL)

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_renders_control_leave` | `CONTROL/leave` → `rendered_texts == ["Имя Фамилия вышел(а) из чата"]`; `attachment_types == ["CONTROL"]`; `chat_title` подставляется из `client.chats`. |
| `test_handle_raw_message_renders_control_add_with_partial_name_resolution` | `CONTROL/add` с двумя `userIds` — один известен в кеше, другой нет → `"Добавлены участники: Имя Фамилия, ещё 1"`. Проверяет частичное разрешение имён. |

### Медиавложения без файла

| Тест | Параметр `attach` | Ожидаемый `rendered_text` |
|------|--------------------|--------------------------|
| `test_handle_raw_message_renders_non_media_supported_attachments[attach0]` | `type=CONTACT, name="Тестовый Контакт"` | `"Контакт: Тестовый Контакт"` |
| `test_handle_raw_message_renders_non_media_supported_attachments[attach1]` | `type=STICKER, audio=False` | `"[Стикер]"` |
| `test_handle_raw_message_renders_non_media_supported_attachments[attach2]` | `type=STICKER, audio=True` | `"[Аудиостикер]"` |

### Дедупликация собственных сообщений

| Тест | Что проверяет |
|------|--------------|
| `test_send_message_waits_for_echo_ack_when_pymax_does_not_return_id` | Когда pymax не возвращает реальный `id` (только `accepted=True`), `send_message` ждёт эхо-сообщение от самого MAX и возвращает его `msg_id`. Обработчик не вызывается (эхо подавляется). |
| `test_own_echo_is_suppressed_when_send_message_returns_real_id` | Когда pymax возвращает настоящий `id`, он сохраняется как "отправленный". Последующее эхо-сообщение с тем же `id` от MAX подавляется — обработчик не вызывается. |

---

## test_bridge_core.py — роутинг MAX→TG и TG→MAX (3 теста)

Используют stub-классы `DummyMax`, `DummyTelegram`, `DummyRepo`. Нет I/O, нет сети.

| Тест | Что проверяет |
|------|--------------|
| `test_forward_to_telegram_sends_media_then_rendered_system_text` | Сообщение с видео-вложением и `rendered_texts`: сначала отправляется видео (`send_video` с caption `[Имя]`), затем текст системного события (`send_text`). Возвращает `message_id` медиа. |
| `test_forward_to_telegram_uses_rendered_text_without_media` | Сообщение типа `CONTROL` без вложений: отправляется только текст из `rendered_texts`. Файловые методы не вызываются. |
| `test_on_tg_reply_prefixes_sender_name_for_max` | Reply из Telegram: текст отправляется в MAX с префиксом `[Марина Ермилова]\nПроверка связи`; `reply_to_msg_id` разрешается через `get_max_msg_id_by_tg`. |

---

## test_main.py — точка входа (5 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_mask_ip_hides_third_octet` | `_mask_ip("204.168.239.217")` → `"204.168.*.217"` (третий октет заменяется `*`). |
| `test_infer_location_from_hetzner_hostname` | `_infer_location("ubuntu-4gb-hel1-6")` → `"Helsinki"` (из маппинга токенов имён датацентров). |
| `test_extract_pytest_summary_uses_terminal_summary` | Из stdout `pytest` извлекается итоговая строка вида `"19 passed in 1.65s"` для последующего включения в startup-уведомление. |
| `test_build_startup_notification_includes_runtime_details` | Стартовое уведомление содержит `"Maxgram запущен и подключён к MAX"`, `runtime: Docker`, hostname, `location: Helsinki`, masked IP. Использует `monkeypatch` для `socket.gethostname`, `Path.exists`, `_detect_primary_ipv4`. |
| `test_build_startup_notification_includes_startup_test_status` | В startup-уведомление добавляется строка вида `"Тесты запуска: ✅ 19 passed in 1.65s"`, если production self-check завершился успешно. |

---

## test_tg_adapter.py — входящие сообщения Telegram (2 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_dispatch_incoming_message_accepts_non_owner_group_member` | Сообщение от не-владельца (user_id=2) в форум-группе в топике → передаётся в reply-обработчик с правильными `(topic_id, text, reply_to_tg_id, sender_name)`. |
| `test_dispatch_incoming_message_ignores_non_owner_commands` | Команда `/status` от не-владельца в форум-группе → `_handle_command` не вызывается. |

---

## Что не покрыто тестами

- `run_periodic_status` / `run_max_watchdog` — бесконечные циклы; проверяются вручную в production
- Реальные сетевые вызовы (MAX WebSocket, Telegram Bot API)
- Retry-логика `_tg_retry` — требует мока `TelegramAPIError`
- `get_chat_activity_since` / `count_messages_since` — SQL запросы; проверяются smoke-скриптом

Смоук-проверка по реальной БД:
```bash
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```
