import asyncio
import os
import ssl
import sqlite3
import stat
import struct
from types import SimpleNamespace

import msgpack
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
    seq_overflow = max_errors.classify_runtime_error(
        struct.error("'B' format requires 0 <= number <= 255")
    )

    assert corrupt is not None
    assert corrupt.kind == "session_corrupt"
    assert corrupt.requires_reauth is True
    assert invalid is not None
    assert invalid.kind == "session_invalid"
    assert incomplete is not None
    assert incomplete.kind == "max_start_incomplete"
    assert seq_overflow is not None
    assert seq_overflow.kind == "pymax_tcp_sequence_overflow"
    assert seq_overflow.requires_reauth is False


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


def test_client_factory_passes_custom_auth_flow(monkeypatch, tmp_path):
    from src.adapters.max.backends.pymax import client_factory as pymax_factory

    calls = {}

    class FakeClient:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    auth_flow = object()
    monkeypatch.setattr(pymax_factory, "Client", FakeClient)

    pymax_factory.create_pymax_client(
        phone="+79991234567",
        data_dir=str(tmp_path),
        session_name="session",
        auth_flow=auth_flow,
    )

    assert calls["auth_flow"] is auth_flow


def test_client_factory_can_disable_legacy_session_import(monkeypatch, tmp_path):
    from src.adapters.max.backends.pymax import client_factory as pymax_factory
    from src.adapters.max.backends.pymax.session_store import BridgeSessionStore

    calls = {}

    class FakeClient:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(pymax_factory, "Client", FakeClient)

    pymax_factory.create_pymax_client(
        phone="+79991234567",
        data_dir=str(tmp_path),
        session_name="session",
        import_legacy_session=False,
    )

    store = calls["extra_config"].store
    assert isinstance(store, BridgeSessionStore)
    assert store._import_legacy is False


def test_pymax_msgpack_codec_tolerates_array_map_keys():
    from src.adapters.max.backends.pymax.transport import BridgeMsgpackPayloadCodec

    packer = msgpack.Packer(use_bin_type=True)
    payload = (
        packer.pack_map_header(2)
        + packer.pack(["complex", "key"])
        + packer.pack({"safe": True})
        + packer.pack("messages")
        + packer.pack([])
    )

    decoded = BridgeMsgpackPayloadCodec().decode(payload)

    assert decoded[("complex", "key")] == {"safe": True}
    assert decoded["messages"] == []


def test_pymax_sequence_guard_uses_pymax_2_1_word_range():
    from pymax.protocol import OutboundFrame

    from src.adapters.max.backends.pymax.transport import (
        BridgeConnectionManager,
        bridge_tcp_protocol,
    )

    connection = BridgeConnectionManager(
        reader=object(),
        transport=object(),
        protocol=bridge_tcp_protocol(),
    )
    connection._seq = 65533

    assert [connection.next_seq() for _ in range(4)] == [65534, 65535, 0, 1]

    for seq in [65534, 65535, 0, 1]:
        connection.protocol.encode(
            OutboundFrame(ver=10, opcode=49, cmd=0, seq=seq, payload={"chatId": 1})
        )

    with pytest.raises(struct.error):
        connection.protocol.encode(
            OutboundFrame(ver=10, opcode=49, cmd=0, seq=65536, payload={"chatId": 1})
        )


def test_bridge_tcp_protocol_keeps_pymax_231_zstd_payload_decoder():
    from pymax.protocol.tcp.compression import ZstdCompression

    from src.adapters.max.backends.pymax.transport import (
        BridgeMsgpackPayloadCodec,
        bridge_tcp_protocol,
    )

    protocol = bridge_tcp_protocol()

    assert isinstance(protocol.payload_decoder.serializer, BridgeMsgpackPayloadCodec)
    assert isinstance(protocol.payload_decoder.zstd_compression, ZstdCompression)


def test_client_factory_installs_bridge_protocol_guards(monkeypatch, tmp_path):
    from src.adapters.max.backends.pymax import client_factory as pymax_factory
    from src.adapters.max.backends.pymax.transport import BridgeMsgpackPayloadCodec

    class FakeDecoder:
        serializer = object()

    class FakeProtocol:
        serializer = object()
        payload_decoder = FakeDecoder()

    class FakeClient:
        def __init__(self, **_kwargs):
            self._connection = SimpleNamespace(protocol=FakeProtocol(), _seq=65533)
            self._app = SimpleNamespace(api=SimpleNamespace(auth=None, users=None))

    monkeypatch.setattr(pymax_factory, "Client", FakeClient)

    client = pymax_factory.create_pymax_client(
        phone="+79991234567",
        data_dir=str(tmp_path),
        session_name="session",
    )

    assert isinstance(client._connection.protocol.serializer, BridgeMsgpackPayloadCodec)
    assert isinstance(client._connection.protocol.payload_decoder.serializer, BridgeMsgpackPayloadCodec)
    assert [client._connection.next_seq() for _ in range(4)] == [65534, 65535, 0, 1]
    assert client._maxtg_msgpack_guard_installed is True
    assert client._connection._maxtg_seq_guard_installed is True
    assert client._app.api.auth.__class__.__name__ == "BridgeAuthService"
    assert client._app.api.users.__class__.__name__ == "BridgeUserService"


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


