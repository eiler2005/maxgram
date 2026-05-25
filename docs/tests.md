# Тест-сьют Maxgram

Запуск:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q
PYTHONPATH=. .venv/bin/pytest --cov=src --cov-report=term-missing --cov-report=xml --cov-report=html --cov-fail-under=75
PYTHONPATH=. .venv/bin/pytest -q -m "not architecture"  # business/functional regression only
PYTHONPATH=. .venv/bin/pytest -q -m architecture        # service-boundary/refactor guards
PYTHONPATH=. .venv/bin/python -m compileall src tests
.venv/bin/ruff check .
.venv/bin/ruff check src/bridge --select E9,F63,F7,F82,F401,F841,B,C4,SIM,RET
.venv/bin/mypy src/bridge/errors.py src/bridge/contracts.py src/db/migrations.py src/adapters/max/backends/base.py src/adapters/max/deps.py src/adapters/max/state.py src/adapters/max/ports.py src/adapters/max/payload.py src/adapters/max/users.py src/adapters/max/errors.py src/adapters/max/media/ua.py src/adapters/max/media/downloader.py
.venv/bin/mypy --check-untyped-defs --no-implicit-optional --ignore-missing-imports --follow-imports=silent src/bridge/core.py src/bridge/status.py src/bridge/media_retry.py src/bridge/recovery/scheduler.py src/bridge/commands/dispatcher.py
```

Всего: **276 тестов**, async-тесты идут через `pytest-asyncio`, property-based parser guards — через `hypothesis`. Внешних зависимостей нет: SQLite через `tmp_path`, MAX и Telegram заменены stub/fake-классами.

GitHub Actions выполняет тот же gate: `compileall`, repo-level `ruff check`, scoped bridge `ruff`, scoped `mypy` для MAX/bridge boundaries, затем `pytest --cov=src --cov-report=term-missing --cov-report=xml --cov-report=html --cov-fail-under=75`. HTML/XML coverage отчёты загружаются artifact-ом `coverage-report`.

Тесты с marker `architecture` — это service-boundary/refactoring guards (`test_bridge_contracts.py`, `test_max_adapter_leaves.py`, `test_pymax_surface_pin.py`). Их можно отделить от бизнес-регресса командой `pytest -m "not architecture"`; пока они остаются частью полного gate и не отключены.

```text
                         pytest -q
                            │
              ┌─────────────┴─────────────┐
              │                           │
  business / functional          architecture / service-boundary
  pytest -m "not architecture"   pytest -m architecture
              │                           │
              ▼                           ▼
  runtime behavior, configs,     import boundaries, pymax isolation,
  DB flows, MAX/TG forwarding,   composition-not-mixins, no god base,
  media retry, commands          pymax-free helper leaves
              │                           │
              └─────────────┬─────────────┘
                            ▼
                 full pre-merge / pre-deploy gate
```

---

## test_bridge_contracts.py — архитектурная граница (12 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_bridge_core_does_not_import_concrete_adapters` | `src.bridge.core` импортирует bridge contracts, но не concrete `src.adapters.max_adapter` / `src.adapters.tg_adapter`. |
| `test_bridge_contracts_stay_transport_neutral` | `src.bridge.contracts` не импортирует `pymax`, `aiogram` или adapter-слой. |
| `test_bridge_uses_max_bridge_port_methods_directly` | Bridge code работает через `MaxBridgePort` methods, а не через concrete MAX adapter internals. |
| `test_main_keeps_runtime_wiring_in_composition_root` | `src.main` не импортирует concrete adapters или `BridgeCore`; runtime wiring живёт в `src.startup.composition`. |
| `test_pymax_imports_stay_inside_max_adapter_boundary` | `pymax` imports разрешены только в `src/adapters/max/backends/pymax/*`; bridge/contracts/services остаются transport-neutral. |
| `test_environment_inventory_documents_reverse_channel_m` | `docs/environment-inventory.md` описывает reverse Channel M, VPS docker bridge, router loopback inbound, env-переменные и отсутствие автоматического fallback; architecture doc ссылается на inventory. |
| `test_deploy_bundle_includes_docs_for_startup_self_tests` | Docker image и Ansible release bundle включают `docs/`, чтобы startup self-tests внутри prod-контейнера видели архитектурные inventory-документы. |
| `test_max_adapter_uses_composition_not_mixins` | `MaxAdapter` собран composition/facade-ом, не через mixin inheritance; сервисы не принимают полный `MaxAdapter`. |
| `test_max_services_use_explicit_dependencies` | MAX services не используют `MaxServiceRegistry`/service `__getattr__`, не принимают `MaxAdapter`, а adapter tests не subclass-ят real adapter для private overrides. |
| `test_max_services_do_not_use_god_base_forwarders` | MAX services не наследуются от `ExplicitMaxService` и не возвращают скрытые cross-service deps-forwarders через `*args, **kwargs`. |
| `test_max_operation_services_do_not_use_pymax_client_shape_directly` | MAX operation services не обращаются к pymax-private/client-shape attrs напрямую (`_send_and_wait`, `_handle_message_notifications`, `fetch_history`, `contacts/dialogs/chats/_users` и т.д.); всё проходит через typed ports. |
| `test_bridge_core_keeps_heavy_leaf_logic_outside_coordinator` | `BridgeCore` не содержит status/recovery/media-retry command-heavy methods/state, не импортирует recovery reporter напрямую и регистрирует команды через dispatcher. |

---

