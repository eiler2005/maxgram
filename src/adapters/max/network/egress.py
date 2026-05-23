from __future__ import annotations

import base64
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from aiohttp import BasicAuth


class MaxEgressUnavailable(ConnectionError):
    """Raised when the configured MAX egress path cannot open a tunnel."""


class MaxSocketConnector(Protocol):
    def connect(self, host: str, port: int, timeout: float | None = None) -> socket.socket: ...


@dataclass(frozen=True)
class MaxHttpClientOptions:
    proxy: str | None = None
    proxy_auth: BasicAuth | None = None

    def as_client_session_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.proxy_auth:
            kwargs["proxy_auth"] = self.proxy_auth
        return kwargs


@dataclass(frozen=True)
class MaxEgressProfile:
    name: str
    type: str
    socket_connector: MaxSocketConnector
    http_client_options: MaxHttpClientOptions
    proxy_host: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.type == "direct"

    @property
    def is_non_ru_warning(self) -> bool:
        return self.name == "hetzner_direct" and self.is_direct

    @property
    def status_label(self) -> str:
        if self.name == "home_ru_proxy":
            return "роутерный РФ Channel M"
        if self.name == "hetzner_direct":
            return "прямой Hetzner VPS (ручной аварийный режим)"
        if self.type == "http_connect":
            return f"HTTP CONNECT proxy ({self.name})"
        return f"direct ({self.name})"

    def safe_log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "max_egress_active": self.name,
            "max_egress_type": self.type,
            "max_egress_label": self.status_label,
        }
        if self.proxy_host:
            fields["max_egress_proxy_host"] = self.proxy_host
        return fields

    def probe(
        self,
        host: str = "api.oneme.ru",
        port: int = 443,
        timeout: float | None = 5.0,
    ) -> dict[str, object]:
        probe = getattr(self.socket_connector, "probe", None)
        if callable(probe):
            result = probe(host, port, timeout=timeout)
        else:
            result = _probe_tls_connect(
                self.socket_connector.connect,
                host=host,
                port=port,
                timeout=timeout,
            )
        return {**self.safe_log_fields(), **result}


class DirectSocketConnector:
    def connect(self, host: str, port: int, timeout: float | None = None) -> socket.socket:
        return socket.create_connection((host, port), timeout=timeout)

    def probe(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
    ) -> dict[str, object]:
        return _probe_tls_connect(self.connect, host=host, port=port, timeout=timeout)


