import asyncio
import json
import logging
import struct
import time
from types import SimpleNamespace

from aiohttp import ClientResponseError
import msgpack
import pytest

from src.adapters.max_adapter import (
    MAX_CDN_ANDROID_CHROME_USER_AGENT,
    MAX_CDN_CHROME_USER_AGENT,
    MAX_CDN_IOS_CHROME_USER_AGENT,
    MAX_CDN_USER_AGENT,
    MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
    MaxAttachment,
    MaxAdapter as RealMaxAdapter,
)
from src.adapters.max.ports import (
    MaxChatView,
    MaxClientMessage,
    MaxDialogView,
    MaxRawInterceptorResult,
    MaxSendResult,
    MaxUserView,
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
        self.contacts = []
        self.me = SimpleNamespace(id=161361072)

    def get_cached_user(self, user_id: int):
        return self._users.get(user_id)

    async def get_users(self, user_ids: list[int]):
        return [self._users[uid] for uid in user_ids if uid in self._users]

    def cached_user(self, user_id: int):
        return MaxUserView.from_object(self.get_cached_user(user_id))

    async def load_users(self, user_ids: list[int]):
        return [
            item
            for user in await self.get_users(user_ids)
            if (item := MaxUserView.from_object(user))
        ]

    def contacts_snapshot(self):
        return [
            item
            for contact in self.contacts
            if (item := MaxUserView.from_object(contact))
        ]

    def users_cache_snapshot(self):
        return {
            key: item
            for key, user in self._users.items()
            if (item := MaxUserView.from_object(user))
        }

    def dialogs_snapshot(self):
        return [
            item
            for dialog in getattr(self, "dialogs", [])
            if (item := MaxDialogView.from_object(dialog))
        ]

    def group_chats_snapshot(self):
        return [
            item
            for chat in self.chats
            if (item := MaxChatView.from_object(chat))
        ]

    def channels_snapshot(self):
        return [
            item
            for channel in getattr(self, "channels", [])
            if (item := MaxChatView.from_object(channel))
        ]

    async def chat(self, chat_id: int):
        get_chat = getattr(self, "get_chat", None)
        if get_chat is None:
            return None
        return MaxChatView.from_object(await get_chat(chat_id))

    def own_user_id(self):
        value = getattr(getattr(self, "me", None), "id", None)
        return str(value) if value is not None else None

    def dialog_last_message(self, chat_id: int):
        for dialog in self.dialogs_snapshot():
            if getattr(dialog, "id", None) == chat_id:
                return dialog.last_message
        return None

    async def send_outbound_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ):
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            kwargs["reply_to"] = reply_to
        if media_path:
            kwargs["attachment"] = SimpleNamespace(path=media_path, media_type=media_type)
        result = await self.send_message(**kwargs)
        return MaxSendResult(message_id=self._extract_result_msg_id(result), raw=result)

    async def raw_request(
        self,
        *,
        opcode_name: str,
        payload: dict,
        default_opcode: int | None = None,
        timeout=None,
        cmd=None,
    ):
        send = getattr(self, "_send_and_wait", None)
        if send is None:
            return None
        opcode = SimpleNamespace(name=opcode_name, value=default_opcode)
        kwargs = {"opcode": opcode, "payload": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if cmd is not None:
            kwargs["cmd"] = cmd
        return await send(**kwargs)

    async def file_url(self, *, chat_id: int, message_id: int, file_id: int):
        get_file = getattr(self, "get_file_by_id", None)
        if get_file is None:
            return None
        file_obj = await get_file(chat_id=chat_id, message_id=message_id, file_id=file_id)
        return getattr(file_obj, "url", None)

    async def video_payload(self, *, chat_id: int, message_id: int, video_id: int):
        data = await self.raw_request(
            opcode_name="VIDEO_PLAY",
            payload={"chatId": chat_id, "messageId": message_id, "videoId": video_id},
        )
        payload = data.get("payload") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else None

    async def raw_history_payload(self, *, chat_id: int, from_time: int, forward: int, backward: int):
        data = await self.raw_request(
            opcode_name="CHAT_HISTORY",
            default_opcode=49,
            payload={
                "chatId": chat_id,
                "from": from_time,
                "forward": forward,
                "backward": backward,
            },
            timeout=10,
        )
        payload = data.get("payload") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else None

    async def history_messages(self, *, chat_id: int, from_time: int, forward: int, backward: int):
        fetch = getattr(self, "fetch_history", None)
        if fetch is None:
            return []
        return [
            MaxClientMessage.from_object(message)
            for message in await fetch(chat_id, from_time=from_time, forward=forward, backward=backward)
        ]

    def install_raw_message_interceptor(self, handler):
        if getattr(self, "_maxtg_raw_interceptor_installed", False):
            return MaxRawInterceptorResult(
                installed=True,
                raw_handler_count=len(getattr(self, "_on_raw_receive_handlers", []) or []),
            )
        original = getattr(self, "_handle_message_notifications", None)
        if original is None:
            return MaxRawInterceptorResult(
                installed=False,
                reason="client_has_no_message_notification_handler",
            )

        async def wrapped(data):
            await handler(data)
            return await original(data)

        self._handle_message_notifications = wrapped
        self._maxtg_raw_interceptor_installed = True
        return MaxRawInterceptorResult(
            installed=True,
            raw_handler_count=len(getattr(self, "_on_raw_receive_handlers", []) or []),
        )

    def _extract_result_msg_id(self, result):
        direct_id = getattr(result, "id", None) or getattr(result, "message_id", None)
        if direct_id is not None:
            return str(direct_id)
        if isinstance(result, dict):
            for key in ("id", "messageId", "message_id"):
                if result.get(key) is not None:
                    return str(result[key])
        return None


class RecoveryClient(LookupClient):
    def __init__(self):
        self.me = SimpleNamespace(id=100)
        self._users = {
            300: make_user("DM", "Partner"),
            500: make_user("Group", "Owner"),
            501: make_user("Group", "Admin"),
        }
        self.chats = [
            SimpleNamespace(
                id=-1,
                title="Cached group",
                type="CHAT",
                access="LINK",
            )
        ]
        self.channels = [
            SimpleNamespace(
                id=-2,
                title="Channel",
                type="CHANNEL",
                participants_count=42,
            )
        ]
        self.dialogs = [SimpleNamespace(id=300, participants={100: None, 300: None})]
        self.contacts = [SimpleNamespace(id=999, names=[SimpleNamespace(name="Address Book Only")])]
        self._enriched = {
            -1: SimpleNamespace(
                id=-1,
                title="Enriched group",
                type="CHAT",
                access="LINK",
                link="https://max.ru/join/example",
                owner=500,
                admins=[501],
                participants_count=9,
            )
        }

    def get_cached_user(self, user_id: int):
        return self._users.get(user_id)

    async def get_users(self, user_ids: list[int]):
        return [self._users[uid] for uid in user_ids if uid in self._users]

    async def get_chat(self, chat_id: int):
        return self._enriched.get(chat_id)


class AdapterHarness:
    def __init__(self, *args, **kwargs):
        self._adapter = RealMaxAdapter(*args, **kwargs)

    def __setattr__(self, name, value):
        if name in {"_download_from_url", "_download_file_by_id", "_download_video_by_id"} and "_adapter" in self.__dict__:
            setattr(self._adapter._media, name, value)
            return
        if name == "_make_client" and "_adapter" in self.__dict__:
            setattr(self._adapter._lifecycle, name, value)
            return
        object.__setattr__(self, name, value)

    def on_message(self, handler):
        return self._adapter.on_message(handler)

    def on_issue(self, handler):
        return self._adapter.on_issue(handler)

    async def start(self):
        return await self._adapter.start()

    def is_ready(self):
        return self._adapter.is_ready()

    async def send_message(self, *args, **kwargs):
        return await self._adapter.send_message(*args, **kwargs)

    async def collect_recovery_snapshot(self):
        return await self._adapter.collect_recovery_snapshot()

    async def download_video_reference(self, *args, **kwargs):
        return await self._adapter.download_video_reference(*args, **kwargs)

    async def download_audio_reference(self, *args, **kwargs):
        return await self._adapter.download_audio_reference(*args, **kwargs)

    async def replay_recent_history(self, *args, **kwargs):
        return await self._adapter.replay_recent_history(*args, **kwargs)

    def get_last_outbound_error(self):
        return self._adapter.get_last_outbound_error()

    def get_last_outbound_attempts(self):
        return self._adapter.get_last_outbound_attempts()

    def get_last_start_error(self):
        return self._adapter.get_last_start_error()

    def get_last_issue(self):
        return self._adapter.get_last_issue()

    def get_pending_empty_recovery_stats(self):
        return self._adapter.get_pending_empty_recovery_stats()

    @property
    def _client(self):
        return self._adapter._state.connection.client

    @_client.setter
    def _client(self, value):
        self._adapter._state.connection.client = value

    @property
    def _own_id(self):
        return self._adapter._state.connection.own_id

    @_own_id.setter
    def _own_id(self, value):
        self._adapter._state.connection.own_id = value

    @property
    def _started(self):
        return self._adapter._state.connection.started

    @_started.setter
    def _started(self, value):
        self._adapter._state.connection.started = value

    @property
    def _pending_empty_recoveries(self):
        return self._adapter._state.empty_recovery.pending_empty_recoveries

    @property
    def _pending_empty_recovery_tasks(self):
        return self._adapter._state.empty_recovery.pending_empty_recovery_tasks

    @property
    def _raw_history_messages(self):
        return self._adapter._state.raw_history.raw_history_messages

    async def _handle_raw_message(self, *args, **kwargs):
        return await self._adapter._events._handle_raw_message(*args, **kwargs)

    async def _handle_raw_receive(self, *args, **kwargs):
        return await self._adapter._events._handle_raw_receive(*args, **kwargs)

    def _install_raw_message_interceptor(self, *args, **kwargs):
        return self._adapter._events._install_raw_message_interceptor(*args, **kwargs)

    async def _make_client(self, *args, **kwargs):
        return await self._adapter._lifecycle._make_client(*args, **kwargs)

    def _build_failfast_interactive_ping(self, *args, **kwargs):
        return self._adapter._lifecycle._build_failfast_interactive_ping(*args, **kwargs)

    def _classify_runtime_error(self, *args, **kwargs):
        return self._adapter._runtime._classify_runtime_error(*args, **kwargs)

    def _remember_runtime_issue(self, *args, **kwargs):
        return self._adapter._runtime._remember_runtime_issue(*args, **kwargs)

    async def _emit_runtime_issue(self, *args, **kwargs):
        return await self._adapter._runtime._emit_runtime_issue(*args, **kwargs)

    def _remember_pending_empty_recovery(self, *args, **kwargs):
        return self._adapter._voice_recovery._remember_pending_empty_recovery(*args, **kwargs)

    async def _attempt_pending_empty_recovery(self, *args, **kwargs):
        return await self._adapter._voice_recovery._attempt_pending_empty_recovery(*args, **kwargs)

    def _download_headers_for_url(self, *args, **kwargs):
        return self._adapter._media._download_headers_for_url(*args, **kwargs)

    def _extract_video_url(self, *args, **kwargs):
        return self._adapter._media._extract_video_url(*args, **kwargs)

    async def _download_video_by_id(self, *args, **kwargs):
        return await self._adapter._media._download_video_by_id(*args, **kwargs)

    async def _download_from_url(self, *args, **kwargs):
        return await self._adapter._media._download_from_url(*args, **kwargs)

    async def _download_attachment(self, *args, **kwargs):
        return await self._adapter._media._download_attachment(*args, **kwargs)

    def _attachment_type_name(self, *args, **kwargs):
        return self._adapter._media._attachment_type_name(*args, **kwargs)

    def _normalize_attachment_type(self, *args, **kwargs):
        return self._adapter._media._normalize_attachment_type(*args, **kwargs)

    def _attachment_filename(self, *args, **kwargs):
        return self._adapter._media._attachment_filename(*args, **kwargs)

    def _duration_seconds(self, *args, **kwargs):
        return self._adapter._media._duration_seconds(*args, **kwargs)

    def _safe_attachment_field_names(self, *args, **kwargs):
        return self._adapter._media._safe_attachment_field_names(*args, **kwargs)

    async def resolve_user_name(self, *args, **kwargs):
        return await self._adapter.resolve_user_name(*args, **kwargs)


class DummyDownloadAdapter(AdapterHarness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._adapter._events._deps.media = self

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


class CapturingAttachmentDownloadAdapter(AdapterHarness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.url_downloads = []
        self.file_downloads = []
        self.url_result = (None, None)
        self.file_result = (None, None)
        self._adapter._media._download_from_url = self._download_from_url
        self._adapter._media._download_file_by_id = self._download_file_by_id
        self._adapter._events._deps.media = self._adapter._media

    async def _download_from_url(
        self,
        url: str,
        prefix: str,
        filename_hint=None,
        default_extension: str = "",
        expected_kind=None,
        flow_id=None,
        download_source=None,
    ):
        self.url_downloads.append(
            (url, prefix, filename_hint, default_extension, expected_kind, download_source)
        )
        return self.url_result

    async def _download_file_by_id(
        self,
        chat_id: str,
        msg_id: str,
        file_id: int,
        prefix: str,
        filename_hint=None,
        default_extension: str = "",
        expected_kind=None,
        flow_id=None,
    ):
        self.file_downloads.append(
            (chat_id, msg_id, file_id, prefix, filename_hint, default_extension, expected_kind)
        )
        return self.file_result


__all__ = [name for name in globals() if not name.startswith("_")]
