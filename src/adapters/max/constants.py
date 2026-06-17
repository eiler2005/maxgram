"""MAX adapter runtime constants."""

import sys
from typing import Any

MAX_DOWNLOAD_ATTEMPTS = 7
MAX_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAX_RAW_HISTORY_CACHE_TTL_SECONDS = 180
MAX_RAW_HISTORY_CACHE_SIZE = 256
MAX_RAW_HISTORY_EXPECTED_TTL_SECONDS = 30
MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS = 180
MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS = 1.0
MAX_DEGRADED_MEDIA_RECOVERY_WAIT_SECONDS = 8.0
MAX_DEGRADED_MEDIA_RECOVERY_POLL_SECONDS = 0.2
MAX_EMPTY_RECOVERY_RETRY_POLL_SECONDS = 30
MAX_EMPTY_RECOVERY_RETRY_BASE_SECONDS = 60
MAX_EMPTY_RECOVERY_RETRY_MAX_SECONDS = 6 * 60 * 60
MAX_EMPTY_RECOVERY_STATE_FILE = "pending_empty_recoveries.json"
MAX_HISTORY_SWEEP_DIAGNOSTIC_TTL_SECONDS = 10 * 60


def get(name: str) -> Any:
    """Read constants through compatibility modules when tests monkeypatch them."""
    for module_name in ("src.adapters.max_adapter", "src.adapters.max.adapter"):
        module = sys.modules.get(module_name)
        if module is not None and name in module.__dict__:
            return module.__dict__[name]
    return globals()[name]