@pytest.mark.asyncio
async def test_pymax2_session_store_can_skip_legacy_pymax1_auth_import(tmp_path):
    from src.adapters.max.backends.pymax.session_store import BridgeSessionStore

    db_path = tmp_path / "session.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE auth (token TEXT NOT NULL, device_id CHAR(32) NOT NULL)")
    con.execute(
        "INSERT INTO auth (token, device_id) VALUES (?, ?)",
        ("stale-token", "legacy-device-id"),
    )
    con.commit()
    con.close()

    store = BridgeSessionStore(
        str(tmp_path),
        "session.db",
        phone="+79991234567",
        import_legacy=False,
    )
    session = await store.load_session()
    await store.close()

    assert session is None


@pytest.mark.asyncio
async def test_pymax2_session_store_can_clear_saved_sessions(tmp_path):
    from src.adapters.max.backends.pymax.session_store import BridgeSessionStore

    db_path = tmp_path / "session.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE sessions (
            token TEXT NOT NULL PRIMARY KEY,
            device_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            mt_instance_id TEXT NOT NULL DEFAULT '',
            chats_sync INTEGER NOT NULL DEFAULT -1,
            contacts_sync INTEGER NOT NULL DEFAULT -1,
            drafts_sync INTEGER NOT NULL DEFAULT -1,
            presence_sync INTEGER NOT NULL DEFAULT -1,
            config_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    con.execute(
        """
        INSERT INTO sessions (
            token, device_id, phone, mt_instance_id,
            chats_sync, contacts_sync, drafts_sync, presence_sync, config_hash
        )
        VALUES (?, ?, ?, '', 0, 0, 0, 0, 'hash')
        """,
        ("old-token", "old-device", "+79991234567"),
    )
    con.commit()
    con.close()

    store = BridgeSessionStore(str(tmp_path), "session.db", phone="+79991234567")
    await store.clear_sessions()
    await store.close()

    con = sqlite3.connect(db_path)
    count = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    con.close()

    assert count == 0


@pytest.mark.asyncio
async def test_reauth_close_after_success_suppresses_ssl_shutdown_noise():
    from src.adapters.max.backends.pymax import reauth as pymax_reauth

    class FakeClient:
        async def stop(self):
            raise ssl.SSLError("application data after close notify")

    await pymax_reauth._close_after_success(FakeClient())


@pytest.mark.asyncio
async def test_reauth_done_callback_suppresses_close_task_noise():
    from src.adapters.max.backends.pymax import reauth as pymax_reauth

    async def fail_close():
        raise ssl.SSLError("application data after close notify")

    task = asyncio.create_task(fail_close())
    await asyncio.sleep(0)

    pymax_reauth._ignore_close_error(task)


def test_max_reauth_refuses_fresh_bridge_heartbeat(tmp_path):
    import time
    from scripts import max_reauth

    heartbeat = tmp_path / "health_heartbeat.json"
    heartbeat.write_text("{}")

    assert max_reauth.bridge_heartbeat_is_fresh(tmp_path) is True
    old = time.time() - 300
    os.utime(heartbeat, (old, old))
    assert max_reauth.bridge_heartbeat_is_fresh(tmp_path) is False


def test_max_reauth_snapshot_session_db_copies_without_token_output(tmp_path):
    from scripts import max_reauth

    session = tmp_path / "session.db"
    session.write_bytes(b"opaque sqlite bytes with token")

    snapshot = max_reauth.snapshot_session_db(tmp_path, "session.db")

    assert snapshot is not None
    assert snapshot.name.startswith("session.db.before-reauth-")
    assert snapshot.read_bytes() == session.read_bytes()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


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


def test_pymax2_login_payload_tolerates_animoji_element_attributes():
    from pymax.types.domain.element import Element

    from src.adapters.max.backends.pymax.login import sanitize_login_payload

    payload = {
        "chats": [
            {
                "lastMessage": {
                    "elements": [
                        {
                            "type": "ANIMOJI",
                            "from": 0,
                            "length": 1,
                            "attributes": {
                                "animojiLottieUrl": "https://cdn.example.test/a.json",
                                "animojiSetId": 1,
                            },
                        },
                        {
                            "type": "LINK",
                            "from": 2,
                            "length": 7,
                            "attributes": {"url": "https://example.test"},
                        },
                    ]
                }
            }
        ]
    }

    sanitized = sanitize_login_payload(payload)
    elements = sanitized["chats"][0]["lastMessage"]["elements"]

    assert "attributes" not in elements[0]
    assert elements[1]["attributes"] == {"url": "https://example.test"}
    assert Element.model_validate(elements[0]).attributes is None
    assert Element.model_validate(elements[1]).attributes.url == "https://example.test"
    assert "attributes" in payload["chats"][0]["lastMessage"]["elements"][0]


