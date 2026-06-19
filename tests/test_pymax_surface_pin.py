"""Pin the PyMax 2 surface the bridge backend depends on."""

import importlib

import pytest
import pymax


pytestmark = pytest.mark.architecture


def test_pymax_runtime_version_is_pinned():
    assert pymax.__version__ == "2.3.0"


PINS = {
    "pymax": (
        "Client",
        "ExtraConfig",
        "SyncOverrides",
        "File",
        "Message",
        "Photo",
        "Video",
        "TypingEvent",
        "PresenceEvent",
        "MessageReadEvent",
        "ReactionUpdateEvent",
        "MessageDeleteEvent",
    ),
    "pymax.types": ("ContactInfo",),
    "pymax.client": ("Client",),
    "pymax.connection": ("ConnectionManager",),
    "pymax.connection.readers": ("TCPReader",),
    "pymax.protocol": ("Command", "Opcode"),
    "pymax.protocol.tcp": ("TcpProtocol",),
    "pymax.protocol.tcp.framing": ("TcpPacketFramer",),
    "pymax.protocol.tcp.payload": ("MsgpackPayloadCodec",),
    "pymax.transport.tcp": ("TCPTransport",),
    "pymax.session": ("SessionStore",),
    "pymax.session.models": ("SessionInfo",),
    "pymax.api.auth.payloads": ("SyncPayload", "WebSyncPayload"),
    "pymax.api.auth.service": ("AuthService",),
    "pymax.api.session.enums": ("DeviceType",),
    "pymax.api.session.payloads": ("MobileUserAgentPayload",),
    "pymax.api.messages.payloads": ("ChatHistoryPayload", "GetVideoPayload"),
    "pymax.auth": ("AuthFlow", "SmsAuthFlow", "ConsoleSmsCodeProvider"),
    "pymax.types.domain.attachments.enums": ("AttachmentType",),
    "pymax.types.domain.login": ("LoginResponse",),
}


@pytest.mark.parametrize(("module_name", "names"), PINS.items())
def test_pymax_backend_surface_is_pinned(module_name: str, names: tuple[str, ...]):
    module = importlib.import_module(module_name)
    missing = [name for name in names if not hasattr(module, name)]

    assert not missing, (
        f"{module_name} no longer exports {missing}. "
        "This is an upstream PyMax surface change; adjust the MAX backend before deploy."
    )


def test_pymax_230_client_methods_are_pinned():
    for name in (
        "import_contacts",
        "on_disconnect",
        "on_error",
        "relogin",
        "delete_chat",
    ):
        assert hasattr(pymax.Client, name), f"pymax.Client.{name} is missing"


def test_pymax_230_session_store_delete_all_sessions_is_pinned():
    from pymax.session import SessionStore

    assert hasattr(SessionStore, "delete_all_sessions")
