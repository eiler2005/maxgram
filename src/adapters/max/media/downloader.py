"""Pymax-free MAX media download helpers."""

import mimetypes
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from aiohttp import ClientResponseError


CONTENT_TYPE_EXTENSIONS = {
    "audio/ogg": ".oga",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "image/jpeg": ".jpg",
}


def fix_filename_encoding(name: str) -> str:
    try:
        fixed = name.encode("latin-1").decode("cp1251")
        return fixed if fixed != name else name
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def build_filename(
    prefix: str,
    filename_hint: Optional[str],
    url: Optional[str],
    content_type: Optional[str],
    default_extension: str = "",
) -> str:
    base_name = Path(filename_hint).name if filename_hint else ""
    stem = Path(base_name).stem if base_name else prefix
    suffix = Path(base_name).suffix

    if not suffix and url:
        suffix = Path(urlparse(url).path).suffix

    if not suffix and content_type:
        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        guessed = CONTENT_TYPE_EXTENSIONS.get(normalized_content_type)
        if guessed is None:
            guessed = mimetypes.guess_extension(normalized_content_type)
        if guessed == ".jpe":
            guessed = ".jpg"
        suffix = guessed or ""

    if not suffix and default_extension:
        suffix = default_extension if default_extension.startswith(".") else f".{default_extension}"

    return f"{stem}{suffix}" if suffix else stem


def extract_video_url(value, *, key_hint: Optional[str] = None) -> Optional[str]:
    candidates: list[tuple[int, str]] = []

    def score_url(url: str, key: Optional[str]) -> int:
        score = 0
        lowered_url = url.lower()
        lowered_key = (key or "").lower()

        if lowered_key in {"url", "src", "source"} or lowered_key.isdigit():
            score += 4
        if "video" in lowered_key or "stream" in lowered_key:
            score += 3
        if "mp4" in lowered_key or "m3u8" in lowered_key or "hls" in lowered_key:
            score += 6
        if any(resolution in lowered_key for resolution in ("144", "240", "360", "480", "720", "1080", "1440", "2160")):
            score += 2
        if any(ext in lowered_url for ext in (".mp4", ".mov", ".m4v", ".webm", ".m3u8")):
            score += 5
        if lowered_key == "external":
            score -= 12
        if any(marker in lowered_key for marker in ("thumbnail", "thumb", "preview")):
            score -= 5
        if "m.ok.ru/video/" in lowered_url or "ok.ru/video/" in lowered_url:
            score -= 8
        if any(ext in lowered_url for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            score -= 6

        return score

    def walk(node, key: Optional[str] = None):
        if isinstance(node, str):
            if node.startswith(("http://", "https://")):
                candidates.append((score_url(node, key), node))
            return

        if isinstance(node, dict):
            for nested_key, nested_value in node.items():
                walk(nested_value, str(nested_key))
            return

        if isinstance(node, (list, tuple, set)):
            for nested_value in node:
                walk(nested_value, key)
            return

        url_attr = getattr(node, "url", None)
        if url_attr is not None and url_attr is not node:
            walk(url_attr, "url")

        if hasattr(node, "__dict__"):
            walk(vars(node), key)

    walk(value, key_hint)
    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def extract_audio_url(value, *, key_hint: Optional[str] = None) -> Optional[str]:
    candidates: list[tuple[int, str]] = []

    def score_url(url: str, key: Optional[str]) -> int:
        score = 0
        lowered_url = url.lower()
        lowered_key = (key or "").lower()

        if lowered_key in {"url", "src", "source", "download"} or lowered_key.isdigit():
            score += 4
        if any(marker in lowered_key for marker in ("audio", "voice", "file", "media")):
            score += 5
        if any(ext in lowered_key for ext in ("ogg", "opus", "mp3", "m4a", "aac", "wav")):
            score += 6
        if any(ext in lowered_url for ext in (".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav")):
            score += 6
        if any(marker in lowered_key for marker in ("thumb", "preview", "image", "photo")):
            score -= 6
        if any(ext in lowered_url for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            score -= 8
        if "video" in lowered_key or any(ext in lowered_url for ext in (".mp4", ".mov", ".m3u8")):
            score -= 4

        return score

    def walk(node, key: Optional[str] = None):
        if isinstance(node, str):
            if node.startswith(("http://", "https://")):
                candidates.append((score_url(node, key), node))
            return
        if isinstance(node, dict):
            for nested_key, nested_value in node.items():
                walk(nested_value, str(nested_key))
            return
        if isinstance(node, (list, tuple, set)):
            for nested_value in node:
                walk(nested_value, key)
            return
        url_attr = getattr(node, "url", None)
        if url_attr is not None and url_attr is not node:
            walk(url_attr, "url")
        if hasattr(node, "__dict__"):
            walk(vars(node), key)

    walk(value, key_hint)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def detect_magic_type(content: bytes) -> str:
    if not content:
        return "unknown"

    head = content[:64]

    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if head.startswith(b"RIFF") and b"WEBP" in content[8:16]:
        return "image"

    if len(content) > 12 and content[4:8] == b"ftyp":
        return "video"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"

    if head.startswith(b"OggS"):
        return "audio"
    if head.startswith(b"ID3"):
        return "audio"

    if head.startswith(b"%PDF"):
        return "document"
    if head.startswith(b"PK\x03\x04"):
        return "document"

    lowered = content[:256].lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "html"
    return "unknown"


def classify_downloaded_content(content_type: Optional[str], content: bytes) -> str:
    magic_type = detect_magic_type(content)
    if magic_type != "unknown":
        return magic_type

    normalized = str(content_type or "").lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("text/html"):
        return "html"
    if normalized.startswith("text/"):
        return "text"
    if normalized.startswith("application/"):
        return "document"
    return "unknown"


def is_download_valid(expected_kind: Optional[str], detected_kind: str) -> bool:
    if detected_kind == "html":
        return False

    expected = str(expected_kind or "").lower()
    if not expected:
        if detected_kind == "text":
            return False
        return True

    if detected_kind == "text":
        return expected == "document"

    expected_map = {
        "photo": {"image"},
        "video": {"video"},
        "audio": {"audio"},
        "document": {"document", "image", "video", "audio", "unknown"},
    }
    allowed = expected_map.get(expected)
    if not allowed:
        return True
    if detected_kind in allowed:
        return True
    if detected_kind == "unknown":
        return True
    return False


def is_retryable_download_error(error: Exception) -> bool:
    if isinstance(error, ClientResponseError):
        return error.status not in {401, 403, 404, 410}
    return True


def download_error_for_log(error: Exception) -> str:
    if isinstance(error, ClientResponseError):
        message = str(error.message or "").strip()
        return f"HTTP {error.status}" + (f" {message}" if message else "")
    return str(error)


def download_error_status(error: Exception) -> Optional[int]:
    if isinstance(error, ClientResponseError):
        return error.status
    return None


def download_retry_delay(attempt: int) -> int:
    return min(2 ** (attempt - 1), 8)
