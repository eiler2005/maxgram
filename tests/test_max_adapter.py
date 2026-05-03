import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.adapters.max_adapter import (
    MAX_CDN_CHROME_USER_AGENT,
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

    async def fake_download(url: str, prefix: str, filename_hint=None, default_extension="", expected_kind=None, flow_id=None):
        captured["url"] = url
        captured["prefix"] = prefix
        captured["filename_hint"] = filename_hint
        captured["default_extension"] = default_extension
        captured["expected_kind"] = expected_kind
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
