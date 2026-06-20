# Changelog

All notable changes to Maxgram are documented here.

---

## Unreleased

### Added
- **MAX join and link buttons in Telegram** — `SHARE`, `inline_keyboard`, nested `web_app.url` / `buttons[].url`, and msgpack text URLs now become Telegram inline buttons. `max.ru/join/...` links create an owner-only `Вступить в MAX` callback that joins through PyMax; external sites use normal URL buttons and are not persisted in SQLite.
- **Encrypted contacts snapshot for new-number recovery** — owner-only `/recovery contacts status`, `/recovery contacts snapshot [--force]`, and `/recovery contacts import dry-run|apply` support PyMax `import_contacts()` migration without writing raw phone numbers to SQLite, logs, health, reports, or normal exports.
- **Durable text outbox in both directions** — failed text-only TG→MAX and MAX→TG deliveries now use SQLite-backed queues with lease/backoff/TTL. Plaintext is kept only while pending and is cleared after delivery or expiration.
- **Shared retry policy module** — bridge retry workers share one lease/backoff/TTL policy, while media remains reference-based and heavy files are not stored in SQLite.
- **MAX account migration recovery registry** — new-phone/new-account recovery is now a first-class subsystem. SQLite stores MAX account generations, topic recovery registry rows, snapshot freshness (`last_scan_at`), and append-only recovery events without message text or raw MAX payloads.
- **DM contact recovery snapshot** — recovery now stores personal contacts only from real MAX DM dialogs or already bound DM topics, with old/current DM chat ids, linked topic, status, and freshness; it does not copy the full MAX address book or `known_users`.
- **Owner-only `/recovery` commands** — `/recovery scan`, `/recovery report`, `/recovery export`, `/recovery set`, and `/recovery remap` support guided migration of existing Telegram topics to newly visible MAX chats.
- **Hybrid recovery snapshots** — bridge scans recovery metadata after successful MAX connect/reconnect, weekly as a safety net, and asynchronously after important MAX-side events: new bindings, title changes, and `CONTROL` events.
- **Quiet recovery status summary** — automatic recovery scans fold routine deltas into the 4-hour status report; owner/ops gets an immediate alert only when a MAX account migration is required.
- **Architecture decision ADR-005** — documents the account migration recovery registry, privacy constraints, remap behavior, and V1 non-automation boundaries.

### Added
- **TypingEvent → Telegram typing indicator** — when a MAX user starts typing, the bridge sends a Telegram `typing` chat action to the bound topic (best-effort, no DB write). Unbound chats are silently ignored.
- **ReactionUpdateEvent mirroring** — standalone MAX reaction-change events are now dispatched to the bridge; the bridge fetches the mapped Telegram message and edits it to reflect a compact reaction summary (emoji + count). Unmapped messages are silently ignored.
- **MessageReadEvent / PresenceEvent diagnostics** — read-receipt and presence events from MAX are subscribed and logged at DEBUG level for observability. No Telegram-side expression; no topic creation.
- **Precise message recovery via `get_message()`** — empty-event voice/forward recovery now attempts the new PyMax 2.2.0 `get_message(chat_id, message_id)` API first, bypassing the time-window history sweep when a precise fetch succeeds. The full fallback chain (cache → raw history payload → `history_messages`) is preserved.