## test_config_loader.py — конфигурация (5 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_load_config_merges_optional_local_override` | `config.local.yaml` перекрывает `config.yaml`: `bridge.default_mode`, список `chats` с `max_chat_id`, `title`, `mode`. Переменные окружения (`TG_BOT_TOKEN` и др.) подставляются в YAML через env-interpolation. |
| `test_load_config_reads_dm_history_sweep_overrides` | `health.dm_history_sweep` читает balanced-настройки нагрузки: enabled, warmup/steady intervals, limit, backfill, jitter и per-chat delay. |
| `test_load_config_reads_secrets_from_dotenv_secrets` | `load_config()` подхватывает секреты из `.env.secrets`, а не только из уже экспортированного окружения; заодно проверяет что `DATA_DIR` берётся из `.env`. |
| `test_load_config_reads_max_egress_profiles` | `max.egress` читает active profile, direct backward-compatible default и `${MAX_EGRESS_PROXY_URL}` для `home_ru_proxy`. |
| `test_load_config_reads_metrics_textfile_overrides` | `health.metrics_textfile_path` по умолчанию пишет в `data/maxtg_bridge.prom`, принимает explicit path и отключается через `off/disabled`. |

---

## test_max_egress.py — MAX-only egress (7 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_http_connect_socket_connector_sends_connect_and_auth` | `HttpConnectSocketConnector` отправляет `CONNECT api.oneme.ru:443` и `Proxy-Authorization` без глобального monkeypatch socket. |
| `test_http_connect_socket_connector_fails_on_non_200` | Не-200 ответ proxy даёт `MaxEgressUnavailable`, а не fallback на direct. |
| `test_http_connect_socket_connector_probe_success` | Safe probe проходит proxy TCP, HTTP CONNECT и TLS stage без реального MAX payload. |
| `test_http_connect_socket_connector_probe_failure_is_redacted` | Ошибки probe не содержат proxy credentials. |
| `test_max_egress_profile_probe_includes_safe_profile_fields` | `MaxEgressProfile.probe()` добавляет только safe profile metadata: active/type/label/proxy host. |
| `test_max_cdn_downloader_passes_proxy_options` | MAX CDN downloader передаёт `proxy`/`proxy_auth` в `aiohttp.ClientSession`, поэтому media downloads используют тот же egress profile. |
| `test_max_egress_unavailable_is_classified_as_fail_closed_issue` | Proxy failure классифицируется как `max_egress_unavailable`, является `MaxTransientError` и считается retryable. |

---

## test_repository.py — работа с SQLite (19 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_schema_migrations_apply_fresh_and_are_idempotent` | Fresh SQLite DB получает baseline schema через `schema_migrations`; повторный запуск миграций безопасен. |
| `test_schema_migrations_baseline_existing_db` | Уже существующая DB с таблицами до runner-а получает baseline marker без изменения семантики схемы. |
| `test_save_message_upserts_tg_fields` | При двойном `save_message` с одним `max_msg_id` второй вызов дополняет запись: `tg_msg_id` и `tg_topic_id` обновляются через `ON CONFLICT DO UPDATE SET ... = COALESCE(excluded, existing)`. Проверяет что `get_max_msg_id_by_tg` находит запись по `tg_msg_id`. |
| `test_tg_reply_mapping_resolves_delayed_media_message` | Поздно досланное media-сообщение в Telegram мапится обратно к исходному MAX message для корректных replies. |
| `test_get_chat_activity_map_since_groups_by_chat` | SQL-агрегация активности по чатам: корректно считает `inbound`, `outbound`, `total` для `/chats`. |
| `test_save_and_find_user_by_name` | `save_user()` сохраняет запись в `known_users`; `find_user_by_name()` возвращает корректный `max_user_id`. |
| `test_find_user_case_insensitive` | `find_user_by_name()` работает без учёта регистра для кириллицы (Python-level сравнение, т.к. SQLite NOCASE не покрывает кириллицу). |
| `test_save_user_upserts_name` | Повторный `save_user()` с тем же `user_id` обновляет `display_name` и `updated_at` (upsert через `ON CONFLICT`). |
| `test_find_user_returns_none_when_not_found` | Возвращает `None` для имени, которого нет в таблице. |
| `test_recovery_registry_snapshot_report_and_export_are_idempotent` | Проверяет `max_account_generations`, migration detection, idempotent recovery snapshot upsert, сохранение invite/admin metadata, `last_scan_at`, report и JSON export. |
| `test_recovery_remap_preserves_topic_and_updates_binding` | `/recovery remap` сохраняет Telegram topic, меняет `chat_bindings.max_chat_id`, фиксирует `old_max_chat_id/current_max_chat_id` и статус `remapped`. |
| `test_dm_contact_recovery_snapshot_upsert_export_and_privacy` | Проверяет создание `dm_contact_recovery_registry`, idempotent upsert, обновление `last_scan_at`, агрегаты report, owner-only export и отсутствие phone/message/raw payload в событиях/export. |
| `test_recovery_snapshot_reports_status_change_deltas` | `upsert_recovery_snapshot()` возвращает deltas `inserted/status_changed/needs_invite` и пишет scan reason без чувствительных полей. |
| `test_find_phantom_topic_bindings_requires_duplicate_real_delivery` | Cleanup phantom topics срабатывает только при подтверждённом duplicate real delivery, не на одном совпадении metadata. |
| `test_pending_media_queue_lifecycle_is_idempotent` | Очередь durable media retry идемпотентно создаёт, reschedule-ит и завершает jobs. |
| `test_pending_outbound_lifecycle_clears_text_after_delivery` | Durable TG→MAX text outbox хранит plaintext только до успешной доставки и очищает `text`. |
| `test_pending_inbound_lifecycle_clears_text_after_delivery` | Durable MAX→TG text outbox хранит plaintext только до успешной доставки и очищает `text`. |

