"""
Слой 3: приёмник push dead-man's switch.

Production-хост раз в минуту сам присылает подписанный снимок состояния.
Ценность не в самих данных (их же отдаёт SSH-опрос), а в направлении: когда
путь наблюдатель → production ломается, push продолжает идти, и watchdog отличает
«bridge умер» от «сломан канал наблюдения» (класс F13).

Payload подписан HMAC-SHA256 и защищён окном времени, поэтому обходится без TLS:
секретов внутри нет, а целостность и защита от повтора обеспечены подписью.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .config import WatchdogConfig

logger = logging.getLogger("watchdog.receiver")

SIGNATURE_HEADER = "X-Watchdog-Signature"
MAX_BODY_BYTES = 64 * 1024


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: Optional[str]) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature.strip())


def push_state_path(cfg: WatchdogConfig) -> Path:
    return Path(cfg.state_path).parent / "push.json"


def read_push(cfg: WatchdogConfig) -> dict[str, Any]:
    try:
        return json.loads(push_state_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_push(cfg: WatchdogConfig, payload: dict[str, Any]) -> None:
    target = push_state_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, target)


def _handler_factory(cfg: WatchdogConfig):
    lock = threading.Lock()

    class PushHandler(BaseHTTPRequestHandler):
        server_version = "maxtg-watchdog"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — переопределяем stdlib
            logger.debug("push %s", fmt % args)

        def _reply(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802 — интерфейс stdlib
            if self.path != "/push":
                self._reply(404, '{"error":"not found"}')
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(400, '{"error":"bad length"}')
                return

            body = self.rfile.read(length)
            if not verify(cfg.push_secret, body, self.headers.get(SIGNATURE_HEADER)):
                logger.warning("rejected push with invalid signature")
                self._reply(401, '{"error":"bad signature"}')
                return

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._reply(400, '{"error":"bad json"}')
                return

            now = int(time.time())
            sent_at = int(payload.get("ts") or 0)
            if abs(now - sent_at) > cfg.push_max_skew_seconds:
                logger.warning("rejected push outside time window (skew %ss)", now - sent_at)
                self._reply(400, '{"error":"stale timestamp"}')
                return

            with lock:
                previous = read_push(cfg)
                if sent_at <= int(previous.get("sent_at") or 0):
                    self._reply(409, '{"error":"replayed timestamp"}')
                    return
                _write_push(cfg, {"received_at": now, "sent_at": sent_at, "payload": payload})

            self._reply(200, '{"status":"ok"}')

    return PushHandler


def serve_forever(cfg: WatchdogConfig) -> None:
    server = ThreadingHTTPServer((cfg.push_bind, int(cfg.push_port)), _handler_factory(cfg))
    logger.info("push receiver listening on %s:%s", cfg.push_bind, cfg.push_port)
    server.serve_forever()


def start_background(cfg: WatchdogConfig) -> Optional[threading.Thread]:
    """Поднимает приёмник в фоне. Без секрета слой 3 просто выключен."""
    if not cfg.push_secret:
        logger.info("push receiver disabled (WATCHDOG_PUSH_SECRET not set)")
        return None
    thread = threading.Thread(target=serve_forever, args=(cfg,), daemon=True, name="push-receiver")
    thread.start()
    return thread
