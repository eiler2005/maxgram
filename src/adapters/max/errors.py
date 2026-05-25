"""Pymax-free MAX adapter error classification helpers."""

import asyncio
from typing import Optional

from ...bridge.contracts import MaxIssue


class MaxError(Exception):
    """Base class for MAX adapter/domain failures."""


class MaxTransientError(MaxError):
    """Failure that may succeed after reconnect/retry."""


class MaxPermanentError(MaxError):
    """Failure that requires code/config/operator intervention."""


class MaxRuntimeContractError(MaxPermanentError):
    """The active MAX backend no longer matches the expected runtime shape."""


class MaxEgressUnavailable(MaxTransientError):
    """Raised when the configured MAX egress path cannot open a tunnel."""


PYMAX_TCP_SEQUENCE_OVERFLOW_KIND = "pymax_tcp_sequence_overflow"
PYMAX_TCP_SEQUENCE_OVERFLOW_ERROR = (
    "pymax_tcp_sequence_overflow: PyMax TCP seq exceeded 255"
)


def _error_text_with_context(error: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip() or current.__class__.__name__
        parts.append(text)
        current = current.__cause__ or current.__context__
    return " | ".join(parts)


def classify_runtime_error(error: BaseException) -> Optional[MaxIssue]:
    raw_error = str(error).strip() or error.__class__.__name__
    lowered = _error_text_with_context(error).lower()

    if is_pymax_tcp_sequence_overflow_error(error):
        return MaxIssue(
            kind=PYMAX_TCP_SEQUENCE_OVERFLOW_KIND,
            summary="PyMax TCP seq вышел за one-byte лимит MAX protocol",
            raw_error=raw_error,
            requires_reauth=False,
        )

    if isinstance(error, MaxEgressUnavailable) or "max egress proxy" in lowered:
        return MaxIssue(
            kind="max_egress_unavailable",
            summary="MAX egress proxy недоступен",
            raw_error=raw_error,
            requires_reauth=False,
        )

    if "max client start returned before on_start" in lowered:
        return MaxIssue(
            kind="max_start_incomplete",
            summary="MAX client завершился до on_start/ONLINE",
            raw_error=raw_error,
            requires_reauth=False,
        )

    corrupt_session_markers = (
        "unsupported file format",
        "database disk image is malformed",
    )
    if any(marker in lowered for marker in corrupt_session_markers):
        return MaxIssue(
            kind="session_corrupt",
            summary="MAX session.db повреждён или не читается",
            raw_error=raw_error,
            requires_reauth=True,
        )

    invalid_token_markers = (
        "invalid token",
        "login.token",
        "fail_login_token",
        "fail_logout_all",
        "авторизируйтесь снова",
        "please, login again",
    )
    if any(marker in lowered for marker in invalid_token_markers):
        return MaxIssue(
            kind="session_invalid",
            summary="MAX сессия недействительна, нужна повторная авторизация",
            raw_error=raw_error,
            requires_reauth=True,
        )

    if "must be online session" in lowered or "недопустимое состояние сессии" in lowered:
        return MaxIssue(
            kind="session_offline",
            summary="MAX сессия не перешла в ONLINE-состояние",
            raw_error=raw_error,
            requires_reauth=False,
        )

    return None


def is_retryable_send_error(error: BaseException) -> bool:
    if isinstance(error, MaxTransientError):
        return True
    if isinstance(
        error,
        (
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionError,
            BrokenPipeError,
            ConnectionResetError,
        ),
    ):
        return True

    error_text = str(error).lower()
    retryable_markers = (
        "socket is not connected",
        "must be online session",
        "недопустимое состояние сессии",
        "broken pipe",
        "connection reset",
        "no route to host",
        "network is unreachable",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "tlsv1 alert",
        "ssl:",
    )
    return any(marker in error_text for marker in retryable_markers)


def is_pymax_tcp_sequence_overflow_error(error: BaseException) -> bool:
    return "'b' format requires 0 <= number <= 255" in _error_text_with_context(error).lower()


def safe_send_error(error: BaseException) -> str:
    if is_pymax_tcp_sequence_overflow_error(error):
        return PYMAX_TCP_SEQUENCE_OVERFLOW_ERROR
    return str(error).strip() or error.__class__.__name__


def is_socket_probe_error(exc: Exception) -> bool:
    return exc.__class__.__name__ in {
        "SocketSendError",
        "SocketNotConnectedError",
        "WebSocketNotConnectedError",
    }
