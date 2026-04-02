# Changelog

All notable changes to MaxBridge are documented here.

---

## [Unreleased]

### In Progress
- Telegram API retry with exponential backoff
- `/status` command with uptime and message statistics
- Alert on MAX session loss (>3 consecutive reconnect failures)

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
