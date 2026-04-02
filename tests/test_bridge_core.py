from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.max_adapter import MaxAttachment, MaxMessage
from src.bridge.core import BridgeCore


class DummyRepo:
    async def get_binding_by_topic(self, tg_topic_id: int):
        return SimpleNamespace(max_chat_id="-70000000000003", tg_topic_id=tg_topic_id, mode="active")

    async def get_max_msg_id_by_tg(self, tg_msg_id: int):
        return "mx-reply-1"

    async def save_message(self, record):
        self.saved_record = record

    async def log_delivery(self, *args, **kwargs):
        self.logged = (args, kwargs)


class DummyMax:
    def on_message(self, handler):
        self.handler = handler

    async def resolve_user_name(self, user_id: str):
        return None

    async def send_message(self, chat_id: str, text: str, reply_to_msg_id=None):
        self.sent = (chat_id, text, reply_to_msg_id)
        return "mx-out-1"


class DummyTelegram:
    def __init__(self):
        self.calls = []

    def on_reply(self, handler):
        self.handler = handler

    async def send_photo(self, topic_id, path, caption=""):
        self.calls.append(("photo", caption))
        return 1

    async def send_document(self, topic_id, path, caption="", filename=""):
        self.calls.append(("document", caption, filename))
        return 2

    async def send_video(self, topic_id, path, caption="", filename="", duration=None, width=None, height=None):
        self.calls.append(("video", caption, filename, duration, width, height))
        return 3

    async def send_audio(self, topic_id, path, caption="", filename="", duration=None):
        self.calls.append(("audio", caption, filename, duration))
        return 4

    async def send_text(self, topic_id, text, reply_to_msg_id=None):
        self.calls.append(("text", text))
        return 5

    async def send_notification(self, text):
        self.calls.append(("notification", text))


@pytest.mark.asyncio
async def test_forward_to_telegram_sends_media_then_rendered_system_text(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    video_path = Path(tmp_path) / "clip.mp4"
    video_path.write_bytes(b"1234")

    msg = MaxMessage(
        msg_id="42",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text=None,
        attachments=[MaxAttachment("video", str(video_path), "clip.mp4", 7, 640, 360, "VIDEO")],
        attachment_types=["VIDEO"],
        rendered_texts=["Участник вышел из чата"],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 3
    assert tg_adapter.calls == [
        ("video", "[Тестовый Пользователь]", "clip.mp4", 7, 640, 360),
        ("text", "Участник вышел из чата"),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_uses_rendered_text_without_media(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="43",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text=None,
        attachments=[],
        attachment_types=["CONTROL"],
        rendered_texts=["Тестовый Пользователь вышел(а) из чата"],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 5
    assert tg_adapter.calls == [
        ("text", "Тестовый Пользователь вышел(а) из чата"),
    ]


@pytest.mark.asyncio
async def test_on_tg_reply_prefixes_sender_name_for_max():
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    await bridge._on_tg_reply(
        topic_id=99,
        text="Проверка связи",
        reply_to_tg_msg_id=123,
        sender_name="Марина Ермилова",
    )

    assert max_adapter.sent == (
        "-70000000000003",
        "[Марина Ермилова]\nПроверка связи",
        "mx-reply-1",
    )
