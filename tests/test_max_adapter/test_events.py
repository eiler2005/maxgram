from .conftest import *  # noqa: F403


@pytest.mark.asyncio
async def test_handle_raw_message_renders_control_leave(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
async def test_handle_raw_message_decodes_bytes_text_before_preview(tmp_path):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=1,
        chat_id=123,
        sender=7001,
        text="Привет".encode(),
        type="TEXT",
        status=None,
        attaches=[],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].text == "Привет"


@pytest.mark.asyncio
async def test_handle_raw_message_extracts_text_from_msgpack_bytes(tmp_path):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=1,
        chat_id=123,
        sender=7001,
        text=msgpack.packb(
            {
                "text": "Самостоятельно можно записаться по ссылке",
                "attaches": [{"_type": "SHARE", "shareId": "redacted"}],
            },
            use_bin_type=True,
        ),
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="SHARE")],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].text == "Самостоятельно можно записаться по ссылке"
    assert "\ufffd" not in received[0].text
    assert received[0].rendered_texts == ["[Вложение MAX: share]"]


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
    adapter = AdapterHarness(
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
async def test_handle_raw_receive_skips_top_level_message_with_only_cid(
    tmp_path,
    caplog,
):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    raw_event = {
        "opcode": 128,
        "payload": {
            "cid": 1779268162669013,
            "id": 116606118527662695,
            "time": 1,
            "sender": 7001,
            "text": "secret text",
            "type": "USER",
            "attaches": [],
            "token": "secret-token",
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_event)

    assert received == []
    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event") == "max.raw.message_skipped"
    )
    fields = record.event_fields
    assert fields["reason"] == "missing_chat_id"
    assert "max_chat_id" not in fields
    assert fields["max_msg_id"] == "116606118527662695"
    assert fields["message_type"] == "USER"
    assert "cid" in fields["message_fields"]
    assert "text" not in fields["message_fields"]
    assert "token" not in fields["message_fields"]
    assert "1779268162669013" not in str(fields)
    assert "secret text" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_handle_raw_receive_prefers_real_chat_id_over_cid(tmp_path):
    adapter = AdapterHarness(
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
            "cid": 1779268162669013,
            "id": 116606118527662696,
            "time": 1,
            "sender": 7001,
            "text": "ok",
            "type": "USER",
            "attaches": [],
        },
    }

    await adapter._handle_raw_receive(raw_event)

    assert len(received) == 1
    assert received[0].chat_id == "-70000000000003"
    assert received[0].msg_id == "116606118527662696"
    assert received[0].text == "ok"


@pytest.mark.asyncio
async def test_handle_raw_receive_logs_top_level_empty_message_diagnostic(tmp_path, caplog):
    adapter = AdapterHarness(
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
async def test_handle_raw_receive_logs_safe_empty_message_diagnostic(tmp_path, caplog):
    adapter = AdapterHarness(
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
async def test_handle_raw_receive_logs_safe_auxiliary_attachment_event(tmp_path, caplog):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    raw_event = {
        "opcode": 136,
        "payload": {
            "chatId": 28093080,
            "messageId": 110,
            "attach": {
                "type": "AUDIO",
                "audioId": 42,
                "url": "https://audio.example.test/secret.ogg",
                "token": "secret-token",
                "text": "secret text",
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_event)

    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event") == "max.raw.auxiliary_event"
    )
    fields = record.event_fields
    assert fields["opcode_name"] == "NOTIF_ATTACH"
    assert fields["max_chat_id"] == "28093080"
    assert fields["max_msg_id"] == "110"
    assert "attach.audioId" in fields["payload_shape"]
    assert "url" not in str(fields)
    assert "secret-token" not in str(fields)
    assert "secret text" not in str(fields)


@pytest.mark.asyncio
async def test_handle_raw_receive_logs_unknown_message_payload_shape_safely(tmp_path, caplog):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    raw_event = {
        "opcode": 128,
        "payload": {
            "chatId": 28093080,
            "event": {
                "kind": "voice",
                "url": "https://audio.example.test/secret.ogg",
                "token": "secret-token",
                "text": "secret text",
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_event)

    record = next(
        r
        for r in caplog.records
        if getattr(r, "event_fields", {}).get("event")
        == "max.raw.unhandled_message_payload"
    )
    fields = record.event_fields
    assert fields["max_chat_id"] == "28093080"
    assert "event.kind" in fields["payload_shape"]
    assert "url" not in str(fields)
    assert "secret-token" not in str(fields)
    assert "secret text" not in str(fields)


@pytest.mark.asyncio
async def test_handle_raw_message_renders_unknown_message_details(tmp_path):
    adapter = AdapterHarness(
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
async def test_handle_raw_message_skips_empty_reaction_only_event(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
