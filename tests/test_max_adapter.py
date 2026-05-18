import asyncio
import logging
from types import SimpleNamespace

from aiohttp import ClientResponseError
import pytest

from src.adapters.max_adapter import (
    MAX_CDN_ANDROID_CHROME_USER_AGENT,
    MAX_CDN_CHROME_USER_AGENT,
    MAX_CDN_IOS_CHROME_USER_AGENT,
    MAX_CDN_USER_AGENT,
    MaxAttachment,
    MaxAdapter,
)


def make_user(first_name: str, last_name: str = ""):
    return SimpleNamespace(
        names=[
            SimpleNamespace(
                first_name=first_name,
                last_name=last_name,
                name=first_name,
            )
        ]
    )


class LookupClient:
    def __init__(self, *, users=None, chats=None):
        self._users = users or {}
        self.chats = chats or []
        self.me = SimpleNamespace(id=161361072)

    def get_cached_user(self, user_id: int):
        return self._users.get(user_id)

    async def get_users(self, user_ids: list[int]):
        return [self._users[uid] for uid in user_ids if uid in self._users]


class DummyDownloadAdapter(MaxAdapter):
    async def _download_attachment(self, chat_id: str, msg_id: str, attach, index: int = 0, flow_id=None):
        raw_type = self._attachment_type_name(attach)
        return MaxAttachment(
            kind="document",
            local_path="/tmp/fake",
            filename=None,
            duration=None,
            width=None,
            height=None,
            source_type=raw_type,
        )