def test_pymax2_login_validation_repairs_noncritical_payload_drift():
    from src.adapters.max.backends.pymax.login import validate_login_response

    payload = {
        "profile": {"contact": {"id": 1}},
        "token": "redacted",
        "chats": [
            {
                "id": 10,
                "type": "CHAT",
                "status": "ACTIVE",
                "owner": 1,
                "lastMessage": {"type": "USER"},
            }
        ],
        "messages": {10: [{"id": 21, "type": "USER"}]},
        "contacts": [{"bad": "shape"}],
    }

    response = validate_login_response(payload)

    assert response.chats[0].last_message is None
    assert response.messages == {10: []}
    assert response.contacts == [None]


def test_pymax212_login_validation_allows_tokenless_response():
    from src.adapters.max.backends.pymax.login import validate_login_response

    response = validate_login_response(
        {
            "profile": {"contact": {"id": 1}},
            "chats": [],
            "messages": {},
            "contacts": [],
        }
    )

    assert response.token is None


def test_pymax2_login_validation_error_is_safe_and_classified():
    from src.adapters.max.backends.pymax.login import (
        PymaxPayloadValidationError,
        validate_login_response,
    )

    with pytest.raises(PymaxPayloadValidationError) as exc_info:
        validate_login_response({"token": "secret", "profile": {"broken": "shape"}})

    message = str(exc_info.value)
    assert "pymax payload validation failed" in message
    assert "input_val" not in message
    assert "secret" not in message

    issue = max_errors.classify_runtime_error(exc_info.value)
    assert issue is not None
    assert issue.kind == "pymax_payload_drift"
    assert issue.requires_reauth is False


def test_pymax2_user_payload_tolerates_numeric_gender_and_web_app_url():
    from pymax.types.domain import User

    from src.adapters.max.backends.pymax.user import sanitize_user_payload

    payload = {
        "contacts": [
            {
                "id": 42,
                "names": [{"name": "Example User"}],
                "gender": 1,
                "webApp": "https://frontend.example.test/preserver",
            }
        ]
    }

    sanitized = sanitize_user_payload(payload)
    user = User.model_validate(sanitized["contacts"][0])

    assert "gender" not in sanitized["contacts"][0]
    assert sanitized["contacts"][0]["webApp"] == {
        "url": "https://frontend.example.test/preserver"
    }
    assert user.gender is None
    assert user.web_app == {"url": "https://frontend.example.test/preserver"}
    assert payload["contacts"][0]["gender"] == 1
    assert payload["contacts"][0]["webApp"] == "https://frontend.example.test/preserver"


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


def test_pymax2_is_connected_checks_transport_connected():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter

    class FakeTransport:
        connected = True

    class FakeConnection:
        _conn_lost = False
        transport = FakeTransport()

        def is_open(self):
            return True

    client = SimpleNamespace(logger=None, _connection=FakeConnection())
    adapter = PymaxClientAdapter(client)

    assert adapter.is_connected is True
    client._connection.transport.connected = False
    assert adapter.is_connected is False
    client._connection.transport.connected = True
    client._connection._conn_lost = True
    assert adapter.is_connected is False


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
async def test_pymax2_adapter_imports_contacts_and_resolves_dm_chat_id():
    from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter
    from src.adapters.max.ports import MaxContactImportEntry

    class FakeClient:
        logger = None
        me = SimpleNamespace(id=100)

        def __init__(self):
            self.imported = []

        async def import_contacts(self, contacts):
            self.imported = contacts
            return [SimpleNamespace(id=300, names=[SimpleNamespace(first_name="Ada")])]

        def get_chat_id(self, *, first_user_id: int, second_user_id: int):
            return f"{first_user_id}:{second_user_id}"

    client = FakeClient()
    adapter = PymaxClientAdapter(client)

    users = await adapter.import_contacts(
        [
            MaxContactImportEntry(
                phone="+79990000000",
                first_name="Ada",
                last_name="Lovelace",
            )
        ]
    )

    assert users[0].id == 300
    assert adapter.dm_chat_id_for_user(300) == "100:300"
    assert client.imported[0].phone == "+79990000000"
    assert client.imported[0].first_name == "Ada"
    assert client.imported[0].last_name == "Lovelace"


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


def test_pymax2_egress_client_uses_bridge_connection_manager(tmp_path):
    from src.adapters.max.backends.pymax.transport import BridgeConnectionManager, EgressClient

    client = EgressClient(
        phone="+79991234567",
        work_dir=str(tmp_path),
        session_name="session",
    )

    assert isinstance(client._connection, BridgeConnectionManager)
    assert client._connection.next_seq() == 0


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
