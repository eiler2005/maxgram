from types import SimpleNamespace

import pytest

from src.adapters.max.backends.pymax.media import PymaxMediaGateway


class EmptyFileClient:
    async def get_file_by_id(self, *, chat_id: int, message_id: int, file_id: int):
        return None


class CapturingRawGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return {"payload": {"result": {"downloadUrl": "https://cdn.example.invalid/file"}}}


@pytest.mark.asyncio
async def test_file_url_falls_back_to_raw_file_download_after_empty_typed_response():
    raw = CapturingRawGateway()
    gateway = PymaxMediaGateway(EmptyFileClient(), raw)

    url = await gateway.file_url(chat_id=11, message_id=22, file_id=33)

    assert url == "https://cdn.example.invalid/file"
    assert raw.calls == [
        {
            "opcode_name": "FILE_DOWNLOAD",
            "default_opcode": 88,
            "payload": {"chatId": 11, "messageId": 22, "fileId": 33},
            "timeout": 5,
        }
    ]
