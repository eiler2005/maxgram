"""Strict, transport-neutral parsing of MAX invite and public channel links."""

import re
from urllib.parse import urlsplit

_URL = re.compile(r"https?://[^\s<>\"']+|(?<![\w@/.-])(?:www\.)?max\.ru/[^\s<>\"']+", re.I)
_PATH = re.compile(r"/(?:join/)?[A-Za-z0-9_-]+/?\Z")
_RESERVED = {"join", "login", "settings", "profile", "download", "share", "app"}
_LINK_INPUT = re.compile(
    r"[a-z][a-z0-9+.-]*://[^\s<>\"']+|(?<![\w@/.-])(?:www\.)?max\.ru(?=$|[/?#\s])",
    re.I,
)


def has_join_link_input(text: str) -> bool:
    """Detect URL input for feedback only; never authorizes a MAX request."""
    return bool(_LINK_INPUT.search(text))


def normalize_max_link(value: str) -> str | None:
    value = value.strip().rstrip(".,;:!?)]}»”'")
    if len(value) > 2048:
        return None
    if value.lower().startswith(("max.ru/", "www.max.ru/")):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.netloc.lower() not in {"max.ru", "www.max.ru"}:
            return None
        if not _PATH.fullmatch(parsed.path):
            return None
        path = parsed.path.rstrip("/")
        if path.strip("/").lower() in _RESERVED:
            return None
    except ValueError:
        return None
    return "https://max.ru" + path


def extract_max_links(text: str) -> list[str]:
    """Extract distinct links, including links in forwarded message text."""
    return list(dict.fromkeys(
        normalized
        for match in _URL.finditer(text)
        if (normalized := normalize_max_link(match.group(0))) is not None
    ))
