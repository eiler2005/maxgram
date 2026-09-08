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

## Why This Is Non-Trivial

1. MAX API is undocumented and reverse-engineered. The upstream Python wrapper (`maxapi-python`) is beta-quality.
2. The bridge runs 24/7 on a single VPS with a single MAX account; any unhandled disconnect can become silent message loss.
3. We mitigate this with 6 surgical PyMax compatibility shims, each small and covered by a regression-marker test:
   - `BridgeSessionStore` — one-shot import of legacy PyMax v1 session table into the v2 schema
   - `BridgeConnectionManager` — keeps the bridge egress connection on PyMax 2.1's 16-bit TCP sequence semantics
   - `BridgeMsgpackPayloadCodec` — handles MAX maps with array-valued keys that strict msgpack rejects
   - `BridgeAuthService` + `validate_login_response` — strips upstream-unknown variants, repairs non-critical initial-sync payload drift, and keeps an explicit SMS reauth working when MAX omits an optional fingerprint seed
   - `EgressTCPTransport` — injects authenticated HTTP CONNECT proxy for MAX-only RU egress
   - `PymaxInternalsContractError` — centralizes private PyMax attr access and fails loudly on upstream drift
4. Architecture replaceability is verified, not asserted: `tests/integration/test_bridge_end_to_end.py` runs the full bridge against `tests/fakes/fake_max_backend.py` in CI. See [docs/architecture-tour.md](docs/architecture-tour.md) for the compact walkthrough and `examples/swap_max_backend.py` for a 30-second demo.

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

- **PyMax 2.4.1 lifecycle compatibility** — bridge-owned auth/user/TCP guards are installed after PyMax creates its lazy runtime; the bridge uses one-shot `connect()` with its existing fresh-client reconnect loop, keeps upstream MAX CA trust on custom egress, resolves its DESKTOP app version/build through PyMax's bundled catalog, and disables automatic upstream relogin
- **PyMax v2 compatibility shim** — the bridge now targets `maxapi-python` 2.x through a typed backend adapter; old PyMax v1 reconnect/OOM behavior is historical context, not the active architecture
- **Explicit adapter/backend boundary** — `BridgeCore` depends on transport-neutral contracts; MAX operation services depend on typed client ports/DTO, and `pymax` imports plus pymax client shape are isolated to `src/adapters/max/backends/pymax/`, with surface-pin and fake-backend integration tests guarding the boundary
- **MAX-only egress profiles** — MAX API/CDN traffic can use authenticated HTTP CONNECT through reverse Channel M (`home_ru_proxy`: router-originated SSH remote-forward to the VPS) while Telegram stays direct; Hetzner direct is retained only as a manual emergency profile
- **Idempotent message deduplication** — `max_msg_id` is written to SQLite *before* forwarding to Telegram, making the system safe to restart at any point without duplicates
- **Privacy-first design** — successful message text/media is not stored; SQLite normally holds routing metadata, with one exception: failed text-only MAX→TG/TG→MAX messages can be queued temporarily in plaintext until delivered or expired
- **Production-deployed** — running on Hetzner Cloud behind Docker Compose with UFW, fail2ban, non-root container, and SSH-key-only access
- **Ansible-driven ops** — regular deploy, backup, recovery, fresh-VM bootstrap, and hardening are all codified as idempotent playbooks under `infra/ansible/`; the documented emergency fallback is backup-first and commit-pinned, while secrets and state remain server-only
- **Supervisor runtime shell** — PID1 inside the bridge container on the Hetzner production VPS keeps the container `Up`, restarts the bridge worker with backoff, and persists health state even when MAX/TG integration degrades
- **Resilient delivery** — Telegram API calls retry with exponential backoff; definite unsent TG→MAX text failures and retryable MAX→TG text delivery failures are queued with lease/backoff/TTL; failed outbound deliveries are written to SQLite with attempt counts; the MAX watchdog in that same container alerts on offline > 60s; video recovery makes six deferred attempts over 18 minutes while voice/photo stable-reference recovery keeps its existing backoff policy; `/status` gives live health snapshot on demand
- **External watchdog on a second VPS** — an observer outside the bridge's failure domain covers the three classes nothing inside can report: a stopped container, a dead host, and the bridge's own Telegram alerting being broken. It only reports: its SSH key is pinned to a read-only probe, so recovery stays a human action. Rules carry hysteresis, cascade suppression and one-shot recovery notices; each names the failure class it covers ([runbook](docs/runbooks/watchdog.md))
- **Read-only status API** — `GET /healthz` and token-gated `GET /status` on loopback expose subsystem issue codes, queue depth, alert outbox size and active MAX egress, without message text, chat titles or exception causes
- **Persistent health model** — `health_state.json`, `health_events.jsonl`, `alert_outbox.jsonl`, and `health_heartbeat.json` make degraded-vs-dead runtime states explicit
- **Prometheus textfile metrics** — health, durable retry queues, delivery totals, worker restarts, and alert outbox depth are exported to `data/maxtg_bridge.prom` by default
- **Account migration recovery registry** — hybrid MAX account snapshots preserve Telegram topic routing, invite/admin metadata, DM partner ids, DM contact snapshots from real dialogs only, and snapshot freshness for guided recovery after a phone/account loss

