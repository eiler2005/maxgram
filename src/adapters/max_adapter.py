"""
MAX Adapter — подключение к MAX через SocketMaxClient (pymax).

Ответственность:
  - Авторизация (сессия уже сохранена в data/)
  - Получение входящих сообщений
  - Скачивание медиафайлов
  - Отправка сообщений
  - Reconnect при обрыве
  - Резолвинг имён пользователей для DM чатов
"""

import asyncio
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Awaitable
from urllib.parse import urlparse
from urllib.parse import parse_qs

from aiohttp import ClientSession

logger = logging.getLogger(__name__)

MAX_CDN_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_CDN_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class MaxAttachment:
    """Нормализованное вложение из MAX."""
    kind: str                     # photo | video | audio | document
    local_path: str               # локальный путь к скачанному файлу
    filename: Optional[str]
    duration: Optional[int]
    width: Optional[int]
    height: Optional[int]
    source_type: Optional[str]    # исходный тип вложения в MAX/pymax


@dataclass
class PendingOutboundAck:
    """Ожидаем подтверждение исходящего сообщения по эху из MAX."""
    chat_id: str
    text: str
    reply_to_msg_id: Optional[str]
    created_monotonic: float
    future: asyncio.Future


@dataclass
class MaxMessage:
    """Нормализованное сообщение из MAX."""
    msg_id: str
    chat_id: str
    chat_title: Optional[str]       # название группы или None для DM
    sender_id: Optional[str]
    sender_name: Optional[str]
    text: Optional[str]
    attachments: list[MaxAttachment]
    attachment_types: list[str]
    rendered_texts: list[str]
    message_type: Optional[str]
    status: Optional[str]
    is_dm: bool                     # True если это личная переписка
    is_own: bool                    # True если сообщение отправлено нашим аккаунтом
    raw: object                     # оригинальный объект библиотеки


MessageHandler = Callable[[MaxMessage], Awaitable[None]]


