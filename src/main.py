"""
MAX → Telegram Bridge — точка входа.

Запуск:
  python -m src.main
  docker compose up
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
from src.adapters.max_adapter import MaxAdapter, MaxIssue
from src.adapters.tg_adapter import TelegramAdapter
from src.bridge.core import BridgeCore
from src.logging_utils import EventFormatter, log_event
from src.runtime.health import (
    RuntimeHealthStore,
    Severity,
    build_operator_alert,
)
from src.runtime.supervisor import BridgeSupervisor, SupervisorConfig


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
    lines.append("Команды: /status · /chats · /dm · /help")
    return "\n".join(lines)


def build_max_issue_notification(issue: MaxIssue) -> str:
    lines = [f"❌ MAX недоступен: {issue.summary}"]
    if issue.requires_reauth:
        lines.append("Нужен reauth: перезапусти bridge и введи новый SMS-код.")
    if issue.raw_error:
        lines.append(f"Причина: {issue.raw_error}")
    lines.append("Проверь /status после восстановления.")
    return "\n".join(lines)


async def _emit_health_change(notifier: TelegramAdapter | None, change):
    if notifier is None or change is None or not getattr(change, "notify", False):
        return
    await notifier.send_system_notification(build_operator_alert(change), category="health")


def _build_max_health_payload(issue: MaxIssue) -> dict:
    operator_hint = (
        "Сделай reauth по SMS: перезапусти bridge и введи новый код."
        if issue.requires_reauth
        else "Проверь /status и логи MAX. Если reconnect не проходит, попробуй перезапуск bridge."
    )
    auto_recovery = (
        "Bridge оставляет контейнер Up и продолжит reconnect, но без reauth восстановление маловероятно."
        if issue.requires_reauth
        else "MAX reconnect loop уже активен и будет продолжать попытки автоматически."
    )
    return {
        "code": issue.kind,
        "summary": issue.summary,
        "raw_cause": issue.raw_error,
        "severity": Severity.CRITICAL if issue.requires_reauth else Severity.ERROR,
        "impact": "Связка MAX ↔ Telegram деградировала: входящие и/или исходящие сообщения могут не проходить.",
        "operator_hint": operator_hint,
        "auto_recovery": auto_recovery,
        "requires_reauth": issue.requires_reauth,
    }


async def run_bridge_worker(cfg,
                            health_store: RuntimeHealthStore,
                            notifier: TelegramAdapter | None,
                            logger: logging.Logger):
    repo: Repository | None = None
    tg_adapter: TelegramAdapter | None = None
    stage = "storage_connect"

    try:
        repo = Repository(cfg.storage.db_path)
        await repo.connect()
        await health_store.mark_healthy(
            "storage",
            summary="SQLite storage подключён и доступен",
            notify=False,
        )
        log_event(
            logger,
            logging.INFO,
            "app.startup.db_connected",
            stage="startup",
            outcome="ok",
            db_path=Path(cfg.storage.db_path).name,
        )

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

        system_notifier = notifier or tg_adapter
        bridge = BridgeCore(
            cfg,
            repo,
            max_adapter,
            tg_adapter,
            ops_notifier=system_notifier,
            health_store=health_store,
        )

        stage = "max_callbacks"
        started_once = False

        async def on_max_ready():
            nonlocal started_once
            change = await health_store.mark_healthy(
                "max_link",
                summary="MAX connected and synchronized",
                notify=True,
            )
            await _emit_health_change(system_notifier, change)

            if started_once:
                return
            started_once = True
            startup_tests = await run_startup_tests(logger)
            await system_notifier.send_system_notification(
                await build_startup_notification(repo, startup_tests=startup_tests),
                category="startup",
            )

        async def on_max_issue(issue: MaxIssue):
            payload = _build_max_health_payload(issue)
            change = await health_store.report_issue(
                "max_link",
                code=payload["code"],
                summary=payload["summary"],
                raw_cause=payload["raw_cause"],
                severity=payload["severity"],
                impact=payload["impact"],
                operator_hint=payload["operator_hint"],
                auto_recovery=payload["auto_recovery"],
                requires_reauth=payload["requires_reauth"],
                notify=True,
            )
            await _emit_health_change(system_notifier, change)

        max_adapter.on_start(on_max_ready)
        max_adapter.on_issue(on_max_issue)

        stage = "tg_setup"
        await tg_adapter.setup()
        await health_store.mark_healthy(
            "tg_link",
            summary="Telegram polling adapter инициализирован",
            notify=False,
        )
        await health_store.mark_healthy(
            "scheduler",
            summary="Background scheduler initialized",
            notify=False,
        )

        runtime_change = await health_store.mark_healthy(
            "runtime",
            summary="Bridge worker запущен и держит task group",
            notify=True,
        )
        await _emit_health_change(system_notifier, runtime_change)

        bot = tg_adapter.get_bot()
        dp = tg_adapter.get_dispatcher()

        log_event(
            logger,
            logging.INFO,
            "app.startup.bridge_starting",
            stage="startup",
            outcome="started",
        )

        stage = "task_group"
        async with asyncio.TaskGroup() as tg:
            tg.create_task(max_adapter.start(), name="max_adapter")
            tg.create_task(
                dp.start_polling(bot, allowed_updates=["message"]),
                name="tg_polling",
            )
            tg.create_task(bridge.run_cleanup(), name="cleanup")
            tg.create_task(bridge.run_max_watchdog(), name="max_watchdog")
            tg.create_task(
                bridge.run_periodic_status(cfg.health.reminder_interval_hours),
                name="periodic_status",
            )
    except Exception as e:
        if stage == "storage_connect":
            await health_store.report_issue(
                "storage",
                code="storage_connect_failed",
                summary="SQLite storage не удалось подключить",
                raw_cause=str(e),
                severity=Severity.ERROR,
                impact="Bridge не может стартовать worker без storage.",
                operator_hint="Проверь путь DATA_DIR, права доступа и целостность SQLite файлов.",
                auto_recovery="Supervisor попытается перезапустить worker автоматически.",
                notify=False,
            )
        elif stage == "tg_setup":
            await health_store.report_issue(
                "tg_link",
                code="telegram_setup_failed",
                summary="Telegram polling adapter не инициализировался",
                raw_cause=str(e),
                severity=Severity.ERROR,
                impact="Команды бота и bridge-алерты через Telegram временно недоступны.",
                operator_hint="Проверь bot token, доступность Telegram API и конфиг forum_group_id.",
                auto_recovery="Supervisor попытается перезапустить worker автоматически.",
                notify=False,
            )
        raise
    finally:
        if tg_adapter is not None:
            await tg_adapter.close()
        if repo is not None:
            await repo.close()


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

    health_store = RuntimeHealthStore(
        cfg.storage.data_dir,
        reminder_interval_hours=cfg.health.reminder_interval_hours,
        heartbeat_interval_seconds=cfg.health.heartbeat_interval_seconds,
    )

    ops_notifier: TelegramAdapter | None = None
    outbox_task: asyncio.Task | None = None
    try:
        ops_notifier = TelegramAdapter(
            bot_token=cfg.telegram.bot_token,
            owner_id=cfg.telegram.owner_id,
            forum_group_id=cfg.telegram.forum_group_id,
            ops_topic_id=cfg.telegram.ops_topic_id,
            tmp_dir=str(cfg.storage.tmp_dir),
            outbox_store=health_store.outbox,
            health_store=health_store,
        )
        await ops_notifier.setup()
        await health_store.mark_healthy(
            "alerting",
            summary="Telegram ops notifier инициализирован",
            notify=False,
        )
        outbox_task = asyncio.create_task(
            ops_notifier.run_notification_outbox(
                poll_interval_seconds=max(5, cfg.health.heartbeat_interval_seconds)
            ),
            name="ops_notification_outbox",
        )
    except Exception as e:
        logger.error("Ops notifier setup failed: %s", e, exc_info=True)
        await health_store.report_issue(
            "alerting",
            code="notifier_setup_failed",
            summary="Telegram ops notifier не инициализировался",
            raw_cause=str(e),
            severity=Severity.ERROR,
            impact="Живые ops-алерты могут не отправляться, но будут копиться в outbox при следующих попытках.",
            operator_hint="Проверь bot token и доступность Telegram API.",
            auto_recovery="После починки notifier начнёт автоматически досылать накопленный outbox.",
            notify=False,
        )
        ops_notifier = None

    supervisor = BridgeSupervisor(
        health_store=health_store,
        worker_factory=lambda: run_bridge_worker(cfg, health_store, ops_notifier, logger),
        notify=ops_notifier.send_system_notification if ops_notifier is not None else None,
        config=SupervisorConfig(
            heartbeat_interval_seconds=cfg.health.heartbeat_interval_seconds,
            worker_restart_backoff_seconds=cfg.health.worker_restart_backoff_seconds,
        ),
    )

    try:
        await supervisor.run()
    finally:
        if outbox_task is not None:
            outbox_task.cancel()
            try:
                await outbox_task
            except asyncio.CancelledError:
                pass
        if ops_notifier is not None:
            await ops_notifier.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBridge stopped.")
