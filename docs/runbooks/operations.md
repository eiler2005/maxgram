# Runbook: Операционные процедуры

## Запуск через Ansible

Альтернатива ручному SSH-workflow ниже. Все playbook'и — в [infra/ansible/](../../infra/ansible/), детали в [infra/ansible/README.md](../../infra/ansible/README.md).

```bash
cd infra/ansible

# Регулярный deploy:
# --check --diff = безопасный preflight verify текущего состояния, без rollout
ansible-playbook deploy.yml --check --diff
ansible-playbook deploy.yml                   # затем реально

# Бэкап на локальную машину перед рискованным изменением
ansible-playbook backup.yml

# Восстановление на свежем VM (после bootstrap.yml)
ansible-playbook recover.yml -e backup_archive=../../backups/maxtg-backup-prod-<TS>.tgz
```

`bootstrap.yml` и `hardening.yml` — только для нового VM, текущий prod уже подготовлен руками.

Ручной workflow ниже остаётся источником правды для шагов, которые Ansible намеренно не автоматизирует (создание VM в Hetzner панели, копирование секретов, SMS reauth).

Важно:

- `deploy.yml --check --diff` не симулирует `docker compose build/up`.
- В check mode playbook делает только preflight: preconditions, healthcheck, logs, smoke-check.
- Реальный rollout выполняется только обычным `ansible-playbook deploy.yml`.

## Production: Hetzner quick checklist

Текущий production-сценарий:

- сервер на Hetzner Cloud
- bridge работает в Docker Compose
- SSH доступ только по ключу
- SSH разрешён только с твоего текущего домашнего IP
- на VM включены `UFW`, `fail2ban`, `unattended-upgrades`

### Как заходить на сервер

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
```

После входа:

```bash
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=50
```

### Как обновлять bridge

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
git pull
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=80
```

### Как восстановиться, если домашний IP сменился

Сейчас SSH разрешён только с твоего текущего IP.

Нужно обновить доступ в двух местах:

1. В панели Hetzner Cloud Firewall:
   - не должно быть широких правил `Any IPv4` / `Any IPv6`
   - должно быть только `22/tcp` с нового IP в формате `x.x.x.x/32`
2. На самом сервере в `UFW`:

```bash
# зайти через Hetzner Console / LISH или временно открыть доступ в Cloud Firewall
sudo ufw delete allow from <OLD_IP> to any port 22 proto tcp
sudo ufw allow from <NEW_IP> to any port 22 proto tcp comment 'SSH from home IP'
sudo ufw status numbered
```

### Как проверить, что bridge жив

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker inspect --format '{{json .State.Health}}' deploy-bridge-1 | jq
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Ожидаемое состояние:

- контейнер `bridge` в статусе `Up`
- Docker healthcheck в статусе `healthy`
- в логах есть `MAX connected`
- в свежем startup-логе есть `Running startup tests` и затем `Startup tests passed: ...`
- нет непрерывных ошибок `TelegramConflictError`
- в `message_map` и `delivery_log` появляются свежие записи
- видео из MAX уходит как `send_video`; signed CDN URL должен открываться с `User-Agent`, соответствующим `srcAg` (`CHROME`, `CHROME_ANDROID`, `CHROME_IPHONE`, Safari/iPhone fallback)
- если видео/медиа не скачалось полностью, bridge делает до 7 попыток и пробует докачку через `Range`; после окончательного провала в Telegram должен прийти текст `⚠️ Не удалось скачать вложение MAX...`, а в `delivery_log.status` будет `partial`

Куда идут служебные сообщения:

- основной канал: личный чат владельца с ботом (`TG_OWNER_ID`)
- дополнительный канал: forum topic внутри `TG_FORUM_GROUP_ID`, только если задан `telegram.ops_topic_id`
- если `ops_topic_id` не задан, все startup/status/error alert сообщения идут только в owner DM

### Что важно помнить

- Доступ к серверу теперь только с твоего текущего IP.
- Если домашний IP изменится, нужно будет обновить правило и в Hetzner Firewall, и в `UFW`.
- Cloud Firewall я не менял через API, потому что для этого нужен отдельный Hetzner API token.
- Поэтому в панели Hetzner отдельно проверь, что там нет широких правил `Any IPv4` / `Any IPv6`, а только `22/tcp` с твоего IP.
- В production у контейнера `restart: always`, поэтому после обычного `reboot` VM bridge должен подняться сам.
- Если сделать `docker compose down`, контейнер будет удалён; после reboot VM он уже не восстановится сам, пока не выполнить `docker compose ... up -d`.
- Теперь PID1 внутри контейнера — supervisor. Даже если MAX/TG интеграция деградирует, контейнер должен оставаться `Up`, а проблема должна отражаться в `data/health_state.json` и в ops-алертах.

