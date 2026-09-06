"""Small, transport-neutral context labels for rendered MAX messages."""

from typing import Optional


def forwarded_context_marker(source_title: Optional[str]) -> str:
    """Render a MAX-forward marker with an optional cache-derived source title."""
    normalized_title = " ".join((source_title or "").split())
    if normalized_title:
        return f"↪️ Переслано из «{normalized_title}»"
    return "↪️ Переслано из MAX"
