"""Конфигурация внешнего watchdog: только env, никаких файлов конфига."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class TelegramTarget:
    label: str
    chat_id: str
    topic_id: str | None = None


@dataclass
class WatchdogConfig:
    # Наблюдаемый хост
    target_name: str = "maxtg-bridge"
    target_host: str = ""
    container_name: str = "deploy-bridge-1"
    ssh_user: str = "deploy"
    ssh_port: int = 22
    ssh_key: str = "/app/ssh/watchdog_key"
    ssh_timeout_seconds: int = 20

    # Интервалы
    poll_interval_seconds: int = 60
    status_interval_seconds: int = 300

    # Пороги правил
    heartbeat_max_age_seconds: int = 180
    disk_min_free_percent: int = 10
    restart_storm_delta: int = 3
    restart_storm_window_seconds: int = 1800
    degraded_grace_seconds: int = 900
    queue_max_age_seconds: int = 1800
    outbox_grace_seconds: int = 600
    expected_egress: str = "home_ru_proxy"
    push_max_age_seconds: int = 300

    # Доставка
    dedup_ttl_seconds: int = 900
    daily_summary_hour_utc: int = -1  # -1 = выключено

    # Push-приёмник (слой 3)
    push_bind: str = "0.0.0.0"
    push_port: int = 18151
    push_secret: str = ""
    push_max_skew_seconds: int = 300

    state_path: str = "/app/state/state.json"

    bot_token: str = ""
    targets: list[TelegramTarget] = field(default_factory=list)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.bot_token and self.targets)


def load_config() -> WatchdogConfig:
    targets: list[TelegramTarget] = []
    owner_id = _str("TG_OWNER_ID")
    if owner_id:
        targets.append(TelegramTarget(label="owner_dm", chat_id=owner_id))
    group_id = _str("TG_FORUM_GROUP_ID")
    ops_topic = _str("TG_OPS_TOPIC_ID")
    if group_id and ops_topic:
        targets.append(TelegramTarget(label="ops_topic", chat_id=group_id, topic_id=ops_topic))

    return WatchdogConfig(
        target_name=_str("WATCHDOG_TARGET_NAME", "maxtg-bridge"),
        target_host=_str("WATCHDOG_TARGET_HOST"),
        container_name=_str("WATCHDOG_CONTAINER_NAME", "deploy-bridge-1"),
        ssh_user=_str("WATCHDOG_SSH_USER", "deploy"),
        ssh_port=_int("WATCHDOG_SSH_PORT", 22),
        ssh_key=_str("WATCHDOG_SSH_KEY", "/app/ssh/watchdog_key"),
        ssh_timeout_seconds=_int("WATCHDOG_SSH_TIMEOUT_SECONDS", 20),
        poll_interval_seconds=_int("WATCHDOG_POLL_INTERVAL_SECONDS", 60),
        status_interval_seconds=_int("WATCHDOG_STATUS_INTERVAL_SECONDS", 300),
        heartbeat_max_age_seconds=_int("WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS", 180),
        disk_min_free_percent=_int("WATCHDOG_DISK_MIN_FREE_PERCENT", 10),
        restart_storm_delta=_int("WATCHDOG_RESTART_STORM_DELTA", 3),
        restart_storm_window_seconds=_int("WATCHDOG_RESTART_STORM_WINDOW_SECONDS", 1800),
        degraded_grace_seconds=_int("WATCHDOG_DEGRADED_GRACE_SECONDS", 900),
        queue_max_age_seconds=_int("WATCHDOG_QUEUE_MAX_AGE_SECONDS", 1800),
        outbox_grace_seconds=_int("WATCHDOG_OUTBOX_GRACE_SECONDS", 600),
        expected_egress=_str("WATCHDOG_EXPECTED_EGRESS", "home_ru_proxy"),
        push_max_age_seconds=_int("WATCHDOG_PUSH_MAX_AGE_SECONDS", 300),
        dedup_ttl_seconds=_int("WATCHDOG_DEDUP_TTL_SECONDS", 900),
        daily_summary_hour_utc=_int("WATCHDOG_DAILY_SUMMARY_HOUR_UTC", -1),
        push_bind=_str("WATCHDOG_PUSH_BIND", "0.0.0.0"),
        push_port=_int("WATCHDOG_PUSH_PORT", 18151),
        push_secret=_str("WATCHDOG_PUSH_SECRET"),
        push_max_skew_seconds=_int("WATCHDOG_PUSH_MAX_SKEW_SECONDS", 300),
        state_path=_str("WATCHDOG_STATE_PATH", "/app/state/state.json"),
        bot_token=_str("TG_BOT_TOKEN"),
        targets=targets,
    )