## Запуск / Остановка

```bash
# Запуск (фоновый, с логом)
cd /path/to/maxtg_bridge
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &

# Проверить что работает
ps aux | grep 'python -m src.main' | grep -v grep
tail -20 data/bridge.log

# Остановить
kill $(ps aux | grep 'python -m src.main' | grep -v grep | awk '{print $2}')

# Перезапуск
kill $(ps aux | grep 'python -m src.main' | grep -v grep | awk '{print $2}')
sleep 2
nohup .venv/bin/python -m src.main >> data/bridge.log 2>&1 &
```

**ВАЖНО:** Никогда не запускать два экземпляра одновременно — два userbot = проблемы с MAX.

## Проверка здоровья

```bash
# Последние события
tail -50 data/bridge.log

# Статистика доставки
.venv/bin/python -c "
import asyncio, aiosqlite

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    db.row_factory = aiosqlite.Row
    print('=== Bindings ===')
    async with db.execute('SELECT * FROM chat_bindings') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    print('=== Recent deliveries ===')
    async with db.execute('SELECT * FROM delivery_log ORDER BY created_at DESC LIMIT 10') as cur:
        for r in await cur.fetchall():
            print(dict(r))
    await db.close()

asyncio.run(main())
"
```

Для production на Hetzner удобнее использовать:

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
cd /opt/maxtg-bridge
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Дополнительные runtime-артефакты:

- `data/health_state.json` — текущий persisted health snapshot supervisor-а
- `data/health_events.jsonl` — история переходов `healthy/degraded/recovering/recovered`
- `data/alert_outbox.jsonl` — системные алерты, которые не удалось отправить в Telegram сразу
- `data/health_heartbeat.json` — heartbeat для Docker healthcheck
- `data/session_backups/` — ring-buffer валидных MAX `session.db` snapshots; содержит auth token, права должны оставаться `0700/0600`

Быстрая диагностика:

```bash
cd /opt/maxtg-bridge
jq . data/health_state.json
tail -50 data/health_events.jsonl
wc -l data/alert_outbox.jsonl
jq . data/health_heartbeat.json
```

Как интерпретировать:

- контейнер `Up` + healthcheck `healthy` + `health_state.json` с `overall_status=degraded` означает: supervisor жив, но одна из подсистем сломана
- если `alert_outbox.jsonl` не пустой, Telegram ops-уведомления не ушли сразу и ждут автоматической досылки
- если проблема в MAX session, `/status` и `health_state.json` должны явно показывать `requires_reauth` / подсказку про SMS reauth
- при порче SQLite header у `data/session.db` bridge сначала пробует восстановить текущий token в clean copy; если не выходит — откатывается на свежий валидный snapshot из `data/session_backups/`
- по умолчанию outbox относится к owner DM; forum-topic fanout участвует только если настроен `ops_topic_id`

## MAX account recovery registry

Recovery registry нужен на случай нового телефона / нового MAX account. Он сохраняет не сообщения, а маршрутную карту восстановления: какие Telegram topics соответствуют MAX чатам, какие чаты требуют invite/admin, где есть invite link, кто owner/admin, и насколько свежий последний snapshot.

Что хранится в `data/bridge.db`:

- `max_account_generations` — поколения MAX аккаунта: `max_user_id`, masked phone, hash fingerprint сессии, статус и first/last seen.
- `chat_recovery_registry` — topic/chat registry: `tg_topic_id`, старый/текущий `max_chat_id`, тип чата, mode, access, invite link, owner/admin contacts, DM partner, participant count, manual note, recovery status, `last_scan_at`.
- `chat_recovery_events` — audit событий scan/set/remap/account change без текста сообщений и raw payload.

Команды владельца:

```text
/recovery scan
/recovery report
/recovery export
/recovery set <topic_id> key=value ...
/recovery remap <topic_id> <new_max_chat_id>
```

