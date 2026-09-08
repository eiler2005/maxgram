import ast
import json
import pathlib
import sys
import time

import pytest

from src.watchdog_external.config import TelegramTarget, WatchdogConfig
from src.watchdog_external.notify import (
    SOURCE_HEADER,
    clean,
    render_alert,
    render_daily_summary,
    render_recovery,
)
from src.watchdog_external.receiver import sign, verify
from src.watchdog_external.rules import (
    Finding,
    Recovery,
    decide,
    evaluate,
    humanize_duration,
    rule_title,
)
from src.watchdog_external.state import WatchdogState

NOW = int(time.time())


def _cfg(**overrides) -> WatchdogConfig:
    base = dict(
        target_name="maxtg-prod",
        target_host="198.51.100.10",
        push_secret="",
        dedup_ttl_seconds=900,
        poll_interval_seconds=60,
        status_interval_seconds=300,
    )
    base.update(overrides)
    return WatchdogConfig(**base)


def _state(tmp_path) -> WatchdogState:
    return WatchdogState(tmp_path / "state.json")


def _healthy_probe(**overrides) -> dict:
    probe = {
        "schema_version": 1,
        "container": {"found": True, "state": "running", "health": "healthy", "restart_count": 1},
        "heartbeat": {"present": True, "ts": NOW - 5, "age_seconds": 5},
        "disk": {"path": "/opt", "free_percent": 55},
        "status_api": {
            "overall_status": "healthy",
            "worker_restart_count": 1,
            "subsystems": [{"name": "max_link", "status": "healthy", "issue": None}],
            "queues": {"inbound": {"pending_count": 0, "oldest_created_at": None}},
            "alert_outbox_size": 0,
            "max_egress_active": "home_ru_proxy",
        },
    }
    probe.update(overrides)
    return probe


def _observation(probe=None, **overrides) -> dict:
    obs = {
        "reachable": True,
        "ssh_ok": True,
        "ssh_error": "",
        "status_requested": True,
        "push": {},
        "probe": probe if probe is not None else _healthy_probe(),
    }
    obs.update(overrides)
    return obs


def test_healthy_host_produces_no_findings(tmp_path):
    evaluation = evaluate(_observation(), _state(tmp_path), _cfg(), NOW)
    assert evaluation.findings == {}


def test_alert_needs_consecutive_failures_then_recovers(tmp_path):
    """Гистерезис: один сбой heartbeat — ещё не повод будить владельца."""
    cfg = _cfg()
    state = _state(tmp_path)
    probe = _healthy_probe(heartbeat={"present": True, "ts": NOW - 900, "age_seconds": 900})

    first = decide(evaluate(_observation(probe), state, cfg, NOW), state, cfg, NOW)
    assert first.alerts == []

    second = decide(evaluate(_observation(probe), state, cfg, NOW), state, cfg, NOW)
    assert [f.rule for f in second.alerts] == ["heartbeat_stale"]
    assert second.alerts[0].failure_class == "F2"

    healed = decide(evaluate(_observation(), state, cfg, NOW), state, cfg, NOW)
    assert [r.rule for r in healed.recoveries] == ["heartbeat_stale"]
    assert healed.alerts == []

    # recovery отправляется ровно один раз
    again = decide(evaluate(_observation(), state, cfg, NOW), state, cfg, NOW)
    assert again.recoveries == []


def test_stopped_container_alerts_immediately_and_suppresses_dependents(tmp_path):
    """F8 — единственный класс, где ждать подтверждения незачем."""
    cfg = _cfg()
    state = _state(tmp_path)
    probe = _healthy_probe(
        container={"found": True, "state": "exited", "health": "none", "exit_code": 0},
    )

    evaluation = evaluate(_observation(probe), state, cfg, NOW)
    assert set(evaluation.findings) == {"container_down"}
    assert "heartbeat_stale" in evaluation.skipped
    assert "subsystem_issue" in evaluation.skipped

    decision = decide(evaluation, state, cfg)
    assert [f.rule for f in decision.alerts] == ["container_down"]
    assert decision.alerts[0].severity == "crit"


def test_unreachable_host_suppresses_every_dependent_rule(tmp_path):
    cfg = _cfg()
    state = _state(tmp_path)
    evaluation = evaluate(_observation(reachable=False), state, cfg, NOW)

    assert set(evaluation.findings) == {"host_unreachable"}
    assert {"container_down", "heartbeat_stale", "disk_low"} <= evaluation.skipped


