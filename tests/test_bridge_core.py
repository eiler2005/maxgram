import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.max_adapter import MaxAttachment, MaxMessage
from src.bridge.core import BridgeCore


class DummyRepo:
    def __init__(self):
        self.bindings = []
        self.activity_map = {}
        self.binding_by_chat = {}

    async def get_binding_by_topic(self, tg_topic_id: int):
        return SimpleNamespace(max_chat_id="-70000000000003", tg_topic_id=tg_topic_id, mode="active")

    async def get_binding(self, max_chat_id: str):
        return self.binding_by_chat.get(max_chat_id)

    async def get_max_msg_id_by_tg(self, tg_msg_id: int):
        return "mx-reply-1"

    async def save_message(self, record):
        self.saved_record = record

    async def save_binding(self, binding):
        self.binding_by_chat[binding.max_chat_id] = binding

    async def update_title(self, max_chat_id: str, title: str):
        binding = self.binding_by_chat.get(max_chat_id)
        if binding is not None:
            binding.title = title

    async def log_delivery(self, *args, **kwargs):
        self.logged = (args, kwargs)

    async def list_bindings(self):
        return self.bindings

    async def get_chat_activity_map_since(self, since_ts: int):
        return self.activity_map


class DummyMax:
    def on_message(self, handler):
        self.handler = handler

    async def resolve_user_name(self, user_id: str):
        return None

    async def resolve_chat_title(self, chat_id: str):
        return None

    async def send_message(self, chat_id: str, text: str, reply_to_msg_id=None,
                           media_path=None, media_type=None, flow_id=None):
        self.sent = (chat_id, text, reply_to_msg_id, flow_id)
        return "mx-out-1"


class DummyTelegram:
    def __init__(self):
        self.calls = []
        self.commands = {}

    def on_reply(self, handler):
        self.handler = handler

    def on_command(self, cmd: str, handler):
        self.commands[cmd] = handler

    async def send_photo(self, topic_id, path, caption="", flow_id=None):
        self.calls.append(("photo", caption))
        return 1

    async def send_document(self, topic_id, path, caption="", filename="", flow_id=None):
        self.calls.append(("document", caption, filename))
        return 2

    async def send_video(self, topic_id, path, caption="", filename="", duration=None, width=None, height=None, flow_id=None):
        self.calls.append(("video", caption, filename, duration, width, height))
        return 3

    async def send_audio(self, topic_id, path, caption="", filename="", duration=None, flow_id=None):
        self.calls.append(("audio", caption, filename, duration))
        return 4

    async def send_voice(self, topic_id, path, caption="", duration=None, flow_id=None):
        self.calls.append(("voice", caption, duration))
        return 6

    async def send_text(self, topic_id, text, reply_to_msg_id=None, flow_id=None):
        self.calls.append(("text", text))
        return 5

    async def send_notification(self, text):
        self.calls.append(("notification", text))

    async def create_topic(self, title, flow_id=None):
        self.calls.append(("create_topic", title, flow_id))
        return 101

    async def rename_topic(self, topic_id, title, flow_id=None):
        self.calls.append(("rename_topic", topic_id, title, flow_id))


class DummyConfig(SimpleNamespace):
    def get_chat_title(self, max_chat_id: str):
        titles = getattr(self, "_chat_titles", {})
        return titles.get(max_chat_id)

    def get_chat_mode(self, max_chat_id: str):
        modes = getattr(self, "_chat_modes", {})
        return modes.get(max_chat_id, "active")


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
async def test_forward_to_telegram_sends_voice_note_for_voice_source(tmp_path):
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

    voice_path = Path(tmp_path) / "voice.ogg"
    voice_path.write_bytes(b"OggSvoice")

    msg = MaxMessage(
        msg_id="44",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text="",
        attachments=[MaxAttachment("audio", str(voice_path), "voice.ogg", 3, None, None, "VOICE")],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 6
    assert tg_adapter.calls == [
        ("voice", "[Тестовый Пользователь]", 3),
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
        tg_msg_id=555,
        text="Проверка связи",
        reply_to_tg_msg_id=123,
        sender_name="Марина Ермилова",
    )

    assert max_adapter.sent == (
        "-70000000000003",
        "[Марина Ермилова]\nПроверка связи",
        "mx-reply-1",
        "tg:99:555",
    )


@pytest.mark.asyncio
async def test_on_tg_reply_rejects_too_large_media(tmp_path):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=0.000001),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    media_path = Path(tmp_path) / "huge.bin"
    media_path.write_bytes(b"0123456789")

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=555,
        text="",
        reply_to_tg_msg_id=None,
        sender_name="Марина Ермилова",
        media_path=str(media_path),
        media_type="document",
    )

    assert not hasattr(max_adapter, "sent")
    assert tg_adapter.calls == [
        ("text", "🚫 [too large: huge.bin] (лимит: 1e-06MB)"),
    ]


