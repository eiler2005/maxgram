"""
MAX Adapter facade.

The public `MaxAdapter` API stays here. Runtime behavior is composed from
operation services over an internal MAX backend boundary; pymax is one backend
implementation, not a dependency of bridge/core.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable

from aiohttp import ClientSession

from .backends.base import MaxBackend
from .constants import (
    MAX_DOWNLOAD_ATTEMPTS,
    MAX_DOWNLOAD_CHUNK_SIZE,
    MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS,
    MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS,
    MAX_EMPTY_RECOVERY_RETRY_BASE_SECONDS,
    MAX_EMPTY_RECOVERY_RETRY_MAX_SECONDS,
    MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS,
    MAX_EMPTY_RECOVERY_STATE_FILE,
    MAX_HISTORY_SWEEP_DIAGNOSTIC_TTL_SECONDS,
    MAX_RAW_HISTORY_CACHE_SIZE,
    MAX_RAW_HISTORY_CACHE_TTL_SECONDS,
    MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS,
)
from .context import MaxAdapterContext
from .events import MaxEventsService
from .lifecycle import MaxLifecycleService
from .media.attachments import MaxMediaService
from .media.downloader import fix_filename_encoding
from .media.ua import (
    MAX_CDN_ANDROID_CHROME_USER_AGENT,
    MAX_CDN_CHROME_USER_AGENT,
    MAX_CDN_IOS_CHROME_USER_AGENT,
    MAX_CDN_USER_AGENT,
)
from .raw_payload import MaxRawPayloadService
from .recovery import MaxRecoveryService
from .resolve import MaxResolveService
from .runtime_state import MaxRuntimeService
from .send import MaxSendService
from .service_base import MaxServiceRegistry
from .state import MaxRuntimeState
from .types import ForwardedPayload, OutboundFailureState, PendingOutboundAck
from .voice_recovery import MaxVoiceRecoveryService
from ..max_session_store import MaxSessionStore
from ...bridge.contracts import (
    IssueHandler,
    MaxAttachment,
    MaxAttachmentFailure,
    MaxIssue,
    MaxMessage,
    MaxRecoveryChatSnapshot,
    MaxRecoveryContactSnapshot,
    MaxRecoverySnapshot,
    MessageHandler,
)

logger = logging.getLogger("src.adapters.max_adapter")


class MaxAdapter:
    def __init__(
        self,
        phone: str,
        data_dir: str,
        session_name: str,
        tmp_dir: str,
        *,
        backend: MaxBackend | None = None,
    ):
        normalized_session_name = Path(session_name).name
        tmp_path = Path(tmp_dir)
        if backend is None:
            from .backends.pymax import PymaxBackend

            backend = PymaxBackend(
                phone=phone,
                data_dir=data_dir,
                session_name=normalized_session_name,
            )
        state = MaxRuntimeState(
            phone=phone,
            data_dir=data_dir,
            session_name=normalized_session_name,
            session_store=MaxSessionStore(data_dir, normalized_session_name),
            backend=backend,
            tmp_dir=tmp_path,
            context=MaxAdapterContext(
                phone=phone,
                data_dir=data_dir,
                session_name=normalized_session_name,
                tmp_dir=tmp_path,
            ),
            client_session_factory=ClientSession,
        )
        services = MaxServiceRegistry(state=state)
        services.runtime = MaxRuntimeService(state, services)
        services.raw_payload = MaxRawPayloadService(state, services)
        services.voice_recovery = MaxVoiceRecoveryService(state, services)
        services.events = MaxEventsService(state, services)
        services.send = MaxSendService(state, services)
        services.resolver = MaxResolveService(state, services)
        services.media = MaxMediaService(state, services)
        services.recovery = MaxRecoveryService(state, services)
        services.lifecycle = MaxLifecycleService(state, services)

        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_services", services)
        services.overrides = {}
        for name, value in {
            name: value.__get__(self, type(self))
            for name, value in type(self).__dict__.items()
            if name.startswith("_")
            and not name.startswith("__")
            and callable(value)
            and name not in MaxAdapter.__dict__
        }.items():
            self._install_override(name, value)
        services.voice_recovery._load_pending_empty_recoveries()

    def __getattr__(self, name: str):
        return self._services.resolve(name)

    def __setattr__(self, name: str, value):
        if self._state.set_attr(name, value):
            return
        if name.startswith("_") and callable(value):
            self._install_override(name, value)
        object.__setattr__(self, name, value)

    def _install_override(self, name: str, value):
        self._services.overrides[name] = value
        for service in self._services.services():
            if getattr(type(service), name, None) is not None:
                object.__setattr__(service, name, value)

    @staticmethod
    def _fix_filename_encoding(name: str) -> str:
        return fix_filename_encoding(name)

    def on_message(self, handler: MessageHandler):
        self._state.handlers.append(handler)

    def on_start(self, handler: Callable):
        self._state.start_handlers.append(handler)

    def on_issue(self, handler: IssueHandler):
        self._state.issue_handlers.append(handler)

    async def start(self):
        return await self._services.lifecycle.start()

    def is_ready(self) -> bool:
        return self._services.lifecycle.is_ready()

    async def send_message(self, *args, **kwargs):
        return await self._services.send.send_message(*args, **kwargs)

    async def resolve_user_name(self, *args, **kwargs):
        return await self._services.resolver.resolve_user_name(*args, **kwargs)

    async def resolve_chat_title(self, *args, **kwargs):
        return await self._services.resolver.resolve_chat_title(*args, **kwargs)

    def get_own_id(self):
        return self._services.resolver.get_own_id()

    def find_user_by_name(self, *args, **kwargs):
        return self._services.resolver.find_user_by_name(*args, **kwargs)

    def get_dm_partner_id(self, *args, **kwargs):
        return self._services.resolver.get_dm_partner_id(*args, **kwargs)

    def get_last_outbound_error(self):
        return self._services.runtime.get_last_outbound_error()

    def get_last_outbound_attempts(self):
        return self._services.runtime.get_last_outbound_attempts()

    def get_last_start_error(self):
        return self._services.runtime.get_last_start_error()

    def get_last_issue(self):
        return self._services.runtime.get_last_issue()

    def get_last_connected_at(self):
        return self._services.runtime.get_last_connected_at()

    async def collect_recovery_snapshot(self):
        return await self._services.recovery.collect_recovery_snapshot()

    async def download_video_reference(self, *args, **kwargs):
        return await self._services.media.download_video_reference(*args, **kwargs)

    async def download_audio_reference(self, *args, **kwargs):
        return await self._services.media.download_audio_reference(*args, **kwargs)

    async def replay_recent_history(self, *args, **kwargs):
        return await self._services.voice_recovery.replay_recent_history(*args, **kwargs)

    def get_pending_empty_recovery_stats(self):
        return self._services.voice_recovery.get_pending_empty_recovery_stats()