def test_broken_ssh_with_live_push_falls_back_to_push_data(tmp_path):
    """F13: сломан путь опроса, но push несёт тот же снимок — наблюдение продолжается."""
    cfg = _cfg(push_secret="s3cret")
    state = _state(tmp_path)
    down = _healthy_probe(
        container={"found": True, "state": "exited", "health": "none", "exit_code": 137}
    )
    obs = _observation(
        ssh_ok=False,
        ssh_error="Connection timed out",
        push={"received_at": NOW - 30, "payload": {"ts": NOW - 30, "probe": down}},
    )
    obs.pop("probe")

    evaluation = evaluate(obs, state, cfg, NOW)

    # путь опроса — предупреждение, потому что данные всё ещё есть
    assert evaluation.findings["ssh_probe_failed"].severity == "warn"
    assert "push" in evaluation.findings["ssh_probe_failed"].detail
    assert "push_stale" not in evaluation.findings
    # и главное: реальная поломка из push-снимка не потерялась
    assert evaluation.findings["container_down"].severity == "crit"


def test_broken_ssh_without_push_is_critical_and_blinds_dependent_rules(tmp_path):
    cfg = _cfg(push_secret="s3cret")
    obs = _observation(ssh_ok=False, ssh_error="Connection timed out")
    obs.pop("probe")

    evaluation = evaluate(obs, _state(tmp_path), cfg, NOW)

    assert evaluation.findings["ssh_probe_failed"].severity == "crit"
    assert "container_down" in evaluation.skipped
    assert "heartbeat_stale" in evaluation.skipped


def test_missing_push_triggers_dead_man_switch(tmp_path):
    cfg = _cfg(push_secret="s3cret")
    state = _state(tmp_path)

    evaluation = evaluate(_observation(), state, cfg, NOW)
    assert "push_stale" in evaluation.findings
    assert evaluation.findings["push_stale"].severity == "crit"


def test_push_rules_are_skipped_when_layer_disabled(tmp_path):
    evaluation = evaluate(_observation(), _state(tmp_path), _cfg(push_secret=""), NOW)
    assert "push_stale" in evaluation.skipped


def test_requires_reauth_is_escalated_to_critical(tmp_path):
    cfg = _cfg()
    status = _healthy_probe()["status_api"]
    status["overall_status"] = "degraded"
    status["subsystems"] = [
        {
            "name": "max_link",
            "status": "degraded",
            "issue": {
                "code": "max_auth_invalid",
                "summary": "MAX token инвалидирован",
                "severity": "critical",
                "requires_reauth": True,
            },
        }
    ]
    evaluation = evaluate(_observation(_healthy_probe(status_api=status)), _state(tmp_path), cfg, NOW)

    finding = evaluation.findings["subsystem_issue"]
    assert finding.severity == "crit"
    assert finding.failure_class == "F5"
    assert "max_reauth.py" in finding.hint


def test_outbox_backlog_surfaces_broken_alert_channel(tmp_path):
    status = _healthy_probe()["status_api"]
    status["alert_outbox_size"] = 4
    evaluation = evaluate(
        _observation(_healthy_probe(status_api=status)), _state(tmp_path), _cfg(), NOW
    )

    finding = evaluation.findings["alert_outbox_backlog"]
    assert finding.failure_class == "F6"


def test_unexpected_egress_mode_is_detected(tmp_path):
    status = _healthy_probe()["status_api"]
    status["max_egress_active"] = "hetzner_direct"
    evaluation = evaluate(
        _observation(_healthy_probe(status_api=status)), _state(tmp_path), _cfg(), NOW
    )

    assert evaluation.findings["egress_mode_unexpected"].failure_class == "F15"


def test_restart_storm_uses_baseline_within_window(tmp_path):
    cfg = _cfg(restart_storm_delta=3, restart_storm_window_seconds=1800)
    state = _state(tmp_path)

    evaluate(_observation(), state, cfg, NOW)  # ставит baseline restart_count=1
    stormy = _healthy_probe(
        container={"found": True, "state": "running", "health": "healthy", "restart_count": 5}
    )
    evaluation = evaluate(_observation(stormy), state, cfg, NOW + 60)

    assert evaluation.findings["restart_storm"].failure_class == "F11"


def test_status_rules_are_skipped_when_status_not_polled(tmp_path):
    """В циклах без опроса API счётчики status-правил не должны обнуляться."""
    probe = _healthy_probe()
    probe.pop("status_api")
    evaluation = evaluate(
        _observation(probe, status_requested=False), _state(tmp_path), _cfg(), NOW
    )

    assert "status_api_unreachable" in evaluation.skipped
    assert "overall_degraded" in evaluation.skipped
    assert evaluation.findings == {}


