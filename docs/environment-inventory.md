# Environment Inventory

This document describes the runtime environment required by the production
MAX -> Telegram bridge. It intentionally uses placeholders instead of real
hosts, IPs, chat IDs or credentials.

## Production Shape

```text
                         Telegram Bot API
                                ^
                                | direct HTTPS from VPS
                                |
+-------------------------------+--------------------------------+
| Hetzner/VPS                                                    |
|                                                                |
|  /opt/maxtg-bridge                                             |
|    deploy-bridge-1 container                                   |
|      MAX adapter                                               |
|        |                                                       |
|        | HTTP CONNECT proxy_url=${MAX_EGRESS_PROXY_URL}        |
|        v                                                       |
|  Docker bridge gateway:<channel-m-reverse-listen-port>         |
|        ^                                                       |
|        | SSH remote-forward listener, bound to docker bridge   |
+--------+-------------------------------------------------------+
         |
         | outbound SSH -R, initiated by the home router
         |
+--------v-------------------------------------------------------+
| Home Russian ASUS/Merlin router                                |
|                                                                |
|  loopback HTTP CONNECT inbound                                 |
|    tag: channel-m-maxtg-reverse-egress                         |
|    route: direct-out                                           |
|        |                                                       |
|        v                                                       |
|  home WAN Russian egress -> MAX API/CDN                        |
+----------------------------------------------------------------+
```

Telegram traffic is not routed through Channel M. It stays direct from the VPS
container to Telegram. Home LAN/Wi-Fi and GhostRoute Channel A/B/C routing are
not changed by this bridge setting.

## Components

| Component | Required state | Owner |
|---|---|---|
| Control machine | Ansible access to both repositories and vault material; generated Channel M artifact stays gitignored | Operator |
| Hetzner/VPS | Docker, compose project `deploy`, `/opt/maxtg-bridge`, SSH access for deploy, docker bridge listener for reverse Channel M | `maxtg_bridge` for app, `router_configuration` for reverse tunnel listener |
| Home router | ASUS/Merlin with sing-box, reverse tunnel watchdog, Channel M loopback HTTP inbound, `direct-out` route | `router_configuration` |
| MAX adapter | `home_ru_proxy` active in production; `hetzner_direct` manual only | `maxtg_bridge` |
| Telegram adapter | Normal Bot API polling/send path, no Channel M proxy | `maxtg_bridge` |

## Network Inventory

| Path | Direction | Exposure | Purpose |
|---|---|---|---|
| VPS -> Telegram Bot API | outbound HTTPS | public internet | Telegram polling and send |
| Bridge container -> VPS docker bridge listener | internal TCP | docker bridge only | MAX HTTP CONNECT entry into reverse Channel M |
| Home router -> VPS SSH | outbound SSH | normal SSH transport | Carries remote-forward back to router loopback |
| Router loopback -> sing-box Channel M reverse inbound | local TCP | router loopback only | Authenticated MAX HTTP CONNECT target |
| Router -> MAX API/CDN | outbound HTTPS | home WAN | MAX sees the Russian home WAN |

The reverse listener port is intentionally internal to the VPS docker bridge. It
must not be opened as a public UFW or cloud-firewall port.

## Configuration Inventory

Production `config.local.yaml`:

```yaml
max:
  egress:
    active: "home_ru_proxy"
    fallback_policy: "manual"
    profiles:
      home_ru_proxy:
        type: "http_connect"
        proxy_url: "${MAX_EGRESS_PROXY_URL}"
      hetzner_direct:
        type: "direct"
```

Production local-only files:

| File | Keys | Notes |
|---|---|---|
| `.env.secrets` | `MAX_EGRESS_PROXY_URL` | Secret authenticated proxy URL. For reverse Channel M it uses `http://...:<channel-m-reverse-listen-port>` because the leg is inside SSH. |
| `.env.host` | `MAX_EGRESS_PROXY_HOST`, `MAX_EGRESS_PROXY_GATEWAY` | Compose `extra_hosts` maps the Channel M hostname to the VPS docker bridge gateway. |
| `config.local.yaml` | `max.egress.active=home_ru_proxy` | Local-only production config; `hetzner_direct` is manual emergency only. |

## Fail-Closed Rule

There is no automatic fallback from `home_ru_proxy` to `hetzner_direct`.

If reverse Channel M is unavailable, MAX becomes degraded with
`max_egress_unavailable`. This is intentional: a silent Russian-home-egress to
Hetzner-direct jump would change the MAX network fingerprint. Manual emergency
switching is done only by changing:

```yaml
max:
  egress:
    active: "hetzner_direct"
```

When this is active, `/status` must show the non-RU direct warning.