class CapturingDownloadAdapter(DummyDownloadAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.download_calls = []

    async def _download_attachment(
        self,
        chat_id: str,
        msg_id: str,
        attach,
        index: int = 0,
        flow_id=None,
    ):
        self.download_calls.append(
            (chat_id, msg_id, self._attachment_type_name(attach), index)
        )
        return await super()._download_attachment(
            chat_id,
            msg_id,
            attach,
            index,
            flow_id,
        )


class CapturingAttachmentDownloadAdapter(MaxAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url_downloads = []
        self.file_downloads = []
        self.url_result = (None, None)
        self.file_result = (None, None)

    async def _download_from_url(
        self,
        url: str,
        prefix: str,
        filename_hint=None,
        default_extension: str = "",
        expected_kind=None,
        flow_id=None,
        download_source=None,
    ):
        self.url_downloads.append(
            (url, prefix, filename_hint, default_extension, expected_kind, download_source)
        )
        return self.url_result

    async def _download_file_by_id(
        self,
        chat_id: str,
        msg_id: str,
        file_id: int,
        prefix: str,
        filename_hint=None,
        default_extension: str = "",
        expected_kind=None,
        flow_id=None,
    ):
        self.file_downloads.append(
            (chat_id, msg_id, file_id, prefix, filename_hint, default_extension, expected_kind)
        )
        return self.file_result


@pytest.mark.asyncio
async def test_handle_raw_message_renders_control_leave(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(
        users={7001: make_user("Тестовый", "Пользователь")},
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")],
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=1,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="leave", extra={})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Тестовый Пользователь вышел(а) из чата"]
    assert received[0].attachment_types == ["CONTROL"]
    assert received[0].chat_title == "Тестовая группа"


@pytest.mark.asyncio
async def test_handle_raw_message_unwraps_forward_link_content(tmp_path):
    adapter = CapturingDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(
        users={7001: make_user("Тестовый", "Пользователь")},
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")],
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    forwarded_message = SimpleNamespace(
        id=901,
        chat_id=-80000000000001,
        sender=None,
        text="Пост из канала",
        type="TEXT",
        status=None,
        attaches=[
            SimpleNamespace(type="PHOTO", url="https://cdn.example.test/photo.jpg")
        ],
        link=None,
    )
    message = SimpleNamespace(
        id=101,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="CHANNEL",
        status=None,
        attaches=[],
        link=SimpleNamespace(
            type="FORWARD",
            chat_id=-80000000000001,
            message=forwarded_message,
        ),
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].msg_id == "101"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].text == "Пост из канала"
    assert received[0].message_type == "TEXT"
    assert received[0].attachment_types == ["PHOTO"]
    assert adapter.download_calls == [("-80000000000001", "901", "PHOTO", 0)]


@pytest.mark.asyncio
async def test_handle_raw_receive_unwraps_channel_wrapper_and_skips_pymax_duplicate(tmp_path):
    adapter = MaxAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")]
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": -70000000000003,
            "message": {
                "id": 102,
                "time": 1,
                "sender": 7001,
                "text": "",
                "type": "CHANNEL",
                "attaches": [],
                "message": {
                    "id": 902,
                    "time": 1,
                    "sender": None,
                    "text": "Реальный пост канала",
                    "type": "TEXT",
                    "attaches": [],
                },
            },
        },
    }

    await adapter._handle_raw_receive(raw_event)
    await adapter._handle_raw_message(
        SimpleNamespace(
            id=102,
            chat_id=-70000000000003,
            sender=7001,
            text="",
            type="CHANNEL",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert len(received) == 1
    assert received[0].msg_id == "102"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].text == "Реальный пост канала"
    assert received[0].message_type == "TEXT"
    assert received[0].rendered_texts == []


@pytest.mark.asyncio
async def test_handle_raw_receive_forwards_regular_audio_before_pymax_can_drop_it(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(chats=[SimpleNamespace(id=28093080, title=None)])
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "message": {
                "id": 103,
                "time": 1,
                "sender": 7001,
                "text": "",
                "type": "USER",
                "attaches": [
                    {
                        "_type": "AUDIO",
                        "audioId": 42,
                        "url": "https://audio.example.test/voice.ogg",
                        "duration": 13,
                        "wave": "abc",
                        "transcriptionStatus": "NONE",
                        "token": "secret-token",
                    }
                ],
            },
        },
    }

    await adapter._handle_raw_receive(raw_event)
    await adapter._handle_raw_message(
        SimpleNamespace(
            id=103,
            chat_id=28093080,
            sender=7001,
            text="",
            type="USER",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert len(received) == 1
    assert received[0].msg_id == "103"
    assert received[0].chat_id == "28093080"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 13, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/voice.ogg",
            "audio_28093080_103",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_raw_message_interceptor_catches_audio_and_suppresses_duplicate(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class NotificationClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})
            self._on_raw_receive_handlers = []
            self.original_calls = 0

        async def _handle_message_notifications(self, data):
            self.original_calls += 1
            await adapter._handle_raw_message(
                SimpleNamespace(
                    id=105,
                    chat_id=28093080,
                    sender=7001,
                    text="",
                    type="USER",
                    status=None,
                    attaches=[],
                    link=None,
                )
            )

    client = NotificationClient()
    adapter._client = client
    adapter._install_raw_message_interceptor(client)

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "message": {
                "id": 105,
                "time": 1,
                "sender": 7001,
                "text": "",
                "type": "USER",
                "attaches": [
                    {
                        "_type": "AUDIO",
                        "audioId": 42,
                        "url": "https://audio.example.test/voice.ogg",
                        "duration": 9,
                        "wave": "abc",
                        "transcriptionStatus": "NONE",
                        "token": "secret-token",
                    }
                ],
            },
        },
    }

    await adapter._handle_raw_receive(raw_event)
    await client._handle_message_notifications(raw_event)

    assert client.original_calls == 1
    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/voice.ogg",
            "audio_28093080_105",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_handle_raw_receive_forwards_top_level_audio_payload(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(users={7001: make_user("Вита")})
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "messageId": 107,
            "time": 1,
            "sender": 7001,
            "text": "",
            "type": "USER",
            "attachments": [
                {
                    "_type": "AUDIO",
                    "audioId": 42,
                    "url": "https://audio.example.test/top-level.ogg",
                    "duration": 7,
                    "wave": "abc",
                    "transcriptionStatus": "NONE",
                    "token": "secret-token",
                }
            ],
        },
    }

    await adapter._handle_raw_receive(raw_event)

    assert len(received) == 1
    assert received[0].msg_id == "107"
    assert received[0].chat_id == "28093080"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 7, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/top-level.ogg",
            "audio_28093080_107",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_handle_raw_receive_logs_top_level_empty_message_diagnostic(tmp_path, caplog):
    adapter = MaxAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "messageId": 108,
            "sender": 7001,
            "text": "",
            "type": "USER",
            "attachments": [],
            "token": "secret-token",
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_event)

    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event") == "max.raw.empty_message"
    )
    fields = record.event_fields
    assert fields["max_chat_id"] == "28093080"
    assert fields["max_msg_id"] == "108"
    assert fields["message_type"] == "USER"
    assert "attachments" in fields["message_fields"]
    assert "token" not in fields["message_fields"]
    assert "text" not in fields["message_fields"]
    assert "secret-token" not in str(fields)


