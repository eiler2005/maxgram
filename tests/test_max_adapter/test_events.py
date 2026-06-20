from .conftest import *  # noqa: F403
from src.adapters.max import constants as max_constants


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
async def test_handle_raw_message_extracts_max_join_action_from_share(tmp_path):
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
        text=None,
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="SHARE", url="https://max.ru/join/abc123")],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == []
    assert [(action.kind, action.label, action.url) for action in received[0].actions] == [
        ("max_join", "Вступить в MAX", "https://max.ru/join/abc123")
    ]


@pytest.mark.asyncio
async def test_handle_raw_message_extracts_external_action_from_inline_keyboard(tmp_path):
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
        text="Оплата",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="INLINE_KEYBOARD",
                buttons=[
                    {
                        "text": "Открыть квитанцию",
                        "web_app": {"url": "https://pay.example.test/invoice/1"},
                    }
                ],
            )
        ],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == []
    assert [(action.kind, action.label, action.url) for action in received[0].actions] == [
        ("open_url", "Открыть квитанцию", "https://pay.example.test/invoice/1")
    ]


@pytest.mark.asyncio
async def test_handle_raw_message_extracts_msgpack_text_url_and_deduplicates(tmp_path):
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

    url = "https://example.test/page"
    message = SimpleNamespace(
        id=1,
        chat_id=123,
        sender=7001,
        text=msgpack.packb(
            {"text": f"Подробнее: {url}", "buttons": [{"text": "Сайт", "url": url}]},
            use_bin_type=True,
        ),
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="SHARE", url=url)],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].text == f"Подробнее: {url}"
    assert received[0].rendered_texts == []
    assert [(action.kind, action.url) for action in received[0].actions] == [
        ("open_url", url)
    ]


@pytest.mark.asyncio
async def test_handle_raw_message_ignores_unsafe_share_url(tmp_path):
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
        text=None,
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="SHARE", url="javascript:alert(1)")],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].actions == []
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
async def test_handle_raw_message_falls_back_from_zero_forward_chat_for_media(tmp_path):
    adapter = CapturingDownloadAdapter(
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

    forwarded_message = SimpleNamespace(
        id=908,
        chat_id=None,
        sender=None,
        text="",
        type="VIDEO",
        status=None,
        attaches=[SimpleNamespace(type="VIDEO", video_id=555)],
        link=None,
    )
    message = SimpleNamespace(
        id=108,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="FORWARD",
        status=None,
        attaches=[],
        link=SimpleNamespace(
            type="FORWARD",
            chat_id=0,
            message=forwarded_message,
        ),
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].msg_id == "108"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].attachment_types == ["VIDEO"]
    assert adapter.download_calls == [("-70000000000003", "908", "VIDEO", 0)]


@pytest.mark.asyncio
async def test_handle_raw_message_recovers_degraded_channel_media_before_partial(tmp_path):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    async def get_message(*, chat_id: int, message_id: int):
        return SimpleNamespace(
            id=message_id,
            chat_id=chat_id,
            sender=7001,
            text="Пост с медиа",
            type="CHANNEL",
            status=None,
            attaches=[
                SimpleNamespace(type="VIDEO", video_id=555, duration=32000),
                SimpleNamespace(type="PHOTO", base_url="https://cdn.example.test/photo.jpg"),
            ],
            link=None,
        )

    client._get_message = get_message
    adapter._client = client
    received = []
    download_calls = []

    async def download_attachment(chat_id: str, msg_id: str, attach, index: int = 0, flow_id=None):
        raw_type = adapter._attachment_type_name(attach)
        download_calls.append((chat_id, msg_id, raw_type, index))
        if raw_type == "VIDEO" and getattr(attach, "video_id", None):
            return MaxAttachment(
                kind="video",
                local_path="/tmp/recovered-video.mp4",
                filename="recovered-video.mp4",
                duration=32,
                width=1280,
                height=720,
                source_type=raw_type,
            )
        if raw_type == "PHOTO" and getattr(attach, "base_url", None):
            return MaxAttachment(
                kind="photo",
                local_path="/tmp/recovered-photo.jpg",
                filename="recovered-photo.jpg",
                duration=None,
                width=1280,
                height=720,
                source_type=raw_type,
            )
        return None

    adapter._adapter._media._download_attachment = download_attachment

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    degraded = SimpleNamespace(
        id=109,
        chat_id=-70000000000003,
        sender=7001,
        text="Пост с медиа",
        type="CHANNEL",
        status=None,
        attaches=[SimpleNamespace(type="VIDEO"), SimpleNamespace(type="PHOTO")],
        link=None,
    )

    await adapter._handle_raw_message(degraded)

    assert len(received) == 1
    assert received[0].attachment_failures == []
    assert [attachment.kind for attachment in received[0].attachments] == ["video", "photo"]
    assert download_calls == [
        ("-70000000000003", "109", "VIDEO", 0),
        ("-70000000000003", "109", "PHOTO", 1),
        ("-70000000000003", "109", "VIDEO", 0),
        ("-70000000000003", "109", "PHOTO", 1),
    ]


