import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from src.adapters.max.adapter import MaxAdapter
from src.bridge.core import BridgeCore
from src.db.repository import Repository
from tests.fakes.fake_max_backend import FakeMaxBackend


class IntegrationConfig(SimpleNamespace):
    def get_chat_title(self, max_chat_id: str):
        return None

    def get_chat_mode(self, max_chat_id: str):
        return "active"


class FakeTelegramBridge:
    def __init__(self):
        self._reply_handlers = []
        self.commands = {}
        self.arg_commands = {}
        self.created_topics: list[dict[str, object]] = []
        self.sent_texts: list[dict[str, object]] = []
        self._next_topic_id = 100
        self._next_message_id = 1000

    def on_reply(self, handler):
        self._reply_handlers.append(handler)

    def on_command(self, cmd: str, handler):
        self.commands[cmd] = handler

    def on_arg_command(self, cmd: str, handler, *, allow_group_general: bool = False):
        self.arg_commands[cmd] = (handler, allow_group_general)

    def on_callback_action(self, handler):
        self.callback_handler = handler

    async def create_topic(self, title: str, *, flow_id=None) -> int:
        topic_id = self._next_topic_id
        self._next_topic_id += 1
        self.created_topics.append({"topic_id": topic_id, "title": title, "flow_id": flow_id})
        return topic_id

    async def rename_topic(self, topic_id: int, new_title: str, *, flow_id=None):
        return None

    async def delete_topic(self, topic_id: int, *, flow_id=None) -> bool:
        return True

    async def close_topic(self, topic_id: int, *, flow_id=None) -> bool:
        return True

    async def send_text(
        self,
        topic_id: int,
        text: str,
        reply_to_msg_id=None,
        flow_id=None,
        buttons=None,
    ):
        self._next_message_id += 1
        self.sent_texts.append(
            {
                "topic_id": topic_id,
                "text": text,
                "reply_to_msg_id": reply_to_msg_id,
                "flow_id": flow_id,
                "buttons": buttons,
                "message_id": self._next_message_id,
            }
        )
        return self._next_message_id

    async def send_photo(self, *args, **kwargs):
        return await self.send_text(args[0], "[photo]", flow_id=kwargs.get("flow_id"))

    async def send_document(self, *args, **kwargs):
        return await self.send_text(args[0], "[document]", flow_id=kwargs.get("flow_id"))

    async def send_video(self, *args, **kwargs):
        return await self.send_text(args[0], "[video]", flow_id=kwargs.get("flow_id"))

    async def send_audio(self, *args, **kwargs):
        return await self.send_text(args[0], "[audio]", flow_id=kwargs.get("flow_id"))

    async def send_voice(self, *args, **kwargs):
        return await self.send_text(args[0], "[voice]", flow_id=kwargs.get("flow_id"))

    async def send_notification(self, text: str) -> bool:
        return True

    async def send_system_notification(self, text: str, *, category: str = "system") -> bool:
        return True

    async def send_owner_document(self, path: str, caption: str = "", filename: str = "") -> bool:
        return True

    def get_last_send_error(self):
        return None

    async def emit_reply(
        self,
        *,
        topic_id: int,
        tg_msg_id: int,
        text: str,
        reply_to_tg_msg_id=None,
        sender_name: str = "Fake Operator",
    ):
        for handler in list(self._reply_handlers):
            await handler(topic_id, tg_msg_id, text, reply_to_tg_msg_id, sender_name, None, None)


async def _wait_until(predicate, *, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for integration condition")


@pytest.mark.asyncio
async def test_fake_max_backend_round_trips_message_and_reply(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    backend = FakeMaxBackend()
    max_adapter = MaxAdapter(
        phone="+70000000000",
        data_dir=str(tmp_path),
        session_name="session.db",
        tmp_dir=str(tmp_path / "tmp"),
        backend=backend,
    )
    tg = FakeTelegramBridge()
    cfg = IntegrationConfig(
        bridge=SimpleNamespace(max_file_size_mb=50),
        content=SimpleNamespace(
            placeholder_unsupported="[unsupported: {type}]",
            placeholder_file_too_large="[too large: {filename}]",
            forward_voice=True,
            forward_documents=True,
            forward_photos=True,
        ),
        health=SimpleNamespace(reminder_interval_hours=4),
    )
    BridgeCore(cfg, repo, max_adapter, tg)

    start_task = asyncio.create_task(max_adapter.start())
    try:
        await _wait_until(max_adapter.is_ready)

        await backend.client.emit_text_message(text="sample text")
        await _wait_until(lambda: bool(tg.sent_texts))

        topic_id = int(tg.created_topics[0]["topic_id"])
        assert tg.created_topics[0]["title"] == "Fake MAX Chat"
        assert tg.sent_texts[0]["topic_id"] == topic_id
        assert tg.sent_texts[0]["text"] == "[Fake Sender] sample text"

        await tg.emit_reply(topic_id=topic_id, tg_msg_id=2001, text="reply sample")
        await _wait_until(lambda: bool(backend.client.sent_messages))

        assert backend.client.sent_messages[0]["chat_id"] == "-70000000000003"
        assert backend.client.sent_messages[0]["text"] == "[Fake Operator]\nreply sample"
    finally:
        await max_adapter.close()
        start_task.cancel()
        with suppress(asyncio.CancelledError):
            await start_task
        await repo.close()