---

## test_runtime_timeouts.py — typed timeout boundary (2 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_with_timeout_raises_typed_external_timeout` | Bounded external await превращается в typed `BridgeExternalTimeout`, который остаётся `BridgeTransientError`. |
| `test_with_timeout_or_none_logs_and_returns_none` | Совместимый helper логирует metadata-only timeout event и возвращает `None` для существующих retry/failure paths. |

---

## test_max_payload_properties.py — property-based raw payload guards (3 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_payload_value_normalizes_case_and_underscore_aliases` | `payload_value()` одинаково читает MAX aliases вроде `chat_id/chatId`, `messageId/message_id`, `audio_id/audioId`. |
| `test_safe_field_paths_never_emit_private_or_unsafe_fields` | Hypothesis генерирует nested msgpack-like payloads с mixed key types; diagnostics не выводят private/url/token/text/raw field paths. |
| `test_raw_regular_message_round_trips_mixed_alias_payloads` | Raw parser строит `MaxClientMessage` из aliases/mixed payload shapes, сохраняя id/chat/sender/text/attachments и `_from_raw_unwrapped`. |

---

## tests/test_max_adapter/ — MAX adapter behavior split (83 теста)

Бывший монолит `tests/test_max_adapter.py` разрезан на пакет:

- `conftest.py` — общий harness, fake clients/download adapters и shared imports.
- `test_events.py` — raw/typed MAX events, CONTROL rendering, safe diagnostics.
- `test_recovery.py` — empty-message/raw-history/durable recovery paths.
- `test_media.py` — audio/video/CDN download behavior and media retry.
- `test_resolve.py` — user-name resolution and negative cache.
- `test_send.py` — outbound send, echo ack, lifecycle and runtime issue classification.

### Системные события (CONTROL)

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_renders_control_leave` | `CONTROL/leave` → `rendered_texts == ["Имя Фамилия вышел(а) из чата"]`; `attachment_types == ["CONTROL"]`; `chat_title` подставляется из `client.chats`. |
| `test_handle_raw_message_decodes_bytes_text_before_preview` | PyMax 2 `message.text` в bytes декодируется до UTF-8 string до logging preview и dispatch. |
| `test_handle_raw_message_extracts_text_from_msgpack_bytes` | SHARE/msgpack-like `message.text` bytes распаковываются до настоящего text без `�` и raw field names. |
| `test_classify_runtime_error_uses_exception_context_for_logout_all` | Если PyMax перекрывает `FAIL_LOGOUT_ALL/login.token` SSL close exception, классификация всё равно требует reauth. |
| `test_handle_raw_message_renders_control_add_with_partial_name_resolution` | `CONTROL/add` с двумя `userIds` — один известен в кеше, другой нет → `"Добавлены участники: Имя Фамилия, ещё 1"`. Проверяет частичное разрешение имён. |
| `test_handle_raw_message_renders_control_join_by_link` | `CONTROL/joinbylink` рендерится в человекочитаемый текст `"Присоединились по ссылке: ..."`, а не сырой `joinbylink`. |

### CHANNEL/forward и неизвестные MAX-типы

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_message_unwraps_forward_link_content` | `CHANNEL`/forward с `link.message` разворачивается до исходного текста и вложений; media download использует исходные `chat_id/message_id`. |
| `test_handle_raw_receive_unwraps_channel_wrapper_and_skips_pymax_duplicate` | Raw `CHANNEL`-обёртка перехватывается до pymax-parser, реальный nested message отправляется дальше, последующий wrapper-дубликат подавляется. |
| `test_raw_message_interceptor_catches_audio_and_suppresses_duplicate` | Внутренний pymax notification handler дополнительно прогоняет raw payload через bridge до typed parsing; последующий пустой typed-дубликат подавляется. |
| `test_handle_raw_message_renders_unknown_message_details` | Для неизвестного `CHANNEL` без доступного nested content формируется подробный `[Неизвестное сообщение MAX]` с `type`, `link_*` и списком полей. |

### Голосовые MAX-вложения

| Тест | Что проверяет |
|------|--------------|
| `test_handle_raw_receive_forwards_regular_audio_before_pymax_can_drop_it` | Raw `AUDIO` voice payload из MAX DM нормализуется и скачивается по `url` до того, как pymax может отдать пустой `USER` event. |
| `test_handle_raw_receive_forwards_top_level_audio_payload` | Raw notification, где `payload` сам является сообщением, а медиа лежит в `attachments`, нормализуется и доставляется как `AUDIO`. |