@pytest.mark.asyncio
async def test_degraded_channel_photo_low_quality_recovery_waits_before_partial(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS", 0.01)
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    async def get_message(*, chat_id: int, message_id: int):
        return SimpleNamespace(
            id=message_id,
            chat_id=chat_id,
            sender=7001,
            text="Пост с фото",
            type="CHANNEL",
            status=None,
            attaches=[SimpleNamespace(type="PHOTO") for _ in range(7)],
            link=None,
        )

    client._get_message = get_message
    adapter._client = client
    adapter._adapter._media._download_attachment = (
        lambda *args, **kwargs: asyncio.sleep(0, result=None)
    )
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    degraded = SimpleNamespace(
        id=110,
        chat_id=-70000000000003,
        sender=7001,
        text="Пост с фото",
        type="CHANNEL",
        status=None,
        attaches=[SimpleNamespace(type="PHOTO") for _ in range(7)],
        link=None,
    )

    task = asyncio.create_task(adapter._handle_raw_message(degraded))
    await asyncio.sleep(0)
    assert received == []

    await task

    assert len(received) == 1
    assert received[0].attachments == []
    assert len(received[0].attachment_failures) == 7


@pytest.mark.asyncio
async def test_degraded_channel_photo_recovers_from_raw_cache_before_partial(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS", 0.01)
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    async def get_message(*, chat_id: int, message_id: int):
        return SimpleNamespace(
            id=message_id,
            chat_id=chat_id,
            sender=7001,
            text="Пост с фото",
            type="CHANNEL",
            status=None,
            attaches=[SimpleNamespace(type="PHOTO") for _ in range(2)],
            link=None,
        )

    client._get_message = get_message
    adapter._client = client

    async def download_attachment(chat_id: str, msg_id: str, attach, index: int = 0, flow_id=None):
        if getattr(attach, "url", None):
            return MaxAttachment(
                kind="photo",
                local_path=f"/tmp/recovered-photo-{index}.jpg",
                filename=f"recovered-photo-{index}.jpg",
                duration=None,
                width=1280,
                height=720,
                source_type="PHOTO",
            )
        return None

    adapter._adapter._media._download_attachment = download_attachment
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    degraded = SimpleNamespace(
        id=111,
        chat_id=-70000000000003,
        sender=7001,
        text="Пост с фото",
        type="CHANNEL",
        status=None,
        attaches=[SimpleNamespace(type="PHOTO") for _ in range(2)],
        link=None,
    )

    task = asyncio.create_task(adapter._handle_raw_message(degraded))
    await asyncio.sleep(0.02)
    adapter._adapter._raw_payload._cache_raw_history_payload(
        {
            "chatId": -70000000000003,
            "messages": [
                {
                    "id": 111,
                    "time": 1,
                    "sender": 7001,
                    "text": "Пост с фото",
                    "type": "CHANNEL",
                    "attaches": [
                        {"_type": "PHOTO", "url": "https://cdn.example.test/photo-1.jpg"},
                        {"_type": "PHOTO", "url": "https://cdn.example.test/photo-2.jpg"},
                    ],
                }
            ],
        }
    )
    await task

    assert len(received) == 1
    assert received[0].attachment_failures == []
    assert [attachment.kind for attachment in received[0].attachments] == ["photo", "photo"]


@pytest.mark.asyncio
async def test_degraded_channel_prod_like_seven_photos_waits_for_raw_refs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS", 0.01)
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    chat_id = -70000000012345
    msg_id = 900000000000001
    client = LookupClient(chats=[SimpleNamespace(id=chat_id, title="Тестовая группа")])

    async def get_message(*, chat_id: int, message_id: int):
        return SimpleNamespace(
            id=message_id,
            chat_id=chat_id,
            sender=7001,
            text="",
            type="CHANNEL",
            status=None,
            attaches=[SimpleNamespace(type="PHOTO") for _ in range(7)],
            link=None,
        )

    client._get_message = get_message
    adapter._client = client
    download_calls = []

    async def download_attachment(chat_id: str, msg_id: str, attach, index: int = 0, flow_id=None):
        download_calls.append((chat_id, msg_id, adapter._attachment_type_name(attach), index))
        if getattr(attach, "base_url", None) or getattr(attach, "url", None):
            return MaxAttachment(
                kind="photo",
                local_path=f"/tmp/prod-like-photo-{index}.jpg",
                filename=f"prod-like-photo-{index}.jpg",
                duration=None,
                width=1280,
                height=720,
                source_type="PHOTO",
            )
        return None

    adapter._adapter._media._download_attachment = download_attachment
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    degraded = SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender=7001,
        text="",
        type="CHANNEL",
        status=None,
        attaches=[SimpleNamespace(type="PHOTO") for _ in range(7)],
        link=None,
    )

    task = asyncio.create_task(adapter._handle_raw_message(degraded))
    await asyncio.sleep(0.02)
    assert received == []
    adapter._adapter._raw_payload._cache_raw_history_payload(
        {
            "chatId": chat_id,
            "messages": [
                {
                    "id": msg_id,
                    "time": 1,
                    "sender": 7001,
                    "text": "",
                    "type": "CHANNEL",
                    "attaches": [
                        {"_type": "PHOTO", "baseUrl": f"https://cdn.example.test/photo-{i}.jpg"}
                        for i in range(7)
                    ],
                }
            ],
        }
    )
    await task

    assert len(received) == 1
    assert received[0].msg_id == str(msg_id)
    assert received[0].chat_id == str(chat_id)
    assert received[0].attachment_failures == []
    assert [attachment.kind for attachment in received[0].attachments] == ["photo"] * 7
    assert download_calls[:7] == [
        (str(chat_id), str(msg_id), "PHOTO", index)
        for index in range(7)
    ]
    assert download_calls[7:] == [
        (str(chat_id), str(msg_id), "PHOTO", index)
        for index in range(7)
    ]


@pytest.mark.asyncio
async def test_degraded_channel_photo_video_recovers_from_raw_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(max_constants, "MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS", 0.01)
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    async def get_message(*, chat_id: int, message_id: int):
        return SimpleNamespace(
            id=message_id,
            chat_id=chat_id,
            sender=7001,
            text="Пост с медиа",
            type="CHANNEL",
            status=None,
            attaches=[SimpleNamespace(type="PHOTO"), SimpleNamespace(type="VIDEO")],
            link=None,
        )

    client._get_message = get_message
    adapter._client = client

    async def download_attachment(chat_id: str, msg_id: str, attach, index: int = 0, flow_id=None):
        raw_type = adapter._attachment_type_name(attach)
        if raw_type == "PHOTO" and getattr(attach, "url", None):
            return MaxAttachment("photo", "/tmp/recovered-photo.jpg", None, None, 640, 480, "PHOTO")
        if raw_type == "VIDEO" and getattr(attach, "video_id", None):
            return MaxAttachment("video", "/tmp/recovered-video.mp4", "v.mp4", 5, 640, 480, "VIDEO")
        return None

    adapter._adapter._media._download_attachment = download_attachment
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    degraded = SimpleNamespace(
        id=112,
        chat_id=-70000000000003,
        sender=7001,
        text="Пост с медиа",
        type="CHANNEL",
        status=None,
        attaches=[SimpleNamespace(type="PHOTO"), SimpleNamespace(type="VIDEO")],
        link=None,
    )

    task = asyncio.create_task(adapter._handle_raw_message(degraded))
    await asyncio.sleep(0.02)
    adapter._adapter._raw_payload._cache_raw_history_payload(
        {
            "chatId": -70000000000003,
            "messages": [
                {
                    "id": 112,
                    "time": 1,
                    "sender": 7001,
                    "text": "Пост с медиа",
                    "type": "CHANNEL",
                    "attaches": [
                        {"_type": "PHOTO", "url": "https://cdn.example.test/photo.jpg"},
                        {"_type": "VIDEO", "videoId": 555, "url": "https://cdn.example.test/video.mp4"},
                    ],
                }
            ],
        }
    )
    await task

    assert len(received) == 1
    assert received[0].attachment_failures == []
    assert [attachment.kind for attachment in received[0].attachments] == ["photo", "video"]


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
async def test_handle_raw_receive_forwards_channel_wrapper_with_direct_attachments(tmp_path):
    adapter = CapturingDownloadAdapter(
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
                "id": 105,
                "time": 1,
                "sender": 7001,
                "text": "Пересланный пост с фото",
                "type": "CHANNEL",
                "attaches": [
                    {"_type": "PHOTO", "url": "https://cdn.example.test/photo.jpg"}
                ],
            },
        },
    }

    await adapter._handle_raw_receive(raw_event)

    assert len(received) == 1
    assert received[0].msg_id == "105"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].text == "Пересланный пост с фото"
    assert received[0].message_type == "CHANNEL"
    assert received[0].attachment_types == ["PHOTO"]
    assert adapter.download_calls == [("-70000000000003", "105", "PHOTO", 0)]


