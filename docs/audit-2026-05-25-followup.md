# Audit 2026-05-25 — Follow-up Actions

**Контекст:** [docs/audit-2026-05-25.md](audit-2026-05-25.md) — канонический аудит. Wave 1-4 закрыты на ~75%. Этот файл — оставшиеся пункты с конкретными acceptance criteria для исполняющей LLM. Каждый пункт имеет **Why / Where / How / Done-when**.

**Status:** реализовано в follow-up. Единственная осознанная коррекция acceptance criteria — не накручивать искусственно `logger.exception` count в `forwarding.py`/`replies.py`, где нет top-level broad catch; traceback добавлен на реальной recovery scheduler boundary, а остальные пути оставлены typed/targeted.

Порядок секций = порядок исполнения. Внутри секции пункты независимые.

---

## 1. 🔴 Критичное — доделать обещанное

### 1.1 Применить `with_timeout` к реальным external awaits
**Why.** На момент follow-up helper [src/runtime/timeouts.py](../src/runtime/timeouts.py) и типизированное `BridgeExternalTimeout` были готовы и покрыты тестами, но `grep -rn 'with_timeout' src/` показывал **0 фактических call-сайтов** (только сам timeouts.py). Зависший MAX/TG-сокет всё ещё мог держать worker вечно. Wave 1 §3.4 был формально done, фактически not effective.

**Where.** Три места с обоснованными лимитами:
1. **MAX outbound send** — [src/adapters/max/send.py](../src/adapters/max/send.py), все `await client.send_message(...)` / send-attachment пути → `DEFAULT_OPERATION_TIMEOUT_SECONDS` (30s).
2. **TG outbound send** — [src/adapters/tg/adapter.py](../src/adapters/tg/adapter.py), все `await self._bot.send_*(...)` (send_message, send_photo, send_video, send_document, send_audio, send_voice) → 30s.
3. **MAX media download (HTTP)** — [src/adapters/max/media/downloader.py](../src/adapters/max/media/downloader.py), каждый external HTTP `await` (chunk read, full download) → `MEDIA_TRANSFER_TIMEOUT_SECONDS` (120s).

**How.** Паттерн:
```python
from ...runtime.timeouts import with_timeout, DEFAULT_OPERATION_TIMEOUT_SECONDS

result = await with_timeout(
    client.send_message(...),
    timeout_seconds=DEFAULT_OPERATION_TIMEOUT_SECONDS,
    operation="max.send_message",
)
```
`BridgeExternalTimeout` — это `BridgeTransientError + TimeoutError`. Поймать его выше **там, где уже есть retry-классификация** (например в `is_retryable_send_error` в [src/adapters/max/errors.py](../src/adapters/max/errors.py)) и относить к retryable. Не добавлять отдельный handling — пусть встраивается в существующий retry path.

**Done-when:**
- `grep -rn 'with_timeout' src/` показывает ≥6 call-сайтов вне `timeouts.py`.
- Регрессионный тест: мок `client.send_message` зависает на `asyncio.sleep(60)`, send вызывается с timeout=1 → `BridgeExternalTimeout` поднимается за ~1s, ловится retry-слоем.

**Effort.** 2-3 часа.

---

### 1.2 Атомарный write health snapshot JSON
**Why.** Если `kill -9` происходит мид-write `health_state.json` / `health_heartbeat.json`, файл остаётся повреждённым → следующий старт bridge падает на JSON parse. Учитывая что Wave 1 закрыл graceful shutdown, остаётся защита именно от kill -9 / OOM.

**Where.** [src/runtime/health/store.py](../src/runtime/health/store.py) — проверить ВСЕ места где пишутся JSON-файлы (`health_state.json`, `health_heartbeat.json`, `health_events.jsonl`, `alert_outbox.jsonl`). Для append-only `.jsonl` — append через `open(..., 'a')` атомарен на POSIX до размера PIPE_BUF (~4KB на Linux), поэтому касается только полных rewrite файлов.

