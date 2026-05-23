from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

from pymax import SocketMaxClient
from pymax.exceptions import SocketNotConnectedError
from pymax.static.constant import DEFAULT_PING_INTERVAL
from pymax.static.enum import Opcode
from pymax.payloads import UserAgentPayload
from pymax.types import Message

from ...network import DirectSocketConnector
from .client_adapter import PymaxClientAdapter


class EgressSocketMaxClient(SocketMaxClient):
    """SocketMaxClient variant with an injectable TCP connector."""

    def __init__(self, *args, socket_connector=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._maxtg_socket_connector = socket_connector or DirectSocketConnector()

    async def connect(self, user_agent: UserAgentPayload | None = None) -> dict:
        if user_agent is None:
            user_agent = UserAgentPayload()
        self.logger.info("Connecting to socket %s:%s", self.host, self.port)
        loop = asyncio.get_running_loop()
        raw_sock = await loop.run_in_executor(
            None,
            lambda: self._maxtg_socket_connector.connect(
                self.host,
                self.port,
                timeout=20.0,
            ),
        )
        self._socket = self._ssl_context.wrap_socket(raw_sock, server_hostname=self.host)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.is_connected = True
        self._incoming = asyncio.Queue()
        self._outgoing = asyncio.Queue()
        self._pending = {}
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._outgoing_task = asyncio.create_task(self._outgoing_loop())
        self.logger.info("Socket connected, starting handshake")
        return await self._handshake(user_agent)


class PymaxBackend:
    def __init__(self, *, phone: str, data_dir: str, session_name: str, egress=None):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = session_name
        self._egress = egress

    def create_raw_client(self):
        if self._egress is None:
            return SocketMaxClient(
                phone=self._phone,
                work_dir=self._data_dir,
                session_name=self._session_name,
                reconnect=False,
                send_fake_telemetry=False,
            )
        socket_connector = self._egress.socket_connector
        return EgressSocketMaxClient(
            phone=self._phone,
            work_dir=self._data_dir,
            session_name=self._session_name,
            reconnect=False,
            send_fake_telemetry=False,
            socket_connector=socket_connector,
        )

    def create_client(self):
        return PymaxClientAdapter(self.create_raw_client())

    def failfast_ping_config(self) -> dict[str, object]:
        return {
            "ping_interval": DEFAULT_PING_INTERVAL,
            "ping_opcode": Opcode.PING,
            "disconnect_error": SocketNotConnectedError,
        }

    def make_file_attachment(self, path: str):
        from pymax.files import File

        return File(path=path)

    def make_photo_attachment(self, path: str):
        from pymax.files import Photo

        return Photo(path=path)

    def make_video_attachment(self, path: str):
        from pymax.files import Video

        return Video(path=path)

    def make_message_from_dict(self, payload: dict):
        return Message.from_dict(payload)

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
    ) -> dict:
        from pymax.payloads import FetchHistoryPayload

        return FetchHistoryPayload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        ).model_dump(by_alias=True)

    def get_video_payload(self, *, chat_id: int, message_id: int, video_id: int) -> dict:
        from pymax.payloads import GetVideoPayload

        return GetVideoPayload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        ).model_dump(by_alias=True)
