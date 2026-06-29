from .conftest import *  # noqa: F403


def write_minimal_mp4_with_duration(path, *, seconds: int, timescale: int = 1000):
    def box(box_type: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", len(payload) + 8, box_type) + payload

    mvhd_payload = (
        b"\x00\x00\x00\x00"
        + struct.pack(">II", 0, 0)
        + struct.pack(">II", timescale, seconds * timescale)
        + b"\x00" * 80
    )
    path.write_bytes(
        box(b"ftyp", b"isom\x00\x00\x02\x00isommp42")
        + box(b"moov", box(b"mvhd", mvhd_payload))
    )


@pytest.mark.asyncio
async def test_download_attachment_populates_media_part_metadata(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    media = adapter._adapter._media
    adapter.url_result = (str(tmp_path / "tmp" / "photo.jpg"), "photo.jpg")
    adapter.file_result = (str(tmp_path / "tmp" / "doc.pdf"), "doc.pdf")

    async def fake_download_video_by_id(*args, **kwargs):
        return str(tmp_path / "tmp" / "video.mp4"), "video.mp4"

    async def fake_download_audio_by_protocol(*args, **kwargs):
        return str(tmp_path / "tmp" / "voice.ogg"), "voice.ogg", False

    media._download_video_by_id = fake_download_video_by_id
    media._download_audio_by_protocol = fake_download_audio_by_protocol

    photo = await media._download_attachment(
        "chat-1",
        "msg-1",
        SimpleNamespace(type="PHOTO", base_url="https://cdn.example/photo.jpg"),
        index=0,
    )
    video = await media._download_attachment(
        "chat-1",
        "msg-1",
        SimpleNamespace(type="VIDEO", video_id=555),
        index=1,
    )
    audio = await media._download_attachment(
        "chat-1",
        "msg-1",
        SimpleNamespace(type="AUDIO", audio_id=92),
        index=2,
    )
    document = await media._download_attachment(
        "chat-1",
        "msg-1",
        SimpleNamespace(type="FILE", file_id=77, filename="doc.pdf"),
        index=3,
    )

    assert [(item.kind, item.attachment_index) for item in (photo, video, audio, document)] == [
        ("photo", 0),
        ("video", 1),
        ("audio", 2),
        ("document", 3),
    ]
    assert all(item.media_chat_id == "chat-1" for item in (photo, video, audio, document))
    assert all(item.media_msg_id == "msg-1" for item in (photo, video, audio, document))
    assert photo.reference_kind is None
    assert photo.reference_id is None
    assert media._attachment_reference(SimpleNamespace(type="PHOTO", id="opaque-photo-ref"), "PHOTO") == (
        None,
        None,
    )
    assert media._attachment_reference(SimpleNamespace(type="PHOTO", photo_id=11), "PHOTO") == (
        "file_id",
        "11",
    )
    assert (video.reference_kind, video.reference_id) == ("video_id", "555")
    assert (audio.reference_kind, audio.reference_id) == ("audio_id", "92")
    assert (document.reference_kind, document.reference_id) == ("file_id", "77")


@pytest.mark.asyncio
async def test_download_audio_reference_refreshes_raw_history_url(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class RawHistoryClient(LookupClient):
        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "id": 116605799957888782,
                            "sender": 7001,
                            "time": 1779263296000,
                            "type": "USER",
                            "attaches": [
                                {
                                    "_type": "UNSUPPORTED",
                                    "payload": {
                                        "audioId": 92,
                                        "url": "https://audio.example.test/retry.ogg",
                                        "duration": 9,
                                        "wave": "abc",
                                    },
                                }
                            ],
                        }
                    ]
                }
            }

    adapter._client = RawHistoryClient()

    attachment = await adapter.download_audio_reference(
        chat_id="200056208",
        msg_id="116605799957888782",
        reference_id="92",
        reference_kind="audio_id",
        duration=9,
        source_type="AUDIO",
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/retry.ogg",
            "audio_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_download_audio_reference_uses_dialog_last_message_url(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")
    adapter._client = LookupClient()
    adapter._client.dialogs = [
        SimpleNamespace(
            id=200056208,
            last_message=SimpleNamespace(
                id=116605799957888782,
                attaches=[
                    SimpleNamespace(
                        type="AUDIO",
                        audio_id=92,
                        url="https://audio.example.test/dialog.ogg",
                        duration=9,
                    )
                ],
            ),
        )
    ]

    attachment = await adapter.download_audio_reference(
        chat_id="200056208",
        msg_id="116605799957888782",
        reference_id="92",
        reference_kind="audio_id",
        duration=9,
        source_type="AUDIO",
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/dialog.ogg",
            "audio_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_download_audio_reference_uses_audio_get_sources_payload(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class ProtocolAudioClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def _send_and_wait(self, opcode, payload, timeout=10):
            opcode_name = getattr(opcode, "name", str(opcode))
            self.calls.append((opcode_name, dict(payload)))
            if opcode_name == "CHAT_HISTORY":
                return {"payload": {"messages": []}}
            if opcode_name == "MSG_GET":
                return {"payload": {"messages": []}}
            if opcode_name == "AUDIO_GET_SOURCES":
                assert payload == {
                    "audioId": 92,
                    "chatId": 200056208,
                    "messageId": 116605799957888782,
                }
                return {
                    "payload": {
                        "opus": "https://audio.example.test/protocol.ogg?secret=1",
                        "m4a": "https://audio.example.test/protocol.m4a?secret=1",
                    }
                }
            if "fileId" in payload:
                return {
                    "payload": {
                        "url": "https://audio.example.test/protocol.ogg?token=secret",
                        "unsafe": False,
                    }
                }
            if "audioId" in payload:
                raise AssertionError("audioId FILE_DOWNLOAD probe is unsafe for userbot audio")
            return {"payload": {"error": {"code": "file.not.found"}}}

    client = ProtocolAudioClient()
    adapter._client = client

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        attachment = await adapter.download_audio_reference(
            chat_id="200056208",
            msg_id="116605799957888782",
            reference_id="92",
            reference_kind="audio_id",
            duration=38360,
            source_type="AUDIO",
        )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 38, None, None, "AUDIO")
    assert any(call[0] == "AUDIO_GET_SOURCES" and "audioId" in call[1] for call in client.calls)
    assert not any(call[0] == "FILE_DOWNLOAD" and "audioId" in call[1] for call in client.calls)
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/protocol.ogg?secret=1",
            "audio_retry_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
            "audio_get_sources",
        )
    ]
    assert adapter.file_downloads == []
    assert "https://audio.example.test/protocol.ogg" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_download_audio_reference_falls_back_to_file_download_after_audio_get_miss(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    class FileIdMissClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def _send_and_wait(self, opcode, payload, timeout=10):
            opcode_name = getattr(opcode, "name", str(opcode))
            self.calls.append((opcode_name, dict(payload)))
            if opcode_name == "CHAT_HISTORY":
                return {"payload": {"messages": []}}
            if opcode_name == "MSG_GET":
                return {"payload": {"messages": []}}
            if opcode_name == "AUDIO_GET_SOURCES":
                return {"payload": {"error": {"code": "audio.not.ready"}}}
            if "fileId" in payload:
                return {"payload": {"error": {"code": "file.not.found"}}}
            if "audioId" in payload:
                raise AssertionError("audioId FILE_DOWNLOAD probe closes MAX socket in prod")
            raise AssertionError(f"unexpected payload shape: {payload!r}")

    client = FileIdMissClient()
    adapter._client = client

    attachment = await adapter.download_audio_reference(
        chat_id="200056208",
        msg_id="116605799957888782",
        reference_id="92",
        reference_kind="audio_id",
        duration=9,
        source_type="AUDIO",
    )

    assert attachment is None
    assert adapter.url_downloads == []
    assert adapter.file_downloads == [
        (
            "200056208",
            "116605799957888782",
            92,
            "audio_retry_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
        )
    ]
    file_download_payloads = [
        payload for opcode_name, payload in client.calls if opcode_name == "FILE_DOWNLOAD"
    ]
    assert file_download_payloads == [
        {"chatId": 200056208, "messageId": 116605799957888782, "fileId": 92},
    ]
    audio_get_payloads = [
        payload for opcode_name, payload in client.calls if opcode_name == "AUDIO_GET_SOURCES"
    ]
    assert audio_get_payloads == [
        {"audioId": 92, "chatId": 200056208, "messageId": 116605799957888782},
    ]


@pytest.mark.asyncio
async def test_download_audio_attachment_passes_in_memory_token_to_audio_get_sources(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class TokenAudioClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def _send_and_wait(self, opcode, payload, timeout=10):
            opcode_name = getattr(opcode, "name", str(opcode))
            self.calls.append((opcode_name, dict(payload)))
            if opcode_name == "AUDIO_GET_SOURCES":
                assert payload == {
                    "audioId": 92,
                    "chatId": 200056208,
                    "messageId": 116605799957888782,
                    "token": "attach-token",
                }
                return {"payload": {"opus": "https://audio.example.test/tokenized.ogg"}}
            raise AssertionError(f"unexpected payload shape: {payload!r}")

    client = TokenAudioClient()
    adapter._client = client

    attachment = await adapter._download_attachment(
        "200056208",
        "116605799957888782",
        SimpleNamespace(type="AUDIO", audio_id=92, token="attach-token", duration=5),
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 5, None, None, "AUDIO")
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/tokenized.ogg",
            "audio_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
            "audio_get_sources",
        )
    ]
    assert not any(opcode_name == "FILE_DOWNLOAD" for opcode_name, _ in client.calls)


