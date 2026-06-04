"""Pin the PyMax 2 surface the bridge backend depends on."""

import importlib

import pytest
import pymax


pytestmark = pytest.mark.architecture


def test_pymax_runtime_version_is_pinned():
    assert pymax.__version__ == "2.1.2"


PINS = {
    "pymax": ("Client", "ExtraConfig", "SyncOverrides", "File", "Message", "Photo", "Video"),
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
