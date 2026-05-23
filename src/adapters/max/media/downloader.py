"""Pymax-free MAX media download helpers."""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from aiohttp import ClientResponseError

from .. import constants as max_constants
from ....logging_utils import log_event, sanitize_path, sanitize_url
from .ua import download_client_profile_for_url

logger = logging.getLogger("src.adapters.max_adapter")


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


class MaxCdnDownloader:
    def __init__(
        self,
        *,
        tmp_dir: Path,
        client_session_factory: Callable[..., Any],
        egress: Any | None = None,
    ):
        self._tmp_dir = tmp_dir
        self._client_session_factory = client_session_factory
        self._egress = egress

    async def write_download_response(self, response, part_path: Path, mode: str) -> int:
        written = 0
        with part_path.open(mode) as fh:
            stream = getattr(getattr(response, "content", None), "iter_chunked", None)
            if callable(stream):
                async for chunk in stream(max_constants.get("MAX_DOWNLOAD_CHUNK_SIZE")):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
            else:
                content = await response.read()
                fh.write(content)
                written += len(content)
        return written

    async def download_from_url(
        self,
        url: str,
        prefix: str,
        filename_hint: Optional[str] = None,
        default_extension: str = "",
        expected_kind: Optional[str] = None,
        flow_id: Optional[str] = None,
        download_source: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        filename = build_filename(prefix, filename_hint, url, None, default_extension)
        local_path = self._tmp_dir / filename
        part_path = self._tmp_dir / f"{filename}.part"
        last_error: Exception | None = None
        content_type: Optional[str] = None

        for attempt in range(1, max_constants.get("MAX_DOWNLOAD_ATTEMPTS") + 1):
            resume_from = part_path.stat().st_size if part_path.exists() else 0
            headers, src_ag, ua_family = download_client_profile_for_url(url)
            if resume_from:
                headers = {**headers, "Range": f"bytes={resume_from}-"}

            try:
                session_kwargs: dict[str, Any] = {"headers": headers}
                if self._egress is not None:
                    session_kwargs.update(
                        self._egress.http_client_options.as_client_session_kwargs()
                    )
                async with self._client_session_factory(**session_kwargs) as session:
                    async with session.get(url) as response:
                        http_status = getattr(response, "status", None)
                        if resume_from and getattr(response, "status", None) == 200:
                            part_path.unlink(missing_ok=True)
                            resume_from = 0
                            log_event(
                                logger,
                                logging.INFO,
                                "max.attachment.download_resume",
                                flow_id=flow_id,
                                direction="inbound",
                                stage="download",
                                outcome="unsupported",
                                source=sanitize_url(url),
                                download_source=download_source,
                                src_ag=src_ag,
                                ua_family=ua_family,
                                http_status=http_status,
                                attempt=attempt,
                            )

                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None
                        mode = "ab" if resume_from and getattr(response, "status", None) == 206 else "wb"
                        bytes_written = await self.write_download_response(response, part_path, mode)

                if bytes_written <= 0 and not part_path.exists():
                    raise RuntimeError("download returned no content")

                content = part_path.read_bytes()
                detected_kind = classify_downloaded_content(content_type, content)
                if not is_download_valid(expected_kind, detected_kind):
                    part_path.unlink(missing_ok=True)
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.download",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="rejected",
                        reason="download_rejected",
                        expected_kind=expected_kind,
                        detected_kind=detected_kind,
                        content_type=content_type,
                        source=sanitize_url(url),
                        download_source=download_source,
                        src_ag=src_ag,
                        ua_family=ua_family,
                        http_status=http_status,
                        attempts=attempt,
                    )
                    return None, None

                part_path.replace(local_path)
                log_event(
                    logger,
                    logging.INFO,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="downloaded",
                    expected_kind=expected_kind,
                    detected_kind=detected_kind,
                    content_type=content_type,
                    source=sanitize_url(url),
                    download_source=download_source,
                    src_ag=src_ag,
                    ua_family=ua_family,
                    http_status=http_status,
                    filename=sanitize_path(filename),
                    size_bytes=local_path.stat().st_size,
                    attempts=attempt,
                    resumed=attempt > 1 or bool(resume_from),
                )
                return str(local_path), filename
            except Exception as e:
                last_error = e
                retryable = is_retryable_download_error(e)
                if retryable and attempt < max_constants.get("MAX_DOWNLOAD_ATTEMPTS"):
                    retry_in_seconds = download_retry_delay(attempt)
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.download_retry",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="download_failed",
                        expected_kind=expected_kind,
                        source=sanitize_url(url),
                        download_source=download_source,
                        src_ag=src_ag,
                        ua_family=ua_family,
                        http_status=download_error_status(e),
                        error=download_error_for_log(e),
                        attempt=attempt,
                        max_attempts=max_constants.get("MAX_DOWNLOAD_ATTEMPTS"),
                        resume_from_bytes=part_path.stat().st_size if part_path.exists() else 0,
                        retry_in_seconds=retry_in_seconds,
                    )
                    await asyncio.sleep(retry_in_seconds)
                    continue

                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="download_failed",
                    expected_kind=expected_kind,
                    source=sanitize_url(url),
                    download_source=download_source,
                    src_ag=src_ag,
                    ua_family=ua_family,
                    http_status=download_error_status(e),
                    error=download_error_for_log(e),
                    attempts=attempt,
                    max_attempts=max_constants.get("MAX_DOWNLOAD_ATTEMPTS"),
                    retryable=retryable,
                    resume_from_bytes=part_path.stat().st_size if part_path.exists() else 0,
                )
                break

        if last_error is not None:
            part_path.unlink(missing_ok=True)
        return None, None