Raw payload implementation is split behind `src/adapters/max/raw_payload.py`: parser/normalizer, raw `CHAT_HISTORY` cache/fetch, empty-message candidate building and diagnostic telemetry live in `src/adapters/max/raw/*`. Tests above intentionally exercise the public `MaxAdapter` path, not those helper classes directly.
| `test_typed_empty_message_recovers_audio_from_recent_history` | Если typed pymax message пустой, adapter пробует добрать ровно этот свежий `msg_id` из recent history и пересылает найденный `AUDIO`. |
| `test_typed_empty_message_recovers_audio_from_raw_history_cache` | Raw `CHAT_HISTORY` с `messages[].cid/id/attaches[]` кешируется на короткое время, и последующий пустой typed `USER` восстанавливается как `AUDIO` без логирования URL/token/text. |
| `test_typed_empty_message_uses_raw_history_after_fetch_socket_error` | Если `fetch_history` падает с `Send and wait failed (socket)`, но raw `CHAT_HISTORY` уже пришёл, adapter восстанавливает голосовое из cache вместо `empty_event`. |
| `test_typed_empty_message_waits_for_delayed_raw_history_cache` | Если raw `CHAT_HISTORY` приходит позже immediate recovery, adapter держит короткую in-memory wait job и досылает найденное `AUDIO`. |
| `test_pending_empty_recovery_worker_delivers_late_audio` | Durable empty-message retry перечитывает history по `chat_id/msg_id`, доставляет поздно появившееся `AUDIO` и очищает meta-only job. |
| `test_pending_empty_recovery_worker_reschedules_empty_history` | Если history всё ещё возвращает пустой message, durable retry увеличивает attempts и планирует следующую попытку без terminal cutoff. |
| `test_replay_recent_history_pre_dedups_known_messages_but_keeps_pending` | DM history sweep пропускает уже известные `(chat_id,msg_id)` до тяжёлой нормализации, но не ломает pending empty recovery. |
| `test_handle_raw_receive_logs_safe_empty_message_diagnostic` | Raw empty-event diagnostic логирует только тип, id и безопасные имена полей, без URL/token/text. |
| `test_handle_raw_receive_logs_top_level_empty_message_diagnostic` | Top-level raw empty payload логируется безопасно, без URL/token/text. |
| `test_download_audio_attachment_uses_direct_url_and_preserves_duration` | `AUDIO` скачивается по прямому `url`; `duration` сохраняется в `MaxAttachment`. |
| `test_download_audio_attachment_normalizes_millisecond_duration` | MAX voice duration в миллисекундах нормализуется в секунды перед отправкой в Telegram. |
| `test_download_audio_attachment_falls_back_to_audio_id` | Если `url` нет и protocol resolver недоступен, `audio_id` используется через legacy download-by-id путь. |
| `test_download_audio_reference_uses_audio_get_sources_payload` | Durable voice retry без `url` пробует MAX Web `audioGetSources` (`opcode=301`), скачивает найденный audio URL и не логирует URL/token. |
| `test_download_audio_reference_falls_back_to_file_download_after_audio_get_miss` | Если `audioGetSources` не вернул URL, bridge пробует только безопасный `FILE_DOWNLOAD fileId`; `FILE_DOWNLOAD audioId` не используется. |
| `test_download_audio_reference_stops_protocol_after_socket_error` | Socket-level ошибка на protocol audio probe останавливает текущую попытку, не пробует рискованные payload shapes и не запускает legacy fallback на уже отвалившемся socket. |
| `test_download_audio_attachment_logs_safe_diagnostic_without_reference` | Voice-вложение без `url/audio_id/id` даёт безопасный diagnostic без раскрытия token/text. |

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
| `test_send_message_retries_retryable_transport_error_and_succeeds` | Временная ошибка транспорта (`Socket is not connected`) вызывает retry; следующая успешная попытка возвращает `msg_id`, а в логах появляется `max.outbound.retry`. |
| `test_send_message_exposes_final_error_after_retries` | После исчерпания retry `send_message()` возвращает `None`, а адаптер сохраняет последнюю ошибку и реальное число попыток для последующей записи в `delivery_log`. |
| `test_send_message_sanitizes_pymax_sequence_overflow_error` | PyMax TCP seq overflow нормализуется в безопасный `pymax_tcp_sequence_overflow` без логирования текста исходящего сообщения. |
| `test_start_path_logs_masked_phone_without_name_error` | Start/reconnect path логирует masked phone без production-only `NameError` после split lifecycle module. |
| `test_is_ready_tracks_underlying_transport_state` | `MaxAdapter.is_ready()` остаётся true только пока `_started=True` и реальный PyMax transport `client.is_connected=True`; закрытый socket виден watchdog/self-heal. |

### Резолв имён пользователей

| Тест | Что проверяет |
|------|--------------|
| `test_resolve_user_name_uses_contacts_cache_before_live_lookup` | Имя пользователя берётся из локального `contacts` cache без live `CONTACT_INFO`. |
| `test_resolve_user_name_live_lookup_has_short_timeout` | Live lookup имени имеет короткий timeout и не блокирует routing надолго при socket timeout. |
| `test_resolve_user_name_negative_cache_suppresses_repeated_live_lookup` | После failed live `get_users` повторный resolve в TTL не делает новый MAX API request; после expiry lookup снова разрешён. |
| `test_collect_recovery_snapshot_captures_access_metadata_without_messages` | `MaxAdapter.collect_recovery_snapshot()` собирает group/channel/DM metadata и DM contact snapshot только из typed dialog snapshots: chat kind, invite link, owner/admin contacts, DM partner, participant count, masked phone и session fingerprint hash без message text/raw payload; `client.contacts` без dialog не попадает в snapshot. |

### Устойчивость reconnect и video CDN

