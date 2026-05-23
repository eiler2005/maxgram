import socket
import threading

import pytest

from src.adapters.max import errors as max_errors
from src.adapters.max.media.downloader import MaxCdnDownloader
from src.adapters.max.network import (
    HttpConnectSocketConnector,
    MaxEgressProfile,
    MaxEgressUnavailable,
    MaxHttpClientOptions,
    build_max_egress_profile,
)
from src.config.loader import MaxEgressConfig, MaxEgressProfileConfig


def _run_proxy_once(status_line: bytes = b"HTTP/1.1 200 Connection Established\r\n"):
    server = socket.create_server(("127.0.0.1", 0))
    request = {}

    def worker():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            request["raw"] = data
            conn.sendall(status_line + b"\r\n")
        server.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return server.getsockname()[1], request, thread


def test_http_connect_socket_connector_sends_connect_and_auth():
    port, request, thread = _run_proxy_once()
    connector = HttpConnectSocketConnector(f"http://user:pass@127.0.0.1:{port}")

    sock = connector.connect("api.oneme.ru", 443, timeout=2)
    sock.close()
    thread.join(timeout=2)

    raw = request["raw"].decode("ascii")
    assert raw.startswith("CONNECT api.oneme.ru:443 HTTP/1.1\r\n")
    assert "Host: api.oneme.ru:443\r\n" in raw
    assert "Proxy-Authorization: Basic dXNlcjpwYXNz\r\n" in raw


def test_http_connect_socket_connector_fails_on_non_200():
    port, _, thread = _run_proxy_once(b"HTTP/1.1 407 Proxy Authentication Required\r\n")
    connector = HttpConnectSocketConnector(f"http://user:bad@127.0.0.1:{port}")

    with pytest.raises(MaxEgressUnavailable):
        connector.connect("api.oneme.ru", 443, timeout=2)
    thread.join(timeout=2)


def test_http_connect_socket_connector_probe_success(monkeypatch):
    class NoopTlsContext:
        def wrap_socket(self, sock, server_hostname=None):
            return sock

    monkeypatch.setattr(
        "src.adapters.max.network.egress.ssl.create_default_context",
        lambda: NoopTlsContext(),
    )
    port, request, thread = _run_proxy_once()
    connector = HttpConnectSocketConnector(f"http://user:pass@127.0.0.1:{port}")

    result = connector.probe("api.oneme.ru", 443, timeout=2)
    thread.join(timeout=2)

    assert result["ok"] is True
    assert result["stage"] == "target_tls"
    assert result["target_host"] == "api.oneme.ru"
    assert request["raw"].startswith(b"CONNECT api.oneme.ru:443 HTTP/1.1\r\n")


def test_http_connect_socket_connector_probe_failure_is_redacted():
    port, _, thread = _run_proxy_once(b"HTTP/1.1 407 Proxy Authentication Required\r\n")
    connector = HttpConnectSocketConnector(f"http://user:secret@127.0.0.1:{port}")

    result = connector.probe("api.oneme.ru", 443, timeout=2)
    thread.join(timeout=2)

    assert result["ok"] is False
    assert result["stage"] == "http_connect"
    assert "secret" not in str(result.get("error"))
    assert "user:secret" not in str(result.get("error"))


def test_max_egress_profile_probe_includes_safe_profile_fields():
    class FakeConnector:
        def probe(self, host, port, timeout=None):
            return {
                "ok": True,
                "stage": "target_tls",
                "target_host": host,
                "target_port": port,
                "latency_ms": 7,
                "checked_at": 1776962052,
            }

        def connect(self, host, port, timeout=None):  # pragma: no cover - protocol shape
            raise AssertionError("probe should use FakeConnector.probe")

    profile = MaxEgressProfile(
        name="home_ru_proxy",
        type="http_connect",
        socket_connector=FakeConnector(),
        http_client_options=MaxHttpClientOptions(proxy="http://proxy.example.invalid:4444"),
        proxy_host="proxy.example.invalid",
    )

    result = profile.probe("api.oneme.ru", 443, timeout=1)

    assert result["ok"] is True
    assert result["stage"] == "target_tls"
    assert result["max_egress_active"] == "home_ru_proxy"
    assert result["max_egress_proxy_host"] == "proxy.example.invalid"
    assert "proxy_auth" not in result
    assert "pass" not in str(result)


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "image/jpeg"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return b"\xff\xd8\xff\xe0image"


class _FakeSession:
    def __init__(self, calls):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url):
        self._calls.append(("get", url))
        return _FakeResponse()


@pytest.mark.asyncio
async def test_max_cdn_downloader_passes_proxy_options(tmp_path):
    calls = []
    egress = build_max_egress_profile(
        MaxEgressConfig(
            active="home_ru_proxy",
            profiles={
                "home_ru_proxy": MaxEgressProfileConfig(
                    type="http_connect",
                    proxy_url="http://user:pass@proxy.example.invalid:4444",
                )
            },
        )
    )

    def session_factory(**kwargs):
        calls.append(("session", kwargs))
        return _FakeSession(calls)

    downloader = MaxCdnDownloader(
        tmp_dir=tmp_path,
        client_session_factory=session_factory,
        egress=egress,
    )

    path, filename = await downloader.download_from_url(
        "https://cdn.example.test/photo.jpg",
        "photo",
        expected_kind="photo",
    )

    assert filename == "photo.jpg"
    assert path is not None
    session_kwargs = calls[0][1]
    assert session_kwargs["proxy"] == "http://proxy.example.invalid:4444"
    assert session_kwargs["proxy_auth"].login == "user"
    assert session_kwargs["proxy_auth"].password == "pass"


def test_max_egress_unavailable_is_classified_as_fail_closed_issue():
    issue = max_errors.classify_runtime_error(MaxEgressUnavailable("MAX egress proxy unavailable"))

    assert issue is not None
    assert issue.kind == "max_egress_unavailable"
    assert issue.requires_reauth is False