**How.** Если ещё не атомарно — единый паттерн:
```python
def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)   # POSIX rename — атомарно
```
Уже используется в [src/runtime/health/metrics.py:122-130](../src/runtime/health/metrics.py#L122-L130) — пере-использовать паттерн.

**Done-when:**
- Все full-rewrite JSON-файлы в `health/store.py` идут через `tmp + rename`.
- Тест: записать большой JSON, прервать процесс `os.kill(os.getpid(), SIGKILL)` мид-write → после рестарта файл либо валидный старый, либо валидный новый, не corrupt.

**Effort.** 30 минут на проверку + правку.

---

## 2. 🟠 Полезное — продолжение Wave 4

### 2.1 `logger.exception` на top-boundary путях
**Why.** Сейчас во всём `src/` только **3** вызова `logger.exception(...)`, остальные 76 `except Exception:` теряют traceback. При странной prod-ошибке оператор получает одну строку без stack trace. Полностью пройти 76 мест — большая работа; ограничиться boundary-путями где это критично.

**Where.** Три файла с самым большим diagnostic-impact:
- [src/bridge/forwarding.py](../src/bridge/forwarding.py) (~553L) — основной forward-loop.
- [src/bridge/replies.py](../src/bridge/replies.py) — TG→MAX reply путь.
- [src/bridge/recovery/orchestrator.py](../src/bridge/recovery/orchestrator.py) — recovery scan.

**How.** В каждом найти все `except Exception as e:` / `except Exception:` и:
1. Если catch protect-ит конкретную операцию (network call, repo write) — заменить `logger.error("... %s", e)` на `logger.exception("...", **structured_fields)`.
2. Если catch на top-loop boundary где re-raise = смерть worker'а — оставить broad catch, но добавить `logger.exception(...)` с фиксированным event-name из словаря.
3. **Не** трогать `except asyncio.CancelledError: raise` — это правильный pattern.

**Done-when:**
- `logger.exception` count в этих 3 файлах ≥10.
- Любая ошибка в forward-loop оставляет в логах полный traceback с `flow_id`, `max_chat_id`, `tg_topic_id`.

**Effort.** 2-3 часа.

---

### 2.2 README "Why this is non-trivial" блок
**Why.** Текущий [README.md:39-52](../README.md#L39-L52) "Engineering Highlights" корректный, но ровный — для FAANG-ревьюера за 30 секунд скана не выделяет hard parts. Явный блок "почему это нетривиально" с 6-bullet shim-списком бьёт сильнее.

**Where.** [README.md](../README.md) — добавить новый раздел **перед** "How It Works", после tagline. Дублировать в [README-ru.md](../README-ru.md).

**How.** Точный текст (EN):
```markdown
## Why this is non-trivial

1. MAX API is undocumented and reverse-engineered. The upstream Python wrapper (`maxapi-python`) is beta-quality.
2. The bridge runs 24/7 on a single VPS with a single MAX account — any unhandled disconnect = silent message loss.
3. We mitigate this with **6 surgical PyMax compatibility shims**, each ~50 LoC, each covered by a regression-marker test:
   - `BridgeSessionStore` — one-shot import of legacy PyMax v1 session table into v2 schema
   - `BridgeConnectionManager` — wraps TCP sequence numbers at 256 (PyMax / MAX server divergence)
   - `BridgeMsgpackPayloadCodec` — handles MAX maps with array-valued keys that strict msgpack rejects
   - `BridgeAuthService` + `sanitize_login_payload` — strip upstream-unknown `UNSUPPORTED` attachment variants before validation
   - `EgressTCPTransport` — injects authenticated HTTP CONNECT proxy for RU-only egress
   - `PymaxInternalsContractError` — centralized accessor for private PyMax attrs that fails loudly on upstream drift

   Each shim has a regression-marker test (`pymax_tcp_sequence_overflow`, etc.) so upstream upgrades fail loud, not silent.
4. Architecture replaceability is **verified, not asserted**: `tests/integration/test_bridge_end_to_end.py` runs the full bridge against `tests/fakes/fake_max_backend.py` in CI. See `examples/swap_max_backend.py` for a 30-second runnable demo.
```

**Done-when:**
- Раздел присутствует в обоих README, аналогичен по смыслу (русский — перевод, не другой текст).
- 6 shim-классов в bullet-списке существуют в коде (проверить grep'ом перед коммитом — если что-то переименовано, обновить bullet).

**Effort.** 30 минут.

---

## 3. 🟡 Архитектурные/документационные пробелы

### 3.1 Review актуальности `docs/architecture.md` после Wave 1-4
**Why.** [docs/architecture.md](../docs/architecture.md) — 718L. После Wave 1-4 в коде появились **новые крупные модули**, которые архитектурный doc может не покрывать: `src/runtime/tasks.py`, `src/runtime/timeouts.py`, `src/runtime/health/metrics.py`, `src/bridge/errors.py`, `src/adapters/max/backends/pymax/internals.py`, `Repository.transaction()`. Если архитектурный doc их не упоминает — он молчаливо устаревает, и через 3 месяца уже неточен.

**Where.** [docs/architecture.md](../docs/architecture.md) — прочитать целиком (это большой файл).

**How.** Пройти по чек-листу:
1. **Supervisor/runtime секция** — упоминает ли `BridgeSupervisor.run(stop_event=...)`, exponential backoff, `cancel_and_wait`?
2. **Persistence секция** — описано ли `Repository.transaction()` и правило "не держать транзакцию вокруг network await"?
3. **Observability секция** — есть ли упоминание Prometheus textfile (`data/maxtg_bridge.prom`)? Перечислены ли экспортируемые метрики?
4. **Async patterns секция** — описан ли `create_logged_task` как канонический способ запуска detached worker'а?
5. **External calls** — упомянуты ли `with_timeout` и `BridgeExternalTimeout` (после §1.1 — обязательно)?
6. **Boundary tests** — обновлён ли список с новыми регрессионными тестами (`test_pymax_surface_pin`, integration `test_bridge_end_to_end`)?
7. **Diagrams** — если есть mermaid component diagram, добавить ли в неё `runtime/tasks`, `runtime/timeouts`, `runtime/health/metrics`?

**Done-when:**
- Каждый из 7 пунктов выше явно покрыт (или принято решение оставить как есть с обоснованием).
- В конце документа добавлена строка "Last reviewed: 2026-MM-DD against commit `<sha>`" — чтобы будущий ревьюер видел свежесть.

**Effort.** 1-2 часа.

---

### 3.2 Добавить `docs/architecture-tour.md` (one-pager для ревью)
**Why.** Был в исходном аудите как **P0 portfolio item**, в реализацию не попал. Реально нужен: ревьюер открывает репо, видит 200-LoC walkthrough с диаграммой — за 10 минут понимает суть. Без него ревьюер либо читает 718L architecture.md (слишком долго), либо ничего (плохой первый impression).

**Where.** Новый файл [docs/architecture-tour.md](../docs/architecture-tour.md) (~200 строк). Линковать из README.md рядом с "How It Works".

**How.** Структура:
1. **Why this exists** (1 абзац) — мандатный мессенджер в школах, нет TG-клиента, нет API.
2. **One sequence diagram (mermaid)** — MAX message → `events.py` → `BridgeCore.forward` → `topics.ensure` → `tg.adapter.send` → `message_map` row.
3. **One component diagram (mermaid)** — три коробки (BridgeCore, MaxAdapter, TgAdapter), стрелки помечены Protocol-портами (`MaxBridgePort`, `TgBridgePort`). Pymax/aiogram только внутри adapter-коробок. SQLite — отдельная коробка с Repository facade.
4. **Replaceability story** (1 абзац) — "MAX backend замена = новый пакет под `backends/`. Доказательство: [tests/integration/test_bridge_end_to_end.py](../tests/integration/test_bridge_end_to_end.py) гоняет полный bridge против [tests/fakes/fake_max_backend.py](../tests/fakes/fake_max_backend.py) каждую CI-сборку. Запустить демо: `python examples/swap_max_backend.py`."
5. **Fragility-mitigation table** — таблица: `Класс` | `Файл` | `Решает что` | `Регрессионный тест`. 6 строк (те же shim'ы что в §2.2). Это уникальная инженерная история.
6. **Where to read next** — links: ADR корпус, runbooks, audit-doc.

Никаких новых сущностей не вводить. Просто свести существующее в один компактный документ.

**Done-when:**
- Файл существует, ~200 LoC, рендерится в GitHub (mermaid syntax корректный).
- Линк добавлен в README обоих языков.
- Тест в CI проверяющий что mermaid-блоки парсятся (опционально).

**Effort.** 2 часа (без диаграмм — 1 час).

---

## 4. ⚪ Превентивные — на потом

Делать только если §1-§3 закрыты.

### 4.1 Health snapshot `schema_version` field
**Why.** Если когда-то поменяется shape `health_state.json` — старый файл сломает старт после деплоя. Сейчас нет защиты.

**How.** Добавить `schema_version: int = 1` в dataclass snapshot'а, при загрузке if mismatch → log warning + start fresh (не падать).

**Effort.** 15 минут.

---

### 4.2 Архивация `docs/migration-pymax-2.0.md`
**Why.** 1166-строчный журнал миграции засоряет `docs/`. ADR-010 уже компактно резюмирует решение. Журнал должен быть архивом, не активным doc.

**How.** `git mv docs/migration-pymax-2.0.md docs/archive/migration-pymax-2.0.md`. Поправить ссылки в ADR-010 и других местах (grep'нуть `migration-pymax-2.0`).

**Effort.** 10 минут.

---

### 4.3 Регрессионный тест для DM history sweep throttle
**Why.** Commit `7ef13f1` "chore: throttle MAX history recovery" ввёл балансировку (warmup 120s, steady 900s, jitter, per-chat delay — per CLAUDE.md §3). Тестового покрытия для throttle config я не видел. Без regression test'а кто-то может в будущем убрать throttle и не заметить regression на rate-limit.

**How.** Unit-тест на helper, который читает throttle config и проверяет: warmup → steady transition timing, jitter в нужном диапазоне, per-chat delay не 0.

**Effort.** 1 час.

---

### 4.4 Coverage report артефакт в CI
**Why.** CI gate `--cov-fail-under=75` есть. Но `term-missing` report виден только в stdout логе. Артефакт coverage-report (HTML или JSON) полезен для ревью PR.

**How.** `pytest --cov=src --cov-report=html --cov-report=xml` + `upload-artifact` в [.github/workflows/tests.yml](../.github/workflows/tests.yml).

**Effort.** 15 минут.

---

## 5. Что НЕ делать сейчас

Подтверждаю относительно [docs/audit-2026-05-25.md §7](audit-2026-05-25.md) список "не делать". После Wave 1-4 ничего из него не стало приоритетом.

Дополнительно **не делать**:
- **Полный обход 76 `except Exception:` для типизации** — §2.1 покрывает high-impact места. Остальные не блокируют ничего.
- **Разрезание больших файлов** (`media/attachments.py` 969L, `voice_recovery.py` 871L, etc.) — не блокер для разработки. Делать только если конкретная фича вынуждает.
- **`requirements.lock` через pip-tools** — пока `maxapi-python==2.0.1` exact-pin держит главный риск, остальные минорные обновления безопасны.
- **TG-DM SMS provider** — UX upgrade, но reauth случается раз в полгода. Откладывать.

---

## 6. Краткий handoff

Делать в порядке: §1.1 → §1.2 → §2.1 → §2.2 → §3.1 → §3.2 → §4 (по желанию).

После каждой группы — отдельный коммит с conventional commit message (`fix:`, `feat:`, `docs:`, `test:`). Не сваливать в один большой PR.

После §1.1+§1.2 запустить full suite (`pytest`) — должен пройти. Затем локальный smoke: `python -m src.main` против test config'а, проверить что `data/maxtg_bridge.prom` пишется, `data/health_*.json` обновляются, `docker stop` корректно завершается без warning'ов о незавершённых task'ах в логах.

После §2.2+§3.1+§3.2 — попросить ревьюера-человека (или другую LLM) пройтись по README + architecture-tour.md и сказать "за 5 минут я понял что это за проект и почему он непрост" — это критерий успеха для portfolio-трека.
