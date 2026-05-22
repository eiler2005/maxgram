"""Runtime context for the MAX adapter facade."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MaxAdapterContext:
    phone: str
    data_dir: str
    session_name: str
    tmp_dir: Path
