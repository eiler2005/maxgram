# ADR-012: External Watchdog On A Second VPS

Date: 2026-09-08

All existing recovery layers of the bridge live inside a single container on a
single host: `BridgeSupervisor` restarts the worker, the MAX watchdog performs a
rate-limited self-exit so Docker `restart: always` can recreate the container,
and the Docker `HEALTHCHECK` only flags `unhealthy` without restarting anything.

A failure model of the runtime (documented in full in
`docs/runbooks/watchdog.md`) shows three classes that no existing layer can
observe, because the observer would have to survive the very failure it reports:

- **F6** — the bridge's own Telegram alert path is broken (`alert_outbox.jsonl`
  grows, `system_notification_failed`). The alert about broken alerting would
  have to travel through the broken channel.
- **F8** — the container is stopped (`docker compose stop/down`, a failed
  deploy). `restart: always` does not apply to an explicit stop, and nothing
  inside a stopped container can report.
- **F9** — the host, VM or Docker daemon is down.

Two more classes were only observable by a human reading `/status`: **F15**
(silent config drift, e.g. `max.egress.active` left on `hetzner_direct`) and
**F16** (growing retry queues).

## Decision

Introduce four observation layers instead of one, each justified by a specific
failure class, and an application-authored status API that makes internal state
structurally available.

- **L1 — status API in the bridge.** `aiohttp` server bound to `127.0.0.1:18140`
  inside the container, published on the host loopback only. `GET /healthz`
  (no auth) and `GET /status` (bearer token from `BRIDGE_STATUS_TOKEN`).
  Without a token the server does not start at all.
- **L2 — pull from the observer host.** A container on a second VPS runs a TCP
  reachability probe plus an SSH call every 60 seconds, and polls the status API
  every 300 seconds. The observer's key is pinned on the production host with
  `command="/usr/local/bin/bridge-status-probe.py",restrict`.
- **L3 — push dead-man's switch.** A systemd timer on the production host posts
  an HMAC-SHA256 signed snapshot to the observer every 60 seconds.
- **L4 — meta-monitoring.** The observer container is registered with the host
  monitoring of its own VPS, the two hosts already probe each other, and the
  watchdog sends a daily "alive" summary.

Alerts go to owner DM and the ops topic using the bridge's own bot token, but
the request originates from the observer host.

## Rationale

- **Why a second VPS and not a systemd unit on the same host.** A host-level
  unit still dies with the host (F9) and cannot report a broken outbound path
  (F6). The observer must not share a failure domain with the observed.
- **Why SSH pull and not a public HTTPS endpoint.** The bridge has never
  accepted inbound connections, `docs/ports.md` in the infrastructure repo
  requires application APIs to bind loopback only, and the production host's
  reverse proxy belongs to a different project. SSH with a forced read-only
  command reuses a path that already exists, adds no listening port, and gives
  strictly more signal (container state, restart count, disk) than an HTTP
  health endpoint could.
- **Why the observer cannot restart anything.** Remote recovery would require
  write access from the observer to production. A compromised observer would
  then be a compromised production host. The forced command is read-only by
  construction; recovery stays a human action.
- **Why the bridge's bot and not the separate infrastructure bot.** The operator
  already watches owner DM and the ops topic, and correlating external alerts
  with the bridge's own timeline matters more than bot independence — the
  failure mode that matters (F6) is the bridge *process* being unable to send,
  not the token being invalid. External alerts are prefixed `[EXT]` so their
  origin is unambiguous.
- **Why plain HTTP for the push receiver.** The payload carries only status
  codes, integrity and replay protection come from the HMAC signature and a
  time window, and the port is restricted to a single `/32` at both the UFW and
  provider firewall level. TLS would add certificate lifecycle for no
  confidentiality gain. This is a deliberate, revisitable trade-off.
- **Why the status payload omits `raw_cause`.** Issue codes, severity and
  `requires_reauth` are sufficient for every rule in the catalogue. Exception
  text is the one field that could carry incidental content, so it stays on the
  production host. Enforced by `test_status_payload_never_carries_raw_cause`.

## Consequences

- The failure model, not intuition, defines the rule catalogue: every rule
  names the class it covers, in code, in alert text and in documentation.
- Alert noise is controlled by hysteresis (N consecutive failures), cascade
  suppression (a dead host silences dependent rules instead of firing all of
  them), skip-on-missing-data, a 900 s dedup window, and one-shot recovery
  notifications.
- Two firewall rules must be maintained by hand on both UFW and the provider
  panel; drift there disables a layer, which surfaces as `ssh_probe_failed` or
  `push_stale` rather than silence.
- The watchdog lives in this repository, not in the infrastructure repository,
  which explicitly does not deploy applications. Its container is registered
  there for drift control only.
- Nothing recovers automatically from F8 or F9: the observer converts silent
  failure into a loud, actionable message, which is the entire goal.
- Quarterly drills are mandatory. An untested dead-man's switch is worse than
  none, because it produces false confidence.