@pytest.mark.asyncio
async def test_empty_recovery_unwraps_forwarded_history_candidate_before_content_check(tmp_path):
    adapter = CapturingDownloadAdapter(
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

    candidate = {
        "id": 106,
        "time": 1,
        "sender": 7001,
        "text": "",
        "type": "CHANNEL",
        "attaches": [],
        "message": {
            "id": 906,
            "time": 1,
            "sender": None,
            "text": "Вложенный пересланный пост",
            "type": "TEXT",
            "attaches": [
                {"_type": "PHOTO", "url": "https://cdn.example.test/photo.jpg"}
            ],
        },
    }

    recovered = adapter._adapter._raw_payload._prepare_empty_recovery_candidate(
        candidate,
        chat_id="-70000000000003",
        chat_id_int=-70000000000003,
        raw_msg_id_str="106",
        flow_id="mx:-70000000000003:106",
        reason="raw_recent_history_match",
    )

    assert recovered is not None

    await adapter._handle_raw_message(recovered)

    assert len(received) == 1
    assert received[0].msg_id == "106"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].text == "Вложенный пересланный пост"
    assert received[0].attachment_types == ["PHOTO"]
    assert adapter.download_calls == [("-70000000000003", "906", "PHOTO", 0)]


@pytest.mark.asyncio
async def test_empty_recovery_unwraps_forward_link_candidate_before_content_check(tmp_path):
    adapter = CapturingDownloadAdapter(
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

    candidate = {
        "id": 107,
        "time": 1,
        "sender": 7001,
        "text": "",
        "type": "FORWARD",
        "attaches": [],
        "link": {
            "type": "FORWARD",
            "chatId": -80000000000001,
            "message": {
                "id": 907,
                "time": 1,
                "sender": None,
                "text": "Связанный пересланный пост",
                "type": "TEXT",
                "attaches": [
                    {"_type": "PHOTO", "url": "https://cdn.example.test/photo.jpg"}
                ],
            },
        },
    }

    recovered = adapter._adapter._raw_payload._prepare_empty_recovery_candidate(
        candidate,
        chat_id="-70000000000003",
        chat_id_int=-70000000000003,
        raw_msg_id_str="107",
        flow_id="mx:-70000000000003:107",
        reason="raw_recent_history_match",
    )

    assert recovered is not None

    await adapter._handle_raw_message(recovered)

    assert len(received) == 1
    assert received[0].msg_id == "107"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].text == "Связанный пересланный пост"
    assert received[0].attachment_types == ["PHOTO"]
    assert adapter.download_calls == [("-80000000000001", "907", "PHOTO", 0)]


