import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from src.adapters.max import errors as max_errors
from src.adapters.max import payload as max_payload
from src.adapters.max import users as max_users
from src.adapters.max.adapter import MaxAdapter
from src.adapters.max.client_factory import create_socket_client
from src.adapters.max.media import downloader as max_downloader
from src.adapters.max.media import ua as max_ua


pytestmark = pytest.mark.architecture


def test_max_ua_mapping_selects_chrome_android_profile():
    headers, src_ag, family = max_ua.download_client_profile_for_url(
        "https://cdn.example.test/file?srcAg=CHROME_ANDROID"
    )

    assert headers == {"User-Agent": max_ua.MAX_CDN_ANDROID_CHROME_USER_AGENT}
    assert src_ag == "CHROME_ANDROID"
    assert family == "chrome_android"


def test_payload_helpers_match_case_and_strip_unsafe_fields():
    payload = {
        "chatId": 42,
        "message": {
            "safe": {"child": 1},
            "downloadUrl": "https://secret.example.test/file",
            "token": "secret",
        },
    }

    assert max_payload.payload_value(payload, "chat_id") == 42
    assert max_payload.payload_value(payload, "CHATID") == 42
    assert max_payload.safe_payload_error_code({"error": {"code": "audio.not.ready"}}) == "audio.not.ready"
    assert max_payload.safe_field_paths(payload) == [
        "chatId",
        "message",
        "message.safe",
        "message.safe.child",
    ]


def test_error_classification_is_pymax_free():
    corrupt = max_errors.classify_runtime_error(
        RuntimeError("sqlite3.DatabaseError: database disk image is malformed")
    )
    invalid = max_errors.classify_runtime_error(RuntimeError("Invalid token"))
    incomplete = max_errors.classify_runtime_error(
        RuntimeError("MAX client start returned before on_start")
    )

    assert corrupt is not None
    assert corrupt.kind == "session_corrupt"
    assert corrupt.requires_reauth is True
    assert invalid is not None
    assert invalid.kind == "session_invalid"
    assert incomplete is not None
    assert incomplete.kind == "max_start_incomplete"


@pytest.mark.asyncio
async def test_pymax_client_adapter_captures_early_startup_errors():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    class FakeClient:
        async def start(self):
            raise RuntimeError("start boom")

    captured = []
    adapter = PymaxClientAdapter(FakeClient())

    async def capture(exc):
        captured.append(str(exc))
        await asyncio.sleep(0)

    adapter.prepare_startup(capture)

    with pytest.raises(RuntimeError, match="start boom"):
        await adapter.raw_client.start()

    assert captured == ["start boom"]


def test_users_and_downloader_helpers_are_plain_object_based():
    dialog = SimpleNamespace(participants=[SimpleNamespace(id="own"), SimpleNamespace(account_id="peer")])
    video_url = max_downloader.extract_video_url(
        {"preview": "https://cdn.example.test/thumb.jpg", "streams": {"360": "https://cdn.example.test/v.mp4"}}
    )

    assert max_users.dialog_partner_id(dialog, "own", extract_user_name=lambda _value: None) == "peer"
    assert max_downloader.fix_filename_encoding("plain.txt") == "plain.txt"
    assert max_downloader.build_filename("voice", None, "https://cdn.example.test/path/audio", "audio/ogg") == "voice.oga"
    assert video_url == "https://cdn.example.test/v.mp4"


def test_client_factory_disables_pymax_reconnect_and_telemetry(monkeypatch, tmp_path):
    from src.adapters.max.backends.pymax import client_factory as pymax_factory
    from src.adapters.max.backends.pymax.session_store import BridgeSessionStore

    calls = {}

    class FakeClient:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(pymax_factory, "Client", FakeClient)

    create_socket_client(phone="+79991234567", data_dir=str(tmp_path), session_name="session")

    assert calls["extra_config"].reconnect is False
    assert calls["extra_config"].telemetry is False
    assert isinstance(calls["extra_config"].store, BridgeSessionStore)
    assert calls["extra_config"].user_agent.device_type.value == "DESKTOP"
    assert calls["extra_config"].sync.chats_sync == 0
    assert calls["extra_config"].sync.contacts_sync == 0
    assert calls["work_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_pymax2_session_store_imports_legacy_pymax1_auth_table(tmp_path):
    from src.adapters.max.backends.pymax.session_store import BridgeSessionStore

    db_path = tmp_path / "session.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE auth (token TEXT NOT NULL, device_id CHAR(32) NOT NULL)")
    con.execute(
        "INSERT INTO auth (token, device_id) VALUES (?, ?)",
        ("test-token", "legacy-device-id"),
    )
    con.commit()
    con.close()

    store = BridgeSessionStore(str(tmp_path), "session.db", phone="+79991234567")
    session = await store.load_session()
    await store.close()

    assert session is not None
    assert session.token == "test-token"
    assert session.device_id == "legacy-device-id"
    assert session.phone == "+79991234567"

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT device_id, phone FROM sessions").fetchone()
    con.close()

    assert row == ("legacy-device-id", "+79991234567")


def test_pymax2_login_payload_drops_unsupported_attachments():
    from src.adapters.max.backends.pymax.login import sanitize_login_payload

    payload = {
        "chats": [
            {
                "lastMessage": {
                    "attaches": [
                        {"type": "UNSUPPORTED", "audioId": "redacted"},
                        {"type": "FILE", "fileId": 1},
                    ]
                }
            }
        ],
        "messages": {
            1: [
                {
                    "attaches": [
                        {"_type": "UNSUPPORTED", "token": "redacted"},
                        {"type": "PHOTO", "photoId": 2},
                    ]
                }
            ]
        },
    }

    sanitized = sanitize_login_payload(payload)

    assert sanitized["chats"][0]["lastMessage"]["attaches"] == [
        {"type": "FILE", "fileId": 1}
    ]
    assert sanitized["messages"][1][0]["attaches"] == [
        {"type": "PHOTO", "photoId": 2}
    ]
    assert len(payload["chats"][0]["lastMessage"]["attaches"]) == 2