Полезные поля для `/recovery set`: `priority=9`, `status=manual_admin_required`, `note="попросить инвайт у Анны"`, `link=https://...`, `admin="Анна:12345"`, `owner="Олег:777"`.

Периодичность и auto-refresh:

- после успешного MAX connect bridge запускает безопасный recovery scan;
- дополнительно раз в неделю запускается `weekly_recovery_snapshot` как safety net;
- event-driven scan запускается асинхронно при важных MAX-side изменениях: создан новый `ChatBinding`, обновился fallback title, пришёл MAX `CONTROL` event;
- `new_binding` ставит high-priority scan примерно через 60 секунд и bypass cooldown; `title_changed` схлопывается коротким debounce; `control_event` имеет cooldown, чтобы не спамить MAX API;
- forwarding сообщений, создание topics и rename не ждут snapshot: scheduler ставит background task, а обычный routing продолжает работу;
- `/recovery report` показывает `Свежесть snapshot` по `last_scan_at`; это source of truth для freshness;
- `/recovery export` отправляет владельцу JSON в DM, включая invite links/admin notes, поэтому не пересылай export в общие чаты.

Auto notifications:

- автоматические scans пишут owner/ops только important-only digest: новый/unmapped чат, `needs_invite`, `manual_admin_required` или `account_migration_required`;
- уведомление содержит только counts/statuses и подсказку открыть `/recovery report`;
- invite links, admin/manual notes, phone numbers, message text, raw MAX fields и signed URLs не попадают в notification/log/health;
- одинаковый notification digest дедупится in-memory примерно на 24 часа.

Когда запускать `/recovery scan` вручную:

- сразу после reauth на новый телефон/MAX account;
- если `/recovery report` показывает устаревший `last_scan_at`;
- после того как админ пригласил новый аккаунт в закрытый чат;
- после ручного исправления notes/link/admin через `/recovery set`, если нужно сверить видимость текущего аккаунта.

Новый телефон / новый MAX account:

1. Сделать reauth с новым номером.
2. Выполнить `/recovery scan`.
3. Посмотреть `/recovery report`: `unmapped` — MAX чаты, видимые новому аккаунту, но ещё не привязанные к старым Telegram topics; `есть invite link` / `нужен админ` — ручные шаги доступа.
4. После invite/join выполнить `/recovery remap <topic_id> <new_max_chat_id>`.
5. Старый `message_map` остаётся для истории, но reply на старое TG сообщение после remap уходит в MAX без `reply_to`, если исходный MAX message был в старом `max_chat_id`.

## Сценарии отказов

Ниже короткая operator-матрица: что ломается, что система делает сама и когда нужен ручной шаг.

| Сценарий | Что делает система автоматически | Куда пишет | Нужен ли ручной шаг |
|---------|----------------------------------|------------|---------------------|
| `MAX disconnected` / краткий сетевой обрыв | MAX adapter держит reconnect-loop, supervisor не даёт контейнеру упасть, health переходит в `degraded`, после восстановления фиксируется `recovered` | owner DM, `health_state.json`, `health_events.jsonl` | Обычно нет |
| MAX reconnect длится слишком долго | bridge остаётся `Up`, watchdog и health snapshot показывают деградацию, периодический `/status` и 4h status отражают active issue | owner DM, `/status`, `health_state.json` | Иногда да, если внешний MAX реально недоступен долго |
| Event-driven recovery snapshot не собрался | forwarding не блокируется; bridge пишет безопасный warning и ждёт следующий event/weekly/manual scan | logs, `/recovery report` freshness | Да, если свежесть критична: выполни `/recovery scan` |
| Битый SQLite header у `data/session.db` | перед стартом MAX-клиента runtime пробует пересобрать clean `session.db` из текущего token; при успехе сохраняет битый файл в `data/session_backups/` и продолжает старт | logs, `data/session_backups/`, затем recovered health event | Обычно нет |
| `Invalid token` / нечитаемая MAX session без валидного snapshot / нужен `reauth` | runtime не падает, проблема классифицируется как issue MAX-сессии, оператор получает подсказку про SMS reauth | owner DM, `/status`, `health_state.json`, `health_events.jsonl` | Да, нужен `/reauth` и SMS-код |
| Telegram Bot API временно недоступен | `TelegramAdapter` делает retry, неотправленные системные alert-сообщения кладутся в `alert_outbox.jsonl`, после восстановления Telegram идёт automatic flush | owner DM после восстановления, `alert_outbox.jsonl`, `health_state.json` | Обычно нет |
| Падает сам bridge worker | supervisor перезапускает worker с backoff, контейнер остаётся `Up`, restart counter и причина попадают в health-state | owner DM, `health_state.json`, `health_events.jsonl` | Обычно нет, если crash разовый |
| Падает или зависает сам supervisor | Docker healthcheck перестаёт видеть heartbeat, контейнер получает `unhealthy`, дальше помогает `restart: always` и ручная проверка compose/logs | `docker ps`, `docker inspect`, `health_heartbeat.json` | Да, это уже runtime-level авария |
| Telegram-уведомление не удалось отправить сразу | сообщение не теряется, а сохраняется в outbox и досылается позже | `alert_outbox.jsonl` | Нет, если Telegram восстановился |
| MAX лежал долго и потом поднялся | bridge пытается восстановиться сам и шлёт `recovered`, но исторические сообщения за время простоя MAX не догружаются | owner DM, `/status`, `health_events.jsonl` | Возможно, если критично вручную проверить пропущенный период |

