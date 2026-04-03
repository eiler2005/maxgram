import asyncio
import os
from types import SimpleNamespace

import pytest

from src.main import _infer_location, _mask_ip, build_startup_notification


def test_mask_ip_hides_third_octet():
    assert _mask_ip("204.168.239.217") == "204.168.*.217"


def test_infer_location_from_hetzner_hostname():
    assert _infer_location("ubuntu-4gb-hel1-6") == "Helsinki"


@pytest.mark.asyncio
async def test_build_startup_notification_includes_runtime_details(monkeypatch):
    monkeypatch.setattr("src.main.socket.gethostname", lambda: "ubuntu-4gb-hel1-6")
    monkeypatch.setattr("src.main._detect_primary_ipv4", lambda: "204.168.239.217")
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
    assert "ip: 204.168.*.217" in text
