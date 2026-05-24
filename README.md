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
- **Explicit adapter/backend boundary** — `BridgeCore` depends on transport-neutral contracts; MAX operation services depend on typed client ports/DTO, and `pymax` imports plus pymax client shape are isolated to `src/adapters/max/backends/pymax/`, with regression tests guarding the boundary
- **MAX-only egress profiles** — MAX API/CDN traffic can use authenticated HTTP CONNECT through reverse Channel M (`home_ru_proxy`: router-originated SSH remote-forward to the VPS) while Telegram stays direct; Hetzner direct is retained only as a manual emergency profile
- **Idempotent message deduplication** — `max_msg_id` is written to SQLite *before* forwarding to Telegram, making the system safe to restart at any point without duplicates
- **Privacy-first design** — no message text or media is ever stored; SQLite only holds routing metadata (chat bindings, message ID map, delivery log)
- **Production-deployed** — running on Hetzner Cloud behind Docker Compose with UFW, fail2ban, non-root container, and SSH-key-only access
- **Ansible-driven ops** — regular deploy, backup, recovery, fresh-VM bootstrap, and hardening are all codified as idempotent playbooks under `infra/ansible/`; the manual runbook is kept only as fallback
- **Supervisor runtime shell** — PID1 is now a supervisor that keeps the container `Up`, restarts the bridge worker with backoff, and persists health state even when MAX/TG integration degrades
- **Resilient delivery** — Telegram API calls retry with exponential backoff; temporary TG→MAX transport failures retry automatically; failed outbound deliveries are written to SQLite with attempt counts; MAX watchdog alerts on offline > 60s; retryable MAX video downloads are persisted until delivered; `/status` gives live health snapshot on demand
- **Persistent health model** — `health_state.json`, `health_events.jsonl`, `alert_outbox.jsonl`, and `health_heartbeat.json` make degraded-vs-dead runtime states explicit
- **Account migration recovery registry** — hybrid MAX account snapshots preserve Telegram topic routing, invite/admin metadata, DM partner ids, DM contact snapshots from real dialogs only, and snapshot freshness for guided recovery after a phone/account loss

---

## Features

- Automatic topic creation for every new MAX chat
- Bidirectional messaging — replies in Telegram → delivered to MAX (including reply-to-message)
- Media forwarding in both directions: photos, video, audio, voice, documents
- MAX video downloads prefer real `MP4_*` streams over `EXTERNAL` player pages and use an adaptive CDN user-agent (`CHROME` vs mobile Safari)
- Retryable MAX video/voice failures are queued in SQLite and sent later to the same Telegram topic without storing signed URLs or tokens
- MAX downloader validates `Content-Type` + file signature and rejects HTML/text fallbacks for expected media
- MAX `CHANNEL`/forward wrappers are unwrapped into the real forwarded text and media instead of a generic system placeholder
- Unknown MAX message shapes are forwarded with diagnostic metadata (`type`, `link_*`, counts, raw field names) so new formats can be fixed from the next occurrence
- MAX attachment aliases (`IMAGE`, `VOICE`, `DOCUMENT`, `DOC`) are normalized consistently across dispatch and download stages
- MAX `VOICE` attachments are delivered as native Telegram voice notes (`send_voice` bubbles)
- MAX voice retry refreshes raw history/dialog metadata and probes conservative audio download payload variants through the existing userbot socket; repeated sweeps update the same pending job instead of duplicating “докачивается” placeholders
- Control events are rendered in human-friendly text (including `joinbylink` join notifications)
- Sender name prefix in group chats: `[First Last] message text`
- Own messages (sent directly in MAX) mirrored to Telegram with `[Вы]` prefix
- DM topics named after the contact (resolved from MAX profile, cache + live API)
- Per-chat modes: `active` / `readonly` / `disabled`
- Deduplication — no duplicate messages on reconnect
- Stable reconnect — no OOM, no SSL storm
- `/status` command — uptime, message stats, recovery snapshot summary, top active chats; works in forum group and personal DM with bot
- `/chats` command — list of bridged chats with topic id, mode, and inbound/outbound counters
- Periodic 4-hour status report — automatic delivery/recovery stats sent to owner DM, with optional fanout to a dedicated forum topic
- MAX account recovery snapshots — `/recovery scan`, `/recovery report`, `/recovery export`, `/recovery set`, and `/recovery remap` help migrate existing Telegram topics to a new MAX phone/account without storing message contents
- Hybrid recovery refresh: scan after successful MAX connect/reconnect, weekly safety-net scan, and event-driven scans after new topic bindings, title changes, and MAX control events; reports include `last_scan_at` freshness
- DM contact recovery snapshot — stores only personal contacts that have real MAX DM dialogs or bound DM topics, never the full MAX address book
- Quiet recovery notifications — routine scan deltas (`unmapped`, `needs_invite`, DM contact changes) are folded into the 4-hour status summary; owner/ops gets an immediate alert only for MAX account migration-required
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
MAX WebSocket
  └─► MaxAdapter facade
        ├─► operation services: lifecycle/events/send/media/recovery/resolve
        ├─► typed MAX client ports + explicit deps/state slices
        └─► MaxBackend ──► PymaxBackend/PymaxClientAdapter
              (only pymax imports and pymax client shape)
              │
              ▼
Bridge Core (contracts) ──► TG Adapter (aiogram) ──► Telegram Topics
              │
              └─► SQLite DB + runtime health
