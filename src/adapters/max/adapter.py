"""
MAX Adapter facade.

The public `MaxAdapter` API stays here; transport-heavy behavior is split into
domain mixins under `src.adapters.max`.
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

from aiohttp import ClientSession

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
from .events import MaxEventsMixin
from .lifecycle import MaxLifecycleMixin
from .media.attachments import MaxAttachmentMixin
from .media.ua import (
    MAX_CDN_ANDROID_CHROME_USER_AGENT,
    MAX_CDN_CHROME_USER_AGENT,
    MAX_CDN_IOS_CHROME_USER_AGENT,
    MAX_CDN_USER_AGENT,
)
from .raw_payload import MaxRawPayloadMixin
from .recovery import MaxRecoveryMixin
from .resolve import MaxResolveMixin
from .runtime_state import MaxRuntimeStateMixin
from .send import MaxSendMixin
from .types import ForwardedPayload, OutboundFailureState, PendingOutboundAck
from .voice_recovery import MaxVoiceRecoveryMixin
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


class MaxAdapter(
    MaxRecoveryMixin,
    MaxRuntimeStateMixin,
    MaxRawPayloadMixin,
    MaxVoiceRecoveryMixin,
    MaxEventsMixin,
    MaxSendMixin,
    MaxResolveMixin,
    MaxAttachmentMixin,
    MaxLifecycleMixin,
):
    def __init__(self, phone: str, data_dir: str, session_name: str, tmp_dir: str):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = Path(session_name).name
        self._session_store = MaxSessionStore(data_dir, self._session_name)
        self._tmp_dir = Path(tmp_dir)
        self._context = MaxAdapterContext(
            phone=phone,
            data_dir=data_dir,
            session_name=self._session_name,
            tmp_dir=self._tmp_dir,
        )
        self._client = None
        self._handlers: list[MessageHandler] = []
        self._started = False
        self._start_handlers: list[Callable] = []
        self._issue_handlers: list[IssueHandler] = []
        self._own_id: Optional[str] = None
        self._pending_outbound_acks: list[PendingOutboundAck] = []
        self._expected_outbound_ids: dict[tuple[str, str], float] = {}
        self._raw_unwrapped_message_ids: dict[tuple[str, str], float] = {}
        self._interactive_ping_failure_limit = 3
        self._last_outbound_failure = OutboundFailureState(error=None, attempts=0)
        self._last_start_error: Optional[str] = None
        self._last_issue: Optional[MaxIssue] = None
        self._last_issue_notification_signature: Optional[str] = None
        self._last_connected_at: Optional[int] = None
        self._raw_processed_message_ids: dict[tuple[str, str], float] = {}
        self._raw_history_messages: dict[tuple[str, str], tuple[float, object]] = {}
        self._expected_raw_history_messages: dict[str, tuple[str, float]] = {}
        self._pending_empty_recovery_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._pending_empty_recoveries: dict[str, dict[str, object]] = {}
        self._pending_empty_recovery_worker: Optional[asyncio.Task] = None
        self._history_sweep_diagnostic_log_until: dict[tuple[str, str, str], float] = {}
        self._load_pending_empty_recoveries()

    def on_message(self, handler: MessageHandler):
        self._handlers.append(handler)

    def on_start(self, handler: Callable):
        self._start_handlers.append(handler)

    def on_issue(self, handler: IssueHandler):
        self._issue_handlers.append(handler)
