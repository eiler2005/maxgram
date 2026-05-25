"""Shared retry policy for unstable bridge transports."""

from __future__ import annotations

import time

TEXT_RETRY_TTL_SECONDS = 48 * 60 * 60
TEXT_RETRY_POLL_SECONDS = 30
TEXT_RETRY_LEASE_SECONDS = 120


def exponential_backoff_seconds(
    attempts_after_failure: int,
    *,
    base_seconds: int,
    cap_seconds: int,
    max_exponent: int = 8,
) -> int:
    exponent = max(0, min(attempts_after_failure - 1, max_exponent))
    return min(cap_seconds, base_seconds * (2 ** exponent))


def is_expired(created_at: int | None, ttl_seconds: int, *, now: int | None = None) -> bool:
    if not created_at:
        return False
    now = int(time.time()) if now is None else now
    return now - int(created_at) >= ttl_seconds
