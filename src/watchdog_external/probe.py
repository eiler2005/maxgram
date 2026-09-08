"""
Слой 2: опрос production-хоста снаружи.

Два независимых действия за цикл:
  * TCP-проба порта SSH — отвечает на вопрос «хост вообще жив»;
  * SSH с forced command — отдаёт один merged JSON про контейнер, heartbeat,
    диск и (по расписанию) ответ status API.

Ключ на стороне production-хоста привязан к read-only скрипту через
`command="...",restrict`, поэтому watchdog физически не может ничего изменить
на наблюдаемом хосте.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from typing import Any, Optional

from .config import WatchdogConfig

logger = logging.getLogger("watchdog.probe")

TCP_TIMEOUT_SECONDS = 5


def tcp_reachable(host: str, port: int, timeout: int = TCP_TIMEOUT_SECONDS) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def ssh_probe(cfg: WatchdogConfig, *, with_status: bool) -> tuple[Optional[dict[str, Any]], str]:
    """Возвращает (payload, error). Payload None означает, что путь опроса сломан."""
    command = [
        "ssh",
        "-i", cfg.ssh_key,
        "-p", str(cfg.ssh_port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={max(3, cfg.ssh_timeout_seconds // 2)}",
        f"{cfg.ssh_user}@{cfg.target_host}",
    ]
    # Forced command игнорирует аргументы, но пробрасывает их в SSH_ORIGINAL_COMMAND.
    # Без ведущих дефисов: ssh разбирает "--no-status" как свою опцию и печатает usage.
    command.append("with-status" if with_status else "no-status")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=cfg.ssh_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {cfg.ssh_timeout_seconds}s"
    except Exception as e:  # noqa: BLE001
        return None, e.__class__.__name__

    if result.returncode != 0:
        # Берём последнюю содержательную строку stderr и схлопываем пробелы:
        # ssh умеет отвечать многострочным usage, и он не должен уезжать в алерт.
        lines = [" ".join(l.split()) for l in result.stderr.decode("utf-8", "replace").splitlines()]
        lines = [l for l in lines if l]
        return None, (lines[-1][:200] if lines else f"exit code {result.returncode}")

    raw = result.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        return None, "probe returned non-JSON output"


def collect(cfg: WatchdogConfig, *, with_status: bool, push: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Одно наблюдение целиком — то, что дальше уходит в rules.evaluate()."""
    observation: dict[str, Any] = {
        "collected_at": int(time.time()),
        "status_requested": with_status,
        "push": push or {},
    }

    observation["reachable"] = tcp_reachable(cfg.target_host, cfg.ssh_port)
    if not observation["reachable"]:
        observation["ssh_ok"] = False
        observation["ssh_error"] = "host not reachable"
        return observation

    payload, error = ssh_probe(cfg, with_status=with_status)
    observation["ssh_ok"] = payload is not None
    observation["ssh_error"] = error
    if payload is not None:
        observation["probe"] = payload
    return observation
