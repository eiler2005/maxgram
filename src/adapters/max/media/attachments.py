from __future__ import annotations

import json
import logging
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from .. import errors as max_errors
from .. import payload as max_payload
from ..deps import MediaDeps
from . import downloader as max_downloader
from .ua import (
    MAX_CDN_ANDROID_CHROME_USER_AGENT,
    MAX_CDN_CHROME_USER_AGENT,
    MAX_CDN_IOS_CHROME_USER_AGENT,
    MAX_CDN_USER_AGENT,
)
from ....bridge.contracts import MaxAttachment
from ....logging_utils import log_event, sanitize_path, sanitize_url

logger = logging.getLogger("src.adapters.max_adapter")


class MaxMediaService:
    def __init__(self, deps: MediaDeps):
        self._deps = deps
        self._downloader = max_downloader.MaxCdnDownloader(
            tmp_dir=deps.tmp_dir,
            client_session_factory=deps.client_session_factory,
            egress=deps.egress,
        )

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _tmp_dir(self):
        return self._deps.tmp_dir

    @property
    def _raw_payload(self):
        return self._deps.raw_payload

    def _attachment_type_name(self, attach) -> str:
        atype = getattr(attach, "type", None)
        if atype is None:
            return ""
        return str(getattr(atype, "value", atype)).upper()

    def _normalize_attachment_type(self, atype: str) -> str:
        if not atype:
            return ""
        upper = str(atype).upper()
        if upper.startswith(("PHOTO", "IMAGE")):
            return "PHOTO"
        if upper.startswith("VIDEO"):
            return "VIDEO"
        if upper.startswith(("AUDIO", "VOICE")):
            return "AUDIO"
        if upper.startswith(("FILE", "DOCUMENT", "DOC")):
            return "FILE"
        return upper

    def _attachment_filename(self, attach) -> Optional[str]:
        name = getattr(attach, "filename", None) or getattr(attach, "name", None)
        return self._fix_filename_encoding(name) if name else None

    def _attachment_reference(
        self,
        attach,
        atype: str,
    ) -> tuple[Optional[str], Optional[str]]:
        if atype == "VIDEO":
            ref = (
                getattr(attach, "video_id", None)
                or getattr(attach, "videoId", None)
                or getattr(attach, "id", None)
            )
            return ("video_id", str(ref)) if ref is not None else (None, None)
        if atype == "AUDIO":
            audio_id = getattr(attach, "audio_id", None) or getattr(attach, "audioId", None)
            if audio_id is not None:
                return "audio_id", str(audio_id)
            file_id = (
                getattr(attach, "file_id", None)
                or getattr(attach, "fileId", None)
                or getattr(attach, "id", None)
            )
            return ("file_id", str(file_id)) if file_id is not None else (None, None)
        if atype == "PHOTO":
            file_id = (
                getattr(attach, "file_id", None)
                or getattr(attach, "fileId", None)
                or getattr(attach, "photo_id", None)
                or getattr(attach, "photoId", None)
            )
            return ("file_id", str(file_id)) if file_id is not None else (None, None)
        if atype == "FILE":
            file_id = (
                getattr(attach, "file_id", None)
                or getattr(attach, "fileId", None)
                or getattr(attach, "id", None)
            )
            return ("file_id", str(file_id)) if file_id is not None else (None, None)
        return None, None

    def _with_attachment_metadata(
        self,
        attachment: MaxAttachment,
        *,
        chat_id: str,
        msg_id: str,
        index: int,
        attach=None,
        reference_kind: Optional[str] = None,
        reference_id: Optional[str] = None,
    ) -> MaxAttachment:
        if attach is not None and (not reference_kind or reference_id is None):
            reference_kind, reference_id = self._attachment_reference(
                attach,
                self._normalize_attachment_type(self._attachment_type_name(attach)),
            )
        attachment.attachment_index = index
        attachment.media_chat_id = str(chat_id) if chat_id is not None else None
        attachment.media_msg_id = str(msg_id) if msg_id is not None else None
        attachment.reference_kind = reference_kind
        attachment.reference_id = str(reference_id) if reference_id is not None else None
        return attachment

    def _duration_seconds(self, duration, *, kind: Optional[str] = None) -> Optional[int]:
        """Normalize MAX media duration to Telegram seconds.

        MAX media payloads observed in prod can use milliseconds (for example
        38360 for a 38 second voice note), while Telegram expects seconds.
        Keep plausible second values intact and convert clearly impossible
        values when no file-level metadata is available.
        """
        if duration is None:
            return None
        try:
            value = float(duration)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        normalized_kind = (kind or "").lower()
        if normalized_kind == "audio" and value > 10 * 60:
            value = value / 1000
        elif normalized_kind == "video" and value > 6 * 60 * 60:
            value = value / 1000
        return max(1, int(round(value)))

    def _mp4_duration_seconds(self, path: str) -> Optional[int]:
        try:
            file_size = Path(path).stat().st_size
        except OSError:
            return None
        if file_size <= 0:
            return None

        def read_box_header(handle, end_pos: int):
            start = handle.tell()
            if start + 8 > end_pos:
                return None
            header = handle.read(8)
            if len(header) != 8:
                return None
            size, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                large_size = handle.read(8)
                if len(large_size) != 8:
                    return None
                size = struct.unpack(">Q", large_size)[0]
                header_size = 16
            elif size == 0:
                size = end_pos - start
            if size < header_size:
                return None
            box_end = min(start + int(size), end_pos)
            if box_end <= handle.tell():
                return None
            return box_type.decode("latin1"), start, box_end, header_size

        def read_mvhd_duration(handle, box_end: int) -> Optional[int]:
            payload = handle.read(min(32, box_end - handle.tell()))
            if len(payload) < 20:
                return None
            version = payload[0]
            try:
                if version == 1:
                    if len(payload) < 32:
                        return None
                    timescale = struct.unpack(">I", payload[20:24])[0]
                    duration = struct.unpack(">Q", payload[24:32])[0]
                else:
                    timescale = struct.unpack(">I", payload[12:16])[0]
                    duration = struct.unpack(">I", payload[16:20])[0]
            except struct.error:
                return None
            if timescale <= 0 or duration <= 0:
                return None
            return max(1, int(round(duration / timescale)))

        def walk_boxes(handle, end_pos: int, depth: int = 0) -> Optional[int]:
            if depth > 4:
                return None
            container_boxes = {"moov", "trak", "mdia", "minf", "stbl", "edts", "udta"}
            while handle.tell() < end_pos:
                header = read_box_header(handle, end_pos)
                if header is None:
                    return None
                box_type, _start, box_end, _header_size = header
                if box_type == "mvhd":
                    duration = read_mvhd_duration(handle, box_end)
                    if duration is not None:
                        return duration
                elif box_type in container_boxes:
                    duration = walk_boxes(handle, box_end, depth + 1)
                    if duration is not None:
                        return duration
                handle.seek(box_end)
            return None

        try:
            with Path(path).open("rb") as handle:
                return walk_boxes(handle, file_size)
        except OSError:
            return None

    def _video_duration_seconds(self, duration, local_path: Optional[str]) -> Optional[int]:
        normalized = self._duration_seconds(duration, kind="video")
        file_duration = self._mp4_duration_seconds(local_path) if local_path else None
        if file_duration is None:
            return normalized
        if normalized is None:
            return file_duration
        if normalized > 6 * 60 * 60:
            return file_duration
        if normalized > file_duration * 10:
            return file_duration
        return normalized

    def _safe_attachment_field_names(self, attach) -> list[str]:
        try:
            names = vars(attach).keys()
        except TypeError:
            names = (
                name
                for name in dir(attach)
                if not name.startswith("_") and not callable(getattr(attach, name, None))
            )
        return sorted(
            name
            for name in names
            if max_payload.is_safe_field_name(name)
        )

    @staticmethod
    def _fix_filename_encoding(name: str) -> str:
        """Fix cp1251-as-latin-1 mojibake in filenames from MAX.

        MAX CDN sometimes returns filenames with cp1251 bytes decoded as latin-1,
        producing garbled text like "Âàëüñ" instead of "Вальс".
        Heuristic: if the string fits in latin-1 and decodes cleanly as cp1251, use it.
        Pure ASCII and already-correct UTF-8 strings pass through unchanged.
        """
        return max_downloader.fix_filename_encoding(name)

    def _build_filename(self, prefix: str, filename_hint: Optional[str],
                        url: Optional[str], content_type: Optional[str],
                        default_extension: str = "") -> str:
        return max_downloader.build_filename(
            prefix,
            filename_hint,
            url,
            content_type,
            default_extension,
        )

    def _extract_video_url(self, value, *, key_hint: Optional[str] = None) -> Optional[str]:
        """Найти реальный URL видео в сыром payload VIDEO_PLAY.

        pymax разбирает VIDEO_PLAY довольно хрупко: берёт первое поле payload,
        которое не EXTERNAL/cache. На практике сервер может вернуть вложенную
        структуру или сначала preview/thumbnail. Здесь ищем лучший URL сами.
        """
        return max_downloader.extract_video_url(value, key_hint=key_hint)

    def _extract_audio_url(self, value, *, key_hint: Optional[str] = None) -> Optional[str]:
        """Найти реальный URL голосового/audio в сыром MAX payload."""
        return max_downloader.extract_audio_url(value, key_hint=key_hint)

    def _safe_payload_error_code(self, payload) -> Optional[str]:
        return max_payload.safe_payload_error_code(payload)

    def _is_socket_probe_error(self, exc: Exception) -> bool:
        return max_errors.is_socket_probe_error(exc)

    def _audio_get_sources_opcode(self):
        """Opcode 301 is used by MAX Web for audioGetSources but is absent in pymax 1.2.5."""
        return SimpleNamespace(value=301, name="AUDIO_GET_SOURCES")

    async def _probe_audio_download_payload(
        self,
        *,
        opcode,
        candidate: str,
        payload: dict,
        chat_id: str,
        msg_id: str,
        flow_id: Optional[str] = None,
    ) -> tuple[Optional[str], bool]:
        if not self._client:
            return None, False
        try:
            data = await self._client.raw_request(
                opcode_name=getattr(opcode, "name", str(opcode)),
                default_opcode=getattr(opcode, "value", None),
                payload=payload,
                timeout=5,
            )
        except Exception as e:
            hard_stop = self._is_socket_probe_error(e)
            log_event(
                logger,
                logging.INFO,
                "max.attachment.audio_protocol_probe",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="failed",
                reason="socket_unavailable" if hard_stop else "send_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                candidate=candidate,
                error_class=e.__class__.__name__,
                hard_stop=hard_stop,
            )
            return None, hard_stop

        raw_payload = data.get("payload") if isinstance(data, dict) else None
        url = self._extract_audio_url(raw_payload)
        error_code = self._safe_payload_error_code(raw_payload)
        log_event(
            logger,
            logging.INFO,
            "max.attachment.audio_protocol_probe",
            flow_id=flow_id,
            direction="inbound",
            stage="download",
            outcome="received" if url else "miss",
            reason=None if url else (error_code or "url_missing"),
            max_chat_id=chat_id,
            max_msg_id=msg_id,
            candidate=candidate,
            has_download_url=bool(url),
            payload_class=raw_payload.__class__.__name__ if raw_payload is not None else None,
            payload_fields=(
                sorted(
                    str(key)
                    for key in raw_payload.keys()
                    if max_payload.is_safe_field_name(key)
                )
                if isinstance(raw_payload, dict)
                else []
            ),
            payload_shape=self._raw_payload._safe_field_paths(raw_payload) if isinstance(raw_payload, dict) else [],
        )
        return url, False

    async def _fetch_raw_message_payload_by_id(
        self,
        *,
        chat_id: str,
        msg_id: str,
        flow_id: Optional[str] = None,
    ) -> Optional[dict]:
        if not self._client:
            return None

        try:
            chat_id_value = int(chat_id)
        except (TypeError, ValueError):
            return None
        msg_values: list[object] = []
        try:
            msg_values.append(int(msg_id))
        except (TypeError, ValueError):
            pass
        msg_values.append(str(msg_id))

        payloads = []
        for msg_value in dict.fromkeys(msg_values):
            payloads.extend([
                {"chatId": chat_id_value, "messageId": msg_value},
                {"chatId": chat_id_value, "messageIds": [msg_value]},
            ])
        if msg_values:
            payloads.append({"chatId": chat_id_value, "ids": [msg_values[0]]})

        for index, payload in enumerate(payloads, start=1):
            try:
                data = await self._client.raw_request(
                    opcode_name="MSG_GET",
                    payload=payload,
                    timeout=5,
                )
            except Exception as e:
                log_event(
                    logger,
                    logging.INFO,
                    "max.raw.message_get_probe",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="send_failed",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    candidate=f"msg_get_{index}",
                    error_class=e.__class__.__name__,
                )
                continue
            raw_payload = data.get("payload") if isinstance(data, dict) else None
            message, outer_chat_id = self._raw_payload._payload_message_dict(raw_payload or {})
            if message is None and isinstance(raw_payload, dict):
                message = self._raw_payload._find_raw_history_message_dict(raw_payload, str(msg_id))
            if message is not None:
                return self._raw_payload._normalize_message_dict(message)
            log_event(
                logger,
                logging.INFO,
                "max.raw.message_get_probe",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="miss",
                reason="message_missing",
                max_chat_id=str(outer_chat_id or chat_id),
                max_msg_id=msg_id,
                candidate=f"msg_get_{index}",
                payload_shape=self._raw_payload._safe_field_paths(raw_payload) if isinstance(raw_payload, dict) else [],
            )
        return None

    async def _download_audio_by_protocol(
        self,
        *,
        chat_id: str,
        msg_id: str,
        reference_id: object,
        reference_kind: str,
        prefix: str,
        filename_hint: Optional[str] = None,
        token: Optional[str] = None,
        flow_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], bool]:
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None, None, False

        reference = str(reference_id)
        ref_values: list[object] = []
        try:
            ref_values.append(int(reference))
        except (TypeError, ValueError):
            pass
        ref_values.append(reference)

        base_messages: list[object] = []
        try:
            base_messages.append(int(msg_id))
        except (TypeError, ValueError):
            pass
        base_messages.append(str(msg_id))

        candidates: list[tuple[str, dict]] = []
        msg_values = list(dict.fromkeys(base_messages))
        reference_values = list(dict.fromkeys(ref_values))
        primary_msg = msg_values[0] if msg_values else str(msg_id)
        primary_ref = reference_values[0] if reference_values else reference

        audio_get_payload = {
            "audioId": primary_ref,
            "chatId": chat_id_int,
            "messageId": primary_msg,
        }
        if token is not None:
            audio_get_payload["token"] = str(token)
        candidates.append((
            "audio_get_sources",
            audio_get_payload,
        ))
        if token is not None:
            candidates.append((
                "audio_get_sources_no_token",
                {
                    "audioId": primary_ref,
                    "chatId": chat_id_int,
                    "messageId": primary_msg,
                },
            ))

        # Userbot FILE_DOWNLOAD is only known safe with MAX fileId shape.
        # audioId/token variants returned proto.payload in prod and closed the socket.
        for msg_value in [primary_msg]:
            for ref_value in [primary_ref]:
                candidates.extend([
                    (
                        "file_download_file_id",
                        {"chatId": chat_id_int, "messageId": msg_value, "fileId": ref_value},
                    ),
                ])

        seen_payloads: set[str] = set()
        for candidate, payload in candidates:
            signature = json.dumps(payload, sort_keys=True, default=str)
            if signature in seen_payloads:
                continue
            seen_payloads.add(signature)
            opcode = (
                self._audio_get_sources_opcode()
                if candidate.startswith("audio_get_sources")
                else SimpleNamespace(value=88, name="FILE_DOWNLOAD")
            )
            url, hard_stop = await self._probe_audio_download_payload(
                opcode=opcode,
                candidate=candidate,
                payload=payload,
                chat_id=chat_id,
                msg_id=msg_id,
                flow_id=flow_id,
            )
            if hard_stop:
                return None, None, True
            if not url:
                continue
            local_path, filename = await self._download_from_url(
                url,
                prefix,
                filename_hint,
                ".ogg",
                expected_kind="audio",
                flow_id=flow_id,
                download_source=candidate,
            )
            return local_path, filename, False

        return None, None, False

    def _download_client_profile_for_url(self, url: str) -> tuple[dict[str, str], Optional[str], str]:
        from .ua import download_client_profile_for_url

        return download_client_profile_for_url(url)

    def _download_headers_for_url(self, url: str) -> dict[str, str]:
        from .ua import download_headers_for_url

        return download_headers_for_url(url)

    def _detect_magic_type(self, content: bytes) -> str:
        return max_downloader.detect_magic_type(content)

    def _classify_downloaded_content(self, content_type: Optional[str], content: bytes) -> str:
        return max_downloader.classify_downloaded_content(content_type, content)

    def _is_download_valid(self, expected_kind: Optional[str], detected_kind: str) -> bool:
        return max_downloader.is_download_valid(expected_kind, detected_kind)

    def _is_retryable_download_error(self, error: Exception) -> bool:
        return max_downloader.is_retryable_download_error(error)

    def _download_error_for_log(self, error: Exception) -> str:
        return max_downloader.download_error_for_log(error)

    def _download_error_status(self, error: Exception) -> Optional[int]:
        return max_downloader.download_error_status(error)

    async def _write_download_response(self, response, part_path: Path, mode: str) -> int:
        return await self._downloader.write_download_response(response, part_path, mode)

    def _download_retry_delay(self, attempt: int) -> int:
        return max_downloader.download_retry_delay(attempt)

    async def _download_from_url(self, url: str, prefix: str,
                                 filename_hint: Optional[str] = None,
                                 default_extension: str = "",
                                 expected_kind: Optional[str] = None,
                                 flow_id: Optional[str] = None,
                                 download_source: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        return await self._downloader.download_from_url(
            url,
            prefix,
            filename_hint,
            default_extension,
            expected_kind=expected_kind,
            flow_id=flow_id,
            download_source=download_source,
        )

    async def _download_file_by_id(self, chat_id: str, msg_id: str, file_id: int,
                                   prefix: str, filename_hint: Optional[str] = None,
                                   default_extension: str = "",
                                   expected_kind: Optional[str] = None,
                                   flow_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать файл через pymax FILE_DOWNLOAD."""
        if not self._client:
            return None, None
        try:
            url = await self._client.file_url(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                file_id=int(file_id),
            )
            if not url:
                return None, None
            return await self._download_from_url(
                url,
                prefix,
                filename_hint,
                default_extension,
                expected_kind=expected_kind,
                flow_id=flow_id,
                download_source="file_download",
            )
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.attachment.download",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="failed",
                reason="file_download_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        return None, None

    async def _download_video_by_id(self, chat_id: str, msg_id: str, video_id: int,
                                    prefix: str, filename_hint: Optional[str] = None,
                                    flow_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать видео через pymax VIDEO_PLAY."""
        if not self._client:
            return None, None
        try:
            raw_payload = await self._client.video_payload(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                video_id=int(video_id),
            )
            url = self._extract_video_url(raw_payload)
            if not url:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.download",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="video_url_missing",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                )
                return None, None
            return await self._download_from_url(
                url,
                prefix,
                filename_hint,
                ".mp4",
                expected_kind="video",
                flow_id=flow_id,
                download_source="video_play",
            )
        except Exception as e:
            log_event(
                logger,
                logging.WARNING,
                "max.attachment.download",
                flow_id=flow_id,
                direction="inbound",
                stage="download",
                outcome="failed",
                reason="video_download_failed",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                error=str(e),
            )
        return None, None

    async def download_video_reference(
        self,
        *,
        chat_id: str,
        msg_id: str,
        video_id: str,
        attachment_index: int = 0,
        filename_hint: Optional[str] = None,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        source_type: Optional[str] = "VIDEO",
        flow_id: Optional[str] = None,
    ) -> Optional[MaxAttachment]:
        """Скачать видео по стабильной ссылке MAX без хранения signed URL."""
        idx = f"_{attachment_index}" if attachment_index > 0 else ""
        try:
            video_id_int = int(video_id)
        except (TypeError, ValueError):
            return None

        local_path, filename = await self._download_video_by_id(
            chat_id,
            msg_id,
            video_id_int,
            f"video_retry_{chat_id}_{msg_id}{idx}",
            filename_hint,
            flow_id=flow_id,
        )
        if not local_path:
            return None
        normalized_duration = self._video_duration_seconds(duration, local_path)
        return self._with_attachment_metadata(
            MaxAttachment(
                kind="video",
                local_path=local_path,
                filename=filename,
                duration=normalized_duration,
                width=width,
                height=height,
                source_type=source_type,
            ),
            chat_id=chat_id,
            msg_id=msg_id,
            index=attachment_index,
            reference_kind="video_id",
            reference_id=str(video_id),
        )

    async def download_audio_reference(
        self,
        *,
        chat_id: str,
        msg_id: str,
        reference_id: str,
        reference_kind: str = "audio_id",
        attachment_index: int = 0,
        filename_hint: Optional[str] = None,
        duration: Optional[int] = None,
        source_type: Optional[str] = "AUDIO",
        flow_id: Optional[str] = None,
    ) -> Optional[MaxAttachment]:
        """Retry MAX audio without persisting signed URLs."""
        idx = f"_{attachment_index}" if attachment_index > 0 else ""
        normalized_duration = self._duration_seconds(duration, kind="audio")
        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None

        if self._client:
            try:
                last_message = self._client.dialog_last_message(chat_id_int)
                if str(getattr(last_message, "id", "")) == str(msg_id):
                    attaches = getattr(last_message, "attaches", None) or []
                    attach_list = attaches if isinstance(attaches, list) else [attaches]
                    for attach in attach_list:
                        atype = self._normalize_attachment_type(
                            self._attachment_type_name(attach)
                        )
                        if atype != "AUDIO":
                            continue
                        attach_refs = {
                            str(value)
                            for value in (
                                getattr(attach, "audio_id", None),
                                getattr(attach, "audioId", None),
                                getattr(attach, "file_id", None),
                                getattr(attach, "fileId", None),
                                getattr(attach, "id", None),
                            )
                            if value is not None
                        }
                        if str(reference_id) not in attach_refs and len(attach_list) > 1:
                            continue
                        attachment = await self._download_attachment(
                            chat_id,
                            msg_id,
                            attach,
                            index=attachment_index,
                            flow_id=flow_id,
                        )
                        if attachment:
                            attachment.duration = attachment.duration or normalized_duration
                            attachment.source_type = source_type or attachment.source_type
                            return attachment
            except Exception as e:
                log_event(
                    logger,
                    logging.INFO,
                    "max.attachment.audio_dialog_fallback",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="dialog_last_message_failed",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    error=str(e),
                )

        raw_payload = await self._raw_payload._fetch_raw_history_payload(
            chat_id_int=chat_id_int,
            from_time=int(time.time() * 1000) + 60_000,
            forward=0,
            backward=30,
            flow_id=flow_id,
        )
        if raw_payload is not None:
            raw_message = self._raw_payload._find_raw_history_message_dict(raw_payload, str(msg_id))
            if raw_message is not None:
                normalized = self._raw_payload._normalize_message_dict(raw_message)
                raw_attaches = self._raw_payload._payload_value(normalized, "attaches", "attachments") or []
                attach_list = raw_attaches if isinstance(raw_attaches, list) else [raw_attaches]
                for attach in attach_list:
                    if not isinstance(attach, dict):
                        continue
                    attach_obj = SimpleNamespace(**self._raw_payload._normalize_message_dict(attach))
                    atype = self._normalize_attachment_type(
                        self._attachment_type_name(attach_obj)
                    )
                    if atype != "AUDIO":
                        continue
                    attach_refs = {
                        str(value)
                        for value in (
                            getattr(attach_obj, "audio_id", None),
                            getattr(attach_obj, "audioId", None),
                            getattr(attach_obj, "file_id", None),
                            getattr(attach_obj, "fileId", None),
                            getattr(attach_obj, "id", None),
                        )
                        if value is not None
                    }
                    if str(reference_id) not in attach_refs and len(attach_list) > 1:
                        continue
                    attachment = await self._download_attachment(
                        chat_id,
                        msg_id,
                        attach_obj,
                        index=attachment_index,
                        flow_id=flow_id,
                    )
                    if attachment:
                        attachment.duration = attachment.duration or normalized_duration
                        attachment.source_type = source_type or attachment.source_type
                        return attachment

        raw_message = await self._fetch_raw_message_payload_by_id(
            chat_id=chat_id,
            msg_id=msg_id,
            flow_id=flow_id,
        )
        if raw_message is not None:
            normalized = self._raw_payload._normalize_message_dict(raw_message)
            raw_attaches = self._raw_payload._payload_value(normalized, "attaches", "attachments") or []
            attach_list = raw_attaches if isinstance(raw_attaches, list) else [raw_attaches]
            for attach in attach_list:
                if not isinstance(attach, dict):
                    continue
                attach_obj = SimpleNamespace(**self._raw_payload._normalize_message_dict(attach))
                atype = self._normalize_attachment_type(
                    self._attachment_type_name(attach_obj)
                )
                if atype != "AUDIO":
                    continue
                attach_refs = {
                    str(value)
                    for value in (
                        getattr(attach_obj, "audio_id", None),
                        getattr(attach_obj, "audioId", None),
                        getattr(attach_obj, "file_id", None),
                        getattr(attach_obj, "fileId", None),
                        getattr(attach_obj, "id", None),
                    )
                    if value is not None
                }
                if str(reference_id) not in attach_refs and len(attach_list) > 1:
                    continue
                attachment = await self._download_attachment(
                    chat_id,
                    msg_id,
                    attach_obj,
                    index=attachment_index,
                    flow_id=flow_id,
                )
                if attachment:
                    attachment.duration = attachment.duration or normalized_duration
                    attachment.source_type = source_type or attachment.source_type
                    return attachment

        try:
            stable_id = int(reference_id)
        except (TypeError, ValueError):
            return None
        local_path, filename, protocol_hard_stop = await self._download_audio_by_protocol(
            chat_id=chat_id,
            msg_id=msg_id,
            reference_id=stable_id,
            reference_kind=reference_kind,
            prefix=f"audio_retry_{chat_id}_{msg_id}{idx}",
            filename_hint=filename_hint,
            flow_id=flow_id,
        )
        if local_path:
            return self._with_attachment_metadata(
                MaxAttachment(
                    kind="audio",
                    local_path=local_path,
                    filename=filename,
                    duration=normalized_duration,
                    width=None,
                    height=None,
                    source_type=source_type,
                ),
                chat_id=chat_id,
                msg_id=msg_id,
                index=attachment_index,
                reference_kind=reference_kind,
                reference_id=str(reference_id),
            )
        if protocol_hard_stop:
            return None
        local_path, filename = await self._download_file_by_id(
            chat_id,
            msg_id,
            stable_id,
            f"audio_retry_{chat_id}_{msg_id}{idx}",
            filename_hint,
            ".ogg",
            expected_kind="audio",
            flow_id=flow_id,
        )
        if not local_path:
            return None
        return self._with_attachment_metadata(
            MaxAttachment(
                kind="audio",
                local_path=local_path,
                filename=filename,
                duration=normalized_duration,
                width=None,
                height=None,
                source_type=source_type,
            ),
            chat_id=chat_id,
            msg_id=msg_id,
            index=attachment_index,
            reference_kind=reference_kind,
            reference_id=str(reference_id),
        )

    async def _download_attachment(self, chat_id: str, msg_id: str,
                                   attach, index: int = 0,
                                   flow_id: Optional[str] = None) -> Optional[MaxAttachment]:
        """Скачать одно вложение и нормализовать в MaxAttachment."""
        raw_type = self._attachment_type_name(attach)
        atype = self._normalize_attachment_type(raw_type)
        filename_hint = self._attachment_filename(attach)
        idx = f"_{index}" if index > 0 else ""

        if "PHOTO" in atype or "IMAGE" in atype:
            url = getattr(attach, "base_url", None) or getattr(attach, "baseRawUrl", None) or getattr(attach, "url", None)
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"photo_{chat_id}_{msg_id}{idx}", filename_hint, ".jpg",
                    expected_kind="photo", flow_id=flow_id, download_source="direct_url",
                )
            else:
                file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
                if not file_id:
                    return None
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"photo_{chat_id}_{msg_id}{idx}",
                    filename_hint, ".jpg", expected_kind="photo", flow_id=flow_id,
                )
            if local_path:
                return self._with_attachment_metadata(
                    MaxAttachment(
                        kind="photo",
                        local_path=local_path,
                        filename=filename,
                        duration=None,
                        width=getattr(attach, "width", None),
                        height=getattr(attach, "height", None),
                        source_type=raw_type,
                    ),
                    chat_id=chat_id,
                    msg_id=msg_id,
                    index=index,
                    attach=attach,
                )
            return None

        if "VIDEO" in atype:
            video_id = getattr(attach, "video_id", None) or getattr(attach, "id", None)
            url = getattr(attach, "url", None)
            local_path = None
            filename = None
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"video_{chat_id}_{msg_id}{idx}", filename_hint, ".mp4",
                    expected_kind="video", flow_id=flow_id, download_source="direct_url",
                )
                if not local_path and video_id:
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.video_fallback",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="direct_url_failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        source=sanitize_url(url),
                        attachment_index=index,
                    )
            if not local_path and video_id:
                local_path, filename = await self._download_video_by_id(
                    chat_id, msg_id, video_id, f"video_{chat_id}_{msg_id}{idx}", filename_hint,
                    flow_id=flow_id,
                )
            if not local_path:
                return None
            if local_path:
                duration = self._video_duration_seconds(
                    getattr(attach, "duration", None),
                    local_path,
                )
                return self._with_attachment_metadata(
                    MaxAttachment(
                        kind="video",
                        local_path=local_path,
                        filename=filename,
                        duration=duration,
                        width=getattr(attach, "width", None),
                        height=getattr(attach, "height", None),
                        source_type=raw_type,
                    ),
                    chat_id=chat_id,
                    msg_id=msg_id,
                    index=index,
                    attach=attach,
                )
            return None

        if "AUDIO" in atype or "VOICE" in atype:
            url = getattr(attach, "url", None)
            audio_id = getattr(attach, "audio_id", None) or getattr(attach, "audioId", None)
            token = getattr(attach, "token", None)
            file_id = (
                getattr(attach, "file_id", None)
                or getattr(attach, "id", None)
                or audio_id
            )
            local_path = None
            filename = None
            protocol_hard_stop = False
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"audio_{chat_id}_{msg_id}{idx}", filename_hint, ".ogg",
                    expected_kind="audio", flow_id=flow_id, download_source="direct_url",
                )
                if not local_path and file_id:
                    log_event(
                        logger,
                        logging.WARNING,
                        "max.attachment.audio_fallback",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="download",
                        outcome="retry",
                        reason="direct_url_failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        attachment_index=index,
                    )
            if not local_path and file_id:
                local_path, filename, protocol_hard_stop = await self._download_audio_by_protocol(
                    chat_id=chat_id,
                    msg_id=msg_id,
                    reference_id=file_id,
                    reference_kind="audio_id" if audio_id is not None else "file_id",
                    prefix=f"audio_{chat_id}_{msg_id}{idx}",
                    filename_hint=filename_hint,
                    token=str(token) if token is not None else None,
                    flow_id=flow_id,
                )
            if not local_path and file_id and not protocol_hard_stop:
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"audio_{chat_id}_{msg_id}{idx}",
                    filename_hint, ".ogg", expected_kind="audio", flow_id=flow_id,
                )
            if not local_path and not file_id:
                log_event(
                    logger,
                    logging.WARNING,
                    "max.attachment.voice_reference_missing",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="download",
                    outcome="failed",
                    reason="voice_reference_missing",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    source_type=raw_type,
                    attachment_class=attach.__class__.__name__,
                    attachment_fields=self._safe_attachment_field_names(attach),
                    attachment_index=index,
                )
                return None
            if local_path:
                return self._with_attachment_metadata(
                    MaxAttachment(
                        kind="audio",
                        local_path=local_path,
                        filename=filename,
                        duration=self._duration_seconds(
                            getattr(attach, "duration", None),
                            kind="audio",
                        ),
                        width=None,
                        height=None,
                        source_type=raw_type,
                    ),
                    chat_id=chat_id,
                    msg_id=msg_id,
                    index=index,
                    attach=attach,
                )
            return None

        if "FILE" in atype or "DOCUMENT" in atype or "DOC" in atype:
            file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
            if not file_id:
                return None
            local_path, filename = await self._download_file_by_id(
                chat_id, msg_id, file_id, f"doc_{chat_id}_{msg_id}{idx}",
                filename_hint, expected_kind="document", flow_id=flow_id,
            )
            if local_path:
                return self._with_attachment_metadata(
                    MaxAttachment(
                        kind="document",
                        local_path=local_path,
                        filename=filename,
                        duration=None,
                        width=None,
                        height=None,
                        source_type=raw_type,
                    ),
                    chat_id=chat_id,
                    msg_id=msg_id,
                    index=index,
                    attach=attach,
                )
            return None

        return None