@pytest.mark.asyncio
async def test_empty_recovery_falls_back_from_zero_forward_chat_for_media(tmp_path):
    adapter = CapturingDownloadAdapter(
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

    candidate = {
        "id": 108,
        "time": 1,
        "sender": 7001,
        "text": "",
        "type": "FORWARD",
        "attaches": [],
        "link": {
            "type": "FORWARD",
            "chatId": 0,
            "message": {
                "id": 908,
                "time": 1,
                "sender": None,
                "text": "",
                "type": "VIDEO",
                "attaches": [
                    {"_type": "VIDEO", "videoId": 555, "url": "https://cdn.example.test/video.mp4"}
                ],
            },
        },
    }

    recovered = adapter._adapter._raw_payload._prepare_empty_recovery_candidate(
        candidate,
        chat_id="-70000000000003",
        chat_id_int=-70000000000003,
        raw_msg_id_str="108",
        flow_id="mx:-70000000000003:108",
        reason="raw_recent_history_match",
    )

    assert recovered is not None

    await adapter._handle_raw_message(recovered)

    assert len(received) == 1
    assert received[0].msg_id == "108"
    assert received[0].chat_id == "-70000000000003"
    assert received[0].attachment_types == ["VIDEO"]
    assert adapter.download_calls == [("-70000000000003", "908", "VIDEO", 0)]


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
async def test_handle_raw_message_skips_channel_metadata_reaction_marker(tmp_path, caplog):
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
        id=104,
        chat_id=-70000000000003,
        sender=7001,
        text="",
        type="CHANNEL",
        status=None,
        attaches=[],
        link=None,
        cid=1779268162669013,
        elements=[],
        mark=42,
        options={},
        prev_message_id=103,
        reactionInfo=None,
        reaction_info=None,
        stats={},
        ttl=None,
        unread=0,
    )

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(message)

    assert received == []
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    skipped = next(
        event
        for event in events
        if event.get("event") == "max.inbound.skipped"
        and event.get("reason") == "channel_metadata_only_event"
    )
    assert skipped["message_type"] == "CHANNEL"
    assert "reactionInfo" in skipped["metadata_fields"]
    assert "mark" in skipped["metadata_fields"]


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
async def test_handle_raw_message_renders_control_remove_with_target_user_id(tmp_path):
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
        id=4,
        chat_id=-70000000000003,
        sender=40053201,
        text="",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="remove", extra={"targetUser": {"userId": 7001}})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Удалены участники: Тестовый Пользователь"]