| Тест | Что проверяет |
|------|--------------|
| `test_failfast_ping_closes_client_after_consecutive_failures` | После серии подряд неудачных interactive ping клиент форсированно закрывается, чтобы внешний reconnect-loop быстро поднял новый MAX socket. |
| `test_failfast_ping_resets_failure_counter_after_success` | Счётчик ping failures сбрасывается после успешного ping, чтобы reconnect не срабатывал на разовых сбоях. |
| `test_extract_video_url_prefers_stream_over_thumbnail` | Из вложенного payload `VIDEO_PLAY` выбирается media stream (`.mp4`), а не thumbnail/preview URL. |
| `test_extract_video_url_prefers_mp4_variant_over_external_page` | Если `VIDEO_PLAY` содержит и `EXTERNAL` HTML-плеер, и `MP4_*` media URL, bridge выбирает `MP4_*`. |
| `test_download_headers_for_url_uses_chrome_user_agent_for_chrome_signed_url` | Для signed MAX CDN URL с `srcAg=CHROME` downloader ставит Chrome `User-Agent`. |
| `test_download_headers_for_url_uses_android_chrome_user_agent` | Для signed MAX CDN URL с `srcAg=CHROME_ANDROID` downloader ставит Android Chrome `User-Agent`. |
| `test_download_headers_for_url_uses_ios_chrome_user_agent` | Для signed MAX CDN URL с `srcAg=CHROME_IPHONE` downloader ставит iOS Chrome `User-Agent`. |
| `test_download_headers_for_url_uses_mobile_safari_for_non_chrome_signed_url` | Для signed MAX CDN URL без `CHROME` downloader использует mobile Safari `User-Agent`. |
| `test_download_video_by_id_uses_raw_video_play_payload` | `_download_video_by_id()` читает сырой payload `VIDEO_PLAY` и скачивает найденный media URL напрямую, не полагаясь на хрупкий upstream parser. |
| `test_handle_raw_message_marks_failed_video_retryable_by_video_id` | Если MAX `VIDEO` не скачался, но есть `video_id`, failure становится retryable и хранит только стабильную meta-ссылку без URL/token. |
| `test_download_from_url_uses_mobile_safari_user_agent` | Базовый downloader создаёт `tmp_dir`, делает HTTP GET с ожидаемым `User-Agent` и сохраняет файл с корректным именем. |
| `test_download_from_url_logs_src_ag_and_sanitized_http_error` | При CDN HTTP-ошибке downloader пишет `src_ag`, `ua_family`, `http_status`, `download_source`, но не раскрывает signed query URL в `error`. |
| `test_download_from_url_resumes_partial_file_after_connection_break` | Generic `media/downloader.py` сохраняет `.part`, повторяет запрос с `Range` и собирает файл без утечки signed URL. |
| `test_download_from_url_rejects_html_for_expected_video` | Post-validation блокирует `text/html`/HTML-body для ожидаемого `video`, чтобы player fallback не уходил в Telegram как файл/медиа. |
| `test_download_from_url_allows_text_for_expected_document` | Для ожидаемого `document` обычный `text/plain` файл остаётся допустимым, чтобы post-validation не ломала пересылку текстовых документов. |

---

## test_max_adapter_leaves.py — pymax-free MAX helper leaves + PyMax backend contracts (27 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_max_ua_mapping_selects_chrome_android_profile` | `media/ua.py` выбирает корректный MAX CDN User-Agent для Chrome/Android/iOS/Safari fallback. |
| `test_payload_helpers_match_case_and_strip_unsafe_fields` | `payload.py` читает nested payload values и чистит unsafe raw fields без pymax. |
| `test_error_classification_is_pymax_free` | `errors.py` классифицирует runtime issue и retryable outbound errors без pymax imports. |
| `test_pymax_client_adapter_captures_early_startup_errors` | `PymaxClientAdapter.prepare_startup()` ловит PyMax 2 `start()` ошибки до `on_start`. |
| `test_users_and_downloader_helpers_are_plain_object_based` | `users.py` и downloader helpers работают с plain objects/URL metadata. |
| `test_client_factory_disables_pymax_reconnect_and_telemetry` | `client_factory.py` создаёт PyMax 2 `Client` с `ExtraConfig(reconnect=False, telemetry=False)`, backend session store, DESKTOP user-agent и sync overrides для legacy session. |
| `test_client_factory_passes_custom_auth_flow` | `client_factory.py` умеет принять custom auth flow для one-shot MAX reauth без изменения runtime path. |
| `test_client_factory_can_disable_legacy_session_import` | Reauth path может отключить legacy PyMax 1 `auth` import, чтобы stale token не импортировался обратно. |
| `test_pymax_msgpack_codec_tolerates_array_map_keys` | Backend-local PyMax codec не падает на raw msgpack map с array-like key, который встречается в некоторых `CHAT_HISTORY` ответах. |
| `test_pymax_sequence_guard_wraps_to_one_byte` | Backend-local PyMax connection guard заворачивает TCP `seq` в one-byte диапазон, чтобы framer не падал после 255 requests. |
| `test_client_factory_installs_bridge_protocol_guards` | `client_factory.py` ставит bridge msgpack и sequence guards на PyMax connection/protocol при создании клиента. |
| `test_pymax2_session_store_imports_legacy_pymax1_auth_table` | `session_store.py` импортирует legacy PyMax 1 `auth(token, device_id)` в PyMax 2 `sessions`, чтобы сохранить existing session без SMS-auth. |
| `test_pymax2_session_store_can_skip_legacy_pymax1_auth_import` | `session_store.py` по явному флагу не импортирует stale legacy token и позволяет начать SMS-flow. |
| `test_pymax2_session_store_can_clear_saved_sessions` | `session_store.py` умеет очищать PyMax 2 `sessions` перед интерактивным reauth. |
| `test_reauth_close_after_success_suppresses_ssl_shutdown_noise` | One-shot reauth не считает PyMax SSL close noise ошибкой после успешной авторизации. |
| `test_reauth_done_callback_suppresses_close_task_noise` | Callback фоновой остановки reauth гасит ожидаемый close noise без `startup task failed`. |
| `test_max_reauth_refuses_fresh_bridge_heartbeat` | Reauth guard не даёт запускать SMS-flow рядом с живым bridge heartbeat без явного `--force`. |
| `test_max_reauth_snapshot_session_db_copies_without_token_output` | Перед reauth создаётся `session.db.before-reauth-*` snapshot с правами `0600`, без чтения/печати token. |
| `test_pymax2_login_payload_drops_unsupported_attachments` | `login.py` удаляет unsupported attachments из initial sync payload до strict PyMax 2 `LoginResponse` validation. |
| `test_pymax2_handler_signatures_are_adapted_to_bridge_callbacks` | PyMax 2 callbacks `(event, client)` адаптируются к bridge callbacks без pymax types снаружи. |
| `test_pymax2_raw_gateway_converts_frames_and_invokes_app` | Native `on_raw` конвертируется в bridge raw dict, а raw requests изолированы через `_app.invoke`. |
| `test_pymax2_send_uses_attachments_list` | Outbound media отправляется через PyMax 2 `attachments=[...]`, не старый `attachment=`. |
| `test_pymax2_snapshots_use_profile_users_and_chat_types` | Own id берётся из `client.me.contact.id`, users/chats snapshots нормализуются через PyMax 2 shape. |
| `test_pymax2_egress_transport_uses_configured_socket_connector` | Custom PyMax 2 transport использует `MaxEgressProfile.socket_connector` и сохраняет TLS server hostname. |
| `test_pymax2_egress_client_uses_bridge_connection_manager` | Egress PyMax client строит guarded `BridgeConnectionManager`, а не upstream `ConnectionManager` с 32-bit seq. |
| `test_max_adapter_can_be_composed_with_fake_backend` | `MaxAdapter` можно собрать с fake backend через internal injection point без изменения публичного constructor use-case. |

