"""
MAX → Telegram Bridge — точка входа.

Запуск:
  python src/main.py
  docker-compose up
"""

import asyncio
import logging
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

# Добавляем корень проекта в path (для запуска из разных директорий)
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config.loader import load_config
from src.db.repository import Repository
from src.adapters.max_adapter import MaxAdapter
from src.adapters.tg_adapter import TelegramAdapter
from src.bridge.core import BridgeCore
from src.logging_utils import EventFormatter, log_event


@dataclass(frozen=True)
class StartupTestReport:
    status: str
    summary: str


def setup_logging():
    """Логирование: только meta, без PII."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt_mode = os.environ.get("LOG_FORMAT", "mixed").strip().lower() or "mixed"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EventFormatter(fmt_mode=fmt_mode))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)

    library_level = logging.DEBUG if _env_flag("LOG_LIBRARIES_DEBUG", default=False) else logging.WARNING
    logging.getLogger("aiogram").setLevel(library_level)
    logging.getLogger("pymax").setLevel(library_level)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def _mask_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{parts[1]}.*.{parts[3]}"
    return ip


def _detect_primary_ipv4() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _infer_location(hostname: str) -> str | None:
    explicit = os.environ.get("BRIDGE_LOCATION", "").strip()
    if explicit:
        return explicit

    hostname_l = hostname.lower()
    mapping = {
        "hel1": "Helsinki",
        "fsn1": "Falkenstein",
        "nbg1": "Nuremberg",
        "ash": "Ashburn",
        "hil": "Hillsboro",
        "sin": "Singapore",
    }
    for token, name in mapping.items():
        if token in hostname_l:
            return name
    return None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _extract_pytest_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "pytest finished without output"

    for line in reversed(lines):
        if " in " not in line:
            continue
        if any(token in line for token in ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed")):
            return line

    return lines[-1]


def _format_startup_tests_line(report: StartupTestReport) -> str:
    if report.status == "passed":
        return f"Тесты запуска: ✅ {report.summary}"
    if report.status == "failed":
        return f"Тесты запуска: ❌ {report.summary}"
    if report.status == "timeout":
        return f"Тесты запуска: ⏱️ {report.summary}"
    if report.status == "skipped":
        return f"Тесты запуска: ⚪ {report.summary}"
    return f"Тесты запуска: ⚠️ {report.summary}"


async def run_startup_tests(logger: logging.Logger) -> StartupTestReport:
    if not _env_flag("STARTUP_TESTS_ENABLED", default=False):
        return StartupTestReport(status="skipped", summary="отключены")

    try:
        timeout = max(1, int(os.environ.get("STARTUP_TESTS_TIMEOUT_SECONDS", "120")))
    except ValueError:
        timeout = 120

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--maxfail=1",
        "-p",
        "no:cacheprovider",
    ]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    logger.info("Running startup tests: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        logger.error("Could not launch startup tests: %s", e, exc_info=True)
        return StartupTestReport(status="error", summary=f"не удалось запустить pytest: {e}")

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.error("Startup tests timed out after %ss", timeout)
        return StartupTestReport(status="timeout", summary=f"таймаут после {timeout}с")

    output = stdout.decode("utf-8", errors="replace")
    summary = _extract_pytest_summary(output)

    if proc.returncode == 0:
        logger.info("Startup tests passed: %s", summary)
        return StartupTestReport(status="passed", summary=summary)

    logger.error("Startup tests failed: %s\n%s", summary, output)
    return StartupTestReport(status="failed", summary=summary)


async def build_startup_notification(repo: Repository,
                                     startup_tests: StartupTestReport | None = None) -> str:
    hostname = socket.gethostname()
    location = _infer_location(hostname)
    masked_ip = _mask_ip(_detect_primary_ipv4())
    runtime = "Docker" if Path("/.dockerenv").exists() else "Local"

    try:
        bindings = await repo.list_bindings()
        total_chats = len(bindings)
        active_chats = sum(1 for b in bindings if b.mode == "active")
        chats_info = f"Чатов: {total_chats} (активных: {active_chats})"
    except Exception:
        chats_info = ""

    lines = ["🚀 Maxgram запущен и подключён к MAX"]
    infra = [f"runtime: {runtime}", f"host: {hostname}"]
    if location:
        infra.append(f"location: {location}")
    if masked_ip:
        infra.append(f"ip: {masked_ip}")
    lines.append(" · ".join(infra))
    if chats_info:
        lines.append(chats_info)
    if startup_tests is not None:
        lines.append(_format_startup_tests_line(startup_tests))
    lines.append("Отправьте /status для подробного отчёта")
    return "\n".join(lines)


async def main():
    setup_logging()
    logger = logging.getLogger("bridge.main")

    config_path = os.environ.get("CONFIG_PATH", "config.yaml")
    log_event(
        logger,
        logging.INFO,
        "app.startup.config_loading",
        stage="startup",
        outcome="started",
        config_path=Path(config_path).name,
    )

    try:
        cfg = load_config(config_path)
    except Exception as e:
        logger.critical("Config error: %s", e)
        sys.exit(1)

    # DB
    repo = Repository(cfg.storage.db_path)
    await repo.connect()
    log_event(
        logger,
        logging.INFO,
        "app.startup.db_connected",
        stage="startup",
        outcome="ok",
        db_path=Path(cfg.storage.db_path).name,
    )

    # Adapters
    max_adapter = MaxAdapter(
        phone=cfg.max.phone,
        data_dir=cfg.storage.session_path,
        session_name=cfg.max.session_filename,
        tmp_dir=str(cfg.storage.tmp_dir),
    )

    tg_adapter = TelegramAdapter(
        bot_token=cfg.telegram.bot_token,
        owner_id=cfg.telegram.owner_id,
        forum_group_id=cfg.telegram.forum_group_id,
        tmp_dir=str(cfg.storage.tmp_dir),
    )

    # Bridge Core — связывает адаптеры
    bridge = BridgeCore(cfg, repo, max_adapter, tg_adapter)

    # Уведомление + fix fallback топиков при первом старте MAX
    _started_once = False

    async def on_max_ready():
        nonlocal _started_once
        if _started_once:
            logger.info("MAX reconnected (skip duplicate notifications)")
            return
        _started_once = True
        startup_tests = await run_startup_tests(logger)
        await tg_adapter.send_notification(
            await build_startup_notification(repo, startup_tests=startup_tests)
        )

    max_adapter.on_start(on_max_ready)

    # Инициализируем Telegram бота (без запуска polling)
    await tg_adapter.setup()
    bot = tg_adapter.get_bot()
    dp  = tg_adapter.get_dispatcher()

    log_event(
        logger,
        logging.INFO,
        "app.startup.bridge_starting",
        stage="startup",
        outcome="started",
    )

    # Запускаем все компоненты параллельно
    async with asyncio.TaskGroup() as tg:
        # MAX: блокирующий, собственный reconnect-цикл с чистым клиентом
        tg.create_task(max_adapter.start(), name="max_adapter")

        # Telegram: polling
        tg.create_task(
            dp.start_polling(bot, allowed_updates=["message"]),
            name="tg_polling",
        )

        # Cleanup: фоновый
        tg.create_task(bridge.run_cleanup(), name="cleanup")

        # MAX watchdog: alert если MAX offline > 60s
        tg.create_task(bridge.run_max_watchdog(), name="max_watchdog")

        # Periodic status: отчёт каждые 4 часа
        tg.create_task(bridge.run_periodic_status(), name="periodic_status")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBridge stopped.")
