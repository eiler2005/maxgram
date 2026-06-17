from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.adapters.max.deps import EventsDeps, MediaDeps, ResolveDeps, SendDeps
from src.adapters.max.events import MaxEventsService
from src.adapters.max.media.attachments import MaxMediaService
from src.adapters.max.ports import (
    MaxClientAttachment,
    MaxRawInterceptorResult,
    MaxSendResult,
    MaxUserView,
)
from src.adapters.max.resolve import MaxResolveService
from src.adapters.max.runtime_state import MaxRuntimeService
from src.adapters.max.send import MaxSendService
from src.adapters.max.state import ConnectionState, OutboundState, RawHistoryState


class PortOnlyResolveClient:
    def __init__(self):
        self._cached = {100: MaxUserView(id=100, display_name="Cached User")}
        self._contacts = [MaxUserView(id=200, display_name="Contact User")]
        self.user_cache = {300: MaxUserView(id=300, display_name="Cache User")}

    def cached_user(self, user_id: int):
        return self._cached.get(user_id)

    async def load_users(self, user_ids: list[int]):
        return [MaxUserView(id=user_ids[0], display_name="Live User")]

    def contacts_snapshot(self):
        return list(self._contacts)

    def users_cache_snapshot(self):
        return dict(self.user_cache)

    def dialogs_snapshot(self):
        return []

    def group_chats_snapshot(self):
        return []

    async def chat(self, _chat_id: int):
        return None


@pytest.mark.asyncio
async def test_resolve_service_uses_client_port_snapshots_without_pymax_attrs():
    client = PortOnlyResolveClient()
    service = MaxResolveService(ResolveDeps(connection=ConnectionState(client=client)))

    assert await service.resolve_user_name("100") == "Cached User"
    assert service.find_user_by_name("Contact User") == "200"
    assert service.find_user_by_name("Cache User") == "300"
    assert not hasattr(client, "contacts")
    assert not hasattr(client, "_users")


class PortOnlySendClient:
    def __init__(self):
        self.calls = []

    async def send_outbound_message(self, **kwargs):
        self.calls.append(kwargs)
        return MaxSendResult(message_id="4242")


@pytest.mark.asyncio
async def test_send_service_uses_outbound_port_method(tmp_path):
    client = PortOnlySendClient()
    connection = ConnectionState(client=client, started=True)
    outbound = OutboundState()
    runtime = MaxRuntimeService(
        SimpleNamespace(
            connection=connection,
            outbound=outbound,
            issue_handlers=[],
        )
    )
    media_path = tmp_path / "voice.ogg"
    media_path.write_bytes(b"audio")
    service = MaxSendService(
        SendDeps(
            connection=connection,
            outbound=outbound,
            backend=SimpleNamespace(),
            runtime=runtime,
        )
    )

    msg_id = await service.send_message(
        "123",
        "hello",
        reply_to_msg_id="99",
        media_path=str(media_path),
        media_type="audio",
    )

    assert msg_id == "4242"
    assert client.calls == [
        {
            "chat_id": 123,
            "text": "hello",
            "reply_to": 99,
            "media_path": str(media_path),
            "media_type": "audio",
        }
    ]


class PortOnlyRawClient:
    def __init__(self):
        self.calls = []

    async def raw_request(self, **kwargs):
        self.calls.append(kwargs)
        return {"payload": {"sources": [{"url": "https://cdn.example.test/voice.ogg"}]}}


@pytest.mark.asyncio
async def test_media_service_uses_raw_request_port_for_audio_probe(tmp_path):
    client = PortOnlyRawClient()
    service = MaxMediaService(
        MediaDeps(
            connection=ConnectionState(client=client),
            backend=SimpleNamespace(),
            tmp_dir=tmp_path,
            client_session_factory=lambda **_kwargs: None,
            raw_payload=SimpleNamespace(_safe_field_paths=lambda _payload: []),
        )
    )

    url, hard_stop = await service._probe_audio_download_payload(
        opcode=SimpleNamespace(name="AUDIO_GET_SOURCES", value=301),
        candidate="audio_get_sources",
        payload={"audioId": 42},
        chat_id="123",
        msg_id="456",
    )

    assert url == "https://cdn.example.test/voice.ogg"
    assert hard_stop is False
    assert client.calls[0]["opcode_name"] == "AUDIO_GET_SOURCES"
    assert client.calls[0]["default_opcode"] == 301


class PortOnlyInterceptorClient:
    def __init__(self):
        self.handler = None

    def install_raw_message_interceptor(self, handler):
        self.handler = handler
        return MaxRawInterceptorResult(installed=True, raw_handler_count=1)


def test_events_service_installs_raw_interceptor_through_port():
    client = PortOnlyInterceptorClient()
    service = MaxEventsService(
        EventsDeps(
            connection=ConnectionState(client=client),
            outbound=OutboundState(),
            handlers=[],
            backend=SimpleNamespace(),
            raw_payload=SimpleNamespace(),
            media=SimpleNamespace(),
            resolver=SimpleNamespace(),
            runtime=SimpleNamespace(),
        )
    )

    returned = service._install_raw_message_interceptor(client)

    assert returned is client
    assert client.handler == service._handle_raw_receive


def test_max_client_attachment_preserves_pydantic_extra_fields():
    class PydanticLikeAttachment:
        __pydantic_extra__ = {"targetUser": {"userId": 7001}}

        def model_dump(self, **kwargs):
            assert kwargs == {"by_alias": True, "exclude_none": True}
            return {"_type": "CONTROL", "event": "remove"}

    attachment = MaxClientAttachment.from_object(PydanticLikeAttachment())

    assert attachment.type == "CONTROL"
    assert attachment.event == "remove"
    assert attachment.targetUser == {"userId": 7001}
