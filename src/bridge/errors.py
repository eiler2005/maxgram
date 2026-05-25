"""Typed bridge-domain exceptions."""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for bridge-domain failures."""


class BridgeTransientError(BridgeError):
    """Failure that may succeed on retry."""


class BridgePermanentError(BridgeError):
    """Failure that requires code/config/operator intervention."""


class BridgeExternalTimeout(BridgeTransientError, TimeoutError):
    """External TG/MAX operation exceeded its bounded wait time."""

    def __init__(self, *, operation: str, timeout_seconds: int | float):
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{operation} timed out after {timeout_seconds}s")