---

## test_max_service_ports.py — MAX client ports (4 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_resolve_service_uses_client_port_snapshots_without_pymax_attrs` | `MaxResolveService` работает через typed snapshots/methods, а не через `contacts`/`_users` shape. |
| `test_send_service_uses_outbound_port_method` | `MaxSendService` вызывает `send_outbound_message(...)` и получает `MaxSendResult`, не создавая library attachments сам. |
| `test_media_service_uses_raw_request_port_for_audio_probe` | `MaxMediaService` делает protocol probe через `raw_request(...)`, без прямого `_send_and_wait`. |
| `test_events_service_installs_raw_interceptor_through_port` | `MaxEventsService` ставит raw interceptor через client port, без доступа к pymax notification handler. |

---

## test_bridge_core.py — роутинг MAX→TG и TG→MAX

Используют stub-классы `DummyMax`, `DummyTelegram`, `DummyRepo`, `DummyConfig`. Нет I/O, нет сети.

### Пересылка сообщений

| Тест | Что проверяет |
|------|--------------|
| `test_forward_to_telegram_sends_media_then_rendered_system_text` | Сообщение с видео-вложением и `rendered_texts`: сначала отправляется видео (`send_video` с caption `[Имя]`), затем текст системного события (`send_text`). Возвращает `message_id` медиа. |
| `test_forward_to_telegram_sends_voice_note_for_voice_source` | Вложение с `source_type=VOICE` отправляется как нативный `send_voice` (voice bubble), а не как обычное аудио. |
| `test_forward_to_telegram_uses_rendered_text_without_media` | Сообщение типа `CONTROL` без вложений: отправляется только текст из `rendered_texts`. Файловые методы не вызываются. |
| `test_on_max_message_queues_text_when_tg_send_fails` | Retryable MAX→TG text delivery failure попадает в durable inbound outbox без увеличения failed-счётчика. |
| `test_pending_inbound_worker_delivers_and_clears_text` | Inbound text worker досылает текст в Telegram, сохраняет mapping/delivery и очищает plaintext. |
| `test_on_tg_reply_prefixes_sender_name_for_max` | Reply из Telegram: текст отправляется в MAX с префиксом `[Мария Иванова]\nПроверка связи`; `reply_to_msg_id` разрешается через `get_max_msg_id_by_tg`. |
| `test_on_tg_reply_rejects_too_large_media` | TG→MAX: если файл превышает лимит `max_file_size_mb`, bridge не отправляет его в MAX и отдаёт явное сообщение в топик. |
| `test_on_tg_reply_logs_forward_completion` | После успешной доставки TG→MAX в логах присутствует событие `bridge.outbound.forward_finished` с `outcome=delivered`. |
| `test_on_tg_reply_queues_definite_unsent_text_after_max_error` | Definite unsent TG→MAX text failure попадает в durable outbox и получает queued notice в Telegram. |
| `test_on_tg_reply_does_not_queue_ambiguous_ack_timeout` | Ambiguous ack timeout не ставится на автоповтор, чтобы не продублировать сообщение в MAX. |
| `test_pending_outbound_worker_delivers_and_clears_text` | Outbox worker досылает текст после восстановления MAX, сохраняет mapping/delivery и очищает plaintext. |
| `test_on_tg_reply_logs_too_large_outbound_failure` | Явно отклонённый oversized TG→MAX файл тоже фиксируется в `delivery_log`, а не только показывается в Telegram topic. |
| `test_on_tg_reply_does_not_persist_failed_media_for_retry` | TG→MAX media failure не сохраняет файл/текст в outbox и просит переотправить вручную. |
| `test_on_max_message_enqueues_retryable_video_failure` | Частично доставленное MAX-сообщение с retryable video failure отправляет фото сразу, показывает queued-placeholder и создаёт `pending_media_downloads` job. |
| `test_existing_pending_audio_failure_does_not_duplicate_placeholder` | Повторный replay того же voice по `media_msg_id/reference_id` переиспользует активный pending job и не отправляет второй queued-placeholder. |
| `test_pending_media_worker_delivers_video_and_maps_reply` | Retry worker скачивает отложенное видео, отправляет `send_video`, закрывает job и сохраняет reply mapping на исходный MAX message. |
| `test_pending_media_worker_reschedules_download_failure` | Временный сбой скачивания переводит job в `retry` с увеличенным attempts и будущим `next_attempt_at`. |
| `test_pending_media_worker_marks_missing_reference_terminal` | Job без стабильного `video_id` становится terminal failure, а не крутится бесконечно. |
| `test_on_tg_reply_to_delayed_video_uses_original_max_message` | Reply на позднее досланное видео резолвится в исходный MAX `max_msg_id`. |
| `test_on_tg_reply_after_remap_skips_stale_reply_to_max_id` | После `/recovery remap` reply на старое TG сообщение не отправляет `reply_to_msg_id`, если исходный MAX message принадлежит старому `max_chat_id`. |
| `test_get_or_create_topic_resolves_group_title_via_live_max_lookup` | Если `chat_title=None`, `_get_or_create_topic` делает live запрос `resolve_chat_title` и создаёт топик с правильным именем. |
| `test_get_or_create_topic_prefers_dm_sender_name_for_title` | Для входящего DM topic создаётся по `sender_name`, если он уже есть в сообщении. |
| `test_get_or_create_topic_uses_dm_sender_id_before_chat_id` | Для нового входящего DM `sender_id` пробуется раньше `chat_id`, потому что `chat_id` может быть id диалога. |

