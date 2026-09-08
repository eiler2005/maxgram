"""CLI внешнего watchdog: цикл наблюдения, одиночный прогон, тест доставки."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import receiver
from .config import WatchdogConfig, load_config
from .notify import SOURCE_HEADER, dispatch, render_daily_summary, send_telegram
from .probe import collect
from .rules import LayerReport, decide, evaluate
from .state import WatchdogState

logger = logging.getLogger("watchdog")

HEARTBEAT_FILE = "watchdog_heartbeat"
LOCK_FILE = "watchdog.lock"


@contextmanager
def single_run(cfg: WatchdogConfig):
    """Не даёт двум процессам оценивать состояние одновременно.

    Иначе `--once` рядом с работающим циклом присылает вторую копию каждого
    сообщения: оба процесса независимо видят переход и оба его отправляют.
    """
    path = Path(cfg.state_path).parent / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.warning("другой процесс watchdog уже выполняет проверку — пропускаю цикл")
            yield False
            return
        yield True
    finally:
        handle.close()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _touch_heartbeat(cfg: WatchdogConfig) -> None:
    """Собственный heartbeat: по нему Docker HEALTHCHECK видит живость наблюдателя."""
    try:
        path = Path(cfg.state_path).parent / HEARTBEAT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("could not write watchdog heartbeat: %s", e)


def layer_status(
    cfg: WatchdogConfig,
    observation: dict,
    now: int,
    last_run_at: int = 0,
) -> dict[str, LayerReport]:
    """Отчёт каждого слоя: вердикт плюс факты, на которых он основан.

    Одного вердикта мало: «отвечает» не показывает, что именно система смотрела,
    поэтому рядом идёт список проверенного — по нему видно и глубину проверки,
    и текущие значения.
    """
    probe = observation.get("probe") or {}
    push = observation.get("push") or {}
    status = probe.get("status_api") or {}
    container = probe.get("container") or {}
    heartbeat = probe.get("heartbeat") or {}
    disk = probe.get("disk") or {}
    push_age = now - int(push.get("received_at") or 0) if push.get("received_at") else None

    # --- L1: что bridge рассказывает о себе ---
    if status:
        subsystems = status.get("subsystems") or []
        healthy = sum(1 for x in subsystems if x.get("status") == "healthy")
        pending = sum(int((q or {}).get("pending_count") or 0) for q in (status.get("queues") or {}).values())
        l1 = LayerReport(
            f"отвечает, состояние {status.get('overall_status')}",
            f"подсистем healthy {healthy}/{len(subsystems)} · очереди {pending} · "
            f"outbox {status.get('alert_outbox_size')} · egress {status.get('max_egress_active')} · "
            f"рестартов worker {status.get('worker_restart_count')}",
        )
    elif not observation.get("ssh_ok"):
        l1 = LayerReport("нет данных: сломан путь опроса", "status API опрашивается через SSH-канал")
    elif probe.get("status_api_error"):
        l1 = LayerReport(f"ошибка: {probe['status_api_error']}", "контейнер жив, но API не ответил")
    else:
        l1 = LayerReport("в этом цикле не опрашивался", f"опрос раз в {cfg.status_interval_seconds} с")

    # --- L2: что видно про контейнер и хост снаружи ---
    if observation.get("ssh_ok"):
        l2 = LayerReport(
            "опрос проходит",
            f"контейнер {container.get('state')}/{container.get('health')} · "
            f"рестартов {container.get('restart_count')} · "
            f"heartbeat {heartbeat.get('age_seconds')} с · "
            f"диск свободно {disk.get('free_percent')}% · "
            f"docker daemon {'жив' if probe.get('docker_ok') else 'не отвечает'}",
        )
    elif not observation.get("reachable"):
        l2 = LayerReport("хост недоступен", f"нет TCP-ответа на порту {cfg.ssh_port}")
    else:
        l2 = LayerReport("SSH-проба не проходит", str(observation.get("ssh_error") or "нет деталей"))

    # --- L3: встречный канал ---
    if not cfg.push_secret:
        l3 = LayerReport("выключен", "нет общего секрета WATCHDOG_PUSH_SECRET")
    elif push_age is None:
        l3 = LayerReport("пуш ни разу не приходил", f"ждём на порту {cfg.push_port}")
    else:
        lag = int(push.get("received_at", 0)) - int(push.get("sent_at", 0))
        verdict = f"последний пуш {push_age} с назад" if push_age <= cfg.push_max_age_seconds else f"молчит {push_age} с"
        l3 = LayerReport(
            verdict,
            f"подпись HMAC верна · задержка доставки {lag} с · "
            f"порог тишины {cfg.push_max_age_seconds} с",
        )

    # --- L4: жив ли сам наблюдатель ---
    # Только меняющееся: постоянное описание механизма ушло в стрелку и runbook.
    seen = f" · предыдущий прогон {now - last_run_at} с назад" if last_run_at else ""
    l4 = LayerReport(
        "цикл проверок работает",
        f"опрос раз в {cfg.poll_interval_seconds} с{seen}",
    )
    return {"L1": l1, "L2": l2, "L3": l3, "L4": l4}


def _maybe_daily_summary(
    cfg: WatchdogConfig,
    state: WatchdogState,
    now: int,
    layers: dict[str, str] | None = None,
) -> None:
    """Мета-мониторинг: молчащий watchdog неотличим от сломанного (класс F14)."""
    if cfg.daily_summary_hour_utc < 0:
        return
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    if today.hour != cfg.daily_summary_hour_utc:
        return
    marker = today.strftime("%Y-%m-%d")
    if state.get("daily_summary_date") == marker:
        return
    text = render_daily_summary(cfg, state.active_alerts(), layers)
    if send_telegram(cfg, text, silent=True):
        state.set("daily_summary_date", marker)


def run_once(cfg: WatchdogConfig, state: WatchdogState, *, with_status: bool) -> int:
    now = int(time.time())
    observation = collect(cfg, with_status=with_status, push=receiver.read_push(cfg))
    evaluation = evaluate(observation, state, cfg, now)
    decision = decide(evaluation, state, cfg, now)

    for finding in decision.alerts:
        logger.warning("[%s] %s — %s", finding.severity, finding.rule, finding.detail)
    for recovery in decision.recoveries:
        logger.info("recovered: %s (длилось %ss)", recovery.rule, recovery.duration_seconds)
    if not decision.alerts and not decision.recoveries:
        logger.info("all checks passed (status polled: %s)", with_status)

    dispatch(cfg, state, alerts=decision.alerts, recoveries=decision.recoveries, now=now)
    _maybe_daily_summary(
        cfg, state, now,
        layer_status(cfg, observation, now, int(state.get("last_run_at") or 0)),
    )
    state.set("last_run_at", now)
    state.save()
    _touch_heartbeat(cfg)

    return 2 if any(f.severity == "crit" for f in decision.alerts) else 0


def run_loop(cfg: WatchdogConfig, state: WatchdogState) -> int:
    receiver.start_background(cfg)
    last_status_poll = 0
    while True:
        now = int(time.time())
        with_status = (now - last_status_poll) >= cfg.status_interval_seconds
        try:
            with single_run(cfg) as acquired:
                if acquired:
                    run_once(cfg, state, with_status=with_status)
                    if with_status:
                        last_status_poll = now
        except Exception as e:  # noqa: BLE001 — наблюдатель не имеет права падать
            logger.exception("watchdog cycle failed: %s", e)
        time.sleep(max(10, cfg.poll_interval_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchdog_external", description="maxtg-bridge external watchdog")
    parser.add_argument("--once", action="store_true", help="один цикл проверки и выход")
    parser.add_argument("--test-alert", action="store_true", help="проверить доставку в Telegram")
    parser.add_argument("--serve-receiver", action="store_true", help="только push-приёмник")
    args = parser.parse_args(argv)

    _setup_logging()
    cfg = load_config()

    if args.test_alert:
        ok = send_telegram(
            cfg,
            f"{SOURCE_HEADER}\n\n🧪 <b>Тест доставки</b>\n"
            f"Наблюдаемый хост: {cfg.target_name}\nКанал доставки работает.",
            silent=True,
        )
        print("delivered" if ok else "FAILED — проверь TG_BOT_TOKEN / TG_OWNER_ID")
        return 0 if ok else 2

    if args.serve_receiver:
        receiver.serve_forever(cfg)
        return 0

    if not cfg.target_host:
        print("WATCHDOG_TARGET_HOST is required", file=sys.stderr)
        return 2

    state = WatchdogState(cfg.state_path)
    if args.once:
        with single_run(cfg) as acquired:
            if not acquired:
                return 0
            return run_once(cfg, state, with_status=True)
    return run_loop(cfg, state)


if __name__ == "__main__":
    raise SystemExit(main())