def test_alert_text_carries_class_and_action_without_private_data(tmp_path):
    cfg = _cfg()
    state = _state(tmp_path)
    probe = _healthy_probe(
        container={"found": True, "state": "exited", "health": "none", "exit_code": 0}
    )
    finding = decide(evaluate(_observation(probe), state, cfg, NOW), state, cfg, NOW).alerts[0]

    text = render_alert(finding, cfg)
    assert "ВНЕШНИЙ WATCHDOG" in text
    assert "F8" in text
    assert "container_down" in text
    assert "up -d bridge" in text
    assert "ВНЕШНИЙ WATCHDOG" in render_recovery(Recovery("container_down", 240), cfg)


def test_push_signature_round_trip():
    body = json.dumps({"ts": NOW, "payload": {"overall_status": "healthy"}}).encode()

    assert verify("secret", body, sign("secret", body)) is True
    assert verify("secret", body, sign("other", body)) is False
    assert verify("secret", body, None) is False
    assert verify("", body, sign("secret", body)) is False


@pytest.mark.architecture
def test_watchdog_depends_only_on_stdlib_and_itself():
    """Наблюдатель обязан переживать любую поломку наблюдаемого.

    Поэтому он не импортирует ни модули bridge, ни сторонние библиотеки:
    контейнер собирается вообще без pip install.
    """
    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "watchdog_external"
    allowed_local = {"config", "notify", "probe", "receiver", "rules", "state"}
    offenders: list[str] = []

    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # относительный импорт внутри пакета
                    if node.module not in allowed_local | {None}:
                        offenders.append(f"{module.name}: relative {node.module}")
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue

            for name in names:
                if name and name not in sys.stdlib_module_names:
                    offenders.append(f"{module.name}: {name}")

    assert offenders == [], f"внешний watchdog тянет лишние зависимости: {offenders}"


def test_telegram_targets_include_owner_and_ops_topic():
    cfg = _cfg()
    cfg.bot_token = "token"
    cfg.targets = [
        TelegramTarget(label="owner_dm", chat_id="1"),
        TelegramTarget(label="ops_topic", chat_id="-100", topic_id="7"),
    ]

    assert cfg.telegram_configured is True
    assert [t.label for t in cfg.targets] == ["owner_dm", "ops_topic"]


def test_recovery_message_is_human_readable_and_reports_duration():
    """Оператору нужно название проблемы и сколько она длилась, а не имя правила."""
    text = render_recovery(Recovery("container_down", 245), _cfg())

    assert "Контейнер bridge" in text          # человекочитаемое название
    assert "container_down" in text            # техническое имя тоже остаётся
    assert "4 мин" in text
    assert "Восстановлено: container_down" not in text


def test_alert_text_collapses_noisy_error_output():
    """stderr чужих утилит не должен уезжать в сообщение многострочным мусором."""
    noisy = "usage: ssh [-46AA]\n\t\t [-Q query_option]\n   [-b bind]"

    assert clean(noisy) == "usage: ssh [-46AA] [-Q query_option] [-b bind]"
    assert clean("x" * 500, 100).endswith("…")
    assert len(clean("x" * 500, 100)) == 100


def test_daily_summary_lists_open_problems_by_name():
    empty = render_daily_summary(_cfg(), [])
    assert "Открытых проблем нет" in empty

    busy = render_daily_summary(_cfg(), ["container_down", "disk_low"])
    assert "Контейнер bridge" in busy
    assert "Свободное место" in busy


def test_duration_humanizer_covers_ranges():
    assert humanize_duration(0) == "меньше минуты"
    assert humanize_duration(45) == "45 с"
    assert humanize_duration(600) == "10 мин"
    assert humanize_duration(7800) == "2 ч 10 мин"


def test_rule_titles_exist_for_every_rule():
    """Иначе в recovery снова уедет техническое имя правила."""
    from src.watchdog_external.rules import FAIL_AFTER, RULE_TITLES

    known = set(FAIL_AFTER) | {"overall_degraded", "alert_outbox_backlog"}
    assert known <= set(RULE_TITLES), known - set(RULE_TITLES)
    for rule in known:
        assert rule_title(rule) != rule


def test_every_rule_is_assigned_to_a_layer():
    """Из алерта должно быть видно, каким слоем поймано — иначе неясно, что чинить."""
    from src.watchdog_external.rules import FAIL_AFTER, RULE_LAYERS, rule_layer

    known = set(FAIL_AFTER) | {"overall_degraded", "alert_outbox_backlog"}
    assert known <= set(RULE_LAYERS), known - set(RULE_LAYERS)
    assert {rule_layer(r) for r in known} == {"L1", "L2", "L3"}


