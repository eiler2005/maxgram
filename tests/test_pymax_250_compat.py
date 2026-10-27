"""Exercise upstream 2.5 event/protocol changes through the bridge boundary.

Clients build a runtime in a temporary directory but never connect to MAX.
"""

import msgpack
import pytest
import pytest_asyncio
from pydantic import ValidationError
from pymax.protocol import Command, InboundFrame, Opcode

from src.adapters.max.backends.pymax.client_factory import (
    bridge_version_catalog,
    create_pymax_client,
    legacy_desktop_user_agent,
)
from src.adapters.max.backends.pymax.events import PymaxEventRouter
from src.adapters.max.backends.pymax.raw_gateway import PymaxRawGateway
from src.adapters.max.backends.pymax.transport import bridge_tcp_protocol
from src.adapters.max.deps import EventsDeps
from src.adapters.max.events import MaxEventsService
from src.adapters.max.state import ConnectionState, OutboundState


@pytest_asyncio.fixture
async def client(tmp_path):
    client = create_pymax_client(
        phone="+70000000000", data_dir=str(tmp_path), session_name="test.db"
    )
    await client._ensure_runtime()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "EDITED"])
async def test_pymax_250_dispatches_reply_and_edit_once_with_raw_fallback(client, status):
    received = []
    raw = []

    async def handler(message):
        received.append(message)

    async def raw_handler(frame):
        raw.append(frame)

    router = PymaxEventRouter(client)
    router.register_message_handler(handler)
    router.register_message_edit_handler(handler)
    gateway = PymaxRawGateway(client)
    gateway.install_raw_handler(raw_handler)
    gateway.install_raw_handler(raw_handler)
    payload = {
        "id": 72, "chatId": -100, "sender": 101,
        "text": "actual answer", "time": 2, "type": "USER",
        "link": {
            "type": "REPLY", "chatId": -100,
            "message": {"id": 71, "text": "original question", "time": 1, "type": "USER"},
        },
    }
    if status:
        payload["status"] = status
    await client._app.dispatcher.dispatch(InboundFrame(
        cmd=Command.REQUEST, opcode=Opcode.NOTIF_MESSAGE, payload=payload
    ))

    assert len(received) == 1
    assert received[0].text == "actual answer"
    assert received[0].link.type.value == "REPLY"
    assert received[0].link.message.id == 71
    assert len(raw) == 1
    assert raw[0]["payload"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mapping", "handler"])
async def test_pymax_250_raw_handler_survives_typed_failure(client, failure):
    received = []

    async def raw_handler(frame):
        received.append(frame)

    PymaxRawGateway(client).install_raw_handler(raw_handler)
    async def fail_handler(event):
        raise RuntimeError("handler failed")

    PymaxEventRouter(client).register_reaction_update_handler(fail_handler)
    frame = InboundFrame(
        cmd=Command.REQUEST,
        opcode=Opcode.NOTIF_MSG_REACTIONS_CHANGED,
        payload={"chatId": -100, "messageId": "invalid-id" if failure == "mapping" else 72},
    )
    with pytest.raises(ValidationError if failure == "mapping" else RuntimeError):
        await client._app.dispatcher.dispatch(frame)

    assert len(received) == 1
    assert received[0]["opcode"] == Opcode.NOTIF_MSG_REACTIONS_CHANGED
    assert received[0]["payload"] == frame.payload


@pytest.mark.asyncio
async def test_pymax_250_nested_reactions_reach_bridge_with_string_message_id(client):
    received = []

    async def handler(event):
        received.append(event)

    service = MaxEventsService(EventsDeps(
        connection=ConnectionState(), outbound=OutboundState(), handlers=[],
        backend=None, raw_payload=None, media=None, resolver=None, runtime=None,
        reaction_update_handlers=[handler],
    ))
    PymaxEventRouter(client).register_reaction_update_handler(service._handle_reaction_update)
    await client._app.dispatcher.dispatch(InboundFrame(
        cmd=Command.REQUEST,
        opcode=Opcode.NOTIF_MSG_REACTIONS_CHANGED,
        payload={
            "chatId": -100, "messageId": 72,
            "reactionInfo": {
                "totalCount": 3,
                "counters": [{"reaction": "👍", "count": 3}],
                "yourReaction": "👍",
            },
        },
    ))

    assert len(received) == 1
    assert received[0].message_id == "72"
    assert received[0].total_count == 3
    assert received[0].counters == [{"emoji": "👍", "count": 3}]


@pytest.mark.parametrize("flags", [0x80, 0xFE])
def test_pymax_250_high_lz4_flags_keep_bridge_msgpack_guard(flags):
    packed = msgpack.packb({"ok": True}, use_bin_type=True)
    # A valid LZ4 literal-only block, independent of an optional encoder package.
    compressed = bytes([len(packed) << 4]) + packed
    protocol = bridge_tcp_protocol()
    assert protocol.payload_decoder.decode(compressed, flags=flags) == {"ok": True}


def test_pymax_250_catalog_keeps_desktop_version_and_build_in_sync():
    catalog = bridge_version_catalog()
    agent = legacy_desktop_user_agent(catalog)
    assert agent.app_version == "26.37.1"
    assert agent.build_number == catalog.resolve(agent.app_version).build_number == 6900