class MaxAdapter:
    def __init__(self, phone: str, data_dir: str, session_name: str, tmp_dir: str):
        self._phone = phone
        self._data_dir = data_dir
        self._session_name = session_name.replace(".db", "")  # pymax добавляет расширение сам
        self._tmp_dir = Path(tmp_dir)
        self._client = None
        self._handlers: list[MessageHandler] = []
        self._started = False
        self._start_handlers: list[Callable] = []
        self._own_id: Optional[str] = None  # ID нашего аккаунта в MAX
        self._pending_outbound_acks: list[PendingOutboundAck] = []
        self._expected_outbound_ids: dict[tuple[str, str], float] = {}
        self._interactive_ping_failure_limit = 3

    def on_message(self, handler: MessageHandler):
        self._handlers.append(handler)

    def on_start(self, handler: Callable):
        self._start_handlers.append(handler)

    def _normalize_outbound_text(self, text: Optional[str]) -> str:
        return (text or "").strip()

    def _cleanup_pending_state(self):
        now = time.monotonic()
        self._pending_outbound_acks = [
            pending
            for pending in self._pending_outbound_acks
            if now - pending.created_monotonic <= 30
        ]
        self._expected_outbound_ids = {
            key: expires_at
            for key, expires_at in self._expected_outbound_ids.items()
            if expires_at > now
        }

    def _remember_expected_outbound_id(self, chat_id: str, msg_id: str):
        self._cleanup_pending_state()
        self._expected_outbound_ids[(str(chat_id), str(msg_id))] = time.monotonic() + 30

    def _consume_expected_outbound_id(self, chat_id: str, msg_id: str) -> bool:
        self._cleanup_pending_state()
        key = (str(chat_id), str(msg_id))
        expires_at = self._expected_outbound_ids.pop(key, None)
        return expires_at is not None

    def _claim_pending_outbound_ack(self, chat_id: str, text: Optional[str],
                                    reply_to_msg_id: Optional[str]) -> Optional[PendingOutboundAck]:
        self._cleanup_pending_state()
        normalized = self._normalize_outbound_text(text)
        if not normalized:
            return None

        for pending in list(self._pending_outbound_acks):
            if pending.chat_id != str(chat_id):
                continue
            if pending.text != normalized:
                continue
            if pending.reply_to_msg_id and reply_to_msg_id and pending.reply_to_msg_id != reply_to_msg_id:
                continue
            self._pending_outbound_acks.remove(pending)
            return pending
        return None

    def _extract_result_msg_id(self, result) -> Optional[str]:
        if result is None:
            return None

        direct_id = getattr(result, "id", None) or getattr(result, "message_id", None)
        if direct_id is not None:
            return str(direct_id)

        def from_dict(data) -> Optional[str]:
            if not isinstance(data, dict):
                return None
            for key in ("id", "messageId", "message_id"):
                if data.get(key) is not None:
                    return str(data[key])
            for key in ("message", "payload", "result", "msg"):
                nested = data.get(key)
                found = from_dict(nested)
                if found:
                    return found
            return None

        return from_dict(result)

    def _extract_reply_to_msg_id(self, message) -> Optional[str]:
        link = getattr(message, "link", None)
        if not link:
            return None

        link_type = str(getattr(link, "type", "") or "").upper()
        if link_type and link_type != "REPLY":
            return None

        linked_msg = getattr(link, "message", None)
        linked_id = getattr(linked_msg, "id", None) if linked_msg else None
        if linked_id is None:
            linked_id = getattr(link, "message_id", None)
        return str(linked_id) if linked_id is not None else None

    def _get_extra_value(self, extra: dict, *keys: str):
        normalized = {
            str(k).lower().replace("_", ""): v
            for k, v in extra.items()
        }
        for key in keys:
            candidate = key.lower().replace("_", "")
            if candidate in normalized:
                return normalized[candidate]
        return None

    def _coerce_user_ids(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value if v is not None]
        return [str(value)]

    async def _render_user_list(self, user_ids: list[str]) -> Optional[str]:
        if not user_ids:
            return None

        names: list[str] = []
        unresolved = 0
        for uid in user_ids:
            name = await self.resolve_user_name(uid)
            if name:
                names.append(name)
            else:
                unresolved += 1

        if names:
            if unresolved:
                names.append(f"ещё {unresolved}")
            return ", ".join(names)

        if len(user_ids) == 1:
            return "участник"
        return f"{len(user_ids)} участников"

    async def _render_control_attach(self, attach, sender_id: Optional[str],
                                     sender_name: Optional[str]) -> Optional[str]:
        event = str(getattr(attach, "event", "") or "").lower()
        extra = getattr(attach, "extra", None) or {}
        user_ids = self._coerce_user_ids(
            self._get_extra_value(extra, "user_ids", "userIds", "users", "members")
        )
        rendered_users = await self._render_user_list(user_ids)
        title = self._get_extra_value(extra, "title", "theme", "name")
        actor = sender_name

        if event in {"add", "invite", "join", "joined"}:
            if rendered_users:
                return f"Добавлены участники: {rendered_users}"
            return "В чат добавлен участник"

        if event in {"leave", "left", "exit"}:
            if actor:
                return f"{actor} вышел(а) из чата"
            if sender_id:
                resolved_actor = await self.resolve_user_name(sender_id)
                if resolved_actor:
                    return f"{resolved_actor} вышел(а) из чата"
            return "Участник вышел из чата"

        if event in {"remove", "removed", "kick"}:
            if rendered_users:
                return f"Удалены участники: {rendered_users}"
            return "Участник удалён из чата"

        if event in {"new", "create", "created"}:
            if title:
                return f"Создан чат «{title}»"
            if rendered_users:
                return f"Создан новый чат, участники: {rendered_users}"
            return "Создан новый чат"

        if event in {"rename", "title", "theme"}:
            if title:
                return f"Изменено название чата: «{title}»"
            return "Изменено название чата"

        if event in {"description", "about", "profile"}:
            return "Изменён профиль чата"

        if event:
            details: list[str] = []
            if title:
                details.append(f"«{title}»")
            if rendered_users:
                details.append(rendered_users)
            suffix = f": {', '.join(details)}" if details else ""
            return f"Системное событие MAX `{event}`{suffix}"

        return "Системное событие MAX"

    def _render_contact_attach(self, attach) -> str:
        name = getattr(attach, "name", None) or " ".join(
            part for part in [
                getattr(attach, "first_name", None),
                getattr(attach, "last_name", None),
            ] if part
        ).strip()
        return f"Контакт: {name or 'без имени'}"

    def _render_sticker_attach(self, attach) -> str:
        if getattr(attach, "audio", False):
            return "[Аудиостикер]"
        return "[Стикер]"

    async def send_message(self, chat_id: str, text: str,
                           reply_to_msg_id: Optional[str] = None,
                           media_path: Optional[str] = None,
                           media_type: Optional[str] = None) -> Optional[str]:
        """Отправить сообщение в MAX чат (текст и/или медиа).

        media_type: "photo" | "video" | "audio" | "document"

        Возвращает:
          str  — real max_msg_id
          None — ошибка
        """
        # Ждём подключения до 15 секунд (на случай reconnect)
        if not self._started:
            for _ in range(3):
                await asyncio.sleep(5)
                if self._started:
                    break
            else:
                logger.error("MAX send_message failed: not connected after retries chat_id=%s", chat_id)
                return None

        if not self._client:
            return None

        normalized_text = self._normalize_outbound_text(text)
        loop = asyncio.get_running_loop()
        pending = PendingOutboundAck(
            chat_id=str(chat_id),
            text=normalized_text,
            reply_to_msg_id=reply_to_msg_id,
            created_monotonic=time.monotonic(),
            future=loop.create_future(),
        )
        self._pending_outbound_acks.append(pending)

        try:
            from pymax.files import File, Photo, Video

            attachment = None
            if media_path and Path(media_path).exists():
                if media_type == "photo":
                    attachment = Photo(path=media_path)
                elif media_type == "video":
                    attachment = Video(path=media_path)
                else:  # audio, document
                    attachment = File(path=media_path)
                logger.debug("Attaching media type=%s path=%s", media_type, media_path)

            kwargs: dict = {"chat_id": int(chat_id), "text": text}
            if reply_to_msg_id:
                kwargs["reply_to"] = int(reply_to_msg_id)
            if attachment is not None:
                kwargs["attachment"] = attachment
            result = await self._client.send_message(**kwargs)
            msg_id = self._extract_result_msg_id(result)
            if msg_id:
                self._remember_expected_outbound_id(chat_id, msg_id)
                return msg_id

            if not normalized_text:
                logger.error("MAX send_message returned without msg_id chat_id=%s", chat_id)
                return None

            try:
                echoed_id = await asyncio.wait_for(asyncio.shield(pending.future), timeout=10)
                return str(echoed_id)
            except asyncio.TimeoutError:
                logger.error("MAX send_message ack timeout chat_id=%s", chat_id)
                return None
        except Exception as e:
            logger.error("MAX send_message failed chat_id=%s: %s", chat_id, e)
            return None
        finally:
            if pending in self._pending_outbound_acks:
                self._pending_outbound_acks.remove(pending)
            if not pending.future.done():
                pending.future.cancel()

    async def resolve_user_name(self, user_id: str) -> Optional[str]:
        """Получить имя пользователя по ID (для DM чатов без названия).
        Сначала пробует кеш (не требует сокета), затем live-запрос.
        """
        if not self._client:
            return None

        # 1. Из кеша (синхронно, всегда доступно после sync)
        try:
            cached = self._client.get_cached_user(int(user_id))
            if cached:
                name = self._extract_user_name(cached)
                if name:
                    logger.debug("resolve_user_name (cache) user_id=%s → %r", user_id, name)
                    return name
        except Exception as e:
            logger.debug("get_cached_user failed user_id=%s: %s", user_id, e)

        # 2. Live-запрос (требует активного сокета)
        try:
            users = await self._client.get_users([int(user_id)])
            if users:
                name = self._extract_user_name(users[0])
                logger.debug("resolve_user_name (live) user_id=%s → %r", user_id, name)
                return name or None
        except Exception as e:
            logger.warning("resolve_user_name failed user_id=%s: %s", user_id, e)
        return None

    def get_own_id(self) -> Optional[str]:
        """Вернуть ID нашего MAX аккаунта (для фильтрации собственных сообщений)."""
        return self._own_id

    def _attachment_type_name(self, attach) -> str:
        atype = getattr(attach, "type", None)
        if atype is None:
            return ""
        return str(getattr(atype, "value", atype)).upper()

    def _attachment_filename(self, attach) -> Optional[str]:
        return getattr(attach, "filename", None) or getattr(attach, "name", None)

    def _build_filename(self, prefix: str, filename_hint: Optional[str],
                        url: Optional[str], content_type: Optional[str],
                        default_extension: str = "") -> str:
        base_name = Path(filename_hint).name if filename_hint else ""
        stem = Path(base_name).stem if base_name else prefix
        suffix = Path(base_name).suffix

        if not suffix and url:
            suffix = Path(urlparse(url).path).suffix

        if not suffix and content_type:
            guessed = mimetypes.guess_extension(content_type)
            if guessed == ".jpe":
                guessed = ".jpg"
            suffix = guessed or ""

        if not suffix and default_extension:
            suffix = default_extension if default_extension.startswith(".") else f".{default_extension}"

        return f"{stem}{suffix}" if suffix else stem

    def _extract_video_url(self, value, *, key_hint: Optional[str] = None) -> Optional[str]:
        """Найти реальный URL видео в сыром payload VIDEO_PLAY.

        pymax разбирает VIDEO_PLAY довольно хрупко: берёт первое поле payload,
        которое не EXTERNAL/cache. На практике сервер может вернуть вложенную
        структуру или сначала preview/thumbnail. Здесь ищем лучший URL сами.
        """
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

    def _download_headers_for_url(self, url: str) -> dict[str, str]:
        src_ag = (
            parse_qs(urlparse(url).query).get("srcAg", [None])[0]
            if url else None
        )
        normalized = str(src_ag or "").upper()
        if normalized == "CHROME":
            user_agent = MAX_CDN_CHROME_USER_AGENT
        else:
            user_agent = MAX_CDN_USER_AGENT
        return {"User-Agent": user_agent}

    async def _download_from_url(self, url: str, prefix: str,
                                 filename_hint: Optional[str] = None,
                                 default_extension: str = "") -> tuple[Optional[str], Optional[str]]:
        """Скачать файл по URL, вернуть (local_path, filename)."""
        try:
            async with ClientSession(headers=self._download_headers_for_url(url)) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    content = await response.read()
                    content_type = response.headers.get("Content-Type", "").split(";")[0].strip() or None

            filename = self._build_filename(prefix, filename_hint, url, content_type, default_extension)
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            local_path = self._tmp_dir / filename
            local_path.write_bytes(content)
            return str(local_path), filename
        except Exception as e:
            logger.warning("download_from_url failed url=%r: %s", url, e)
        return None, None

    async def _download_file_by_id(self, chat_id: str, msg_id: str, file_id: int,
                                   prefix: str, filename_hint: Optional[str] = None,
                                   default_extension: str = "") -> tuple[Optional[str], Optional[str]]:
        """Скачать файл через pymax FILE_DOWNLOAD."""
        if not self._client:
            return None, None
        try:
            file_obj = await self._client.get_file_by_id(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                file_id=int(file_id),
            )
            url = getattr(file_obj, "url", None)
            if not url:
                return None, None
            return await self._download_from_url(url, prefix, filename_hint, default_extension)
        except Exception as e:
            logger.warning("download_file_by_id failed chat_id=%s msg_id=%s file_id=%s: %s",
                           chat_id, msg_id, file_id, e)
        return None, None

    async def _download_video_by_id(self, chat_id: str, msg_id: str, video_id: int,
                                    prefix: str, filename_hint: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Скачать видео через pymax VIDEO_PLAY."""
        if not self._client:
            return None, None
        try:
            from pymax.payloads import GetVideoPayload
            from pymax.static.enum import Opcode

            payload = GetVideoPayload(
                chat_id=int(chat_id),
                message_id=int(msg_id),
                video_id=int(video_id),
            ).model_dump(by_alias=True)
            data = await self._client._send_and_wait(opcode=Opcode.VIDEO_PLAY, payload=payload)
            raw_payload = data.get("payload") if isinstance(data, dict) else None
            url = self._extract_video_url(raw_payload)
            if not url:
                logger.warning(
                    "VIDEO_PLAY returned no downloadable URL chat_id=%s msg_id=%s video_id=%s payload=%r",
                    chat_id, msg_id, video_id, raw_payload,
                )
                return None, None
            return await self._download_from_url(url, prefix, filename_hint, ".mp4")
        except Exception as e:
            logger.warning("download_video_by_id failed chat_id=%s msg_id=%s video_id=%s: %s",
                           chat_id, msg_id, video_id, e)
        return None, None

    async def _download_attachment(self, chat_id: str, msg_id: str,
                                   attach, index: int = 0) -> Optional[MaxAttachment]:
        """Скачать одно вложение и нормализовать в MaxAttachment."""
        atype = self._attachment_type_name(attach)
        filename_hint = self._attachment_filename(attach)
        idx = f"_{index}" if index > 0 else ""

        if "PHOTO" in atype or "IMAGE" in atype:
            url = getattr(attach, "base_url", None) or getattr(attach, "baseRawUrl", None) or getattr(attach, "url", None)
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"photo_{chat_id}_{msg_id}{idx}", filename_hint, ".jpg"
                )
            else:
                file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
                if not file_id:
                    return None
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"photo_{chat_id}_{msg_id}{idx}", filename_hint, ".jpg"
                )
            if local_path:
                return MaxAttachment(
                    kind="photo",
                    local_path=local_path,
                    filename=filename,
                    duration=None,
                    width=getattr(attach, "width", None),
                    height=getattr(attach, "height", None),
                    source_type=atype,
                )
            return None

        if "VIDEO" in atype:
            video_id = getattr(attach, "video_id", None) or getattr(attach, "id", None)
            url = getattr(attach, "url", None)
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"video_{chat_id}_{msg_id}{idx}", filename_hint, ".mp4"
                )
            elif video_id:
                local_path, filename = await self._download_video_by_id(
                    chat_id, msg_id, video_id, f"video_{chat_id}_{msg_id}{idx}", filename_hint
                )
            else:
                return None
            if local_path:
                return MaxAttachment(
                    kind="video",
                    local_path=local_path,
                    filename=filename,
                    duration=getattr(attach, "duration", None),
                    width=getattr(attach, "width", None),
                    height=getattr(attach, "height", None),
                    source_type=atype,
                )
            return None

        if "AUDIO" in atype or "VOICE" in atype:
            url = getattr(attach, "url", None)
            if url:
                local_path, filename = await self._download_from_url(
                    url, f"audio_{chat_id}_{msg_id}{idx}", filename_hint, ".ogg"
                )
            else:
                file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
                if not file_id:
                    return None
                local_path, filename = await self._download_file_by_id(
                    chat_id, msg_id, file_id, f"audio_{chat_id}_{msg_id}{idx}", filename_hint, ".ogg"
                )
            if local_path:
                return MaxAttachment(
                    kind="audio",
                    local_path=local_path,
                    filename=filename,
                    duration=getattr(attach, "duration", None),
                    width=None,
                    height=None,
                    source_type=atype,
                )
            return None

        if "FILE" in atype or "DOCUMENT" in atype or "DOC" in atype:
            file_id = getattr(attach, "file_id", None) or getattr(attach, "id", None)
            if not file_id:
                return None
            local_path, filename = await self._download_file_by_id(
                chat_id, msg_id, file_id, f"doc_{chat_id}_{msg_id}{idx}", filename_hint
            )
            if local_path:
                return MaxAttachment(
                    kind="document",
                    local_path=local_path,
                    filename=filename,
                    duration=None,
                    width=None,
                    height=None,
                    source_type=atype,
                )
            return None

        return None

    def _extract_user_name(self, user_obj) -> Optional[str]:
        """Извлечь имя из pymax User/Contact/Names объекта."""
        if user_obj is None:
            return None
        # User/Contact имеют .names: list[Names], где Names.name, first_name, last_name
        names_list = getattr(user_obj, "names", None)
        if names_list:
            n = names_list[0]
            first = getattr(n, "first_name", None) or getattr(n, "name", None) or ""
            last  = getattr(n, "last_name", None) or ""
            return f"{first} {last}".strip() or None
        # Fallback: прямые атрибуты (для других объектов)
        first = getattr(user_obj, "first_name", None) or getattr(user_obj, "name", None) or ""
        last  = getattr(user_obj, "last_name", None) or ""
        return f"{first} {last}".strip() or None

    async def _handle_raw_message(self, message):
        """Конвертируем raw MAX Message → MaxMessage и вызываем handlers.

        pymax Message fields:
          .id         — int message id
          .chat_id    — int (положительный = DM, отрицательный = группа)
          .sender     — int user_id отправителя (не объект!)
          .text       — str
          .attaches   — list вложений
        """
        try:
            raw_msg_id = str(getattr(message, "id", None) or "")
            chat_id = str(getattr(message, "chat_id", "") or "")
            text    = getattr(message, "text", None) or None
            if text == "":
                text = None
            message_type = str(getattr(message, "type", None) or "") or None
            status = str(getattr(message, "status", None) or "").upper() or None
            msg_id = f"{raw_msg_id}:{status}" if raw_msg_id and status else raw_msg_id

            # Отправитель: message.sender — это int
            sender_int = getattr(message, "sender", None)
            sender_id  = str(sender_int) if sender_int is not None else None
            reply_to_msg_id = self._extract_reply_to_msg_id(message)

            if not raw_msg_id or not chat_id:
                logger.debug("Skipping message without id/chat_id")
                return

            is_own = bool(self._own_id and sender_id == self._own_id)
            if is_own:
                if self._consume_expected_outbound_id(chat_id, raw_msg_id):
                    logger.debug("Suppressed expected own echo chat_id=%s msg_id=%s", chat_id, raw_msg_id)
                    return
                pending = self._claim_pending_outbound_ack(chat_id, text, reply_to_msg_id)
                if pending:
                    if not pending.future.done():
                        pending.future.set_result(raw_msg_id)
                    logger.debug("Suppressed acknowledged own echo chat_id=%s msg_id=%s", chat_id, raw_msg_id)
                    return

            # DM: chat_id > 0 (личная переписка), группа/канал: chat_id < 0
            try:
                chat_id_int = int(chat_id)
                is_dm = chat_id_int > 0
            except (ValueError, TypeError):
                is_dm = not chat_id.startswith("-")

            # Название чата: для групп ищем в кеше client.chats
            chat_title = None
            if not is_dm and self._client:
                try:
                    chat_obj = next(
                        (c for c in self._client.chats if c.id == chat_id_int), None
                    )
                    if chat_obj:
                        chat_title = getattr(chat_obj, "title", None)
                except Exception:
                    pass

            # Имя отправителя: кеш + live fallback (важно для групповых чатов)
            sender_name = None
            if sender_id:
                sender_name = await self.resolve_user_name(sender_id)

            attaches = getattr(message, "attaches", None) or []
            attach_list = attaches if isinstance(attaches, list) else [attaches]
            attachment_types = [
                self._attachment_type_name(attach)
                for attach in attach_list
                if attach is not None
            ]

            logger.info(
                "MAX message: chat_id=%s is_dm=%s sender_id=%s sender_name=%r "
                "chat_title=%r type=%s status=%s has_text=%s attach_types=%s",
                chat_id, is_dm, sender_id, sender_name, chat_title, message_type,
                status, bool(text), attachment_types,
            )

            # own_id сохраняем в msg для фильтрации в BridgeCore
            # (не фильтруем здесь — bridge решает сам)

            # Вложения (в pymax Message это .attaches)
            attachments: list[MaxAttachment] = []
            rendered_texts: list[str] = []
            media_index = 0
            for attach in attach_list:
                if attach is None:
                    continue
                atype = self._attachment_type_name(attach)
                if atype in {"PHOTO", "VIDEO", "AUDIO", "FILE"}:
                    attachment = await self._download_attachment(
                        chat_id, raw_msg_id, attach, index=media_index
                    )
                    media_index += 1
                    if attachment:
                        attachments.append(attachment)
                    continue

                if atype == "CONTROL":
                    rendered = await self._render_control_attach(attach, sender_id, sender_name)
                elif atype == "CONTACT":
                    rendered = self._render_contact_attach(attach)
                elif atype == "STICKER":
                    rendered = self._render_sticker_attach(attach)
                else:
                    rendered = f"[Вложение MAX: {atype.lower()}]" if atype else None

                if rendered:
                    rendered_texts.append(rendered)

            if status == "EDITED":
                rendered_texts.insert(0, "[Сообщение отредактировано]")
            elif status == "REMOVED":
                rendered_texts = ["[Сообщение удалено]"]

            if not text and not attachments and not rendered_texts and message_type:
                if message_type.upper() not in {"TEXT", "USER"}:
                    rendered_texts.append(f"[Системное сообщение MAX: {message_type.lower()}]")

            mx_msg = MaxMessage(
                msg_id=msg_id,
                chat_id=chat_id,
                chat_title=chat_title,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                attachments=attachments,
                attachment_types=attachment_types,
                rendered_texts=rendered_texts,
                message_type=message_type,
                status=status,
                is_dm=is_dm,
                is_own=is_own,
                raw=message,
            )

            for handler in self._handlers:
                try:
                    await handler(mx_msg)
                except Exception as e:
                    logger.error("Message handler error: %s", e, exc_info=True)

        except Exception as e:
            logger.error("_handle_raw_message error: %s", e, exc_info=True)

    def _build_failfast_interactive_ping(self, client, *, ping_interval: float,
                                         failure_limit: int, ping_opcode,
                                         disconnect_error):
        """Создать ping loop, который форсирует reconnect после серии ошибок.

        Upstream pymax логирует `Interactive ping failed`, но сам reconnect не
        инициирует. В результате сокет может висеть в полуживом состоянии
        несколько минут и терять входящие события. Здесь после N подряд ошибок
        мы закрываем клиента и отдаём управление нашему outer reconnect loop.
        """
        normalized_interval = max(0.0, float(ping_interval))
        normalized_limit = max(1, int(failure_limit))

        async def _send_interactive_ping() -> None:
            consecutive_failures = 0

            while getattr(client, "is_connected", False):
                try:
                    await client._send_and_wait(
                        opcode=ping_opcode,
                        payload={"interactive": True},
                        cmd=0,
                    )
                    if consecutive_failures:
                        client.logger.info(
                            "Interactive ping recovered after %s failure(s)",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    client.logger.debug("Interactive ping sent successfully")
                except disconnect_error:
                    client.logger.debug("Socket disconnected, exiting ping loop")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_failures += 1
                    client.logger.warning(
                        "Interactive ping failed (%s/%s): %s",
                        consecutive_failures,
                        normalized_limit,
                        exc,
                    )
                    if consecutive_failures >= normalized_limit:
                        client.logger.error(
                            "Interactive ping failure limit reached (%s), forcing reconnect",
                            normalized_limit,
                        )
                        try:
                            await client.close()
                        except Exception:
                            client.logger.exception(
                                "Failed to close MAX client after ping failure limit"
                            )
                        break

                await asyncio.sleep(normalized_interval)

        return _send_interactive_ping

    def _install_failfast_interactive_ping(self, client):
        try:
            from pymax.exceptions import SocketNotConnectedError
            from pymax.static.constant import DEFAULT_PING_INTERVAL
            from pymax.static.enum import Opcode
        except Exception as e:
            logger.warning("Could not install fail-fast interactive ping loop: %s", e)
            return client

        client._send_interactive_ping = self._build_failfast_interactive_ping(
            client,
            ping_interval=DEFAULT_PING_INTERVAL,
            failure_limit=self._interactive_ping_failure_limit,
            ping_opcode=Opcode.PING,
            disconnect_error=SocketNotConnectedError,
        )
        logger.debug(
            "Installed fail-fast interactive ping loop failure_limit=%s interval=%ss",
            self._interactive_ping_failure_limit,
            DEFAULT_PING_INTERVAL,
        )
        return client

    async def _make_client(self):
        """Создать свежий SocketMaxClient (без накопленного кеша)."""
        from pymax import SocketMaxClient
        session_name = Path(self._session_name).stem
        client = SocketMaxClient(
            phone=self._phone,
            work_dir=self._data_dir,
            session_name=session_name,
            reconnect=False,              # управляем reconnect сами
            send_fake_telemetry=False,    # отключаем телеметрию — она вызывает SSL ошибки
        )
        return self._install_failfast_interactive_ping(client)

    async def start(self):
        """Запустить клиент с собственным reconnect-циклом.

        reconnect=False в pymax + outer loop: каждый раз создаём свежий клиент,
        чтобы не накапливать кеш dialogs/chats (pymax bug при reconnect=True).
        """
        retry_delay = 5
        first_connect = True

        while True:
            self._client = await self._make_client()

            async def _on_start():
                logger.info("MAX connected")
                self._started = True
                # Получаем ID собственного аккаунта для фильтрации эхо
                try:
                    me = self._client.me
                    if me:
                        self._own_id = str(getattr(me, "id", None) or "")
                        logger.info("Own MAX user_id=%s", self._own_id)
                    else:
                        logger.warning("client.me is None after connect")
                except Exception as e:
                    logger.warning("Could not get own user_id: %s", e)

                nonlocal first_connect
                if first_connect:
                    first_connect = False
                    for h in self._start_handlers:
                        try:
                            await h()
                        except Exception as e:
                            logger.error("on_start handler error: %s", e)
                else:
                    logger.info("MAX reconnected")

            self._client.on_start(_on_start)
            self._client.on_message()(self._handle_raw_message)
            self._client.on_message_edit()(self._handle_raw_message)
            self._client.on_message_delete()(self._handle_raw_message)

            logger.info("Starting MAX adapter phone=%s", self._phone)
            try:
                await self._client.start()
            except Exception as e:
                logger.error("MAX client error: %s", e)

            # Клиент завершился — ждём перед перезапуском
            logger.info("MAX disconnected, reconnecting in %ds...", retry_delay)
            self._started = False
            await asyncio.sleep(retry_delay)

    def is_ready(self) -> bool:
        return self._started
