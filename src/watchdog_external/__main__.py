"""CLI внешнего watchdog: цикл наблюдения, одиночный прогон, тест доставки."""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from pathlib import Path

from . import receiver
from .config import WatchdogConfig, load_config
from .notify import dispatch, render_daily_summary, send_telegram
from .probe import collect
from .rules import decide, evaluate
from .state import WatchdogState

logger = logging.getLogger("watchdog")

HEARTBEAT_FILE = "watchdog_heartbeat"


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


def _maybe_daily_summary(cfg: WatchdogConfig, state: WatchdogState, now: int) -> None:
    """Мета-мониторинг: молчащий watchdog неотличим от сломанного (класс F14)."""
    if cfg.daily_summary_hour_utc < 0:
        return
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    if today.hour != cfg.daily_summary_hour_utc:
        return
    marker = today.strftime("%Y-%m-%d")
    if state.get("daily_summary_date") == marker:
        return
    if send_telegram(cfg, render_daily_summary(cfg, state.active_alerts()), silent=True):
        state.set("daily_summary_date", marker)


def run_once(cfg: WatchdogConfig, state: WatchdogState, *, with_status: bool) -> int:
    now = int(time.time())
    observation = collect(cfg, with_status=with_status, push=receiver.read_push(cfg))
    evaluation = evaluate(observation, state, cfg, now)
    decision = decide(evaluation, state, cfg)

    for finding in decision.alerts:
        logger.warning("[%s] %s — %s", finding.severity, finding.rule, finding.detail)
    for rule in decision.recoveries:
        logger.info("recovered: %s", rule)
    if not decision.alerts and not decision.recoveries:
        logger.info("all checks passed (status polled: %s)", with_status)

    dispatch(cfg, state, alerts=decision.alerts, recoveries=decision.recoveries, now=now)
    _maybe_daily_summary(cfg, state, now)
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
            f"🧪 <b>[EXT] Тест доставки</b>\nВнешний watchdog для {cfg.target_name} на связи.",
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
        return run_once(cfg, state, with_status=True)
    return run_loop(cfg, state)


if __name__ == "__main__":
    raise SystemExit(main())