def test_alert_names_its_layer_and_where_to_look(tmp_path):
    cfg = _cfg()
    state = _state(tmp_path)
    probe = _healthy_probe(
        container={"found": True, "state": "exited", "health": "none", "exit_code": 0}
    )
    finding = decide(evaluate(_observation(probe), state, cfg, NOW), state, cfg, NOW).alerts[0]

    text = render_alert(finding, cfg)
    assert "Слой: L2" in text
    assert "опрос с наблюдателя" in text
    assert "Где смотреть:" in text
    assert "docker compose logs watchdog" in text


def test_daily_summary_reports_all_four_layers():
    """Молчащий слой неотличим от сломанного, поэтому раз в сутки отчитываются все."""
    layers = {
        "L1": "отвечает",
        "L2": "опрос проходит",
        "L3": "последний пуш 30 с назад",
        "L4": "цикл проверок работает",
    }
    text = render_daily_summary(_cfg(), [], layers)

    for layer in ("L1", "L2", "L3", "L4"):
        assert layer in text
    assert "последний пуш 30 с назад" in text
    assert "Открытых проблем нет" in text


def test_layer_status_reflects_broken_paths():
    from src.watchdog_external.__main__ import layer_status

    cfg = _cfg(push_secret="s3cret")
    broken = {"reachable": True, "ssh_ok": False, "push": {}, "probe": {}}
    st = layer_status(cfg, broken, NOW)

    assert st["L2"].status == "SSH-проба не проходит"
    assert "нет данных" in st["L1"].status
    assert "ни разу" in st["L3"].status

    healthy = {
        "reachable": True, "ssh_ok": True,
        "push": {"received_at": NOW - 20, "sent_at": NOW - 22},
        "probe": {
            "docker_ok": True,
            "container": {"state": "running", "health": "healthy", "restart_count": 0},
            "heartbeat": {"age_seconds": 12},
            "disk": {"free_percent": 40},
            "status_api": {
                "overall_status": "healthy", "worker_restart_count": 0,
                "subsystems": [{"name": "max_link", "status": "healthy"}],
                "queues": {"inbound": {"pending_count": 0}},
                "alert_outbox_size": 0, "max_egress_active": "home_ru_proxy",
            },
        },
    }
    ok = layer_status(cfg, healthy, NOW)
    assert ok["L1"].status == "отвечает, состояние healthy"
    assert ok["L2"].status == "опрос проходит"
    assert "20 с назад" in ok["L3"].status

    # главное: рядом с вердиктом есть конкретика, на которой он основан
    assert "подсистем healthy 1/1" in ok["L1"].checked
    assert "egress home_ru_proxy" in ok["L1"].checked
    assert "running/healthy" in ok["L2"].checked
    assert "heartbeat 12 с" in ok["L2"].checked
    assert "диск свободно 40%" in ok["L2"].checked
    assert "задержка доставки 2 с" in ok["L3"].checked


def test_layer_status_marks_push_layer_disabled_without_secret():
    from src.watchdog_external.__main__ import layer_status

    st = layer_status(_cfg(push_secret=""), {"reachable": True, "ssh_ok": True, "probe": {}}, NOW)
    assert "выключен" in st["L3"].status
    assert "WATCHDOG_PUSH_SECRET" in st["L3"].checked


def test_every_external_message_says_who_wrote_it():
    """Источник виден в первой строке: внутренние алерты bridge при его смерти
    не приходят вовсе, поэтому важно понимать, кому верить."""
    cfg = _cfg()
    finding = Finding(
        rule="container_down", failure_class="F8", severity="crit",
        title="Контейнер bridge не работает", detail="exited", hint="подними вручную",
    )
    messages = [
        render_alert(finding, cfg),
        render_recovery(Recovery("container_down", 60), cfg),
        render_daily_summary(cfg, [], {"L1": "ok", "L2": "ok", "L3": "ok", "L4": "ok"}),
    ]
    for text in messages:
        assert text.startswith(SOURCE_HEADER), text[:60]
        assert "ВНЕШНИЙ WATCHDOG" in text


def test_internal_and_external_headers_do_not_collide():
    """Две шапки должны быть отличимы с одного взгляда."""
    from src.runtime.health.rendering import SOURCE_HEADER as INTERNAL_HEADER

    assert "ВНЕШНИЙ WATCHDOG" in SOURCE_HEADER
    assert "BRIDGE" in INTERNAL_HEADER
    assert INTERNAL_HEADER != SOURCE_HEADER
    # внутренние сообщения уходят plain text — HTML-разметки в шапке быть не должно
    assert "<" not in INTERNAL_HEADER