@pytest.mark.asyncio
async def test_handle_raw_message_renders_control_add_with_nested_embedded_name(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=5,
        chat_id=-70000000000003,
        sender=40053201,
        text="",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="CONTROL",
                event="add",
                extra={
                    "member": {
                        "accountId": 7002,
                        "firstName": "Новый",
                        "lastName": "Участник",
                    }
                },
            )
        ],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].rendered_texts == ["Добавлены участники: Новый Участник"]


@pytest.mark.asyncio
async def test_handle_raw_message_delivers_text_when_control_rendering_fails(tmp_path):
    class FailingResolver:
        async def resolve_user_name(self, _user_id):
            raise RuntimeError("lookup failed")

    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient(chats=[SimpleNamespace(id=-70000000000003, title="Тестовая группа")])
    adapter._adapter._events._deps.resolver = FailingResolver()

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    message = SimpleNamespace(
        id=6,
        chat_id=-70000000000003,
        sender=7003,
        text="Основной текст",
        type="USER",
        status=None,
        attaches=[SimpleNamespace(type="CONTROL", event="add", extra={"userIds": [7001]})],
        link=None,
    )

    await adapter._handle_raw_message(message)

    assert len(received) == 1
    assert received[0].text == "Основной текст"
    assert received[0].rendered_texts == ["В чат добавлен участник"]


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


@pytest.mark.asyncio
async def test_handle_raw_message_tolerates_pymax_220_delete_event_shape(tmp_path):
    """PyMax 2.2.0 breaking change: MessageDeleteEvent carries message_ids list + guaranteed
    chat_id at top level; .chat and .message are optional and may be absent.
    The bridge must not crash and must not trigger spurious voice-recovery sweeps."""
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._client = LookupClient()

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    delete_event = SimpleNamespace(
        chat_id=-70000000000099,
        message_ids=[1001, 1002],
        # .id, .chat, .message, .status intentionally absent — 2.2.0 shape
    )

    # Must not raise; must not trigger recovery (no msg id to recover)
    await adapter._handle_raw_message(delete_event)

    # Delete event is silently dropped — no text, no attachments, no status=REMOVED
    assert received == []


@pytest.mark.asyncio
async def test_handle_typing_dispatches_to_typing_handlers(tmp_path):
    """TypingEvent from MAX is dispatched to registered typing_handlers."""
    from src.adapters.max.deps import EventsDeps
    from src.adapters.max.events import MaxEventsService
    from src.adapters.max.state import ConnectionState, OutboundState

    received = []

    async def typing_handler(event):
        received.append(event)

    deps = EventsDeps(
        connection=ConnectionState(),
        outbound=OutboundState(),
        handlers=[],
        backend=None,
        raw_payload=None,
        media=None,
        resolver=None,
        runtime=None,
        typing_handlers=[typing_handler],
    )
    svc = MaxEventsService(deps)

    event = SimpleNamespace(chat_id=-999, user_id=42)
    await svc._handle_typing(event)

    assert len(received) == 1
    assert received[0].chat_id == "-999"
    assert received[0].user_id == "42"