Что важно помнить:

- Автовосстановление покрывает временные transport/runtime проблемы, reconnect и crash worker.
- Автовосстановление не может само пройти SMS reauth за владельца.
- Даже при исправном runtime pymax не умеет полноценный history replay, поэтому сообщения за длительный downtime MAX могут быть потеряны.

## Режимы логирования

Поддерживаются env-переключатели:

```bash
LOG_LEVEL=INFO|DEBUG
LOG_FORMAT=text|json|mixed
LOG_PREVIEW_CHARS=120
LOG_LIBRARIES_DEBUG=0|1
LOG_TO_FILE=1|0
LOG_FILE=/custom/path/bridge.log
```

Рекомендуемый production-режим:

```bash
LOG_LEVEL=INFO
LOG_FORMAT=mixed
LOG_LIBRARIES_DEBUG=0
LOG_TO_FILE=1
```

Для детальной диагностики конкретного кейса:

```bash
LOG_LEVEL=DEBUG
LOG_FORMAT=mixed
LOG_PREVIEW_CHARS=120
```

Что важно:

- на `INFO` логируются route/outcome/meta без полного текста сообщений
- на `DEBUG` появляются safe preview текста
- `LOG_LIBRARIES_DEBUG=1` поднимает `pymax`/`aiogram`, использовать только временно
- по умолчанию лог пишется и в stdout, и в `${DATA_DIR}/bridge.log` (`./data/bridge.log` локально)

## Как искать трассу сообщения

Основные поля в логах:

- `event=...`
- `flow_id=mx:<chat_id>:<msg_id>` для MAX -> Telegram
- `flow_id=tg:<topic_id>:<tg_msg_id>` для Telegram -> MAX

Быстрые команды:

```bash
# все шаги по конкретному MAX-сообщению
rg 'flow_id=mx:-70000000000003:4242' data/bridge.log

# все шаги по конкретному Telegram-сообщению
rg 'flow_id=tg:99:777' data/bridge.log

# только завершения маршрута
rg 'event=bridge\.(inbound|outbound)\.forward_finished' data/bridge.log

# только retry/fail отправки в Telegram
rg 'event=tg\.outbound\.(retry|failed|sent)' data/bridge.log

# retry/fail отправки из Telegram в MAX
rg 'event=max\.outbound\.(retry|failed|sent)' data/bridge.log

# retry/resume/fail скачивания MAX-вложений
rg 'event=max\.attachment\.(download|download_retry|download_resume|video_fallback|audio_fallback|voice_reference_missing)' data/bridge.log

# voice diagnostics/recovery for pymax-empty events
rg 'event=max\.(raw|inbound)\.(empty_message|empty_recovery|auxiliary_event|handler_registered|interceptor_installed)' data/bridge.log

# последние неуспешные TG -> MAX доставки из SQLite
sqlite3 -header -column data/bridge.db \
  "SELECT max_msg_id, max_chat_id, error, attempts, datetime(created_at, 'unixepoch', 'localtime') AS created_local \
   FROM delivery_log \
   WHERE direction='outbound' AND status='failed' \
   ORDER BY created_at DESC LIMIT 20"
```

Полезные `event`-группы:

- `max.inbound.*` — что пришло из MAX и как нормализовали
- `bridge.inbound.*` — routing/dedup/topic resolution для MAX -> TG
- `tg.outbound.*` — отправка в Telegram, retry и fail
- `tg.inbound.*` — что пришло из Telegram и скачивание медиа
- `bridge.outbound.*` — routing/reply resolution и доставка TG -> MAX

## Reason codes

Нормальные/ожидаемые:

- `duplicate`
- `empty_event`
- `readonly`
- `disabled`

Требуют внимания:

- `no_topic`
- `too_large`
- `tg_send_failed`
- `max_send_failed`
- `download_rejected`
- `download_failed`
- `ack_timeout`

## Видео и большие вложения MAX

MAX-видео приходят через signed CDN URL. Bridge выбирает `User-Agent` по `srcAg` в URL (`CHROME`, `CHROME_ANDROID`, `CHROME_IPHONE`, Safari/iPhone fallback), потому что OK CDN может отвечать `400 Bad Request` на неподходящий клиент.

Загрузка медиа:

- до 7 попыток на файл;
- файл пишется во временный `*.part`;
- при обрыве следующая попытка отправляет `Range: bytes=<уже_скачано>-`;
- если CDN не поддерживает `Range` и отвечает `200`, bridge удаляет `*.part` и качает заново;
- если прямой URL видео не скачался, bridge пробует fallback через MAX `VIDEO_PLAY`;
- если ретриабельное видео или голосовое всё равно не скачалось, bridge отправляет остальные части сообщения сразу, показывает `⏳ Видео MAX #N докачивается...` / `⏳ Голосовое MAX #N докачивается...` и кладёт job в `pending_media_downloads`;
- повторный sweep той же voice/media-reference не отправляет второй queued-placeholder: существующий pending job переиспользуется по `media_chat_id/media_msg_id/attachment_index/kind/reference_*`;
- retry worker для видео заново получает playable URL через `VIDEO_PLAY`; для голосовых заново читает raw `CHAT_HISTORY`, пробует exact `MSG_GET`, dialog cache, MAX Web `audioGetSources` (`opcode=301`) и только затем известный pymax/userbot-safe `FILE_DOWNLOAD` payload (`fileId`) + legacy pymax `get_file_by_id`; signed URL/token/text не хранятся, медиа досылается в тот же Telegram topic отдельным сообщением. `audioId`/token payload для `FILE_DOWNLOAD` в prod вернул `proto.payload` и закрыл socket, поэтому он отключён.

Что смотреть в логах:

```bash
rg 'flow_id=mx:<chat_id>:<msg_id>' data/bridge.log
rg 'event=max\.attachment\.(download|download_retry|download_resume|video_fallback|audio_fallback|audio_protocol_probe|voice_reference_missing)' data/bridge.log
rg 'event=bridge\.media_retry\.(enqueued|attempt_started|retry_scheduled|delivered|failed)' data/bridge.log
rg 'event=bridge\.inbound\.forward_finished .*outcome=partial' data/bridge.log
```

Для CDN download-ошибок смотри поля `src_ag`, `ua_family`, `http_status` и `download_source`. Signed query-параметры URL в логах не должны появляться.

Очередь durable media retry:

```bash
sqlite3 -header -column data/bridge.db \
  "SELECT id, max_msg_id, max_chat_id, attachment_index, status, attempts, datetime(next_attempt_at, 'unixepoch', 'localtime') AS next_local, last_error \
   FROM pending_media_downloads \
   WHERE status IN ('pending','retry','leased') \
   ORDER BY next_attempt_at LIMIT 20"
```

В `delivery_log` для частичной доставки:

```bash
sqlite3 -header -column data/bridge.db \
  "SELECT max_msg_id, max_chat_id, status, error, datetime(created_at, 'unixepoch', 'localtime') AS created_local \
   FROM delivery_log \
   WHERE direction='inbound' AND status='partial' \
   ORDER BY created_at DESC LIMIT 20"
```

## Базовая живая smoke-проверка на тестовых чатах

Этот сценарий нужен после деплоя или после правок в routing.

### Подготовка

- выдели 1 тестовый MAX-чат или DM
- дождись, чтобы для него уже существовал Telegram topic
- не используй прод-критичный чат для первой проверки

### Проверка 1: MAX -> Telegram