class HttpConnectSocketConnector:
    def __init__(self, proxy_url: str):
        parsed = urlparse(proxy_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("MAX HTTP CONNECT proxy URL must use http:// or https://")
        if not parsed.hostname or not parsed.port:
            raise ValueError("MAX HTTP CONNECT proxy URL must include host and port")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._username = parsed.username
        self._password = parsed.password
        self.proxy_host = parsed.hostname
        self._aiohttp_proxy_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"

    @property
    def http_client_options(self) -> MaxHttpClientOptions:
        auth = None
        if self._username is not None:
            auth = BasicAuth(self._username, self._password or "")
        return MaxHttpClientOptions(proxy=self._aiohttp_proxy_url, proxy_auth=auth)

    def _proxy_authorization_header(self) -> str | None:
        if self._username is None:
            return None
        token = f"{self._username}:{self._password or ''}".encode("utf-8")
        return "Basic " + base64.b64encode(token).decode("ascii")

    def _open_tunnel(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        *,
        stage: Callable[[str], None] | None = None,
    ) -> socket.socket:
        if stage:
            stage("proxy_tcp")
        raw_sock = socket.create_connection((self._host, self._port), timeout=timeout)
        raw_sock.settimeout(timeout)
        proxy_sock: socket.socket = raw_sock
        if self._scheme == "https":
            if stage:
                stage("proxy_tls")
            context = ssl.create_default_context()
            proxy_sock = context.wrap_socket(raw_sock, server_hostname=self._host)

        if stage:
            stage("http_connect")
        request_lines = [
            f"CONNECT {host}:{port} HTTP/1.1",
            f"Host: {host}:{port}",
        ]
        auth_header = self._proxy_authorization_header()
        if auth_header:
            request_lines.append(f"Proxy-Authorization: {auth_header}")
        request_lines.extend(["Proxy-Connection: keep-alive", "", ""])
        proxy_sock.sendall("\r\n".join(request_lines).encode("ascii"))

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = proxy_sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 65536:
                raise MaxEgressUnavailable("MAX egress proxy response headers are too large")

        header = bytes(response).split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        parts = header.split()
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != 200:
            raise MaxEgressUnavailable("MAX egress proxy CONNECT failed")
        return proxy_sock

    def connect(self, host: str, port: int, timeout: float | None = None) -> socket.socket:
        try:
            return self._open_tunnel(host, port, timeout=timeout)
        except MaxEgressUnavailable:
            raise
        except Exception as exc:
            raise MaxEgressUnavailable("MAX egress proxy unavailable") from exc

    def probe(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        stage_name = "proxy_tcp"
        sock: socket.socket | None = None

        def set_stage(value: str) -> None:
            nonlocal stage_name
            stage_name = value

        try:
            sock = self._open_tunnel(host, port, timeout=timeout, stage=set_stage)
            stage_name = "target_tls"
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
            return _probe_result(
                ok=True,
                stage=stage_name,
                started=started,
                target_host=host,
                target_port=port,
            )
        except Exception as exc:
            return _probe_result(
                ok=False,
                stage=stage_name,
                started=started,
                target_host=host,
                target_port=port,
                error=exc,
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", r"\1***:***@", text, flags=re.I)
    text = re.sub(
        r"(?i)(proxy-authorization:\s*basic\s+)[a-z0-9+/=]+",
        r"\1***",
        text,
    )
    return f"{exc.__class__.__name__}: {text}"[:240]


def _probe_tls_connect(
    connect: Callable[[str, int, float | None], socket.socket],
    *,
    host: str,
    port: int,
    timeout: float | None,
) -> dict[str, object]:
    started = time.monotonic()
    stage_name = "target_tcp"
    sock: socket.socket | None = None
    try:
        sock = connect(host, port, timeout)
        stage_name = "target_tls"
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=host)
        return _probe_result(
            ok=True,
            stage=stage_name,
            started=started,
            target_host=host,
            target_port=port,
        )
    except Exception as exc:
        return _probe_result(
            ok=False,
            stage=stage_name,
            started=started,
            target_host=host,
            target_port=port,
            error=exc,
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _probe_result(
    *,
    ok: bool,
    stage: str,
    started: float,
    target_host: str,
    target_port: int,
    error: BaseException | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": ok,
        "stage": stage,
        "target_host": target_host,
        "target_port": target_port,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "checked_at": int(time.time()),
    }
    if error is not None:
        result["error"] = _safe_error(error)
    return result


def build_max_egress_profile(config) -> MaxEgressProfile:
    active = getattr(config, "active", "hetzner_direct")
    profiles = getattr(config, "profiles", {}) or {}
    profile_config = profiles.get(active)
    if profile_config is None:
        raise ValueError(f"MAX egress active profile {active!r} is not defined")

    profile_type = str(getattr(profile_config, "type", "direct") or "direct")
    if profile_type == "direct":
        return MaxEgressProfile(
            name=active,
            type="direct",
            socket_connector=DirectSocketConnector(),
            http_client_options=MaxHttpClientOptions(),
        )

    if profile_type == "http_connect":
        proxy_url = getattr(profile_config, "proxy_url", None)
        if not proxy_url:
            raise ValueError(f"MAX egress profile {active!r} requires proxy_url")
        connector = HttpConnectSocketConnector(proxy_url)
        return MaxEgressProfile(
            name=active,
            type="http_connect",
            socket_connector=connector,
            http_client_options=connector.http_client_options,
            proxy_host=connector.proxy_host,
        )

    raise ValueError(f"Unsupported MAX egress profile type {profile_type!r}")
