"""
Каталог правил внешнего watchdog.

Каждое правило привязано к классу отказа из модели (docs/runbooks/watchdog.md),
чтобы из полученного алерта за один шаг находилось действие оператора.

Две вещи, которые здесь важнее самих проверок:

* **Подавление каскадов.** Если хост недоступен, бессмысленно отдельно кричать
  про контейнер, heartbeat и диск — мы про них просто ничего не знаем. Правила
  верхнего уровня глушат зависимые, а те помечаются как "нет данных" и не
  обнуляют свои счётчики.
* **Гистерезис.** Алерт уходит только после N подряд неудачных проверок, а
  recovery — сразу и в обход dedup. Это разница между наблюдателем и генератором
  шума.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple, Optional

from .config import WatchdogConfig
from .state import WatchdogState

# Классы отказов из модели. Держим строками, чтобы код и документация не разъезжались.
F_WORKER_CRASH = "F1"
F_WORKER_HANG = "F2"
F_MAX_LINK = "F3"
F_MAX_EGRESS = "F4"
F_MAX_REAUTH = "F5"
F_ALERT_CHANNEL = "F6"
F_STORAGE = "F7"
F_CONTAINER_DOWN = "F8"
F_HOST_DOWN = "F9"
F_DISK = "F10"
F_RESTART_STORM = "F11"
F_OBSERVER_PATH = "F13"
F_CONFIG_DRIFT = "F15"
F_QUEUES = "F16"

CRIT = "crit"
WARN = "warn"

#: Человекочитаемые названия правил. Нужны там, где сообщение адресовано человеку
#: (recovery, сводка): "container_down" ничего не говорит, "Контейнер bridge" — говорит.
RULE_TITLES = {
    "host_unreachable": "Production-хост",
    "ssh_probe_failed": "Путь опроса production-хоста",
    "container_down": "Контейнер bridge",
    "container_unhealthy": "Docker healthcheck контейнера",
    "heartbeat_stale": "Heartbeat bridge",
    "restart_storm": "Перезапуски контейнера",
    "disk_low": "Свободное место на production-хосте",
    "status_api_unreachable": "Status API bridge",
    "overall_degraded": "Общее состояние bridge",
    "subsystem_issue": "Подсистемы bridge",
    "alert_outbox_backlog": "Доставка алертов bridge",
    "queue_backlog": "Очереди доставки",
    "egress_mode_unexpected": "Режим MAX egress",
    "push_stale": "Push-сигналы с production-хоста",
}


def rule_title(rule: str) -> str:
    return RULE_TITLES.get(rule, rule)


#: Каким слоем наблюдения поймано правило. Из слоя сразу следует, что чинить:
#: у каждого слоя свой набор компонентов и своя команда для проверки.
RULE_LAYERS = {
    # L1 — данные пришли из status API самого bridge
    "overall_degraded": "L1",
    "subsystem_issue": "L1",
    "alert_outbox_backlog": "L1",
    "queue_backlog": "L1",
    "egress_mode_unexpected": "L1",
    "status_api_unreachable": "L1",
    # L2 — данные пришли из опроса хоста снаружи
    "host_unreachable": "L2",
    "ssh_probe_failed": "L2",
    "container_down": "L2",
    "container_unhealthy": "L2",
    "heartbeat_stale": "L2",
    "restart_storm": "L2",
    "disk_low": "L2",
    # L3 — встречный push-канал
    "push_stale": "L3",
}

#: Что смотреть при разборе, в порядке «от самого вероятного».
LAYER_DIAGNOSTICS = {
    "L1": "status API bridge — docker logs deploy-bridge-1 | grep status_api; curl localhost:18140/healthz",
    "L2": "опрос с наблюдателя — docker compose logs watchdog; ssh -i <ключ> deploy@<prod> (должен вернуть JSON)",
    "L3": "push с production — systemctl status maxtg-watchdog-push.timer; journalctl -u maxtg-watchdog-push",
    "L4": "сам наблюдатель — docker ps | grep maxtg-watchdog на хосте-наблюдателе",
}

LAYER_NAMES = {
    "L1": "status API bridge",
    "L2": "опрос с наблюдателя",
    "L3": "push-канал",
    "L4": "мета-мониторинг",
}

#: Что слой ловит — человеческими словами, без терминов.
#: Нужно в ежедневной сводке: перечислить слои мало, надо ещё объяснить,
#: за чем каждый следит, иначе список названий ничего не говорит.
LAYER_PURPOSE = {
    "L1": "что болит внутри bridge",
    "L2": "жив ли контейнер и хост",
    "L3": "не оборвалась ли связь с наблюдателем",
    "L4": "жив ли сам наблюдатель",
}


class LayerReport(NamedTuple):
    """Отчёт одного слоя: вердикт и конкретика, на которой он основан.

    Без `checked` сводка говорит «отвечает», но не говорит, что именно
    проверено, — и по ней нельзя понять, что система действительно смотрела.
    """

    status: str
    checked: str = ""


def rule_layer(rule: str) -> str:
    return RULE_LAYERS.get(rule, "L2")


def layer_hint(rule: str) -> str:
    return LAYER_DIAGNOSTICS.get(rule_layer(rule), "")


def humanize_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "меньше минуты"
    if seconds < 90:
        return f"{int(seconds)} с"
    minutes = int(seconds) // 60
    if minutes < 90:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч {minutes % 60:02d} мин"


#: Правила, для которых порог подряд идущих сбоев фиксирован.
FAIL_AFTER = {
    "host_unreachable": 2,
    "ssh_probe_failed": 2,
    "container_down": 1,
    "container_unhealthy": 3,
    "heartbeat_stale": 2,
    "restart_storm": 1,
    "disk_low": 2,
    "status_api_unreachable": 2,
    "subsystem_issue": 2,
    "queue_backlog": 2,
    "egress_mode_unexpected": 2,
    "push_stale": 1,
}


@dataclass(frozen=True)
class Finding:
    rule: str
    failure_class: str
    severity: str
    title: str
    detail: str
    hint: str


class Evaluation(NamedTuple):
    findings: dict[str, Finding]
    #: правила, по которым в этом цикле нет данных — счётчики не трогаем
    skipped: set[str]


class Recovery(NamedTuple):
    rule: str
    #: сколько проблема продержалась; 0 — если начало неизвестно
    duration_seconds: int


class Decision(NamedTuple):
    alerts: list[Finding]
    recoveries: list[Recovery]


def fail_after(rule: str, cfg: WatchdogConfig) -> int:
    """Порог для правил, чей grace задан в секундах, считается из интервала опроса."""
    if rule == "overall_degraded":
        return max(1, cfg.degraded_grace_seconds // max(1, cfg.status_interval_seconds))
    if rule == "alert_outbox_backlog":
        return max(1, cfg.outbox_grace_seconds // max(1, cfg.status_interval_seconds))
    return FAIL_AFTER.get(rule, 2)


def _minutes(seconds: Optional[int]) -> str:
    if seconds is None:
        return "неизвестно"
    if seconds < 120:
        return f"{int(seconds)} с"
    return f"{int(seconds) // 60} мин"


def _push_age(obs: dict[str, Any], now: int) -> Optional[int]:
    push = obs.get("push") or {}
    received_at = push.get("received_at")
    if not received_at:
        return None
    return max(0, now - int(received_at))


def _evaluate_container(probe: dict[str, Any], cfg: WatchdogConfig) -> dict[str, Finding]:
    findings: dict[str, Finding] = {}
    container = probe.get("container") or {}

    if not container.get("found") or container.get("state") != "running":
        state = "отсутствует" if not container.get("found") else str(container.get("state"))
        exit_code = container.get("exit_code")
        suffix = f", exit code {exit_code}" if exit_code is not None else ""
        findings["container_down"] = Finding(
            rule="container_down",
            failure_class=F_CONTAINER_DOWN,
            severity=CRIT,
            title="Контейнер bridge не работает",
            detail=f"Контейнер {cfg.container_name}: {state}{suffix}.",
            hint=(
                "Docker restart: always не действует на явную остановку. "
                "Подними вручную: docker compose --project-name deploy "
                "-f deploy/docker-compose.prod.yml up -d bridge"
            ),
        )
        # Остальные контейнерные правила бессмысленны, пока он лежит.
        return findings

    if container.get("health") == "unhealthy":
        findings["container_unhealthy"] = Finding(
            rule="container_unhealthy",
            failure_class=F_WORKER_HANG,
            severity=WARN,
            title="Docker healthcheck: unhealthy",
            detail=f"Контейнер {cfg.container_name} запущен, но healthcheck не проходит.",
            hint="HEALTHCHECK не рестартует контейнер сам. Смотри логи и heartbeat.",
        )
    return findings


def _evaluate_status(status: dict[str, Any], cfg: WatchdogConfig, now: int) -> dict[str, Finding]:
    findings: dict[str, Finding] = {}

    if status.get("overall_status") not in (None, "healthy"):
        findings["overall_degraded"] = Finding(
            rule="overall_degraded",
            failure_class=f"{F_WORKER_CRASH}/{F_STORAGE}",
            severity=WARN,
            title="Bridge держится в состоянии degraded",
            detail=(
                f"overall_status={status.get('overall_status')}, "
                f"рестартов worker: {status.get('worker_restart_count')}."
            ),
            hint="Проверь /status в Telegram и логи: что-то не выходит из деградации само.",
        )

    worst: Optional[Finding] = None
    for subsystem in status.get("subsystems") or []:
        issue = subsystem.get("issue")
        if not issue:
            continue
        requires_reauth = bool(issue.get("requires_reauth"))
        severity = CRIT if requires_reauth or issue.get("severity") == "critical" else WARN
        if requires_reauth:
            failure_class, hint = F_MAX_REAUTH, (
                "Останови bridge и выполни scripts/max_reauth.py — автоматического "
                "reauth нет и быть не должно."
            )
        elif issue.get("code") == "max_egress_unavailable":
            failure_class, hint = F_MAX_EGRESS, (
                "Рестарт не поможет: проверь reverse Channel M на роутере и VPS-listener."
            )
        else:
            failure_class, hint = F_MAX_LINK, "Смотри подсистему в /status и логи bridge."
        candidate = Finding(
            rule="subsystem_issue",
            failure_class=failure_class,
            severity=severity,
            title=f"Подсистема {subsystem.get('name')} деградировала",
            detail=f"{issue.get('summary')} (код {issue.get('code')}).",
            hint=hint,
        )
        if worst is None or (worst.severity != CRIT and candidate.severity == CRIT):
            worst = candidate
    if worst is not None:
        findings["subsystem_issue"] = worst

    outbox = int(status.get("alert_outbox_size") or 0)
    if outbox > 0:
        findings["alert_outbox_backlog"] = Finding(
            rule="alert_outbox_backlog",
            failure_class=F_ALERT_CHANNEL,
            severity=WARN,
            title="Bridge не может доставить свои алерты в Telegram",
            detail=f"В alert_outbox накопилось сообщений: {outbox}.",
            hint=(
                "Это тот случай, ради которого нужен внешний наблюдатель: "
                "собственный канал алертов bridge сейчас не работает. Проверь bot token и сеть."
            ),
        )

    stale_queues = []
    for name, queue in (status.get("queues") or {}).items():
        oldest = (queue or {}).get("oldest_created_at")
        if oldest and (now - int(oldest)) > cfg.queue_max_age_seconds:
            stale_queues.append(f"{name}: {_minutes(now - int(oldest))}")
    if stale_queues:
        findings["queue_backlog"] = Finding(
            rule="queue_backlog",
            failure_class=F_QUEUES,
            severity=WARN,
            title="Очереди доставки не разгребаются",
            detail="Старейшие недоставленные записи — " + ", ".join(sorted(stale_queues)) + ".",
            hint="Проверь MAX/TG-связность и логи retry-задач.",
        )

    active_egress = status.get("max_egress_active")
    if active_egress and cfg.expected_egress and active_egress != cfg.expected_egress:
        findings["egress_mode_unexpected"] = Finding(
            rule="egress_mode_unexpected",
            failure_class=F_CONFIG_DRIFT,
            severity=WARN,
            title="MAX работает не через ожидаемый egress",
            detail=f"Активен {active_egress}, ожидался {cfg.expected_egress}.",
            hint="Аварийный direct-режим не должен оставаться включённым: верни home_ru_proxy.",
        )

    return findings


def evaluate(
    obs: dict[str, Any],
    state: WatchdogState,
    cfg: WatchdogConfig,
    now: int,
) -> Evaluation:
    """Считает, какие правила сработали прямо сейчас, с подавлением каскадов."""
    findings: dict[str, Finding] = {}
    skipped: set[str] = set()

    host_rules = {
        "ssh_probe_failed",
        "container_down",
        "container_unhealthy",
        "heartbeat_stale",
        "restart_storm",
        "disk_low",
        "status_api_unreachable",
    }
    status_rules = {
        "overall_degraded",
        "subsystem_issue",
        "alert_outbox_backlog",
        "queue_backlog",
        "egress_mode_unexpected",
    }

    # push_stale считается всегда: это независимый канал, он и различает F9 и F13.
    if cfg.push_secret:
        push_age = _push_age(obs, now)
        if push_age is None or push_age > cfg.push_max_age_seconds:
            findings["push_stale"] = Finding(
                rule="push_stale",
                failure_class=f"{F_HOST_DOWN}/{F_OBSERVER_PATH}",
                severity=CRIT,
                title="Прекратились push-сигналы с production-хоста",
                detail=(
                    "Пуш ни разу не приходил." if push_age is None
                    else f"Последний push {_minutes(push_age)} назад."
                ),
                hint="Проверь maxtg-watchdog-push.timer на production-хосте и сеть до наблюдателя.",
            )
    else:
        skipped.add("push_stale")

    if not obs.get("reachable", False):
        findings["host_unreachable"] = Finding(
            rule="host_unreachable",
            failure_class=F_HOST_DOWN,
            severity=CRIT,
            title="Production-хост не отвечает",
            detail=f"{cfg.target_name}: нет TCP-ответа на порту {cfg.ssh_port}.",
            hint="Проверь консоль провайдера: хост, сеть или firewall.",
        )
        skipped.update(host_rules | status_rules)
        return Evaluation(findings, skipped)

    probe = obs.get("probe") or {}

    if not obs.get("ssh_ok", False):
        # Пуш несёт тот же снимок состояния, поэтому при живом push мы не слепнем:
        # опрос деградирует до резервного источника, а не до полного отсутствия данных.
        push_age = _push_age(obs, now)
        fresh = push_age is not None and push_age <= cfg.push_max_age_seconds
        fallback = ((obs.get("push") or {}).get("payload") or {}).get("probe") or {}
        usable = fresh and isinstance(fallback, dict) and not fallback.get("probe_failed")

        findings["ssh_probe_failed"] = Finding(
            rule="ssh_probe_failed",
            failure_class=F_OBSERVER_PATH,
            severity=WARN if usable else CRIT,
            title=(
                "Сломан путь опроса, данные идут через push" if usable
                else "Не удаётся опросить production-хост"
            ),
            detail=(
                (
                    f"SSH-проба не проходит ({obs.get('ssh_error', 'нет деталей')}), "
                    f"но push приходит ({_minutes(push_age)} назад) — состояние bridge "
                    "оцениваем по нему."
                )
                if usable
                else f"SSH-проба не проходит: {obs.get('ssh_error', 'нет деталей')}."
            ),
            hint=(
                "Проверь UFW/Cloud Firewall на 22 порт, fail2ban и sshd. "
                "Наблюдение продолжается через push, но резерва у него уже нет."
            ),
        )

        if not usable:
            skipped.update(host_rules - {"ssh_probe_failed"})
            skipped.update(status_rules)
            return Evaluation(findings, skipped)
        probe = fallback

    findings.update(_evaluate_container(probe, cfg))
    if "container_down" in findings:
        # Контейнер лежит: heartbeat и status API заведомо мертвы, не дублируем.
        skipped.update({"container_unhealthy", "heartbeat_stale", "status_api_unreachable"})
        skipped.update(status_rules)

    heartbeat = probe.get("heartbeat") or {}
    if "heartbeat_stale" not in skipped:
        age = heartbeat.get("age_seconds")
        if not heartbeat.get("present") or age is None or int(age) > cfg.heartbeat_max_age_seconds:
            findings["heartbeat_stale"] = Finding(
                rule="heartbeat_stale",
                failure_class=F_WORKER_HANG,
                severity=CRIT,
                title="Heartbeat bridge протух",
                detail=(
                    "Файл heartbeat отсутствует." if not heartbeat.get("present")
                    else f"Heartbeat не обновлялся {_minutes(age)}."
                ),
                hint="Внутри контейнера рестарта по этому поводу нет — нужен ручной перезапуск.",
            )

    container = probe.get("container") or {}
    restart_count = container.get("restart_count")
    if restart_count is not None and "container_down" not in findings:
        baseline = state.get("restart_baseline") or {}
        baseline_ts = int(baseline.get("ts", 0))
        baseline_count = int(baseline.get("count", restart_count))
        if not baseline_ts or (now - baseline_ts) > cfg.restart_storm_window_seconds:
            state.set("restart_baseline", {"ts": now, "count": int(restart_count)})
        else:
            delta = int(restart_count) - baseline_count
            if delta >= cfg.restart_storm_delta:
                findings["restart_storm"] = Finding(
                    rule="restart_storm",
                    failure_class=F_RESTART_STORM,
                    severity=WARN,
                    title="Контейнер циклически перезапускается",
                    detail=(
                        f"{delta} перезапусков за последние "
                        f"{_minutes(now - baseline_ts)}."
                    ),
                    hint="Самовосстановление не сходится: смотри логи причины падений.",
                )

    disk = probe.get("disk") or {}
    free_percent = disk.get("free_percent")
    if free_percent is not None and int(free_percent) < cfg.disk_min_free_percent:
        findings["disk_low"] = Finding(
            rule="disk_low",
            failure_class=F_DISK,
            severity=WARN,
            title="Заканчивается место на production-хосте",
            detail=f"Свободно {free_percent}% на {disk.get('path', '/')}.",
            hint="Опережающий сигнал: при заполнении диска сломаются SQLite и health-файлы.",
        )

    status = probe.get("status_api")
    if "container_down" in findings:
        return Evaluation(findings, skipped)

    if not status:
        if obs.get("status_requested", True):
            findings["status_api_unreachable"] = Finding(
                rule="status_api_unreachable",
                failure_class=f"{F_WORKER_HANG}/{F_ALERT_CHANNEL}",
                severity=WARN,
                title="Status API bridge не отвечает",
                detail=(
                    f"Контейнер работает, но /status недоступен: "
                    f"{probe.get('status_api_error', 'нет деталей')}."
                ),
                hint="Проверь status_api в config.local.yaml и BRIDGE_STATUS_TOKEN.",
            )
        else:
            skipped.add("status_api_unreachable")
        skipped.update(status_rules)
        return Evaluation(findings, skipped)

    findings.update(_evaluate_status(status, cfg, now))
    return Evaluation(findings, skipped)


def decide(
    evaluation: Evaluation,
    state: WatchdogState,
    cfg: WatchdogConfig,
    now: int = 0,
) -> Decision:
    """Применяет гистерезис: превращает мгновенные срабатывания в алерты и recovery."""
    alerts: list[Finding] = []
    recoveries: list[Recovery] = []

    known = set(FAIL_AFTER) | {"overall_degraded", "alert_outbox_backlog"}
    for rule in sorted(known):
        if rule in evaluation.skipped:
            continue

        finding = evaluation.findings.get(rule)
        if finding is not None:
            count = state.bump_fail(rule)
            if count >= fail_after(rule, cfg):
                # Повторные срабатывания приглушает dedup TTL в notify.
                state.set_alerting(rule, True, now=now)
                alerts.append(finding)
            continue

        state.clear_fail(rule)
        if state.is_alerting(rule):
            started = state.alert_started_at(rule)
            state.set_alerting(rule, False)
            recoveries.append(Recovery(rule, max(0, now - started) if started else 0))

    return Decision(alerts, recoveries)
