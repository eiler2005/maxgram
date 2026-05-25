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
from .deps import (
    EventsDeps,
    LifecycleDeps,
    MediaDeps,
    RawPayloadDeps,
    RecoveryDeps,
    ResolveDeps,
    RuntimeDeps,
    SendDeps,
    VoiceRecoveryDeps,
)
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
from .network import build_max_egress_profile
from .raw_payload import MaxRawPayloadService
from .recovery import MaxRecoveryService
from .resolve import MaxResolveService
from .runtime_state import MaxRuntimeService
from .send import MaxSendService
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
from ...logging_utils import log_event

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
        egress_config=None,
    ):
        normalized_session_name = Path(session_name).name
        tmp_path = Path(tmp_dir)
        egress = build_max_egress_profile(egress_config) if egress_config is not None else None
        if backend is None:
            from .backends.pymax import PymaxBackend

            backend = PymaxBackend(
                phone=phone,
                data_dir=data_dir,
                session_name=normalized_session_name,
                egress=egress,
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
            client_session_factory=lambda **kwargs: ClientSession(**kwargs),
        )
        runtime = MaxRuntimeService(
            RuntimeDeps(
                connection=state.connection,
                outbound=state.outbound,
                issue_handlers=state.issue_handlers,
            )
        )
        resolver = MaxResolveService(ResolveDeps(connection=state.connection))
        raw_payload_deps = RawPayloadDeps(
            connection=state.connection,
            raw_history=state.raw_history,
            backend=backend,
        )
        raw_payload = MaxRawPayloadService(raw_payload_deps)
        media = MaxMediaService(
            MediaDeps(
                connection=state.connection,
                backend=backend,
                tmp_dir=state.tmp_dir,
                client_session_factory=state.client_session_factory,
                egress=egress,
                raw_payload=raw_payload,
            )
        )
        raw_payload_deps.media = media
        voice_recovery_deps = VoiceRecoveryDeps(
            connection=state.connection,
            raw_history=state.raw_history,
            empty_recovery=state.empty_recovery,
            data_dir=state.data_dir,
            raw_payload=raw_payload,
        )
        voice_recovery = MaxVoiceRecoveryService(voice_recovery_deps)
        events = MaxEventsService(
            EventsDeps(
                connection=state.connection,
                outbound=state.outbound,
                handlers=state.handlers,
                backend=backend,
                raw_payload=raw_payload,
                media=media,
                resolver=resolver,
                runtime=runtime,
                voice_recovery=voice_recovery,
            )
        )
        voice_recovery_deps.events = events
        send = MaxSendService(
            SendDeps(
                connection=state.connection,
                outbound=state.outbound,
                backend=backend,
                runtime=runtime,
            )
        )
        recovery = MaxRecoveryService(
            RecoveryDeps(
                connection=state.connection,
                phone=state.phone,
                data_dir=state.data_dir,
                session_name=state.session_name,
                session_store=state.session_store,
                resolver=resolver,
            )
        )
        lifecycle = MaxLifecycleService(
            LifecycleDeps(
                connection=state.connection,
                backend=backend,
                phone=state.phone,
                start_handlers=state.start_handlers,
                interactive_ping_failure_limit=state.interactive_ping_failure_limit,
                runtime=runtime,
                recovery=recovery,
                events=events,
                voice_recovery=voice_recovery,
            )
        )

        self._state = state
        self._runtime = runtime
        self._raw_payload = raw_payload
        self._voice_recovery = voice_recovery
        self._events = events
        self._send = send
        self._resolver = resolver
        self._media = media
        self._recovery = recovery
        self._lifecycle = lifecycle
        self._egress = egress
        self._voice_recovery._load_pending_empty_recoveries()

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
        return await self._lifecycle.start()

    async def close(self):
        await self._voice_recovery.close()
        await self._lifecycle.close()

    def is_ready(self) -> bool:
        return self._lifecycle.is_ready()

    async def send_message(self, *args, **kwargs):
        return await self._send.send_message(*args, **kwargs)

    async def resolve_user_name(self, *args, **kwargs):
        return await self._resolver.resolve_user_name(*args, **kwargs)

    async def resolve_chat_title(self, *args, **kwargs):
        return await self._resolver.resolve_chat_title(*args, **kwargs)

    def get_own_id(self):
        return self._resolver.get_own_id()

    def find_user_by_name(self, *args, **kwargs):
        return self._resolver.find_user_by_name(*args, **kwargs)

    def get_dm_partner_id(self, *args, **kwargs):
        return self._resolver.get_dm_partner_id(*args, **kwargs)

    def get_last_outbound_error(self):
        return self._runtime.get_last_outbound_error()

    def get_last_outbound_attempts(self):
        return self._runtime.get_last_outbound_attempts()

    def get_last_start_error(self):
        return self._runtime.get_last_start_error()

    def get_last_issue(self):
        return self._runtime.get_last_issue()

    def get_last_connected_at(self):
        return self._runtime.get_last_connected_at()

    def get_egress_status(self) -> dict[str, object] | None:
        if self._egress is None:
            return None
        status = self._egress.safe_log_fields()
        if self._egress.is_non_ru_warning:
            status["warning"] = "MAX uses non-RU direct egress"
        return status

    def get_last_egress_probe(self) -> dict[str, object] | None:
        return self._state.connection.last_egress_probe

    async def probe_egress(self) -> dict[str, object] | None:
        if self._egress is None:
            return None
        result = await asyncio.to_thread(self._egress.probe)
        self._state.connection.last_egress_probe = result
        log_event(
            logger,
            logging.INFO if result.get("ok") else logging.WARNING,
            "max.egress.probe",
            stage=str(result.get("stage") or "unknown"),
            outcome="ok" if result.get("ok") else "failed",
            max_egress_active=result.get("max_egress_active"),
            max_egress_type=result.get("max_egress_type"),
            max_egress_proxy_host=result.get("max_egress_proxy_host"),
            target_host=result.get("target_host"),
            target_port=result.get("target_port"),
            latency_ms=result.get("latency_ms"),
            error=result.get("error"),
        )
        return result

    async def collect_recovery_snapshot(self):
        return await self._recovery.collect_recovery_snapshot()

    async def download_video_reference(self, *args, **kwargs):
        return await self._media.download_video_reference(*args, **kwargs)

    async def download_audio_reference(self, *args, **kwargs):
        return await self._media.download_audio_reference(*args, **kwargs)

    async def replay_recent_history(self, *args, **kwargs):
        return await self._voice_recovery.replay_recent_history(*args, **kwargs)

    def get_pending_empty_recovery_stats(self):
        return self._voice_recovery.get_pending_empty_recovery_stats()