### Статус и чаты

| Тест | Что проверяет |
|------|--------------|
| `test_build_chats_message_lists_topics_with_activity` | `/chats` показывает чат, topic_id, режим и счётчики `↓/↑` за период. |
| `test_build_status_message_includes_max_issue_summary` | `/status` показывает текущую MAX-проблему и необходимость `reauth`, если адаптер сообщил о деградации сессии. |
| `test_build_status_message_includes_safe_egress_probe` | `/status` показывает последнюю Channel M probe stage/latency/error без credentials. |
| `test_build_status_message_refreshes_home_proxy_egress_probe` | `/status` перед рендером обновляет probe для `home_ru_proxy`, чтобы не показывать stale failure после восстановления. |
| `test_build_status_message_uses_shared_health_snapshot` | `/status` читает единый persisted health snapshot и отражает runtime/max health из supervisor-контура. |
| `test_recovery_auto_changes_are_summarized_in_status_not_notified` | Обычные recovery auto-scan дельты не отправляют отдельный alert, но `/status` показывает агрегаты без invite/title/phone/raw payload. |
| `test_watchdog_sends_gap_notice_after_reconnect` | После offline-окна watchdog отправляет и alert про downtime, и уведомление о возможном `missed messages gap` после восстановления. |
| `test_max_watchdog_reports_egress_down_without_restart` | При упавшем `home_ru_proxy` watchdog пишет `max_egress_unavailable`, но не рестартит процесс. |
| `test_max_watchdog_restarts_once_when_proxy_ok_but_max_stays_offline` | При healthy proxy/TLS и зависшем MAX watchdog пишет cooldown-файл и запускает rate-limited self-exit. |
| `test_dm_history_sweep_skips_until_max_is_ready` | DM history sweep не стреляет `CHAT_HISTORY` raw requests до `MAX connected`, чтобы не шуметь `Not connected`/pending futures на старте. |
| `test_dm_history_sweep_uses_warmup_interval` | После ready sweep работает в warmup-фазе с коротким интервалом. |
| `test_dm_history_sweep_uses_steady_interval_after_warmup` | После warmup window sweep переходит на спокойный steady interval. |

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

### Команда `/recovery`

| Тест | Что проверяет |
|------|--------------|
| `test_cmd_recovery_scan_report_set_remap_and_export` | Owner-only recovery flow: scan, report со свежестью snapshot, отсутствие invite link в report/logs, ручной `set`, `remap`, owner-DM export и обновление binding. |
| `test_new_binding_recovery_scan_is_async_and_does_not_delay_forwarding` | Новый `ChatBinding` ставит recovery scan в background task; Telegram topic/message создаются сразу и не ждут snapshot, а auto scan обновляет DM contact registry асинхронно. |
| `test_control_events_debounce_into_one_recovery_scan` | Повторные MAX `CONTROL` события схлопываются в один recovery scan. |
| `test_recovery_account_migration_notification_is_redacted_and_deduped` | Срочный recovery alert остаётся только для MAX account migration-required, не раскрывает phone/session hash и дедупится в памяти. |
| `test_recovery_scan_updates_dm_contact_registry_and_report` | `/recovery scan` обновляет chat registry и DM contact registry вместе; `/recovery report` показывает только агрегаты DM contacts, а owner-only export содержит контактный snapshot. |

### Персистирование пользователей

| Тест | Что проверяет |
|------|--------------|
| `test_on_max_message_persists_sender_to_db` | При входящем сообщении от другого пользователя `save_user()` вызывается с правильными `sender_id` и `sender_name`. |
| `test_on_max_message_does_not_persist_own_sender` | Собственные сообщения (`is_own=True`) не сохраняются в `known_users`. |

---

