# Конфигурация внешнего watchdog — карта настроек

Полный перечень того, что должно быть настроено, **без реальных значений**:
репозиторий публичный, поэтому здесь только плейсхолдеры и указание, где лежит
настоящее значение.

Полный снимок с реальными адресами собирается локально:

```bash
./scripts/watchdog_snapshot.sh          # → deploy/external-watchdog/CONFIGURATION.local.md
```

Этот файл в `.gitignore` и в публичный git не попадает.

Обозначения: `<prod_ip>` — production-хост с bridge, `<observer_ip>` — второй VPS
с наблюдателем.

---

## 1. Production-хост

### `.env.secrets` (вручную по `scp`, не в git)

| Ключ | Значение | Назначение |
|---|---|---|
| `BRIDGE_STATUS_TOKEN` | 64 hex-символа (`openssl rand -hex 32`) | доступ к `/status`; без него API не поднимается |
| `WATCHDOG_PUSH_URL` | `http://<observer_ip>:18151/push` | куда шлёт push-таймер |
| `WATCHDOG_PUSH_SECRET` | 64 hex-символа, **общий с наблюдателем** | HMAC-подпись push |

### `config.local.yaml` (не в git)

```yaml
status_api:
  enabled: true
  host: "0.0.0.0"   # адрес ВНУТРИ контейнера; наружу порт публикуется на 127.0.0.1
  port: 18140
```

### Файлы, которые раскладывает ansible-роль `watchdog_peer`

| Путь | Что это |
|---|---|
| `/usr/local/bin/bridge-status-probe.py` | read-only проба, цель forced command |
| `/usr/local/bin/maxtg-watchdog-push.py` | отправитель push |
| `/etc/systemd/system/maxtg-watchdog-push.{service,timer}` | таймер push, интервал 60 с |
| `~deploy/.ssh/authorized_keys` | строка `command="/usr/local/bin/bridge-status-probe.py",restrict <ключ наблюдателя>` |

### Docker

`deploy/docker-compose.prod.yml` публикует `127.0.0.1:18140:18140` — только петля хоста.

---

## 2. Хост-наблюдатель

### `/opt/maxtg-watchdog/.env.secrets` (не в git)

| Ключ | Значение |
|---|---|
| `WATCHDOG_TARGET_HOST` | `<prod_ip>` |
| `WATCHDOG_TARGET_NAME` | человекочитаемое имя для сообщений |
| `WATCHDOG_CONTAINER_NAME` | `deploy-bridge-1` |
| `WATCHDOG_SSH_USER` / `WATCHDOG_SSH_PORT` / `WATCHDOG_SSH_KEY` | `deploy` / `22` / `/app/ssh/watchdog_key` |
| `TG_BOT_TOKEN` / `TG_OWNER_ID` / `TG_FORUM_GROUP_ID` | те же, что у bridge |
| `TG_OPS_TOPIC_ID` | необязательно; без него алерты идут только в owner DM |
| `WATCHDOG_PUSH_SECRET` | **тот же**, что на production |
| `WATCHDOG_PUSH_BIND_ADDRESS` / `WATCHDOG_PUSH_PORT` | `0.0.0.0` / `18151` |
| `WATCHDOG_SUMMARY_HOURS_UTC` | часы UTC через запятую: `6,10,14,18` = 09/13/17/21 МСК; `off` выключает |

Пороги (`WATCHDOG_POLL_INTERVAL_SECONDS` и прочие) — см. таблицу в
[docs/runbooks/watchdog.md](../../docs/runbooks/watchdog.md#5-пороги); в
`.env.secrets` их задают только при отклонении от умолчаний.

### Файлы

| Путь | Владелец | Права |
|---|---|---|
| `/opt/maxtg-watchdog/secrets/watchdog_key` | uid `10001` (пользователь контейнера) | `0600` |
| `/opt/maxtg-watchdog/.env.secrets` | `deploy` | `0600` |
| volume `maxtg-watchdog_watchdog_state` | — | состояние правил, push, heartbeat |

---

## 3. Сеть

| Правило | Где заводится | Значение |
|---|---|---|
| входящее TCP 22 с `<observer_ip>/32` | UFW production **и** Cloud Firewall провайдера | опрос L2 |
| исходящее TCP 18151 к `<observer_ip>/32` | Cloud Firewall провайдера production | push L3 |
| входящее TCP 18151 с `<prod_ip>/32` | UFW наблюдателя | приём push |

У production-хоста Cloud Firewall ограничивает **и исходящий** трафик — без
второго правила push не уходит, а UFW при этом показывает `allow (outgoing)`.

---

## 4. Ключи и секреты — где лежит истина

| Что | Где хранится | Резервная копия |
|---|---|---|
| приватный ключ наблюдателя | `deploy/external-watchdog/secrets/watchdog_key` (локально, в `.gitignore`) | нет — при утере генерируется новый и перезаливается роль |
| `BRIDGE_STATUS_TOKEN` | `.env.secrets` production | локальный снимок конфигурации |
| `WATCHDOG_PUSH_SECRET` | `.env.secrets` обоих хостов | локальный снимок конфигурации |
| TG bot token | `.env.secrets` обоих хостов | как у bridge |
| адреса хостов | vault `vps_management` | vault |

Ротация: сгенерировать новое значение → положить в оба `.env.secrets` →
перезапустить bridge и контейнер наблюдателя. Push-секрет обязан меняться на
обоих хостах одновременно, иначе приёмник начнёт отбраковывать подписи.