### Changed
- **PyMax 2.3.1 upgrade** — `maxapi-python` is pinned to 2.3.1 and `zstandard` is installed for upstream TCP payload decoding. The bridge pins the new `forward_message()` / `Message.forward()` surface and keeps its msgpack guard limited to serializer replacement, preserving PyMax 2.3.1 Zstandard/LZ4 payload decoding.
- **PyMax 2.3.0 upgrade** — `maxapi-python` is pinned to 2.3.0. The bridge pins the new PyMax surface (`ContactInfo`, `import_contacts()`, `on_disconnect()`, `on_error()`, `relogin()`, `delete_chat()`, `SessionStore.delete_all_sessions()`), uses `on_disconnect()` only for safe diagnostics, and keeps guarded reauth unchanged.
- **PyMax 2.2.0 upgrade** — `maxapi-python` is pinned to 2.2.0 (from 2.1.2). The 2.2.0 breaking change for `MessageDeleteEvent` (`chat` and `message` fields now optional; `chat_id` always present) is tolerated by the existing defensive `MaxClientMessage.from_object` extraction. Spurious empty-event recovery sweeps for delete events are prevented by an early exit when `raw_msg_id` is absent.
- **PyMax 2.1.2 upgrade** — `maxapi-python` is pinned to 2.1.2. Bridge login validation now accepts tokenless `LOGIN` responses without re-injecting the saved session token, while keeping backend-local sanitizers for unsupported initial-sync payload drift.
- **Telegram command access model** — `/dm` remains public only in General via an explicit allowlist; `/recovery ...` and other arg commands remain owner-only even in General.
- **Remap-safe reply routing** — after `/recovery remap`, replies to old Telegram messages no longer send stale MAX `reply_to_msg_id` values when the mapped MAX message belongs to the old chat id.
- **Recovery snapshot upsert deltas** — `upsert_recovery_snapshot()` now returns `inserted`, `status_changed`, `unmapped`, `needs_invite`, and `manual_admin_required`, and stores a redacted scan reason in recovery events.
- **Recovery reports and exports** — `/recovery report` now includes aggregate DM contact counts/freshness, while owner-only export includes the full DM contact recovery list.
- **MAX video CDN User-Agent matching** — signed MAX/OK CDN downloads now distinguish `CHROME_IPHONE` from desktop Chrome and use an iOS Chrome `User-Agent`, preventing `400 Bad Request` failures when MAX issues iPhone Chrome video URLs.
- Download failure logs now include `src_ag`, `ua_family`, `http_status`, and `download_source`, while keeping signed CDN query parameters out of logged error strings.

### Fixed
- **MAX video duration metadata** — delayed and direct MAX video forwarding now normalizes millisecond video durations and falls back to MP4 `mvhd` metadata when MAX omits duration, preventing Telegram videos from showing `00:00` or multi-hour bogus durations.
- **Stale MAX TCP readiness** — `PymaxClientAdapter.is_connected()` now checks `ConnectionManager.is_open()` and `transport.connected`, so a closed TCP socket no longer looks healthy and `Not connected to the server` send failures can trigger reconnect recovery.
- **New DM topic names** — incoming DM topics now prefer the sender name / sender id over the chat id when resolving titles, so dialog ids are less likely to appear as `Чат <id>` for new users.
- **MAX→TG voice delivery for pymax-empty DM events** — raw MAX notifications are now intercepted on the pymax message-notification path as well as `on_raw_receive`, so `AUDIO` voice payloads can be forwarded before upstream parsing drops them.
- **Live empty-event recovery** — when pymax still emits a fresh empty `USER` event, the bridge tries a narrow recent-history lookup for that exact `msg_id` and forwards the recovered voice attachment if present. Diagnostic logs include only safe class/field names.
- **Top-level MAX voice payloads** — raw notifications where `payload` itself is the message and media is stored under `attachments` are now normalized before pymax can drop the attachment list.
- **Forwarded/channel MAX recovery** — empty-event recovery now unwraps `CHANNEL`/`FORWARD` history candidates with nested `message` or `link.message` before deciding they are contentless; raw receive also forwards direct `CHANNEL` wrappers with media instead of logging them as missing-chat-id metadata.
- **Forwarded media source fallback** — MAX forwarded payloads with source `chatId=0` now fall back to the receiving chat id while keeping the nested media message id; pending video retry also tries the wrapper message id if MAX returns `not.found`.