@pytest.mark.asyncio
async def test_get_or_create_topic_resolves_group_title_via_live_max_lookup():
    class GroupAwareMax(DummyMax):
        async def resolve_chat_title(self, chat_id: str):
            assert chat_id == "-70243447272944"
            return "2104 ПН 16:40 Scratch Jr"

    repo = DummyRepo()
    max_adapter = GroupAwareMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=DummyConfig(
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

    msg = MaxMessage(
        msg_id="42",
        chat_id="-70243447272944",
        chat_title=None,
        sender_id="10",
        sender_name="Наталья Ростовцева",
        text="Тест",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    topic_id = await bridge._get_or_create_topic(msg, flow_id="mx:-70243447272944:42")

    assert topic_id == 101
    assert tg_adapter.calls == [
        ("create_topic", "2104 ПН 16:40 Scratch Jr", "mx:-70243447272944:42"),
    ]
    assert repo.binding_by_chat["-70243447272944"].title == "2104 ПН 16:40 Scratch Jr"


@pytest.mark.asyncio
async def test_build_chats_message_lists_topics_with_activity():
    repo = DummyRepo()
    repo.bindings = [
        SimpleNamespace(max_chat_id="-1", tg_topic_id=101, title="Школьный чат", mode="active", created_at=1),
        SimpleNamespace(max_chat_id="-2", tg_topic_id=102, title="Секция", mode="readonly", created_at=2),
    ]
    repo.activity_map = {
        "-1": {"inbound": 3, "outbound": 1, "total": 4},
        "-2": {"inbound": 0, "outbound": 2, "total": 2},
    }

    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=DummyMax(),
        tg_adapter=DummyTelegram(),
    )

    text = await bridge._build_chats_message(period_hours=24)

    assert "🗂 Чаты: 2 (активных: 1)" in text
    assert "✅ #101 Школьный чат · ↓3 ↑1" in text
    assert "🔒 #102 Секция · ↓0 ↑2" in text


@pytest.mark.asyncio
async def test_watchdog_sends_gap_notice_after_reconnect():
    class WatchdogMax:
        def __init__(self):
            self.calls = 0

        def on_message(self, handler):
            self.handler = handler

        async def resolve_user_name(self, user_id: str):
            return None

        def is_ready(self):
            self.calls += 1
            if self.calls == 1:
                return False
            return True

    repo = DummyRepo()
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
        max_adapter=WatchdogMax(),
        tg_adapter=tg_adapter,
    )

    task = asyncio.create_task(
        bridge.run_max_watchdog(alert_after_seconds=0, check_interval=0)
    )
    try:
        for _ in range(100):
            if len([c for c in tg_adapter.calls if c[0] == "notification"]) >= 3:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    notifications = [c[1] for c in tg_adapter.calls if c[0] == "notification"]
    assert any("MAX недоступен уже" in text for text in notifications)
    assert any("Возможен пропуск сообщений MAX" in text for text in notifications)
    assert any("MAX восстановлен" in text for text in notifications)


@pytest.mark.asyncio
async def test_on_tg_reply_logs_forward_completion(caplog):
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

    with caplog.at_level(logging.INFO, logger="src.bridge.core"):
        await bridge._on_tg_reply(
            topic_id=99,
            tg_msg_id=777,
            text="Проверка логов",
            reply_to_tg_msg_id=123,
            sender_name="Марина Ермилова",
        )

    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "bridge.outbound.forward_finished"
        and event.get("outcome") == "delivered"
        for event in events
    )


# ---------------------------------------------------------------------------
# MaxAdapter._fix_filename_encoding — cp1251-as-latin-1 mojibake
# ---------------------------------------------------------------------------

def test_fix_filename_encoding_fixes_cyrillic_mojibake():
    from src.adapters.max_adapter import MaxAdapter
    # "Вальс из к/ф Маскарад - Арам Хачатурян.mp3" stored as cp1251, read as latin-1
    garbled = "Âàëüñ èç ê/ô Ìàñêàðàä - Àðàì Õà÷àòóðÿí.mp3"
    fixed = MaxAdapter._fix_filename_encoding(garbled)
    assert fixed == "Вальс из к/ф Маскарад - Арам Хачатурян.mp3"


def test_fix_filename_encoding_leaves_ascii_unchanged():
    from src.adapters.max_adapter import MaxAdapter
    assert MaxAdapter._fix_filename_encoding("audio_track.ogg") == "audio_track.ogg"


def test_fix_filename_encoding_leaves_proper_utf8_unchanged():
    from src.adapters.max_adapter import MaxAdapter
    # Already correct UTF-8 Cyrillic — encode("latin-1") raises, original returned
    name = "Вальс.mp3"
    assert MaxAdapter._fix_filename_encoding(name) == name