## test_max_session_store.py — MAX session snapshots (3 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_backup_current_writes_valid_snapshot_and_skips_unchanged` | Валидный `session.db` snapshot сохраняется, а неизменённая сессия не плодит дубликаты. |
| `test_recover_if_needed_repairs_header_corruption_without_losing_token` | При SQLite header corruption store пытается пересобрать clean session из текущего token. |
| `test_recover_if_needed_restores_latest_valid_snapshot` | Если текущая сессия невосстановима, store откатывается на свежий валидный snapshot. |

---

## test_main.py — точка входа (6 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_mask_ip_hides_third_octet` | `_mask_ip("203.0.113.217")` → `"203.0.*.217"` (третий октет заменяется `*`). |
| `test_infer_location_from_hetzner_hostname` | `_infer_location("ubuntu-4gb-hel1-6")` → `"Helsinki"` (из маппинга токенов имён датацентров). |
| `test_extract_pytest_summary_uses_terminal_summary` | Из stdout `pytest` извлекается итоговая строка вида `"17 passed in 1.49s"` для последующего включения в startup-уведомление. |
| `test_setup_logging_writes_to_data_bridge_log` | `setup_logging()` создаёт `${DATA_DIR}/bridge.log` и пишет туда структурированные runtime-логи. |
| `test_build_startup_notification_includes_runtime_details` | Стартовое уведомление содержит `"Maxgram запущен и подключён к MAX"`, `runtime: Docker`, hostname, `location: Helsinki`, masked IP. Использует `monkeypatch` для `socket.gethostname`, `Path.exists`, `_detect_primary_ipv4`. |
| `test_build_startup_notification_includes_startup_test_status` | В startup-уведомление добавляется строка вида `"Тесты запуска: ✅ 17 passed in 1.49s"`, если production self-check завершился успешно. |

---

## test_tg_adapter.py — входящие сообщения Telegram и system notifications (7 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_dispatch_incoming_message_accepts_non_owner_group_member` | Сообщение от не-владельца (user_id=2) в форум-группе в топике → передаётся в reply-обработчик с правильными `(topic_id, text, reply_to_tg_id, sender_name)`. |
| `test_dispatch_incoming_message_ignores_non_owner_commands` | Команда `/status` от не-владельца в форум-группе → `_handle_command` не вызывается. |
| `test_dispatch_incoming_message_allows_public_dm_only_in_general` | `/dm` доступна участникам группы только в General (`message_thread_id=None`) через explicit allowlist. |
| `test_dispatch_incoming_message_keeps_recovery_owner_only_in_general` | `/recovery` остаётся owner-only даже в General, чтобы export/invite/admin metadata не раскрывались группе. |
| `test_tg_retry_logs_retry_and_success` | `_tg_retry` делает повторную попытку при `TelegramRetryAfter` и логирует событие retry; после успеха возвращает корректный результат. |
| `test_send_system_notification_fans_out_to_dm_and_ops_topic` | Системное уведомление уходит и в owner DM, и в ops topic, если `ops_topic_id` задан. |
| `test_send_system_notification_queues_failed_target_and_flushes_outbox` | Неотправленный ops-alert попадает в `alert_outbox.jsonl`, а после восстановления Telegram досылается из outbox. |

---

## test_logging_utils.py — structured logging privacy (4 теста)

| Тест | Что проверяет |
|------|--------------|
| `test_sanitize_preview_masks_digits_and_newlines` | Preview sanitization маскирует длинные цифровые последовательности и убирает переносы строк. |
| `test_sanitize_url_strips_query_parameters` | URL sanitizer оставляет origin/path, но удаляет query-параметры с token/signature. |
| `test_event_formatter_mixed_renders_key_val_fields` | Mixed formatter пишет event и key-value поля в читаемом формате. |
| `test_event_formatter_json_renders_valid_json_line` | JSON formatter выдаёт валидную JSON-строку со структурированными полями события. |

---

## test_runtime_health.py — runtime health / supervisor (7 тестов)

| Тест | Что проверяет |
|------|--------------|
| `test_report_issue_deduplicates_same_signature` | Повтор той же причины деградации не создаёт новый alert-state-change и не должен спамить оператора. |
| `test_mark_healthy_after_issue_returns_recovered_and_clears_issue` | Переход `degraded → healthy` формирует `recovered`, очищает active issue и готовит recovery alert. |
| `test_health_snapshot_schema_version_and_mismatch_falls_back` | `health_state.json` пишет `schema_version`; несовместимый persisted snapshot не валит старт и заменяется fresh state. |
| `test_supervisor_restarts_worker_and_writes_heartbeat` | Supervisor перезапускает упавший worker, увеличивает restart counter и пишет heartbeat для Docker healthcheck. |
| `test_supervisor_stops_worker_without_crash_alert` | Intentional shutdown через stop event не шлёт crash alert и корректно закрывает worker. |
| `test_supervisor_restart_delay_is_capped` | Exponential backoff с jitter растёт до cap и не превращает сетевой flap в сотни рестартов. |
| `test_logged_detached_task_reports_exception` | Detached task exception логируется с traceback, а не теряется как unhandled asyncio task. |

---

## Что не покрыто тестами

- `run_periodic_status` — бесконечный цикл; проверяется вручную в production
- `run_max_watchdog` покрыт базовым reconnect-сценарием, но full production-поведение также проверяется вручную
- `run_weekly_recovery_snapshot` как бесконечный scheduler-loop проверяется через командный scan/repo tests и вручную в production
- Реальные сетевые вызовы (MAX WebSocket, Telegram Bot API)
- Полный live MAX recovery snapshot зависит от реального `pymax` cache/API; unit tests покрывают fake chat/dialog/channel objects и DM contacts из typed dialog snapshots.

Смоук-проверка по реальной БД:
```bash
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```
