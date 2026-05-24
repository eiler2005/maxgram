from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pymax import File, Photo, Video
from pymax.api.messages.payloads import ChatHistoryPayload, GetVideoPayload

from ...ports import MaxClientMessage, MaxSendResult
from .models import model_dump
from .raw_gateway import PymaxRawGateway


class PymaxMediaGateway:
    def __init__(self, client, raw_gateway: PymaxRawGateway) -> None:
        self._client = client
        self._raw = raw_gateway

    def make_attachment(self, *, media_path: str | None, media_type: str | None):
        if not media_path:
            return None
        if media_type == "photo":
            return Photo(path=media_path)
        if media_type == "video":
            return Video(path=media_path)
        return File(path=media_path)

    async def send_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ) -> MaxSendResult:
        attachment = self.make_attachment(media_path=media_path, media_type=media_type)
        attachments = [attachment] if attachment is not None else None
        result = await self._client.send_message(
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            attachments=attachments,
        )
        return MaxSendResult(message_id=self.extract_result_message_id(result), raw=result)

    async def file_url(self, *, chat_id: int, message_id: int, file_id: int) -> str | None:
        file_obj = await self._client.get_file_by_id(
            chat_id=chat_id,
            message_id=message_id,
            file_id=file_id,
        )
        url = getattr(file_obj, "url", None)
        if not url:
            data = model_dump(file_obj) or {}
            url = data.get("url")
        return str(url) if url else None

    async def video_payload(
        self, *, chat_id: int, message_id: int, video_id: int
    ) -> dict[str, Any] | None:
        payload = get_video_payload(
            chat_id=chat_id,
            message_id=message_id,
            video_id=video_id,
        )
        data = await self._raw.request(opcode_name="VIDEO_PLAY", payload=payload)
        raw_payload = data.get("payload") if isinstance(data, dict) else None
        return raw_payload if isinstance(raw_payload, dict) else None

    async def raw_history_payload(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> dict[str, Any] | None:
        payload = fetch_history_payload(
            chat_id=chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )
        data = await self._raw.request(
            opcode_name="CHAT_HISTORY",
            default_opcode=49,
            payload=payload,
            timeout=10,
        )
        raw_payload = data.get("payload") if isinstance(data, dict) else None
        return raw_payload if isinstance(raw_payload, dict) else None

    async def history_messages(
        self, *, chat_id: int, from_time: int, forward: int, backward: int
    ) -> Iterable[MaxClientMessage]:
        messages = await self._client.fetch_history(
            chat_id,
            from_time=from_time,
            forward=forward,
            backward=backward,
        )
        return [MaxClientMessage.from_object(message) for message in messages or []]

    def extract_result_message_id(self, result) -> str | None:
        if result is None:
            return None

        direct_id = getattr(result, "id", None) or getattr(result, "message_id", None)
        if direct_id is not None:
            return str(direct_id)

        def from_dict(data) -> str | None:
            if not isinstance(data, dict):
                return None
            for key in ("id", "messageId", "message_id"):
                if data.get(key) is not None:
                    return str(data[key])
            for key in ("message", "payload", "result", "msg"):
                found = from_dict(data.get(key))
                if found:
                    return found
            return None

        return from_dict(result)


def fetch_history_payload(
    *,
    chat_id: int,
    from_time: int,
    forward: int,
    backward: int,
) -> dict[str, Any]:
    return ChatHistoryPayload(
        chat_id=chat_id,
        from_=from_time,
        forward=forward,
        backward=backward,
    ).to_payload()


def get_video_payload(*, chat_id: int, message_id: int, video_id: int) -> dict[str, Any]:
    return GetVideoPayload(
        chat_id=chat_id,
        message_id=message_id,
        video_id=video_id,
    ).to_payload()
