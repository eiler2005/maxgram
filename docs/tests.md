# Тест-сьют Maxgram

Запуск:

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

Всего: **66 тестов**, все асинхронные через `pytest-asyncio`. Внешних зависимостей нет — SQLite в памяти (`tmp_path`), MAX и Telegram заменены stub-классами.

---

## test_config_loader.py — конфигурация (1 тест)

| Тест | Что проверяет |
|------|--------------|
| `test_load_config_merges_optional_local_override` | `config.local.yaml` перекрывает `config.yaml`: `bridge.default_mode`, список `chats` с `max_chat_id`, `title`, `mode`. Переменные окружения (`TG_BOT_TOKEN` и др.) подставляются в YAML через env-interpolation. |

---

## test_repository.py — работа с SQLite (6 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_save_message_upserts_tg_fields` | При двойном `save_message` с одним `max_msg_id` второй вызов дополняет запись: `tg_msg_id` и `tg_topic_id` обновляются через `ON CONFLICT DO UPDATE SET ... = COALESCE(excluded, existing)`. Проверяет что `get_max_msg_id_by_tg` находит запись по `tg_msg_id`. |
| `test_get_chat_activity_map_since_groups_by_chat` | SQL-агрегация активности по чатам: корректно считает `inbound`, `outbound`, `total` для `/chats`. |
| `test_save_and_find_user_by_name` | `save_user()` сохраняет запись в `known_users`; `find_user_by_name()` возвращает корректный `max_user_id`. |
| `test_find_user_case_insensitive` | `find_user_by_name()` работает без учёта регистра для кириллицы (Python-level сравнение, т.к. SQLite NOCASE не покрывает кириллицу). |
| `test_save_user_upserts_name` | Повторный `save_user()` с тем же `user_id` обновляет `display_name` и `updated_at` (upsert через `ON CONFLICT`). |
| `test_find_user_returns_none_when_not_found` | Возвращает `None` для имени, которого нет в таблице. |

---

## test_max_adapter.py — парсинг сырых сообщений MAX (28 тестов)

### Системные события (CONTROL)

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_renders_control_leave` | `CONTROL/leave` → `rendered_texts == ["Имя Фамилия вышел(а) из чата"]`; `attachment_types == ["CONTROL"]`; `chat_title` подставляется из `client.chats`. |
| `test_handle_raw_message_renders_control_add_with_partial_name_resolution` | `CONTROL/add` с двумя `userIds` — один известен в кеше, другой нет → `"Добавлены участники: Имя Фамилия, ещё 1"`. Проверяет частичное разрешение имён. |
| `test_handle_raw_message_renders_control_join_by_link` | `CONTROL/joinbylink` рендерится в человекочитаемый текст `"Присоединились по ссылке: ..."`, а не сырой `joinbylink`. |

### CHANNEL/forward и неизвестные MAX-типы

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_unwraps_forward_link_content` | `CHANNEL`/forward с `link.message` разворачивается до исходного текста и вложений; media download использует исходные `chat_id/message_id`. |
| `test_handle_raw_receive_unwraps_channel_wrapper_and_skips_pymax_duplicate` | Raw `CHANNEL`-обёртка перехватывается до pymax-parser, реальный nested message отправляется дальше, последующий wrapper-дубликат подавляется. |
| `test_handle_raw_message_renders_unknown_message_details` | Для неизвестного `CHANNEL` без доступного nested content формируется подробный `[Неизвестное сообщение MAX]` с `type`, `link_*` и списком полей. |

### Медиавложения без файла

| Тест | Параметр `attach` | Ожидаемый `rendered_text` |
|------|--------------------|--------------------------|
| `test_handle_raw_message_renders_non_media_supported_attachments[attach0]` | `type=CONTACT, name="Тестовый Контакт"` | `"Контакт: Тестовый Контакт"` |
| `test_handle_raw_message_renders_non_media_supported_attachments[attach1]` | `type=STICKER, audio=False` | `"[Стикер]"` |
| `test_handle_raw_message_renders_non_media_supported_attachments[attach2]` | `type=STICKER, audio=True` | `"[Аудиостикер]"` |