@pytest.mark.asyncio
async def test_handle_typing_no_op_when_no_chat_id(tmp_path):
    """TypingEvent without chat_id is silently dropped."""
    from src.adapters.max.deps import EventsDeps
    from src.adapters.max.events import MaxEventsService
    from src.adapters.max.state import ConnectionState, OutboundState

    received = []

    async def typing_handler(event):
        received.append(event)

    deps = EventsDeps(
        connection=ConnectionState(),
        outbound=OutboundState(),
        handlers=[],
        backend=None,
        raw_payload=None,
        media=None,
        resolver=None,
        runtime=None,
        typing_handlers=[typing_handler],
    )
    svc = MaxEventsService(deps)

    await svc._handle_typing(SimpleNamespace())
    assert received == []


@pytest.mark.asyncio
async def test_handle_reaction_update_dispatches_counters(tmp_path):
    """ReactionUpdateEvent is normalized and dispatched to reaction_update_handlers."""
    from src.adapters.max.deps import EventsDeps
    from src.adapters.max.events import MaxEventsService
    from src.adapters.max.state import ConnectionState, OutboundState

    received = []

    async def reaction_handler(event):
        received.append(event)

    deps = EventsDeps(
        connection=ConnectionState(),
        outbound=OutboundState(),
        handlers=[],
        backend=None,
        raw_payload=None,
        media=None,
        resolver=None,
        runtime=None,
        reaction_update_handlers=[reaction_handler],
    )
    svc = MaxEventsService(deps)

    counter = SimpleNamespace(emoji="👍", count=3)
    event = SimpleNamespace(chat_id=-100, message_id="msg42", total_count=3, counters=[counter])
    await svc._handle_reaction_update(event)

    assert len(received) == 1
    assert received[0].chat_id == "-100"
    assert received[0].message_id == "msg42"
    assert received[0].total_count == 3
    assert received[0].counters == [{"emoji": "👍", "count": 3}]


@pytest.mark.asyncio
async def test_handle_reaction_update_dispatches_actor_and_pymax_reaction_field(tmp_path):
    """PyMax counters use .reaction, and event may include the reacting user id."""
    from src.adapters.max.deps import EventsDeps
    from src.adapters.max.events import MaxEventsService
    from src.adapters.max.state import ConnectionState, OutboundState

    class Resolver:
        async def resolve_user_name(self, user_id):
            return "Марина Ермилова" if user_id == "7001" else None

    received = []

    async def reaction_handler(event):
        received.append(event)

    deps = EventsDeps(
        connection=ConnectionState(),
        outbound=OutboundState(),
        handlers=[],
        backend=None,
        raw_payload=None,
        media=None,
        resolver=Resolver(),
        runtime=None,
        reaction_update_handlers=[reaction_handler],
    )
    svc = MaxEventsService(deps)

    counter = SimpleNamespace(reaction="👍", count=3)
    event = SimpleNamespace(
        chat_id=-100,
        message_id="msg42",
        total_count=3,
        counters=[counter],
        userId=7001,
        reaction="👍",
    )
    await svc._handle_reaction_update(event)

    assert len(received) == 1
    assert received[0].counters == [{"emoji": "👍", "count": 3}]
    assert received[0].actor_user_id == "7001"
    assert received[0].actor_name == "Марина Ермилова"
    assert received[0].reaction == "👍"


@pytest.mark.asyncio
async def test_handle_presence_and_read_do_not_raise(tmp_path):
    """MessageReadEvent and PresenceEvent are consumed silently (diagnostics only)."""
    from src.adapters.max.deps import EventsDeps
    from src.adapters.max.events import MaxEventsService
    from src.adapters.max.state import ConnectionState, OutboundState

    deps = EventsDeps(
        connection=ConnectionState(),
        outbound=OutboundState(),
        handlers=[],
        backend=None,
        raw_payload=None,
        media=None,
        resolver=None,
        runtime=None,
    )
    svc = MaxEventsService(deps)

    await svc._handle_message_read(SimpleNamespace(chat_id=-5, user_id=7, mark=1001))
    await svc._handle_presence(SimpleNamespace(user_id=7, presence=SimpleNamespace(online=True)))