1. Отправь в тестовый MAX-чат короткий текст, например: `SMOKE MAX -> TG`.
2. Убедись, что он появился в соответствующем Telegram topic.
3. Если это группа, проверь что в Telegram есть префикс отправителя `[Имя]`.

### Проверка 1b: MAX video -> Telegram

1. Отправь короткое видео в тестовый MAX-чат или DM.
2. Убедись, что в Telegram оно пришло именно видео-сообщением.
3. В логах проверь `max.attachment.download outcome=downloaded`, затем `tg.outbound.sent media_type=video`.
4. Если пришёл текст `⏳ Видео MAX #N докачивается...`, проверь `bridge.media_retry.enqueued`, затем `bridge.media_retry.delivered` и `tg.outbound.sent media_type=video`.
5. Если видео стало terminal failure, смотри `bridge.media_retry.failed` и строку в `pending_media_downloads.last_error`.

### Проверка 1c: MAX voice -> Telegram

1. Отправь короткое голосовое в тестовый MAX DM.
2. Убедись, что в Telegram topic пришёл native voice bubble.
3. В логах проверь `attachment_types=["AUDIO"]` или `["VOICE"]`, затем `tg.outbound.sent media_type=voice`.
4. Если MAX сначала отдаёт пустой typed `USER`, bridge делает recent-history recovery. Успешный raw-history путь виден как `max.raw.history_fetch outcome=received`, затем `max.inbound.empty_recovery outcome=recovered reason=raw_recent_history_match`, `raw_history_cache_match` или `raw_history_cache_after_fetch_error`.
5. Если raw `CHAT_HISTORY` задержался, bridge до 180 секунд держит in-memory wait job: сначала `max.inbound.empty_recovery outcome=queued reason=raw_history_cache_wait`, затем при успехе `reason=raw_history_cache_delayed_match`.
6. Если MAX/history всё ещё отдаёт пустой message без `attaches`, bridge кладёт meta-only retry в `data/pending_empty_recoveries.json` и перечитывает history без лимита по времени. Ищи `reason=durable_history_retry`, `retry_scheduled`, затем при успехе `durable_history_recovered`.
7. Каждые 120 секунд `bridge.dm_history_sweep.worker_started` перечитывает последние 30 сообщений активных DM за окно 48 часов и досылает пропущенные сообщения через обычный dedup path. При успехе ищи `max.history_sweep.replayed` и `tg.outbound.sent media_type=voice`.
8. Если voice распознан, но MAX пока не отдаёт скачиваемый файл, bridge ставит `kind=audio` в `pending_media_downloads`. Ищи `bridge.media_retry.enqueued`, `attempt_started`, `retry_scheduled`, затем `bridge.media_retry.delivered` и `tg.outbound.sent media_type=voice`.
9. Для protocol-level audio диагностики смотри `max.attachment.audio_protocol_probe`: там есть только candidate, outcome, error code/class и безопасная форма payload (`payload_fields/payload_shape`), без URL/token/text.
10. Если Telegram пустой, смотри `max.raw.empty_message`, `max.inbound.empty_message`, `max.inbound.empty_recovery` и `max.attachment.voice_reference_missing`. Эти diagnostics не должны содержать URL, token или текст сообщения. Для новых неизвестных форм полезны безопасные поля `element_count`, `element_types`, `element_fields`, `options_fields`.

## Phantom topics `Чат 1779...`

MAX raw payload может содержать `cid` — timestamp-like client id. Он не является `chat_id`; bridge пропускает такие события с `reason=probable_client_cid_chat_id` и не создаёт topic.

One-time cleanup ошибочно созданных topics:

```bash
python scripts/cleanup_phantom_topics.py
```

Скрипт выбирает только fallback bindings `Чат 1779...`, у которых тот же `max_msg_id` уже доставлен в настоящий чат, вызывает Telegram `delete_forum_topic`, а если Telegram отказал — `close_forum_topic`. После этого binding получает `mode='disabled'` и title `[deleted phantom] ...`.

### Проверка 2: Telegram -> MAX

1. В том же Telegram topic отправь сообщение, например: `SMOKE TG -> MAX`.
2. Убедись, что оно появилось в MAX.
3. Проверь, что в MAX сообщение пришло с префиксом автора Telegram:

```text
[Имя Фамилия]
SMOKE TG -> MAX
```

### Проверка 3: метаданные bridge

Сразу после ручной проверки выполни:

```bash
cd /opt/maxtg-bridge
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Ожидаемый результат:

- есть свежая запись `direction='inbound'` для MAX -> Telegram
- есть свежая запись `direction='outbound'` для Telegram -> MAX
- нет свежих `failed` записей по тестовому окну

### Когда применять

- после деплоя на новый сервер
- после изменений в `src/adapters/tg_adapter.py`
- после изменений в `src/adapters/max_adapter.py`
- после изменений в `src/bridge/core.py`

## Добавить новый чат

1. Найти `max_chat_id` из лога: `rg 'event=bridge.inbound.topic_resolved .*outcome=created' data/bridge.log`
2. Добавить в `config.local.yaml` раздел `chats:` (опционально — если нужен специфичный режим)
3. При `forward_all: true` чат добавится автоматически при первом сообщении

## Изменить режим чата

**Через DB напрямую:**
```bash
.venv/bin/python -c "
import asyncio, aiosqlite

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    await db.execute(\"UPDATE chat_bindings SET mode=? WHERE max_chat_id=?\",
                     ('readonly', '-70000000000001'))
    await db.commit()
    await db.close()
    print('done')

asyncio.run(main())
"
```

Режимы: `active` | `readonly` | `disabled`

## Переименовать топик вручную

Топик переименовывается автоматически когда:
1. Приходит новое сообщение и текущее название — fallback ("Чат XXXXXX")
2. Bridge корректно определяет имя из профиля MAX

Если нужно вручную — переименовать прямо в Telegram.

## Проблема: bridge не форвардит сообщения

1. Проверить что bridge запущен: `ps aux | grep 'python -m src.main'`
2. Проверить лог: `tail -100 data/bridge.log`
3. Проверить persisted health snapshot: `jq . data/health_state.json`
4. Проверить outbox алертов: `wc -l data/alert_outbox.jsonl`
5. Проверить delivery_log в DB (см. выше)
6. Если `SSL: TLSV1_ALERT_RECORD_OVERFLOW` → убедиться что используется `send_fake_telemetry=False`
7. Если `dialogs=NNN` растёт (>12 при каждом reconnect) → убедиться что `reconnect=False`
8. Если после reboot VM кажется, что bridge "не поднялся" → проверить `docker compose ps`, Docker healthcheck и startup-лог; в production сначала должны появиться `MAX connected`, потом `Running startup tests`, потом `Startup tests passed: ...`

## Проблема: "❌ Не удалось отправить сообщение в MAX"

Теперь bridge сначала делает до 3 попыток отправки при временных транспортных ошибках MAX:

- `Socket is not connected`
- `Must be ONLINE session`
- timeout / broken pipe / connection reset

Если после retry сообщение всё равно не ушло:

- в Telegram появится `❌ Не удалось отправить сообщение в MAX`
- в `delivery_log` появится запись `direction='outbound'`, `status='failed'`
- в `error` будет последняя причина, а в `attempts` — число попыток

Быстрая проверка:

```bash
sqlite3 -header -column data/bridge.db \
  "SELECT max_msg_id, error, attempts, datetime(created_at, 'unixepoch', 'localtime') AS created_local \
   FROM delivery_log \
   WHERE direction='outbound' AND status='failed' \
   ORDER BY created_at DESC LIMIT 10"
```

И в логах:

```bash
rg 'event=max\.outbound\.(retry|failed|sent)' data/bridge.log
```

## Проблема: сообщение не появилось (потеряно)

pymax **не воспроизводит историю** после reconnect. Сообщения отправленные во время downtime теряются.
Это известное ограничение — не баг.

## Обновление зависимостей

```bash
source .venv/bin/activate
pip install --upgrade maxapi-python aiogram aiosqlite
# Проверить что всё работает
python -m src.main
```

## Очистка старых данных вручную

```bash
.venv/bin/python -c "
import asyncio, aiosqlite, time

async def main():
    db = await aiosqlite.connect('data/bridge.db')
    cutoff = int(time.time()) - 30 * 86400
    await db.execute('DELETE FROM message_map WHERE created_at < ?', (cutoff,))
    await db.execute('DELETE FROM delivery_log WHERE created_at < ?', (cutoff,))
    await db.commit()
    await db.close()
    print('cleanup done')

asyncio.run(main())
"
```

Автоочистка запускается каждые 30 минут автоматически.