def test_daily_summary_explains_what_each_layer_watches():
    """Список названий слоёв сам по себе ничего не говорит — нужна расшифровка."""
    text = render_daily_summary(
        _cfg(), [],
        {"L1": "отвечает", "L2": "опрос проходит", "L3": "пуш 24 с назад", "L4": "работает"},
    )

    assert "что болит внутри bridge" in text
    assert "жив ли контейнер и хост" in text
    assert "не оборвалась ли связь с наблюдателем" in text
    assert "жив ли сам наблюдатель" in text


def test_every_layer_has_a_plain_language_purpose():
    from src.watchdog_external.rules import LAYER_NAMES, LAYER_PURPOSE

    assert set(LAYER_PURPOSE) == set(LAYER_NAMES) == {"L1", "L2", "L3", "L4"}
    for purpose in LAYER_PURPOSE.values():
        assert purpose and purpose[0].islower()  # фраза, а не заголовок


def test_daily_summary_puts_each_problem_under_its_layer():
    """Не просто «что-то сломано», а на каком рубеже наблюдения."""
    text = render_daily_summary(
        _cfg(), ["push_stale", "disk_low"],
        {"L1": "отвечает", "L2": "опрос проходит", "L3": "молчит 420 с", "L4": "работает"},
    )
    # ⚠️ состоит из двух кодовых точек, поэтому сравниваем префиксом, а не символом
    layer_lines = [l for l in text.splitlines() if l.startswith(("✅", "⚠️"))]
    marks = {
        l.split()[1].replace("<b>", "").replace("</b>", ""): l.startswith("⚠️")
        for l in layer_lines
    }

    assert marks["L1"] is False and marks["L2"] is True      # disk_low живёт на L2
    assert marks["L3"] is True and marks["L4"] is False
    assert "⚠️ Push-сигналы" in text
    assert "⚠️ Свободное место" in text
    assert "Открытых проблем: 2" in text


def test_daily_summary_shows_what_was_actually_checked():
    """Вердикта мало: рядом должны стоять факты, на которых он основан."""
    from src.watchdog_external.rules import LayerReport

    text = render_daily_summary(_cfg(), [], {
        "L1": LayerReport("отвечает, состояние healthy", "подсистем healthy 6/6 · очереди 0"),
        "L2": LayerReport("опрос проходит", "контейнер running/healthy · heartbeat 12 с"),
        "L3": LayerReport("последний пуш 19 с назад", "подпись HMAC верна · задержка 4 с"),
        "L4": LayerReport("цикл проверок работает", "опрос раз в 60 с"),
    })

    assert "итог: отвечает, состояние healthy" in text
    assert "проверено: подсистем healthy 6/6" in text
    assert "контейнер running/healthy" in text
    assert "подпись HMAC верна" in text


def test_daily_summary_accepts_plain_strings_for_backward_compatibility():
    text = render_daily_summary(_cfg(), [], {"L1": "отвечает"})
    assert "итог: отвечает" in text


def test_every_layer_declares_the_direction_of_its_check():
    """Стрелка показывает, кто кого спрашивает: из неё видно, какой конец чинить."""
    from src.watchdog_external.rules import LAYER_FLOW, LAYER_NAMES

    assert set(LAYER_FLOW) == set(LAYER_NAMES)
    # L2 инициирует наблюдатель, L3 — наоборот, сам production
    assert LAYER_FLOW["L2"].startswith("наблюдатель")
    assert LAYER_FLOW["L3"].startswith("production")
    assert all("──" in flow for flow in LAYER_FLOW.values())


def test_summary_and_alert_both_show_the_direction():
    from src.watchdog_external.rules import LayerReport

    summary = render_daily_summary(_cfg(), [], {"L2": LayerReport("опрос проходит", "heartbeat 5 с")})
    assert "наблюдатель ──SSH──► production" in summary
    assert "production ──HMAC POST──► наблюдатель" in summary

    alert = render_alert(
        Finding(rule="container_down", failure_class="F8", severity="crit",
                title="Контейнер bridge не работает", detail="exited", hint="up -d"),
        _cfg(),
    )
    assert "наблюдатель ──SSH──► production" in alert


def test_meta_layer_reports_only_changing_facts():
    """Постоянное описание механизма ушло в стрелку — в статусе только цифры."""
    from src.watchdog_external.__main__ import layer_status

    st = layer_status(_cfg(), {"reachable": True, "ssh_ok": True, "probe": {}}, NOW, NOW - 11)
    assert "предыдущий прогон 11 с назад" in st["L4"].checked
    assert "host-мониторинг" not in st["L4"].checked  # это теперь в стрелке
