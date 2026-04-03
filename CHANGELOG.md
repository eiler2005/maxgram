# Changelog

All notable changes to Maxgram are documented here.

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