### Нормализация alias-типов вложений

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_normalizes_alias_attachment_types[IMAGE-PHOTO]` | Alias `IMAGE` нормализуется в `PHOTO` и проходит через медиа-пайплайн как фото. |
| `test_handle_raw_message_normalizes_alias_attachment_types[VOICE-AUDIO]` | Alias `VOICE` нормализуется в `AUDIO`. |
| `test_handle_raw_message_normalizes_alias_attachment_types[DOCUMENT-FILE]` | Alias `DOCUMENT` нормализуется в `FILE`. |
| `test_handle_raw_message_normalizes_alias_attachment_types[DOC-FILE]` | Alias `DOC` нормализуется в `FILE`. |

### Дедупликация собственных сообщений

| Тест | Что проверяет |
|------|--------------|
| `test_send_message_waits_for_echo_ack_when_pymax_does_not_return_id` | Когда pymax не возвращает реальный `id` (только `accepted=True`), `send_message` ждёт эхо-сообщение от самого MAX и возвращает его `msg_id`. Обработчик не вызывается (эхо подавляется). |
| `test_own_echo_is_suppressed_when_send_message_returns_real_id` | Когда pymax возвращает настоящий `id`, он сохраняется как "отправленный". Последующее эхо-сообщение с тем же `id` от MAX подавляется — обработчик не вызывается. |

### Устойчивость reconnect и video CDN

| Тест | Что проверяет |
|------|--------------|
| `test_failfast_ping_closes_client_after_consecutive_failures` | После серии подряд неудачных interactive ping клиент форсированно закрывается, чтобы внешний reconnect-loop быстро поднял новый MAX socket. |
| `test_failfast_ping_resets_failure_counter_after_success` | Счётчик ping failures сбрасывается после успешного ping, чтобы reconnect не срабатывал на разовых сбоях. |
| `test_extract_video_url_prefers_stream_over_thumbnail` | Из вложенного payload `VIDEO_PLAY` выбирается media stream (`.mp4`), а не thumbnail/preview URL. |
| `test_extract_video_url_prefers_mp4_variant_over_external_page` | Если `VIDEO_PLAY` содержит и `EXTERNAL` HTML-плеер, и `MP4_*` media URL, bridge выбирает `MP4_*`. |
| `test_download_headers_for_url_uses_chrome_user_agent_for_chrome_signed_url` | Для signed MAX CDN URL с `srcAg=CHROME` downloader ставит Chrome `User-Agent`. |
| `test_download_headers_for_url_uses_mobile_safari_for_non_chrome_signed_url` | Для signed MAX CDN URL без `CHROME` downloader использует mobile Safari `User-Agent`. |
| `test_download_video_by_id_uses_raw_video_play_payload` | `_download_video_by_id()` читает сырой payload `VIDEO_PLAY` и скачивает найденный media URL напрямую, не полагаясь на хрупкий upstream parser. |
| `test_download_from_url_uses_mobile_safari_user_agent` | Базовый downloader создаёт `tmp_dir`, делает HTTP GET с ожидаемым `User-Agent` и сохраняет файл с корректным именем. |
| `test_download_from_url_rejects_html_for_expected_video` | Post-validation блокирует `text/html`/HTML-body для ожидаемого `video`, чтобы player fallback не уходил в Telegram как файл/медиа. |
| `test_download_from_url_allows_text_for_expected_document` | Для ожидаемого `document` обычный `text/plain` файл остаётся допустимым, чтобы post-validation не ломала пересылку текстовых документов. |

---

## test_bridge_core.py — роутинг MAX→TG и TG→MAX (19 тестов)

Используют stub-классы `DummyMax`, `DummyTelegram`, `DummyRepo`, `DummyConfig`. Нет I/O, нет сети.

### Пересылка сообщений

| Тест | Что проверяет |
|------|--------------|
| `test_forward_to_telegram_sends_media_then_rendered_system_text` | Сообщение с видео-вложением и `rendered_texts`: сначала отправляется видео (`send_video` с caption `[Имя]`), затем текст системного события (`send_text`). Возвращает `message_id` медиа. |
| `test_forward_to_telegram_sends_voice_note_for_voice_source` | Вложение с `source_type=VOICE` отправляется как нативный `send_voice` (voice bubble), а не как обычное аудио. |
| `test_forward_to_telegram_uses_rendered_text_without_media` | Сообщение типа `CONTROL` без вложений: отправляется только текст из `rendered_texts`. Файловые методы не вызываются. |
| `test_on_tg_reply_prefixes_sender_name_for_max` | Reply из Telegram: текст отправляется в MAX с префиксом `[Марина Ермилова]\nПроверка связи`; `reply_to_msg_id` разрешается через `get_max_msg_id_by_tg`. |
| `test_on_tg_reply_rejects_too_large_media` | TG→MAX: если файл превышает лимит `max_file_size_mb`, bridge не отправляет его в MAX и отдаёт явное сообщение в топик. |
| `test_on_tg_reply_logs_forward_completion` | После успешной доставки TG→MAX в логах присутствует событие `bridge.outbound.forward_finished` с `outcome=delivered`. |
| `test_get_or_create_topic_resolves_group_title_via_live_max_lookup` | Если `chat_title=None`, `_get_or_create_topic` делает live запрос `resolve_chat_title` и создаёт топик с правильным именем. |

### Статус и чаты

| Тест | Что проверяет |
|------|--------------|
| `test_build_chats_message_lists_topics_with_activity` | `/chats` показывает чат, topic_id, режим и счётчики `↓/↑` за период. |
| `test_watchdog_sends_gap_notice_after_reconnect` | После offline-окна watchdog отправляет и alert про downtime, и уведомление о возможном `missed messages gap` после восстановления. |

### Кодировка файлов

| Тест | Что проверяет |
|------|--------------|
| `test_fix_filename_encoding_fixes_cyrillic_mojibake` | `_fix_filename_encoding()` исправляет cp1251-как-latin-1 кракозябры в именах файлов MAX. |
| `test_fix_filename_encoding_leaves_ascii_unchanged` | ASCII-имена остаются нетронутыми. |
| `test_fix_filename_encoding_leaves_proper_utf8_unchanged` | Корректные UTF-8 строки не изменяются. |

### Команда `/dm`

| Тест | Что проверяет |
|------|--------------|
| `test_cmd_dm_finds_user_in_db_and_sends` | Пользователь найден в `known_users` БД → сообщение отправлено в MAX с правильным `user_id` и текстом. |
| `test_cmd_dm_falls_back_to_pymax_cache_when_db_empty` | При пустой БД поиск переходит к pymax in-memory кешу → сообщение доставлено. |
| `test_cmd_dm_returns_error_when_user_not_found` | Если ни БД, ни pymax кеш не содержат пользователя → возвращается понятная ошибка `❌`. |
| `test_cmd_dm_returns_usage_hint_when_no_args` | При пустом или однословном аргументе → возвращается подсказка о формате команды. |
| `test_cmd_dm_tries_longest_name_prefix_first` | Алгоритм пробует самый длинный prefix (3 слова для 4-словного ввода) раньше более коротких. |

### Персистирование пользователей

| Тест | Что проверяет |
|------|--------------|
| `test_on_max_message_persists_sender_to_db` | При входящем сообщении от другого пользователя `save_user()` вызывается с правильными `sender_id` и `sender_name`. |
| `test_on_max_message_does_not_persist_own_sender` | Собственные сообщения (`is_own=True`) не сохраняются в `known_users`. |

---

## test_main.py — точка входа (5 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_mask_ip_hides_third_octet` | `_mask_ip("204.168.239.217")` → `"204.168.*.217"` (третий октет заменяется `*`). |
| `test_infer_location_from_hetzner_hostname` | `_infer_location("ubuntu-4gb-hel1-6")` → `"Helsinki"` (из маппинга токенов имён датацентров). |
| `test_extract_pytest_summary_uses_terminal_summary` | Из stdout `pytest` извлекается итоговая строка вида `"17 passed in 1.49s"` для последующего включения в startup-уведомление. |
| `test_build_startup_notification_includes_runtime_details` | Стартовое уведомление содержит `"Maxgram запущен и подключён к MAX"`, `runtime: Docker`, hostname, `location: Helsinki`, masked IP. Использует `monkeypatch` для `socket.gethostname`, `Path.exists`, `_detect_primary_ipv4`. |
| `test_build_startup_notification_includes_startup_test_status` | В startup-уведомление добавляется строка вида `"Тесты запуска: ✅ 17 passed in 1.49s"`, если production self-check завершился успешно. |

---

## test_tg_adapter.py — входящие сообщения Telegram (3 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_dispatch_incoming_message_accepts_non_owner_group_member` | Сообщение от не-владельца (user_id=2) в форум-группе в топике → передаётся в reply-обработчик с правильными `(topic_id, text, reply_to_tg_id, sender_name)`. |
| `test_dispatch_incoming_message_ignores_non_owner_commands` | Команда `/status` от не-владельца в форум-группе → `_handle_command` не вызывается. |
| `test_tg_retry_logs_retry_and_success` | `_tg_retry` делает повторную попытку при `TelegramRetryAfter` и логирует событие retry; после успеха возвращает корректный результат. |

---

## Что не покрыто тестами

- `run_periodic_status` — бесконечный цикл; проверяется вручную в production
- `run_max_watchdog` покрыт базовым reconnect-сценарием, но full production-поведение также проверяется вручную
- Реальные сетевые вызовы (MAX WebSocket, Telegram Bot API)
- Retry-логика `_tg_retry` — требует мока `TelegramAPIError`
- `get_chat_activity_since` / `count_messages_since` — SQL запросы; проверяются smoke-скриптом

Смоук-проверка по реальной БД:
```bash
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```
