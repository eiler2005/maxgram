import asyncio
import os
from types import SimpleNamespace

import pytest

from src.main import (
    StartupTestReport,
    _extract_pytest_summary,
    _infer_location,
    _mask_ip,
    build_startup_notification,
)


def test_mask_ip_hides_third_octet():
    assert _mask_ip("203.0.113.217") == "203.0.*.217"


def test_infer_location_from_hetzner_hostname():
    assert _infer_location("ubuntu-4gb-hel1-6") == "Helsinki"


def test_extract_pytest_summary_uses_terminal_summary():
    output = """
    tests/test_main.py ..

    17 passed in 1.49s
    """.strip()

    assert _extract_pytest_summary(output) == "17 passed in 1.49s"


@pytest.mark.asyncio
async def test_build_startup_notification_includes_runtime_details(monkeypatch):
    monkeypatch.setattr("src.main.socket.gethostname", lambda: "ubuntu-4gb-hel1-6")
    monkeypatch.setattr("src.main._detect_primary_ipv4", lambda: "203.0.113.217")
    monkeypatch.delenv("BRIDGE_LOCATION", raising=False)
    monkeypatch.setattr("src.main.Path.exists", lambda self: True if str(self) == "/.dockerenv" else False)

    class FakeRepo:
        async def list_bindings(self):
            return []

    text = await build_startup_notification(FakeRepo())

    assert "Maxgram запущен и подключён к MAX" in text
    assert "runtime: Docker" in text
    assert "host: ubuntu-4gb-hel1-6" in text
    assert "location: Helsinki" in text
    assert "ip: 203.0.*.217" in text


@pytest.mark.asyncio
async def test_build_startup_notification_includes_startup_test_status(monkeypatch):
    monkeypatch.setattr("src.main.socket.gethostname", lambda: "ubuntu-4gb-hel1-6")
    monkeypatch.setattr("src.main._detect_primary_ipv4", lambda: "203.0.113.217")
    monkeypatch.delenv("BRIDGE_LOCATION", raising=False)
    monkeypatch.setattr("src.main.Path.exists", lambda self: True if str(self) == "/.dockerenv" else False)

    class FakeRepo:
        async def list_bindings(self):
            return []

    text = await build_startup_notification(
        FakeRepo(),
        startup_tests=StartupTestReport(status="passed", summary="17 passed in 1.49s"),
    )

    assert "Тесты запуска: ✅ 17 passed in 1.49s" in text