@pytest.mark.asyncio
async def test_download_audio_reference_stops_protocol_after_socket_error(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    class SocketSendError(Exception):
        pass

    class DisconnectingProtocolClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def _send_and_wait(self, opcode, payload, timeout=10):
            opcode_name = getattr(opcode, "name", str(opcode))
            self.calls.append((opcode_name, dict(payload)))
            if opcode_name == "CHAT_HISTORY":
                return {"payload": {"messages": []}}
            if opcode_name == "MSG_GET":
                return {"payload": {"messages": []}}
            if opcode_name == "AUDIO_GET_SOURCES":
                raise SocketSendError()
            raise AssertionError(f"unexpected payload shape: {payload!r}")

    client = DisconnectingProtocolClient()
    adapter._client = client

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        attachment = await adapter.download_audio_reference(
            chat_id="200056208",
            msg_id="116605799957888782",
            reference_id="92",
            reference_kind="audio_id",
            duration=9,
            source_type="AUDIO",
        )

    assert attachment is None
    assert adapter.url_downloads == []
    assert adapter.file_downloads == []
    audio_get_payloads = [
        payload for opcode_name, payload in client.calls if opcode_name == "AUDIO_GET_SOURCES"
    ]
    assert audio_get_payloads == [
        {"audioId": 92, "chatId": 200056208, "messageId": 116605799957888782},
    ]
    assert not any(opcode_name == "FILE_DOWNLOAD" for opcode_name, _ in client.calls)
    assert any(
        getattr(record, "event_fields", {}).get("hard_stop") is True
        for record in caplog.records
    )
    assert "mediaId" not in caplog.text


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
async def test_download_audio_attachment_normalizes_millisecond_duration(tmp_path):
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
            duration=38360,
        ),
    )

    assert attachment == MaxAttachment("audio", local_path, "voice.ogg", 38, None, None, "AUDIO")