### Tests
- Added coverage for MAX share/inline-keyboard URL extraction, Telegram URL buttons, owner-only MAX join callbacks, callback action SQLite lifecycle, and PyMax join group/channel fallback.
- Added regressions for MAX video duration normalization and MP4 metadata fallback on durable video retry.
- Added PyMax 2.3.1 surface pinning for `forward_message()`, `Message.forward()`, `ForwardMessagePayload`, `TcpPayloadDecoder`, and `ZstdCompression`, plus a bridge protocol regression that keeps upstream Zstandard decoding while installing the bridge msgpack guard.
- Added PyMax 2.3.0 surface pinning and coverage for encrypted recovery contact snapshot round-trip, missing/corrupt key handling, `0600` file mode, phone filtering/privacy, import dry-run no-write, import apply registry upsert, and safe `on_disconnect()` diagnostics.
- Added PyMax 2.1.2 runtime version pinning and a regression test for tokenless `LoginResponse` validation.
- Added coverage for durable inbound/outbound text queues, plaintext clearing after delivery, stale MAX transport readiness, non-queued ambiguous ack timeouts, and non-persisted TG→MAX media failures.
- Added coverage for DM title resolution order, cached contact name lookup, raw message interceptor, duplicate suppression, top-level raw audio payloads, and recent-history recovery of typed-empty MAX voice events.
- Added coverage for direct-media `CHANNEL` wrappers, forwarded empty-recovery candidates with nested `message` / `link.message` payloads, and zero-source forwarded media fallback.
- Added coverage for SQLite recovery migrations/idempotency/deltas, DM contact recovery upsert/export/privacy, recovery report/export/remap, MAX recovery snapshot collection, async event-driven recovery scans, quiet status-summary recovery alerts, account-migration notification privacy/deduplication, owner-only `/recovery`, command allowlist privacy, stale reply routing after remap, and privacy of recovery reports/logs.
- Added PyMax 2.2.0 surface pin, `MessageDeleteEvent` shape regression test (chat_id only, no crash/recovery), typing/reaction/read/presence handler dispatch tests, and precise `get_message()` recovery test (history sweep skipped on precise hit).

---

## [1.1.7] — 2026-04-24

### Added
- **Ansible automation для prod-операций** — `infra/ansible/` с пятью playbook'ами (`deploy`, `backup`, `recover`, `bootstrap`, `hardening`), повторяющими ручной runbook без отклонений: rsync релиз-бандла, `docker compose build/up -d` без `down`, polling Docker healthcheck до `healthy`, smoke check по `bridge.db`. `bootstrap`/`hardening` — только для новых VM, текущий prod не трогается. Inventory с реальным IP — в `.gitignore`. Quickstart — в `infra/ansible/README.md`.

### Changed
- Раздел "Запуск через Ansible" добавлен в `docs/runbooks/operations.md`; `infra/ansible/` упомянут в карте файлов `CLAUDE.md`.
- `deploy.yml --check --diff` теперь явно документирован как preflight verify текущего состояния без `docker compose build/up`, чтобы dry-run semantics не обещали несуществующую симуляцию контейнерного rollout.

---

## [1.1.6] — 2026-04-19

### Changed
- **TG→MAX transport retry** — outbound MAX sends now retry temporary transport/session failures (`Socket is not connected`, `Must be ONLINE session`, timeout, broken pipe, reset) up to 3 attempts with short backoff before surfacing an error to Telegram.
- **Outbound failure audit trail** — failed Telegram→MAX deliveries are now always persisted into `delivery_log` with a synthetic `out_fail:<topic_id>:<tg_msg_id>` id, stored error reason, and real `attempts` count, so operational queries no longer miss visible send failures.

### Tests
- Added coverage for successful retry after a temporary MAX transport failure.
- Added coverage for exposing the final MAX error after all retries are exhausted.
- Added coverage for persisting failed outbound deliveries with the MAX error and attempt count.
- Added coverage for persisting oversized outbound media rejection into `delivery_log`.

---

## [1.1.5] — 2026-04-10

### Changed
- **MAX channel/forward forwarding** — `CHANNEL` and forward wrappers are unwrapped into the real forwarded text and media. Media downloads now use the source `chat_id/message_id` from the forwarded payload, and duplicate pymax wrapper events are suppressed.
- **Unknown MAX message diagnostics** — unsupported message shapes now render as `[Неизвестное сообщение MAX]` with `type`, `status`, `link_*`, text/attachment counts, attachment types, and raw field names instead of a short system placeholder.