@pytest.mark.asyncio
async def test_typed_empty_message_recovers_audio_from_recent_history(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    recovered_message = SimpleNamespace(
        id=106,
        chat_id=28093080,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="AUDIO",
                audio_id=84,
                url="https://audio.example.test/recovered.ogg",
                duration=12,
                wave="abc",
                token="secret-token",
            )
        ],
        link=None,
    )

    class HistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})
            self.history_calls = []

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            self.history_calls.append((chat_id, from_time, forward, backward))
            return [recovered_message]

    client = HistoryClient()
    adapter._client = client
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=106,
                chat_id=28093080,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 12, None, None, "AUDIO")
    ]
    assert client.history_calls
    assert client.history_calls[0][0] == 28093080
    assert client.history_calls[0][2:] == (0, 10)
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "recovered"
        and event.get("attachment_types") == ["AUDIO"]
        for event in events
    )
    assert not any(
        event.get("event") == "max.inbound.skipped"
        and event.get("max_msg_id") == "106"
        for event in events
    )


@pytest.mark.asyncio
async def test_handle_raw_receive_logs_safe_empty_message_diagnostic(tmp_path, caplog):
    adapter = MaxAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "message": {
                "id": 104,
                "sender": 7001,
                "text": "",
                "type": "USER",
                "attaches": [],
                "token": "secret-token",
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_event)

    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event") == "max.raw.empty_message"
    )
    fields = record.event_fields
    assert fields["max_chat_id"] == "28093080"
    assert fields["max_msg_id"] == "104"
    assert fields["message_type"] == "USER"
    assert "message" in fields["payload_fields"]
    assert "token" not in fields["message_fields"]
    assert "text" not in fields["message_fields"]
    assert "secret-token" not in str(fields)


@pytest.mark.asyncio
async def test_handle_raw_message_renders_unknown_message_details(tmp_path):
    adapter = MaxAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")]
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=103,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="CHANNEL",
        status=None,
        attaches=[],
        link=SimpleNamespace(type="FORWARD", chat_id=-80000000000001, message=None),
        mysteryPayload={"kind": "new-shape"},
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts
    rendered = received[0].rendered_texts[0]
    assert rendered.startswith("[Неизвестное сообщение MAX]")
    assert "type=CHANNEL" in rendered
    assert "link_type=FORWARD" in rendered
    assert "link_chat_id=-80000000000001" in rendered
    assert "outer_fields=" in rendered
    assert "mysteryPayload" in rendered


@pytest.mark.asyncio
async def test_handle_raw_message_renders_control_add_with_partial_name_resolution(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(
        users={7001: make_user("Тестовый", "Пользователь")},
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")],
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=2,
        chat_id=-70000000000003,
        sender=40053201,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="add", extra={"userIds": [7001, 12345]})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Добавлены участники: Тестовый Пользователь, ещё 1"]


@pytest.mark.asyncio
async def test_handle_raw_message_renders_control_join_by_link(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(
        users={7001: make_user("Тестовый", "Пользователь")},
        chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")],
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=2,
        chat_id=-70000000000003,
        sender=40053201,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="joinbylink", extra={"userIds": [7001]})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Присоединились по ссылке: Тестовый Пользователь"]


@pytest.mark.asyncio
async def test_handle_raw_message_renders_join_by_link_with_sender_when_no_user_ids(tmp_path):
    """joinbylink без userIds — имя берётся из sender."""
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(
        users={7001: make_user("Екатерина", "Глебова")},
        chats=[SimpleNamespace(id=-70000000000003, title="Родительский чат")],
    )

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=3,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="joinbylink", extra={})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Присоединился по ссылке: Екатерина Глебова"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attach", "expected"),
    [
        (
            SimpleNamespace(type="CONTACT", name="Тестовый Контакт", first_name="Тестовый", last_name="Контакт"),
            "Контакт: Тестовый Контакт",
        ),
        (SimpleNamespace(type="STICKER", audio=False), "[Стикер]"),
        (SimpleNamespace(type="STICKER", audio=True), "[Аудиостикер]"),
    ],
)
async def test_handle_raw_message_renders_non_media_supported_attachments(tmp_path, attach, expected):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=123456789, title=None)])

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=3,
        chat_id=123456789,
        sender=37294736,
        text="",
        type="USER",
        status=None,
        attaches=[attach],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("IMAGE", "PHOTO"),
        ("VOICE", "AUDIO"),
        ("DOCUMENT", "FILE"),
        ("DOC", "FILE"),
    ],
)
async def test_handle_raw_message_normalizes_alias_attachment_types(tmp_path, raw_type, expected):
    adapter = DummyDownloadAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=123456789, title=None)])

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=3,
        chat_id=123456789,
        sender=37294736,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type=raw_type, url="https://example.test/file")],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].attachment_types == [expected]
    assert len(received[0].attachments) == 1


