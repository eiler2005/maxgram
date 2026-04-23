# Maxgram

**Personal MAX → Telegram bridge for your school and community chats**

[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Messenger](https://img.shields.io/badge/Messenger-MAX-orange)](https://max.ru)
[![Bot](https://img.shields.io/badge/Bot-Telegram-2CA5E0)](https://core.telegram.org/bots)
[![Python](https://img.shields.io/badge/Python-3.13%2B-blue)](https://www.python.org)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](https://github.com/eiler2005/maxgram)

> *Your chats route themselves. Silently.*

**MAX** is a mandatory messenger in Russian schools and public institutions. It has no official Telegram client and no open API. **Maxgram** solves this by running an unofficial WebSocket userbot that mirrors every MAX chat into a Telegram Forum Supergroup topic — and routes replies back.

**Result:** read and answer all MAX conversations from Telegram, without installing MAX.

**[Русская версия / Russian version →](README-ru.md)**

---

## How It Works

```
MAX (personal account)        Telegram Forum Supergroup
┌───────────────────┐         ┌──────────────────────────┐
│ DM: Contact       │────────►│  📁 Contact Name          │
│ Group: School     │────────►│  📁 School Group          │
│ Group: Sports     │────────►│  📁 Sports Club           │
│ ...               │         │  📁 ...                   │
└───────────────────┘         └──────────────────────────┘
         ▲                               │
         └──────── Reply in topic ────────┘
```

Each MAX chat (DM or group) becomes a separate Telegram topic, created automatically on first message. Replying in a topic sends the message back to MAX.

---

## Engineering Highlights

- **Unofficial WebSocket API** — reverse-engineered `pymax` userbot with a custom reconnect loop that fixes an OOM bug in the upstream library (`reconnect=False` + outer `while True`)
- **Idempotent message deduplication** — `max_msg_id` is written to SQLite *before* forwarding to Telegram, making the system safe to restart at any point without duplicates
- **Privacy-first design** — no message text or media is ever stored; SQLite only holds routing metadata (chat bindings, message ID map, delivery log)
- **Production-deployed** — running on Hetzner Cloud behind Docker Compose with UFW, fail2ban, non-root container, and SSH-key-only access
- **Supervisor runtime shell** — PID1 is now a supervisor that keeps the container `Up`, restarts the bridge worker with backoff, and persists health state even when MAX/TG integration degrades
- **Resilient delivery** — Telegram API calls retry with exponential backoff; temporary TG→MAX transport failures retry automatically; failed outbound deliveries are written to SQLite with attempt counts; MAX watchdog alerts on offline > 60s; `/status` gives live health snapshot on demand
- **Persistent health model** — `health_state.json`, `health_events.jsonl`, `alert_outbox.jsonl`, and `health_heartbeat.json` make degraded-vs-dead runtime states explicit

---

## Features

- Automatic topic creation for every new MAX chat
- Bidirectional messaging — replies in Telegram → delivered to MAX (including reply-to-message)
- Media forwarding in both directions: photos, video, audio, voice, documents
- MAX video downloads prefer real `MP4_*` streams over `EXTERNAL` player pages and use an adaptive CDN user-agent (`CHROME` vs mobile Safari)
- MAX downloader validates `Content-Type` + file signature and rejects HTML/text fallbacks for expected media
- MAX `CHANNEL`/forward wrappers are unwrapped into the real forwarded text and media instead of a generic system placeholder
- Unknown MAX message shapes are forwarded with diagnostic metadata (`type`, `link_*`, counts, raw field names) so new formats can be fixed from the next occurrence
- MAX attachment aliases (`IMAGE`, `VOICE`, `DOCUMENT`, `DOC`) are normalized consistently across dispatch and download stages
- MAX `VOICE` attachments are delivered as native Telegram voice notes (`send_voice` bubbles)
- Control events are rendered in human-friendly text (including `joinbylink` join notifications)
- Sender name prefix in group chats: `[First Last] message text`
- Own messages (sent directly in MAX) mirrored to Telegram with `[Вы]` prefix
- DM topics named after the contact (resolved from MAX profile, cache + live API)
- Per-chat modes: `active` / `readonly` / `disabled`
- Deduplication — no duplicate messages on reconnect
- Stable reconnect — no OOM, no SSL storm
- `/status` command — uptime, message stats, top active chats; works in forum group and personal DM with bot
- `/chats` command — list of bridged chats with topic id, mode, and inbound/outbound counters
- Periodic 4-hour status report — automatic delivery stats sent to owner DM, with optional fanout to a dedicated forum topic
- MAX offline watchdog — alert if MAX unreachable > 60 seconds
- Reconnect gap warning — after recovery, owner gets a reminder about possible missed messages during downtime
- Telegram API retry with exponential backoff (3 attempts, respects `Retry-After`)
- MAX outbound retry for temporary transport/session failures (3 attempts, short backoff)
- Failed TG→MAX deliveries are persisted in `delivery_log` with error reason and attempt count
- Startup self-check in production — after boot, the bot notification includes `pytest` result summary
- System alerts survive temporary Telegram outages via persistent outbox + retry
- Docker healthcheck is tied to supervisor heartbeat, not external MAX/TG availability

---

## Architecture

```
MAX WebSocket ──► MAX Adapter ──► Bridge Core ──► TG Adapter ──► Telegram
                  (pymax)         (routing)        (aiogram)      (Topics API)
                                      │
                                  SQLite DB
                              (bindings, dedup,
                               delivery log)
```

One Python service with two layers: a long-lived supervisor plus a restartable bridge worker. No external queues or services. SQLite and persisted health files are the only state stores.

Details: [docs/architecture.md](docs/architecture.md)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| MAX userbot | [`pymax`](https://github.com/MaxApiTeam/PyMax) / `maxapi-python` |
| Telegram bot | `aiogram` 3.x |
| Database | SQLite + `aiosqlite` |
| Config | YAML + `python-dotenv` |
| Runtime | Python 3.13+, `asyncio` |
| Deployment | Docker Compose / Hetzner Cloud |

---

## Production Status

Bridge is running in production on **Hetzner Cloud**.

- Runtime: Docker Compose (non-root container, `cap_drop: ALL`, `restart: always`)
- State: SQLite + MAX session in a bind-mounted `data/` directory
- Health: Docker `HEALTHCHECK` uses supervisor heartbeat freshness instead of checking external integrations
- Access: SSH key only, restricted by IP via UFW
- Security: `fail2ban`, `unattended-upgrades`, no public HTTP ports
- Boot signal: startup notification in Telegram owner DM includes runtime/host info plus startup `pytest` summary

---

## Quick Start

**Requirements:** Python 3.13+, Telegram bot ([@BotFather](https://t.me/BotFather)), Forum Supergroup with Topics enabled, MAX account.

```bash
# 1. Dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Config + secrets
cp .env.example .env
cp .env.secrets.example .env.secrets
# Fill in secrets in .env.secrets:
# TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE

# 3. (optional) Local chat bindings
cp config.local.yaml.example config.local.yaml

# 4. First run — MAX authorization via SMS code
.venv/bin/python -m src.main

# 5. Background run
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &
```

Via Docker:
```bash
docker compose -f deploy/docker-compose.yml up -d
```

---

## Project Structure

```
maxgram/
├── src/
│   ├── main.py                ← supervisor entry point + worker bootstrap
│   ├── adapters/
│   │   ├── max_adapter.py     ← MAX userbot: connect, recv, send, reconnect
│   │   └── tg_adapter.py      ← Telegram bot: topics, send, receive, ops alerts
│   ├── bridge/
│   │   └── core.py           ← all routing logic
│   ├── config/loader.py
│   ├── runtime/
│   │   ├── health.py         ← persisted health snapshot/events/outbox/heartbeat
│   │   ├── supervisor.py     ← worker restart loop + alert integration
│   │   └── healthcheck.py    ← Docker healthcheck entry point
│   └── db/
│       ├── models.py          ← SQLite schema (3 tables)
│       └── repository.py
│
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   ├── decisions/             ← ADR-001…004
│   └── runbooks/
│
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-compose.prod.yml
│
├── tests/                     ← pytest regression suite
└── scripts/smoke_check.py
```

---

## Known Limitations

- Messages during downtime are **lost** — pymax has no history replay API
- Unofficial userbot — potential ToS violation with MAX
- Bot commands (`/status`, `/chats`, `/reauth`) are owner-only
- `ops_topic_id` is optional; without it, ops alerts go only to owner DM

---

## License

MIT — see [LICENSE](LICENSE)

---

*See [README-ru.md](README-ru.md) for full Russian documentation.*