### Tests
- Added coverage for `link.message` forward unwrap with media source ids.
- Added coverage for raw `CHANNEL` wrapper unwrap and duplicate suppression.
- Added coverage for detailed unknown MAX message fallback.
- All 66 tests pass.

---

## [1.1.4] — 2026-04-05

### Added
- **`/chats` command** — owner can request a compact list of configured chats with topic id, mode, and per-chat message counters (`↓ inbound`, `↑ outbound`) for the selected period.
- **Native voice note bubbles (MAX→TG)** — attachments with source type `VOICE` are now sent via Telegram `send_voice`, so they render as voice notes instead of generic audio files.
- **Missed messages gap notice** — after MAX reconnect (following a watchdog alert), bridge sends an additional warning that messages from downtime may be missing because history replay is unavailable.

### Changed
- **Explicit TG→MAX large file handling** — outbound media above configured `max_file_size_mb` is now rejected early with a clear message in the topic (`Файл слишком большой...`), instead of a silent failure path.
- **Post-download media validation** — downloader now checks `Content-Type` and binary signatures (magic bytes) and rejects HTML/text fallbacks for expected media, preventing `.html` player pages from being forwarded as media.

### Tests
- Added coverage for `/chats` formatter and per-chat activity SQL aggregation.
- Added coverage for `VOICE` → `send_voice` routing.
- Added coverage for watchdog reconnect gap-notice flow.
- Added coverage for TG→MAX explicit oversized-file rejection.
- Added coverage for downloader HTML fallback rejection.
- All 39 tests pass.

---

## [1.1.3] — 2026-04-05

### Changed
- **Unified MAX attachment type normalization** — alias media types such as `IMAGE`, `VOICE`, `DOCUMENT`, and `DOC` are now normalized through one shared mapper used by both top-level attachment dispatch and the download pipeline.
- **Readable `joinbylink` control events** — MAX `CONTROL/joinbylink` is now rendered as a human-readable join message (`Присоединились по ссылке: ...`) instead of raw event text.

### Tests
- Added coverage for `CONTROL/joinbylink` rendering.
- Added coverage for alias attachment type normalization (`IMAGE→PHOTO`, `VOICE→AUDIO`, `DOCUMENT/DOC→FILE`).
- All 32 tests pass.

---

## [1.1.2] — 2026-04-04

### Fixed
- **MAX→Telegram video forwarding hardened** — `VIDEO_PLAY` payloads may contain both an HTML player URL (`EXTERNAL`) and one or more real media URLs (`MP4_*`). The bridge now prefers downloadable media variants over the external player page, so videos no longer arrive in Telegram as `.html` documents.
- **MAX CDN user-agent handling** — signed `okcdn` video URLs now use a user-agent that matches the `srcAg` query parameter (`CHROME` vs mobile Safari), which fixes `400 Bad Request` responses on valid MAX video links.
- **Temporary media download directory creation** — `_download_from_url()` now creates the temp directory before writing the downloaded file, avoiding false download failures on a clean runtime.

### Tests
- Added coverage for preferring `MP4_*` URLs over `EXTERNAL` HTML player links
- Added coverage for adaptive CDN user-agent selection based on `srcAg`
- All 27 tests pass

---

## [1.1.1] — 2026-04-04

### Changed
- **Production runtime upgraded to Python 3.13** — production Docker image now uses `python:3.13-slim` because `pymax` socket connections were unstable on Python 3.12
- **Production restart policy hardened** — `docker-compose.prod.yml` now uses `restart: always`, so the bridge container is recreated automatically after a VM reboot
- **Startup notification now includes self-check status** — after the first successful `MAX connected`, the bridge runs the local regression suite inside the container and appends the `pytest` summary to the Telegram startup message

### Tests
- Added startup-notification coverage for embedded startup test status
- Added parser coverage for `pytest` terminal summary extraction
- All 19 tests pass

---

## [1.1.0] — 2026-04-03