@pytest.mark.asyncio
async def test_download_audio_attachment_uses_direct_url_and_preserves_duration(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    attachment = await adapter._download_attachment(
        "28093080",
        "116562825769007612",
        SimpleNamespace(
            type="AUDIO",
            audio_id=42,
            url="https://audio.example.test/voice.ogg",
            duration=13,
            wave="abc",
        ),
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 13, None, None, "AUDIO")
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/voice.ogg",
            "audio_28093080_116562825769007612",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]
    assert adapter.file_downloads == []


@pytest.mark.asyncio
async def test_download_audio_attachment_falls_back_to_audio_id(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.file_result = (local_path, "voice.ogg")

    attachment = await adapter._download_attachment(
        "28093080",
        "116562825769007612",
        SimpleNamespace(type="AUDIO", audio_id=42, duration=13, wave="abc"),
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 13, None, None, "AUDIO")
    assert adapter.url_downloads == []
    assert adapter.file_downloads == [
        (
            "28093080",
            "116562825769007612",
            42,
            "audio_28093080_116562825769007612",
            None,
            ".ogg",
            "audio",
        )
    ]


@pytest.mark.asyncio
async def test_download_audio_attachment_logs_safe_diagnostic_without_reference(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    caplog.set_level(logging.WARNING)

    attachment = await adapter._download_attachment(
        "28093080",
        "116562825769007612",
        SimpleNamespace(type="AUDIO", duration=13, token="secret-token", url=None, text="secret"),
    )

    assert attachment is None
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event") == "max.attachment.voice_reference_missing"
    )
    fields = record.event_fields
    assert fields["attachment_class"] == "SimpleNamespace"
    assert "duration" in fields["attachment_fields"]
    assert "token" not in fields["attachment_fields"]
    assert "url" not in fields["attachment_fields"]
    assert "text" not in fields["attachment_fields"]
    assert "secret-token" not in str(fields)
    assert "secret" not in str(fields)


@pytest.mark.asyncio
async def test_handle_raw_message_skips_empty_reaction_only_event(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=77,
        chat_id=-70000000000003,
        sender=40053201,
        text="",
        type="USER",
        status=None,
        attaches=[],
        reactionInfo=SimpleNamespace(total_count=3),
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert received == []


@pytest.mark.asyncio
async def test_handle_raw_message_logs_received_and_skip_reason(tmp_path, caplog):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=77,
                chat_id=-70000000000003,
                sender=40053201,
                text="",
                type="USER",
                status=None,
                attaches=[],
                reactionInfo=SimpleNamespace(total_count=3),
                link=None,
            )
        )

    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(event.get("event") == "max.inbound.received" for event in events)
    empty_event = next(
        event for event in events if event.get("event") == "max.inbound.empty_message"
    )
    assert empty_event["message_class"] == "SimpleNamespace"
    assert "text" not in empty_event["message_fields"]
    assert any(
        event.get("event") == "max.inbound.skipped" and event.get("reason") == "empty_event"
        for event in events
    )


class EchoAckClient(LookupClient):
    def __init__(self, adapter):
        super().__init__()
        self._adapter = adapter

    async def send_message(self, **kwargs):
        async def emit_echo():
            await asyncio.sleep(0.01)
            await self._adapter._handle_raw_message(
                SimpleNamespace(
                    id=4242,
                    chat_id=kwargs["chat_id"],
                    sender=161361072,
                    text=kwargs["text"],
                    type="USER",
                    status=None,
                    attaches=[],
                    link=None,
                )
            )

        asyncio.create_task(emit_echo())
        return {"payload": {"accepted": True}}


