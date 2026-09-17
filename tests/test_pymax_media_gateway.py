from types import SimpleNamespace

import pytest
from pymax.formatting.markdown import Formatter

from src.adapters.max.backends.pymax.media import PymaxMediaGateway, render_bare_urls_as_max_links


class EmptyFileClient:
    async def get_file_by_id(self, *, chat_id: int, message_id: int, file_id: int):
        return None


class FileClient:
    def __init__(self, url: str | None) -> None:
        self.url = url

    async def get_file_by_id(self, *, chat_id: int, message_id: int, file_id: int):
        return SimpleNamespace(url=self.url)


class VideoClient:
    def __init__(self, url: str | None) -> None:
        self.url = url

    async def get_video_by_id(self, *, chat_id: int, message_id: int, video_id: int):
        return SimpleNamespace(url=self.url)


class CapturingRawGateway:
    def __init__(self, response=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or {
            "payload": {"result": {"downloadUrl": "https://cdn.example.invalid/file"}}
        }

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class SendingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=44)


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


@pytest.mark.asyncio
async def test_file_url_uses_safe_typed_url_without_raw_request():
    raw = CapturingRawGateway()
    gateway = PymaxMediaGateway(FileClient("https://cdn.example.invalid/typed-file"), raw)

    url = await gateway.file_url(chat_id=11, message_id=22, file_id=33)

    assert url == "https://cdn.example.invalid/typed-file"
    assert raw.calls == []


@pytest.mark.asyncio
async def test_file_url_discards_unsafe_typed_url_and_uses_safe_raw_fallback():
    raw = CapturingRawGateway()
    gateway = PymaxMediaGateway(FileClient("file:///unexpected"), raw)

    url = await gateway.file_url(chat_id=11, message_id=22, file_id=33)

    assert url == "https://cdn.example.invalid/file"
    assert len(raw.calls) == 1


@pytest.mark.asyncio
async def test_video_url_discards_non_http_typed_url_so_caller_can_use_raw_fallback():
    raw = CapturingRawGateway()
    gateway = PymaxMediaGateway(VideoClient("javascript:unexpected"), raw)

    url = await gateway.video_url(chat_id=11, message_id=22, video_id=33)

    assert url is None
    assert raw.calls == []


@pytest.mark.asyncio
async def test_outbound_bare_url_becomes_clickable_max_link_with_same_visible_text():
    url = "https://max.ru/join/example_invite_token"
    text = f"Это для меня: {url}."
    client = SendingClient()
    gateway = PymaxMediaGateway(client, CapturingRawGateway())

    result = await gateway.send_outbound_message(chat_id=11, text=text)

    assert result.message_id == "44"
    assert client.calls == [
        {
            "chat_id": 11,
            "text": f"Это для меня: [{url}]({url}).",
            "reply_to": None,
            "attachments": None,
        }
    ]
    visible_text, entities = Formatter.format_markdown(client.calls[0]["text"])
    assert visible_text == text
    assert len(entities) == 1
    assert entities[0].attributes.url == url


def test_outbound_url_renderer_preserves_existing_markdown_and_unsafe_parentheses():
    markdown_link = "[Сайт](https://example.invalid/already-linked)"
    parenthesized_url = "https://example.invalid/path(with-parentheses)"

    rendered = render_bare_urls_as_max_links(f"{markdown_link} {parenthesized_url}")

    assert rendered == f"{markdown_link} {parenthesized_url}"
