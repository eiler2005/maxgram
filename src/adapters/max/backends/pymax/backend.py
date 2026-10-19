from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pymax import File, Message, Photo, Video
from pymax.protocol import Opcode

from .client_adapter import PymaxClientAdapter
from .client_factory import create_pymax_client, make_extra_config
from .media import fetch_history_payload, get_video_payload
from .transport import EgressTCPTransport


class PymaxBackend:
    def __init__(self, *, phone: str, data_dir: str, session_name: str, egress=None):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = session_name
        self._egress = egress
        self._media_ssl_context = None

    def create_raw_client(self):
        return create_pymax_client(
            phone=self._phone,
            data_dir=self._data_dir,
            session_name=self._session_name,
            egress=self._egress,
        )

    def create_client(self):
        return PymaxClientAdapter(self.create_raw_client())

    def set_egress(self, profile):
        self._egress = profile

    def probe_egress(self, profile):
        config = make_extra_config()
        transport = EgressTCPTransport(socket_connector=profile.socket_connector,
                                       host=config.host, port=config.port,
                                       use_ssl=config.use_ssl)
        return profile.probe(host=config.host, port=config.port,
                             ssl_context=transport._ssl_ctx)

    def media_ssl_context(self):
        """Return PyMax's MAX-CA-aware TLS context for CDN transfers."""
        if self._media_ssl_context is None:
            config = make_extra_config()
            transport = EgressTCPTransport(
                socket_connector=self._egress.socket_connector if self._egress else None,
                host=config.host,
                port=config.port,
                use_ssl=config.use_ssl,
            )
            self._media_ssl_context = transport._ssl_ctx
        return self._media_ssl_context

    def failfast_ping_config(self) -> None:
        return None

    def make_file_attachment(self, path: str):
        return File(path=path)

    def make_photo_attachment(self, path: str):
        return Photo(path=path)

    def make_video_attachment(self, path: str):
        return Video(path=path)

    def make_message_from_dict(self, payload: dict[str, Any]):
        return Message.model_validate(payload)

    def opcode(self, name: str, default: int | None = None):
        value = getattr(Opcode, name, None)
        if value is not None:
            return value
        if default is None:
            return None
        return SimpleNamespace(value=default, name=name)

    def opcode_value(self, name: str, default: int) -> int:
        value = self.opcode(name, default)
        return int(getattr(value, "value", value))

    def opcode_name(self, value: object) -> str | None:
        opcode_value = getattr(value, "value", value)
        try:
            return Opcode(opcode_value).name
        except Exception:
            return str(getattr(value, "name", "") or "") or None

    def fetch_history_payload(
        self,
        *,
        chat_id: int,
        from_time: int,
        forward: int,
        backward: int,
    ) -> dict[str, Any]:
        return fetch_history_payload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )

    def get_video_payload(self, *, chat_id: int, message_id: int, video_id: int) -> dict[str, Any]:
        return get_video_payload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        )