```

One Python service with two layers: a long-lived supervisor plus a restartable bridge worker. Runtime wiring lives in `src/startup/composition.py`; SQLite and persisted health files are the only state stores.

MAX network traffic is scoped to the MAX adapter. In production `home_ru_proxy` sends MAX API and CDN downloads through reverse Channel M: the home router opens an outbound SSH remote-forward to the VPS docker bridge, and the bridge uses authenticated HTTP CONNECT against that VPS-local listener. `hetzner_direct` remains a manual break-glass profile and is never used as automatic fallback. Telegram traffic, LAN/Wi-Fi, and router A/B/C routing are not changed by this bridge setting.

Runtime environment requirements and the reverse Channel M text diagram are in
[docs/environment-inventory.md](docs/environment-inventory.md).

Details: [docs/architecture.md](docs/architecture.md)

---

## MAX Account Migration Recovery

MAX does not support changing the phone number attached to a profile. In practice, a new phone number means a new MAX account, and private/admin-only chats may need a fresh invite from their owner or an admin.

Maxgram cannot clone a MAX account or restore MAX-side message history. Instead, it preserves the bridge recovery context needed to keep Telegram continuity:

- Telegram topic bindings: stable `tg_topic:<topic_id>` keys, old/current `max_chat_id`, title, mode, and recovery status
- MAX account generations: `max_user_id`, masked phone, session fingerprint hash, active/retired/lost status, first/last seen timestamps
- Chat access metadata: chat kind, invite link, owner/admin ids and names, DM partner id/name, participant count, manual notes
- DM contact recovery metadata: `max_user_id`, display name, old/current DM chat id, linked Telegram topic, status, and `last_scan_at` for people with real MAX DM conversations only
- Freshness metadata: every registry row has `last_scan_at`; `/recovery report` shows how old the latest snapshot is
- Audit trail: scan, note, account-change, and remap events are appended without message text or raw MAX payloads

Owner-only commands:

```text
/recovery scan
/recovery report
/recovery export
/recovery set <topic_id> key=value ...
/recovery remap <topic_id> <new_max_chat_id>
```

Normal operation runs a safe registry scan after successful MAX connect/reconnect and then once a week. It also refreshes snapshots asynchronously when important MAX-side changes are observed: new Telegram topic binding, fallback title update, or MAX `CONTROL` event. These event-driven scans are debounced in `bridge/recovery/scheduler.py`, run in background tasks, and never block message forwarding or topic creation. The same scan updates DM contact recovery from typed dialog snapshots only; `client.contacts` and `known_users` are not copied into the recovery contact registry. Routine automatic scan deltas are summarized in the 4-hour `/status` report with aggregate counts and `/recovery report` as the detail view; owner/ops gets an immediate alert only when a new MAX account requires the migration flow.

If you lose the old MAX account, reauthorize with the new phone, run `/recovery scan`, inspect `/recovery report`, ask admins for invites where needed, and then use `/recovery remap <topic_id> <new_max_chat_id>` to keep the existing Telegram topic while routing replies to the new MAX chat.

Privacy rule: the registry stores recovery metadata only. It does not store message contents, media URLs, signed tokens, phone numbers, raw MAX payloads, or the full MAX address book. Group-visible reports, logs, health state, and automatic notifications do not include invite links, manual notes, DM contact names, phone numbers, message text, or raw MAX fields. `/recovery export` may include invite links, admin notes, and the DM contact recovery list, so it is sent only to the owner DM.

Full runbook: [docs/runbooks/operations.md#max-account-recovery-registry](docs/runbooks/operations.md#max-account-recovery-registry)

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
| Ops automation | Ansible (`infra/ansible/`) |

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
# Optional for production MAX home-router egress:
# .env.secrets: MAX_EGRESS_PROXY_URL
# .env.host: MAX_EGRESS_PROXY_HOST, MAX_EGRESS_PROXY_GATEWAY

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
│   ├── main.py                ← thin entry point: logging, config, supervisor
│   ├── startup/
│   │   └── composition.py     ← runtime wiring / DI
│   ├── adapters/
│   │   ├── max/               ← MAX userbot package; pymax boundary
│   │   ├── max_adapter.py     ← compatibility import
│   │   ├── tg/                ← Telegram adapter + notifier
│   │   └── tg_adapter.py      ← compatibility import
│   ├── bridge/
│   │   ├── contracts.py       ← transport-neutral models and ports
│   │   ├── core.py            ← coordinator
│   │   ├── forwarding.py
│   │   ├── replies.py
│   │   ├── topics.py
│   │   ├── commands/
│   │   └── recovery/
│   ├── config/loader.py
│   ├── runtime/
│   │   ├── health/           ← persisted health snapshot/events/outbox/heartbeat
│   │   ├── supervisor.py     ← worker restart loop + alert integration
│   │   └── healthcheck.py    ← Docker healthcheck entry point
│   └── db/
│       ├── models.py          ← SQLite schema: bindings, messages, health/retry, recovery registry
│       ├── repository.py      ← public facade
│       └── repos/             ← subdomain repositories
│
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   ├── decisions/             ← ADR-001…006
│   └── runbooks/
│
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-compose.prod.yml
│
├── infra/
│   └── ansible/               ← deploy / backup / recover / bootstrap / hardening
│
├── tests/                     ← pytest regression suite
└── scripts/smoke_check.py
```

---

## Known Limitations

- Messages during downtime are **lost** — pymax has no history replay API
- A new MAX phone/account still requires manual invites for private/admin-only chats; the recovery registry guides the process but does not auto-join chats
- Unofficial userbot — potential ToS violation with MAX
- Bot commands (`/status`, `/chats`, `/reauth`, `/recovery ...`) are owner-only
- `ops_topic_id` is optional; without it, ops alerts go only to owner DM

---

## License

MIT — see [LICENSE](LICENSE)

---

*See [README-ru.md](README-ru.md) for full Russian documentation.*