---

## Features

- Automatic topic creation for every new MAX chat
- Bidirectional messaging — replies in Telegram → delivered to MAX (including reply-to-message)
- MAX replies become native Telegram replies when the original mapping is available; unmapped replies and forwarded messages receive short context markers without quoted content
- Media forwarding in both directions: photos, video, audio, voice, documents
- MAX video downloads use PyMax 2.4.1 `get_video_by_id()` first, keep raw `VIDEO_PLAY` as a fallback, prefer real `MP4_*` streams over `EXTERNAL` player pages, and use an adaptive CDN user-agent (`CHROME` vs mobile Safari)
- Retryable MAX video failures are queued by stable reference for six deferred attempts every three minutes; a terminal warning replaces indefinite waiting after 18 minutes, without storing signed URLs or tokens
- MAX downloader validates `Content-Type` + file signature and rejects HTML/text fallbacks for expected media
- MAX `CHANNEL`/forward wrappers are unwrapped into the real forwarded text and media instead of a generic system placeholder; forwarded messages show their source chat/channel from the MAX forward payload, falling back to the local MAX cache, otherwise a neutral MAX marker is used
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
- Durable text-only MAX→TG and TG→MAX outboxes for retryable transport/delivery failures; text is cleared after delivery/TTL
- Media retry uses stable MAX media references where available; heavy files are not stored in SQLite, and TG→MAX outbound media must be resent manually after a failure
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
- Recovery boundary: the supervisor and MAX watchdog run inside the bridge container; Docker restart policy runs in Docker Engine on the same Hetzner VPS. `HEALTHCHECK` reports a stale heartbeat but does not restart a container itself; an explicit `docker compose stop`/`down` must be followed by `docker compose ... up -d bridge`.
- Access: SSH key only, restricted by IP via UFW
- Security: `fail2ban`, `unattended-upgrades`, no public HTTP ports
- Boot signal: startup notification in Telegram owner DM includes runtime/host info plus startup `pytest` summary

### Watchdog: what breaks, and who notices

Every in-container recovery layer shares a failure domain with the thing it
protects, so an observer on a **second VPS** covers what the bridge structurally
cannot report about itself. It only reports — the pinned SSH key runs a
read-only probe, so recovery stays a human action.

| What breaks | Who notices | Recovery |
|---|---|---|
| Worker crashes | `BridgeSupervisor`, inside the container | automatic, with backoff |
| MAX link hangs while egress is healthy | MAX watchdog → self-exit → Docker `restart: always` | automatic, rate-limited |
| Worker hangs (container up, heartbeat stale) | external watchdog | manual |
| **Container stopped** (`docker compose stop/down`) | **external watchdog only** — `restart: always` does not apply to an explicit stop | manual |
| **Host / VM / Docker daemon down** | **external watchdog only** | manual |
| **The bridge's own Telegram alerting is broken** | **external watchdog only** — it has an independent network path | depends on cause |
| The external watchdog itself dies | host monitoring on its VPS + mutual host probes + a daily summary | manual |

#### Four observation layers

Every alert names the layer that caught it, so the message itself says where to
look. A layer that goes quiet is indistinguishable from a broken one, so all
four report in the daily summary.

| Layer | In one line | Where it runs | Interval | Catches |
|---|---|---|---|---|
| **L1** status API | what hurts inside the bridge | `127.0.0.1:18140` in the bridge container | 300 s | MAX egress/auth issues, alert outbox backlog, egress drift, queue backlog |
| **L2** SSH pull | is the container and host alive | observer container, read-only forced command | 60 s | stopped container, dead host, stale heartbeat, restart storm, low disk |
| **L3** push dead-man's switch | did the observation path break | `maxtg-watchdog-push.timer` → observer `:18151` | 60 s | tells "the bridge is dead" apart from "the observation path is dead" |
| **L4** meta-monitoring | is the observer itself alive | host monitor + mutual probes + daily summary | 5 min / 24 h | a dead observer |

#### Direction of each check

```
L0:  inside the bridge container                 (supervisor, MAX watchdog, HEALTHCHECK)
L1:  EXTERNAL observer ──HTTP──► INTERNAL bridge status API
L2:  EXTERNAL observer ──SSH──► production host
L3:  production host ──HMAC POST──► EXTERNAL observer
L4:  host monitor (neighbour on that host) ──► observer container
```

The direction is not decoration. It says which end to fix: L1 and L2 are
initiated by the observer, so anything on the observer → production path breaks
them (firewall, sshd, fail2ban). L3 travels the other way, so it survives that
path breaking — which is exactly how it tells "the bridge died" apart from "the
polling path died".