@pytest.mark.asyncio
async def test_send_message_waits_for_echo_ack_when_pymax_does_not_return_id(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._own_id = "161361072"
    adapter._client = EchoAckClient(adapter)

    received = []

    async def handler(msg):
        received.append(msg.msg_id)

    adapter.on_message(handler)

    msg_id = await adapter.send_message("123456789", "тест исходящего сообщения")

    assert msg_id == "4242"
    assert received == []


class DirectIdClient(LookupClient):
    async def send_message(self, **kwargs):
        return SimpleNamespace(id=31337)


@pytest.mark.asyncio
async def test_own_echo_is_suppressed_when_send_message_returns_real_id(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._own_id = "161361072"
    adapter._client = DirectIdClient()

    received = []

    async def handler(msg):
        received.append(msg.msg_id)

    adapter.on_message(handler)

    msg_id = await adapter.send_message("123456789", "тест")
    assert msg_id == "31337"

    await adapter._handle_raw_message(
        SimpleNamespace(
            id=31337,
            chat_id=123456789,
            sender=161361072,
            text="тест",
            type="USER",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert received == []


class FlakyRetryClient(LookupClient):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_send_message_retries_retryable_transport_error_and_succeeds(tmp_path, monkeypatch, caplog):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._client = FlakyRetryClient(
        [
            RuntimeError("Socket is not connected"),
            SimpleNamespace(id=4243),
        ]
    )

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        msg_id = await adapter.send_message("123456789", "тест")

    assert msg_id == "4243"
    assert adapter._client.calls == 2
    assert adapter.get_last_outbound_error() is None
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(event.get("event") == "max.outbound.retry" for event in events)
    assert any(event.get("event") == "max.outbound.sent" and event.get("attempt") == 2 for event in events)


@pytest.mark.asyncio
async def test_send_message_exposes_final_error_after_retries(tmp_path, monkeypatch):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._client = FlakyRetryClient(
        [
            RuntimeError("Socket is not connected"),
            RuntimeError("Socket is not connected"),
            RuntimeError("Socket is not connected"),
        ]
    )

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    msg_id = await adapter.send_message("123456789", "тест")

    assert msg_id is None
    assert adapter._client.calls == 3
    assert adapter.get_last_outbound_error() == "Socket is not connected"
    assert adapter.get_last_outbound_attempts() == 3


class FakeSocketNotConnectedError(Exception):
    pass


class PingClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.is_connected = True
        self.close_calls = 0
        self.send_calls = 0
        self.logger = logging.getLogger(f"tests.max_adapter.ping.{id(self)}")

    async def _send_and_wait(self, **kwargs):
        self.send_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if not self.outcomes:
            self.is_connected = False
        return outcome

    async def close(self):
        self.close_calls += 1
        self.is_connected = False


@pytest.mark.asyncio
async def test_failfast_ping_closes_client_after_consecutive_failures(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    client = PingClient([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])

    ping_loop = adapter._build_failfast_interactive_ping(
        client,
        ping_interval=0,
        failure_limit=3,
        ping_opcode=object(),
        disconnect_error=FakeSocketNotConnectedError,
    )

    await ping_loop()

    assert client.send_calls == 3
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_failfast_ping_resets_failure_counter_after_success(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    client = PingClient(
        [
            RuntimeError("boom"),
            {"ok": True},
            RuntimeError("boom"),
            FakeSocketNotConnectedError(),
        ]
    )

    ping_loop = adapter._build_failfast_interactive_ping(
        client,
        ping_interval=0,
        failure_limit=2,
        ping_opcode=object(),
        disconnect_error=FakeSocketNotConnectedError,
    )

    await ping_loop()

    assert client.send_calls == 4
    assert client.close_calls == 0


def test_classify_runtime_error_marks_corrupt_session_as_reauth(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    issue = adapter._classify_runtime_error(RuntimeError("sqlite3.OperationalError: unsupported file format"))

    assert issue is not None
    assert issue.kind == "session_corrupt"
    assert issue.requires_reauth is True


def test_classify_runtime_error_marks_malformed_session_as_reauth(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    issue = adapter._classify_runtime_error(RuntimeError("sqlite3.DatabaseError: database disk image is malformed"))

    assert issue is not None
    assert issue.kind == "session_corrupt"
    assert issue.requires_reauth is True


@pytest.mark.asyncio
async def test_emit_runtime_issue_notifies_only_once_per_signature(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    seen = []

    async def handler(issue):
        seen.append((issue.kind, issue.summary))

    adapter.on_issue(handler)
    issue = adapter._remember_runtime_issue(
        adapter._classify_runtime_error(RuntimeError("Invalid token"))  # type: ignore[arg-type]
    )

    await adapter._emit_runtime_issue(issue)
    await adapter._emit_runtime_issue(issue)

    assert seen == [("session_invalid", "MAX сессия недействительна, нужна повторная авторизация")]


class VideoPlayClient(LookupClient):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.last_request = None

    async def _send_and_wait(self, **kwargs):
        self.last_request = kwargs
        return {"payload": self.payload}


def test_extract_video_url_prefers_stream_over_thumbnail(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    payload = {
        "EXTERNAL": False,
        "cache": True,
        "preview": {
            "thumbnail": "https://cdn.example.com/thumb.jpg",
        },
        "streams": {
            "360": "https://cdn.example.com/clip-360.mp4",
            "720": "https://cdn.example.com/clip-720.mp4",
        },
    }

    assert adapter._extract_video_url(payload) == "https://cdn.example.com/clip-360.mp4"


def test_extract_video_url_prefers_mp4_variant_over_external_page(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    payload = {
        "cache": True,
        "EXTERNAL": "https://m.ok.ru/video/13208513634267",
        "MP4_720": "https://maxvd677.okcdn.ru/?expires=1&srcIp=203.0.113.217&type=3&id=13644091493083",
    }

    assert adapter._extract_video_url(payload) == "https://maxvd677.okcdn.ru/?expires=1&srcIp=203.0.113.217&type=3&id=13644091493083"


def test_download_headers_for_url_uses_chrome_user_agent_for_chrome_signed_url(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd677.okcdn.ru/?expires=1&srcAg=CHROME&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_android_chrome_user_agent(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd217.okcdn.ru/?expires=1&srcAg=CHROME_ANDROID&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_ANDROID_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_ios_chrome_user_agent(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd587.okcdn.ru/?expires=1&srcAg=CHROME_IPHONE&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_IOS_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_mobile_safari_for_non_chrome_signed_url(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd204.okcdn.ru/?expires=1&srcAg=SAFARI_IPHONE_OTHER&id=13636639132379"
    )

    assert headers == {"User-Agent": MAX_CDN_USER_AGENT}


@pytest.mark.asyncio
async def test_download_video_by_id_uses_raw_video_play_payload(tmp_path):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = VideoPlayClient(
        {
            "EXTERNAL": False,
            "cache": True,
            "preview": {
                "thumbnail": "https://cdn.example.com/thumb.jpg",
            },
            "url": {
                "source": "https://cdn.example.com/video.mp4",
            },
        }
    )

    captured = {}

    async def fake_download(
        url: str,
        prefix: str,
        filename_hint=None,
        default_extension="",
        expected_kind=None,
        flow_id=None,
        download_source=None,
    ):
        captured["url"] = url
        captured["prefix"] = prefix
        captured["filename_hint"] = filename_hint
        captured["default_extension"] = default_extension
        captured["expected_kind"] = expected_kind
        captured["download_source"] = download_source
        return ("/tmp/video.mp4", "video.mp4")

    adapter._download_from_url = fake_download

    local_path, filename = await adapter._download_video_by_id(
        "123456789",
        "987654321",
        555,
        "video_123456789_987654321",
        "clip.mp4",
    )

    assert (local_path, filename) == ("/tmp/video.mp4", "video.mp4")
    assert captured == {
        "url": "https://cdn.example.com/video.mp4",
        "prefix": "video_123456789_987654321",
        "filename_hint": "clip.mp4",
        "default_extension": ".mp4",
        "expected_kind": "video",
        "download_source": "video_play",
    }


@pytest.mark.asyncio
async def test_download_from_url_uses_mobile_safari_user_agent(tmp_path, monkeypatch):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    captured = {}

    class FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "video/mp4"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def read(self):
            return b"video-bytes"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr("src.adapters.max_adapter.ClientSession", FakeSession)

    local_path, filename = await adapter._download_from_url(
        "https://cdn.example.com/video.mp4",
        "video_test",
        "clip.mp4",
        ".mp4",
    )

    assert filename == "clip.mp4"
    assert local_path is not None
    assert captured == {
        "headers": {"User-Agent": MAX_CDN_USER_AGENT},
        "url": "https://cdn.example.com/video.mp4",
    }


@pytest.mark.asyncio
async def test_download_from_url_logs_src_ag_and_sanitized_http_error(tmp_path, monkeypatch, caplog):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    signed_url = (
        "https://maxvd587.okcdn.ru/?expires=1778779666&srcAg=CHROME_IPHONE"
        "&sig=secret&id=13644091493083"
    )

    class FakeResponse:
        status = 400
        headers = {"Content-Type": "text/plain"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            raise ClientResponseError(None, (), status=400, message="Bad Request")

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, _url):
            return FakeResponse()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("src.adapters.max_adapter.ClientSession", FakeSession)
    monkeypatch.setattr("src.adapters.max_adapter.asyncio.sleep", no_sleep)

    with caplog.at_level(logging.WARNING, logger="src.adapters.max_adapter"):
        local_path, filename = await adapter._download_from_url(
            signed_url,
            "video_test",
            "clip.mp4",
            ".mp4",
            expected_kind="video",
            download_source="video_play",
        )

    assert local_path is None
    assert filename is None
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    final_event = next(
        event
        for event in events
        if event.get("event") == "max.attachment.download" and event.get("outcome") == "failed"
    )
    assert final_event["src_ag"] == "CHROME_IPHONE"
    assert final_event["ua_family"] == "chrome_ios"
    assert final_event["http_status"] == 400
    assert final_event["download_source"] == "video_play"
    assert final_event["error"] == "HTTP 400 Bad Request"
    assert "sig=secret" not in final_event["error"]


@pytest.mark.asyncio
async def test_download_from_url_resumes_partial_file_after_connection_break(tmp_path, monkeypatch):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    captured_headers = []

    class BrokenStream:
        async def iter_chunked(self, _size):
            yield b"video-"
            raise ConnectionResetError("socket closed")

    class GoodStream:
        async def iter_chunked(self, _size):
            yield b"bytes"

    class FakeResponse:
        def __init__(self, status, content):
            self.status = status
            self.content = content
            self.headers = {"Content-Type": "video/mp4"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

    class FakeSession:
        calls = 0

        def __init__(self, *args, **kwargs):
            captured_headers.append(kwargs.get("headers"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, _url):
            FakeSession.calls += 1
            if FakeSession.calls == 1:
                return FakeResponse(200, BrokenStream())
            return FakeResponse(206, GoodStream())

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("src.adapters.max_adapter.ClientSession", FakeSession)
    monkeypatch.setattr("src.adapters.max_adapter.asyncio.sleep", no_sleep)

    local_path, filename = await adapter._download_from_url(
        "https://cdn.example.com/video.mp4",
        "video_test",
        "clip.mp4",
        ".mp4",
        expected_kind="video",
    )

    assert filename == "clip.mp4"
    assert local_path is not None
    assert (tmp_path / "tmp" / "clip.mp4").read_bytes() == b"video-bytes"
    assert captured_headers[1]["Range"] == "bytes=6-"


@pytest.mark.asyncio
async def test_download_from_url_rejects_html_for_expected_video(tmp_path, monkeypatch):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    class FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def read(self):
            return b"<!doctype html><html><body>player</body></html>"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("src.adapters.max_adapter.ClientSession", FakeSession)

    local_path, filename = await adapter._download_from_url(
        "https://m.ok.ru/video/13208513634267",
        "video_test",
        "clip.mp4",
        ".mp4",
        expected_kind="video",
    )

    assert local_path is None
    assert filename is None


@pytest.mark.asyncio
async def test_download_from_url_allows_text_for_expected_document(tmp_path, monkeypatch):
    adapter = MaxAdapter(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    class FakeResponse:
        def __init__(self):
            self.headers = {"Content-Type": "text/plain; charset=utf-8"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def read(self):
            return b"plain text file"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("src.adapters.max_adapter.ClientSession", FakeSession)

    local_path, filename = await adapter._download_from_url(
        "https://cdn.example.com/file.txt",
        "doc_test",
        "file.txt",
        ".txt",
        expected_kind="document",
    )

    assert local_path is not None
    assert filename == "file.txt"
