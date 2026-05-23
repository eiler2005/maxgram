from __future__ import annotations

import base64
import socket
import ssl
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


class DirectSocketConnector:
    def connect(self, host: str, port: int, timeout: float | None = None) -> socket.socket:
        return socket.create_connection((host, port), timeout=timeout)


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

    def connect(self, host: str, port: int, timeout: float | None = None) -> socket.socket:
        try:
            raw_sock = socket.create_connection((self._host, self._port), timeout=timeout)
            proxy_sock: socket.socket = raw_sock
            if self._scheme == "https":
                context = ssl.create_default_context()
                proxy_sock = context.wrap_socket(raw_sock, server_hostname=self._host)

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
        except MaxEgressUnavailable:
            raise
        except Exception as exc:
            raise MaxEgressUnavailable("MAX egress proxy unavailable") from exc


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