@pytest.mark.asyncio
async def test_download_video_attachment_normalizes_millisecond_duration(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = tmp_path / "tmp" / "clip.mp4"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    write_minimal_mp4_with_duration(local_path, seconds=71)
    adapter.url_result = (str(local_path), "clip.mp4")

    attachment = await adapter._download_attachment(
        "28093080",
        "116562825769007612",
        SimpleNamespace(
            type="VIDEO",
            video_id=42,
            url="https://video.example.test/clip.mp4",
            duration=71000,
            width=640,
            height=360,
        ),
    )

    assert attachment == MaxAttachment("video", str(local_path), "clip.mp4", 71, 640, 360, "VIDEO")


@pytest.mark.asyncio
async def test_download_video_reference_uses_mp4_duration_when_max_duration_missing(tmp_path):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = tmp_path / "tmp" / "retry.mp4"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    write_minimal_mp4_with_duration(local_path, seconds=12)

    async def fake_download_video_by_id(*args, **kwargs):
        return str(local_path), "retry.mp4"

    adapter._download_video_by_id = fake_download_video_by_id

    attachment = await adapter.download_video_reference(
        chat_id="-75100771505615",
        msg_id="116562825769007612",
        video_id="42",
        duration=None,
        width=640,
        height=360,
        source_type="VIDEO",
    )

    assert attachment == MaxAttachment("video", str(local_path), "retry.mp4", 12, 640, 360, "VIDEO")


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


class VideoPlayClient(LookupClient):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.last_request = None

    async def _send_and_wait(self, **kwargs):
        self.last_request = kwargs
        return {"payload": self.payload}


def test_extract_video_url_prefers_stream_over_thumbnail(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    payload = {
        "cache": True,
        "EXTERNAL": "https://m.ok.ru/video/13208513634267",
        "MP4_720": "https://maxvd677.okcdn.ru/?expires=1&srcIp=203.0.113.217&type=3&id=13644091493083",
    }

    assert adapter._extract_video_url(payload) == "https://maxvd677.okcdn.ru/?expires=1&srcIp=203.0.113.217&type=3&id=13644091493083"


def test_download_headers_for_url_uses_chrome_user_agent_for_chrome_signed_url(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd677.okcdn.ru/?expires=1&srcAg=CHROME&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_android_chrome_user_agent(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd217.okcdn.ru/?expires=1&srcAg=CHROME_ANDROID&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_ANDROID_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_ios_chrome_user_agent(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd587.okcdn.ru/?expires=1&srcAg=CHROME_IPHONE&id=13644091493083"
    )

    assert headers == {"User-Agent": MAX_CDN_IOS_CHROME_USER_AGENT}


def test_download_headers_for_url_uses_mobile_safari_for_non_chrome_signed_url(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    headers = adapter._download_headers_for_url(
        "https://maxvd204.okcdn.ru/?expires=1&srcAg=SAFARI_IPHONE_OTHER&id=13636639132379"
    )

    assert headers == {"User-Agent": MAX_CDN_USER_AGENT}


@pytest.mark.asyncio
async def test_download_video_by_id_uses_raw_video_play_payload(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
async def test_handle_raw_message_marks_failed_video_retryable_by_video_id(tmp_path):
    class FailingVideoAdapter(AdapterHarness):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._adapter._media._download_video_by_id = self._download_video_by_id

        async def _download_video_by_id(self, *args, **kwargs):
            return None, None

    adapter = FailingVideoAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(users={7001: make_user("Вита")})
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    await adapter._handle_raw_message(
        SimpleNamespace(
            id=777,
            chat_id=-70000000000003,
            sender=7001,
            text="",
            type="USER",
            status=None,
            attaches=[
                SimpleNamespace(
                    type="VIDEO",
                    video_id=555,
                    duration=10,
                    width=640,
                    height=360,
                    url=None,
                    token="secret-token",
                )
            ],
            link=None,
        )
    )

    assert len(received) == 1
    failure = received[0].attachment_failures[0]
    assert failure.kind == "video"
    assert failure.retryable is True
    assert failure.reference_kind == "video_id"
    assert failure.reference_id == "555"
    assert failure.media_chat_id == "-70000000000003"
    assert failure.media_msg_id == "777"
    assert failure.duration == 10
    assert failure.width == 640
    assert failure.height == 360
    assert "secret-token" not in str(failure)
    assert "http" not in str(failure)


@pytest.mark.asyncio
async def test_download_from_url_uses_mobile_safari_user_agent(tmp_path, monkeypatch):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

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
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

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