L0 never appears in the daily summary: it lives inside the container and reports
on itself under the `🌉 BRIDGE` header. When the container is dead, L0 is silent
— which is the entire reason L2 and L3 exist.

#### What an alert looks like

```
🛰 EXTERNAL WATCHDOG · checked from a separate VPS

🔴 The bridge container is not running
Observed host: maxtg-bridge-prod
Layer: L2 poll from the observer — is the container and the host alive
Failure class: F8 · container_down

What happened: Container deploy-bridge-1: exited, exit code 137.
What to do: Docker restart: always does not apply to an explicit stop.
  Bring it back: docker compose --project-name deploy -f ... up -d bridge
Where to look: poll from the observer — docker compose logs watchdog; ssh -i <key> deploy@<prod>
```

The daily summary reports every layer and files each open problem under the
layer that found it, so the marked layer is where to start digging:

```
✅ L1 bridge status API — what hurts inside the bridge
      verdict: answering, state healthy
      checked: subsystems healthy 6/6 · queues 0 · outbox 0 · egress home_ru_proxy
⚠️ L2 poll from the observer — is the container and the host alive
      verdict: poll succeeds
      checked: container running/healthy · heartbeat 26 s · disk free 8%
      ⚠️ Low disk space on the production host (disk_low)
✅ L3 push channel — did the observation path break
      verdict: last push 9 s ago
      checked: HMAC signature valid · delivery lag 4 s
✅ L4 meta-monitoring — is the observer itself alive
      verdict: the check loop is running
```

The `checked` line carries the facts behind the verdict rather than the verdict
alone, so it is visible that the check is real rather than a formality.

> Alerts are rendered in Russian on the deployed instance — this is a personal,
> single-operator bridge. The samples above are translated; the structure is
> exactly what arrives.

Recovery reports how long the problem lasted. Alerts use hysteresis (N
consecutive failures), cascade suppression, a 15-minute dedup window, and
one-shot recovery notices — in a normal week the only message is the daily
summary.

#### Where the code lives

| Path | What |
|---|---|
| `src/runtime/status_api.py` | L1 — the bridge's own status endpoint |
| `src/watchdog_external/rules.py` | the rule catalogue: thresholds, layers, failure classes, alert text |
| `src/watchdog_external/probe.py` · `receiver.py` | L2 pull · L3 push receiver |
| `src/watchdog_external/notify.py` | Telegram rendering and delivery |
| `infra/ansible/roles/watchdog_peer/` | production-side probe and push timer |
| `deploy/external-watchdog/` | observer stack + `deploy.sh` + `CONFIGURATION.md` |

Everything under `src/watchdog_external/` is standard library only and imports
no bridge module — an observer that shares dependencies with the observed is
not an observer.

#### Tests

```bash
pytest tests/test_status_api.py tests/test_watchdog_external.py -q   # 27 + 4 cases
```

They cover hysteresis, cascade suppression, the push fallback, severity
escalation, message rendering, HMAC verification, and two architectural
guarantees: the status payload never carries exception causes, and the
watchdog never grows a dependency on the bridge.

How the mechanisms actually work — processes, containers, what each cycle does,
state files and the security model: [runbook §3](docs/runbooks/watchdog.md#3-как-это-устроено-механизмы-процессы-контейнеры).

Full failure model (F1–F16), alert catalogue, thresholds, setup, install traps
and quarterly drills: [docs/runbooks/watchdog.md](docs/runbooks/watchdog.md) ·
configuration map: [deploy/external-watchdog/CONFIGURATION.md](deploy/external-watchdog/CONFIGURATION.md) ·
decision record: [ADR-012](docs/decisions/ADR-012-external-watchdog.md).

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
# Optional for encrypted new-number recovery contacts snapshot:
# MAX_RECOVERY_CONTACTS_KEY
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
│   │   ├── message_context.py ← reply/forward labels
│   │   ├── replies.py
│   │   ├── topics.py
│   │   ├── commands/
│   │   └── recovery/
│   ├── config/loader.py
│   ├── runtime/
│   │   ├── health/           ← persisted health snapshot/events/outbox/heartbeat
│   │   ├── status_api.py     ← read-only loopback status API for the observer
│   │   ├── supervisor.py     ← worker restart loop + alert integration
│   │   └── healthcheck.py    ← Docker healthcheck entry point
│   ├── watchdog_external/    ← external watchdog (stdlib only, second VPS)
│   └── db/
│       ├── models.py          ← SQLite schema: bindings, messages, health/retry, recovery registry
│       ├── repository.py      ← public facade
│       └── repos/             ← subdomain repositories
│
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   ├── decisions/             ← ADR-001…012
│   └── runbooks/              ← operations, deployment, watchdog
│
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── docker-compose.prod.yml
│   └── external-watchdog/     ← observer stack: Dockerfile, compose, deploy.sh
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