### Added
- **Media forwarding TG→MAX** — photos, video, audio, voice, documents sent via pymax `attachment=` API; files are downloaded from Telegram Bot API and passed directly to MAX
- **Telegram API retry + exponential backoff** — all TG send calls retry up to 3 times (delays 1s / 2s); `TelegramRetryAfter` respected (waits the exact `retry_after` value)
- **MAX offline watchdog** — background task alerts the owner if MAX is unreachable for more than 60 seconds
- **`/status` command** — returns uptime, message counts (inbound/outbound, text/media split), errors, and top-10 active chats over the last 4 hours; works in both forum group and personal DM with the bot
- **Periodic 4-hour status report** — automatic delivery stats sent to the owner without any manual command
- **Extended startup notification** — now includes runtime (Docker/Local), hostname, inferred datacenter location, masked IP, and active chat count
- **`/status` in personal DM** — owner can send `/status` directly to the bot in a private chat, not only in the forum group

### Fixed
- **Group sender names were empty** — `message.sender` is a bare `int`; name is now resolved via `client.get_cached_user()` + live `get_users()` API fallback
- **Own-message echo** — messages sent directly in MAX were not mirrored to Telegram; fixed by storing the real `max_msg_id` returned by `send_message` in DB, so echo detection works correctly

### Tests
- Fixed `DummyTelegram` missing `on_command` stub in `test_bridge_core.py`
- Fixed `build_startup_notification` async signature in `test_main.py`
- Fixed `_make_message` missing media attributes in `test_tg_adapter.py`
- All 17 tests pass

---

## [1.0.0] — 2026-04-02

### Production milestone: bridge deployed to Hetzner Cloud

### Added
- MAX WebSocket userbot via `pymax` (`SocketMaxClient`)
- Telegram Forum Supergroup topic management via `aiogram` 3.x
- Bidirectional routing: MAX → Telegram topics, Telegram replies → MAX
- Auto-creation of Telegram topics on first message from new MAX chat
- DM topic naming from MAX contact profile (`user.names[0].first_name`)
- Fallback title auto-rename: `"Чат XXXXX"` → real name on next incoming message
- Per-chat modes: `active` / `readonly` / `disabled`
- Idempotent message deduplication via SQLite `message_map`
- Reply routing: Telegram reply-to-message → `send_message(reply_to=max_msg_id)`
- Sender name prefix in group chats: `[First Last] message text`
- Own-message forwarding: messages sent directly in MAX are mirrored to Telegram with `[Вы]` prefix
- Media forwarding: photos, documents, video, audio
- System event rendering: `ControlAttach` (join/leave/add), `ContactAttach`, `StickerAttach`
- Outbound retry on reconnect: wait up to 15s if MAX is reconnecting
- Docker Compose deployment (local + production variant)
- Hetzner Cloud production setup with UFW, fail2ban, non-root container
- Startup notification with runtime, host, location, masked IP
- SQLite smoke-check script (`scripts/smoke_check.py`)
- Regression test suite: routing, dedup, media, system events, reply filtering

### Fixed (critical bugs discovered in production)
- **pymax OOM**: `reconnect=True` causes unbounded growth of `chats`/`dialogs` lists → replaced with outer `while True` loop and fresh client per reconnect
- **SSL storm**: default `send_fake_telemetry=True` triggers `TLSV1_ALERT_RECORD_OVERFLOW` on every connect → set `send_fake_telemetry=False`
- **`sender_name=None`**: `message.sender` is an `int`, not a User object → use `client.get_cached_user(int(sender_id))`
- **send_message fails on reconnect**: socket not ready → retry 3×5s before failing
- **Startup notification spam**: `on_start` fires on every reconnect → guarded with `first_connect` flag

### Architecture decisions
- [ADR-001](docs/decisions/ADR-001-unofficial-userbot.md) — Unofficial userbot over official Bot API
- [ADR-002](docs/decisions/ADR-002-telegram-forum-topics.md) — Forum Supergroup + Topics as UI
- [ADR-003](docs/decisions/ADR-003-python-monolith-sqlite.md) — Python async monolith + SQLite
- [ADR-004](docs/decisions/ADR-004-pymax-reconnect-strategy.md) — Fresh client per reconnect

---

*Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).*