@pytest.mark.asyncio
async def test_pymax2_handler_signatures_are_adapted_to_bridge_callbacks():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    class FakeClient:
        logger = None

        def on_start(self):
            def register(handler):
                self.start_handler = handler
                return handler

            return register

        def on_message(self):
            def register(handler):
                self.message_handler = handler
                return handler

            return register

        on_message_edit = on_message_delete = on_message

    client = FakeClient()
    adapter = PymaxClientAdapter(client)
    start_calls = []
    messages = []

    async def on_start():
        start_calls.append("started")

    async def on_message(message):
        messages.append(message)

    adapter.register_start_handler(on_start)
    adapter.register_message_handler(on_message)

    await client.start_handler(client)
    await client.message_handler(SimpleNamespace(id=10, chat_id=20, sender=30), client)

    assert start_calls == ["started"]
    assert messages[0].id == 10
    assert messages[0].chat_id == 20


@pytest.mark.asyncio
async def test_pymax2_raw_gateway_converts_frames_and_invokes_app():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    class FakeApp:
        def __init__(self):
            self.calls = []

        async def invoke(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(opcode=49, cmd=1, seq=7, payload={"ok": True})

    class FakeClient:
        logger = None

        def __init__(self):
            self._app = FakeApp()

        def on_raw(self):
            def register(handler):
                self.raw_handler = handler
                return handler

            return register

    client = FakeClient()
    adapter = PymaxClientAdapter(client)
    received = []

    result = adapter.install_raw_message_interceptor(received.append)
    await client.raw_handler(SimpleNamespace(opcode=128, cmd=0, seq=5, payload={"p": 1}), client)
    response = await adapter.raw_request(
        opcode_name="CHAT_HISTORY",
        payload={"chatId": 1},
        timeout=10,
    )

    assert result.installed is True
    assert received == [{"opcode": 128, "cmd": 0, "seq": 5, "payload": {"p": 1}}]
    assert client._app.calls[0]["opcode"] == 49
    assert client._app.calls[0]["payload"] == {"chatId": 1}
    assert response == {"opcode": 49, "cmd": 1, "seq": 7, "payload": {"ok": True}}


@pytest.mark.asyncio
async def test_pymax2_send_uses_attachments_list(tmp_path):
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    class FakeClient:
        logger = None

        async def send_message(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(id=123)

    media_path = tmp_path / "photo.jpg"
    media_path.write_bytes(b"fake image")

    client = FakeClient()
    adapter = PymaxClientAdapter(client)

    result = await adapter.send_outbound_message(
        chat_id=1,
        text="hello",
        reply_to=2,
        media_path=str(media_path),
        media_type="photo",
    )

    assert result.message_id == "123"
    assert "attachment" not in client.kwargs
    assert len(client.kwargs["attachments"]) == 1


def test_pymax2_snapshots_use_profile_users_and_chat_types():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    client = SimpleNamespace(
        logger=None,
        me=SimpleNamespace(contact=SimpleNamespace(id=100)),
        contacts=[SimpleNamespace(id=1)],
        _app=SimpleNamespace(users={2: SimpleNamespace(id=2)}),
        chats=[
            SimpleNamespace(id=10, type="DIALOG", participants={}),
            SimpleNamespace(id=20, type="CHAT", title="group"),
            SimpleNamespace(id=30, type="CHANNEL", title="channel"),
        ],
    )
    adapter = PymaxClientAdapter(client)

    assert adapter.own_user_id() == "100"
    assert list(adapter.users_cache_snapshot()) == [2]
    assert [item.id for item in adapter.contacts_snapshot()] == [1]
    assert [item.id for item in adapter.dialogs_snapshot()] == [10]
    assert [item.id for item in adapter.group_chats_snapshot()] == [20]
    assert [item.id for item in adapter.channels_snapshot()] == [30]


@pytest.mark.asyncio
async def test_pymax2_egress_transport_uses_configured_socket_connector(monkeypatch):
    from src.adapters.max.backends.pymax.transport import EgressTCPTransport

    calls = []

    class FakeSocket:
        def setsockopt(self, *args):
            calls.append(("setsockopt", args))

        def setblocking(self, value):
            calls.append(("setblocking", value))

    class FakeConnector:
        def connect(self, host, port, timeout=None):
            calls.append(("connect", host, port, timeout))
            return FakeSocket()

    async def fake_open_connection(**kwargs):
        calls.append(("open_connection", kwargs))
        return object(), object()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    transport = EgressTCPTransport(
        socket_connector=FakeConnector(),
        host="api.oneme.ru",
        port=443,
    )
    await transport.connect()

    assert calls[0] == ("connect", "api.oneme.ru", 443, 20.0)
    assert calls[-1][0] == "open_connection"
    assert calls[-1][1]["ssl"] is True
    assert calls[-1][1]["server_hostname"] == "api.oneme.ru"


def test_max_adapter_can_be_composed_with_fake_backend(tmp_path):
    class FakeBackend:
        pass

    backend = FakeBackend()
    adapter = MaxAdapter(
        "+79991234567",
        str(tmp_path),
        "session",
        str(tmp_path / "tmp"),
        backend=backend,
    )

    assert adapter._state.backend is backend
