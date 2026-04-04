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
- **Async Python monolith** — single `asyncio.TaskGroup` process; no queues, no microservices, no external state
- **Resilient delivery** — Telegram API calls retry with exponential backoff; MAX watchdog alerts on offline > 60s; `/status` gives live health snapshot on demand

---

## Features

- Automatic topic creation for every new MAX chat
- Bidirectional messaging — replies in Telegram → delivered to MAX (including reply-to-message)
- Media forwarding in both directions: photos, video, audio, voice, documents
- Sender name prefix in group chats: `[First Last] message text`
- Own messages (sent directly in MAX) mirrored to Telegram with `[Вы]` prefix
- DM topics named after the contact (resolved from MAX profile, cache + live API)
- Per-chat modes: `active` / `readonly` / `disabled`
- Deduplication — no duplicate messages on reconnect
- Stable reconnect — no OOM, no SSL storm
- `/status` command — uptime, message stats, top active chats; works in forum group and personal DM with bot
- Periodic 4-hour status report — automatic delivery stats sent to owner
- MAX offline watchdog — alert if MAX unreachable > 60 seconds
- Telegram API retry with exponential backoff (3 attempts, respects `Retry-After`)
- Startup self-check in production — after boot, the bot notification includes `pytest` result summary

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

One Python async process. No external services. SQLite as the sole state store.

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
- Access: SSH key only, restricted by IP via UFW
- Security: `fail2ban`, `unattended-upgrades`, no public HTTP ports
- Boot signal: startup notification in Telegram includes runtime/host info plus startup `pytest` summary

---

## Quick Start

**Requirements:** Python 3.13+, Telegram bot ([@BotFather](https://t.me/BotFather)), Forum Supergroup with Topics enabled, MAX account.

```bash
# 1. Dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Secrets
cp .env.example .env
# Fill in: TG_BOT_TOKEN, TG_OWNER_ID, TG_FORUM_GROUP_ID, MAX_PHONE

# 3. (optional) Local chat bindings
cp config.local.yaml.example config.local.yaml

# 4. First run — MAX authorization via SMS code
python src/main.py

# 5. Background run
nohup .venv/bin/python src/main.py >> data/bridge.log 2>&1 &
```

Via Docker:
```bash
docker-compose -f deploy/docker-compose.yml up -d
```

---

## Project Structure

```
maxgram/
├── src/
│   ├── main.py                ← entry point, asyncio.TaskGroup
│   ├── adapters/
│   │   ├── max_adapter.py     ← MAX userbot: connect, recv, send, reconnect
│   │   └── tg_adapter.py     ← Telegram bot: topics, send, receive
│   ├── bridge/
│   │   └── core.py           ← all routing logic
│   ├── config/loader.py
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
- Bot commands (`/status`, `/reauth`) are owner-only

---

## License

MIT — see [LICENSE](LICENSE)

---

*See [README-ru.md](README-ru.md) for full Russian documentation.*
