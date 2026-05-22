from __future__ import annotations

from types import SimpleNamespace

from pymax import SocketMaxClient
from pymax.exceptions import SocketNotConnectedError
from pymax.files import File, Photo, Video
from pymax.payloads import FetchHistoryPayload, GetVideoPayload
from pymax.static.constant import DEFAULT_PING_INTERVAL
from pymax.static.enum import Opcode
from pymax.types import Message


class PymaxBackend:
    def __init__(self, *, phone: str, data_dir: str, session_name: str):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = session_name

    def create_client(self):
        return SocketMaxClient(
            phone=self._phone,
            work_dir=self._data_dir,
            session_name=self._session_name,
            reconnect=False,
            send_fake_telemetry=False,
        )

    def failfast_ping_config(self) -> dict[str, object]:
        return {
            "ping_interval": DEFAULT_PING_INTERVAL,
            "ping_opcode": Opcode.PING,
            "disconnect_error": SocketNotConnectedError,
        }

    def make_file_attachment(self, path: str):
        return File(path=path)

    def make_photo_attachment(self, path: str):
        return Photo(path=path)

    def make_video_attachment(self, path: str):
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
        return FetchHistoryPayload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        ).model_dump(by_alias=True)

    def get_video_payload(self, *, chat_id: int, message_id: int, video_id: int) -> dict:
        return GetVideoPayload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        ).model_dump(by_alias=True)
