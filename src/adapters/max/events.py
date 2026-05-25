from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import msgpack

from ...bridge.contracts import (
    MaxAttachment,
    MaxAttachmentFailure,
    MaxMessage,
    is_probable_client_cid,
)
from .deps import EventsDeps
from ...logging_utils import build_max_flow_id, log_event, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")

_CHANNEL_METADATA_MESSAGE_TYPES = {"CHANNEL", "FORWARD", "FORWARDED"}
_CHANNEL_METADATA_ONLY_FIELDS = {
    "attaches",
    "attachments",
    "chatId",
    "chat_id",
    "cid",
    "elements",
    "id",
    "link",
    "mark",
    "messageId",
    "message_id",
    "options",
    "prevMessageId",
    "prev_message_id",
    "reactionInfo",
    "reaction_info",
    "sender",
    "stats",
    "status",
    "text",
    "time",
    "ttl",
    "type",
    "unread",
}
_CHANNEL_METADATA_MARKER_FIELDS = {
    "cid",
    "mark",
    "options",
    "prevMessageId",
    "prev_message_id",
    "reactionInfo",
    "reaction_info",
    "stats",
    "unread",
}


class MaxEventsService:
    def __init__(self, deps: EventsDeps):
        self._deps = deps

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    @property
    def _handlers(self):
        return self._deps.handlers

    @property
    def _raw_payload(self):
        return self._deps.raw_payload

    @property
    def _media(self):
        return self._deps.media

    @property
    def _resolver(self):
        return self._deps.resolver

    @property
    def _runtime(self):
        return self._deps.runtime

    @property
    def _voice_recovery(self):
        return self._deps.voice_recovery

    @staticmethod
    def _normalize_text(value) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, bytes):
            msgpack_text = MaxEventsService._extract_msgpack_text(value)
            if msgpack_text:
                return msgpack_text
            try:
                return value.decode("utf-8") or None
            except UnicodeDecodeError:
                return None
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _extract_msgpack_text(value: bytes) -> str | None:
        try:
            payload = msgpack.unpackb(value, raw=False, strict_map_key=False)
        except Exception:
            return None
        return MaxEventsService._find_text_value(payload)

    @staticmethod
    def _find_text_value(value) -> str | None:
        if isinstance(value, dict):
            direct = value.get("text")
            if isinstance(direct, str) and direct:
                return direct
            for key in ("message", "msg", "content"):
                nested = MaxEventsService._find_text_value(value.get(key))
                if nested:
                    return nested
        return None

    async def _handle_raw_receive(self, data: dict):
        """Перехватить channel wrappers до потери вложенного контента в pymax."""
        raw_opcode = data.get("opcode") if isinstance(data, dict) else None
        opcode_value = getattr(raw_opcode, "value", raw_opcode)
        if not isinstance(data, dict):
            return
        if int(opcode_value or 0) != 128:
            if int(opcode_value or 0) == 49:
                self._raw_payload._cache_raw_history_payload(data.get("payload") or {})
            self._raw_payload._log_raw_auxiliary_event(data)
            return

        payload = data.get("payload") or {}
        identity = self._raw_payload._raw_payload_message_identity(payload)
        if identity and self._raw_payload._is_raw_processed_message(*identity):
            return
        if identity:
            self._raw_payload._mark_raw_processed_message(*identity)

        unwrapped = self._raw_payload._build_unwrapped_channel_message(payload)
        if unwrapped is None:
            regular = self._raw_payload._build_raw_regular_message(payload)
            if regular is None:
                message, _outer_chat_id = self._raw_payload._payload_message_dict(payload)
                if message:
                    if self._raw_payload._message_dict_has_content(message):
                        self._raw_payload._log_raw_message_missing_chat_id(payload)
                    else:
                        self._raw_payload._log_raw_empty_message(payload)
                else:
                    self._raw_payload._log_raw_unhandled_message_payload(payload)
                return

            chat_id = str(getattr(regular, "chat_id", "") or "")
            msg_id = str(getattr(regular, "id", "") or "")
            if chat_id and msg_id:
                self._raw_payload._mark_raw_unwrapped_message(chat_id, msg_id)

            await self._handle_raw_message(regular)
            return

        chat_id = str(getattr(unwrapped, "chat_id", "") or "")
        msg_id = str(getattr(unwrapped, "id", "") or "")
        if chat_id and msg_id:
            self._raw_payload._mark_raw_unwrapped_message(chat_id, msg_id)

        await self._handle_raw_message(unwrapped)

    def _install_raw_message_interceptor(self, client):
        result = client.install_raw_message_interceptor(self._handle_raw_receive)
        if not result.installed:
            log_event(
                logger,
                logging.WARNING,
                "max.raw.interceptor_missing",
                stage="startup",
                outcome="skipped",
                reason=result.reason or "client_has_no_message_notification_handler",
            )
            return client
        log_event(
            logger,
            logging.INFO,
            "max.raw.interceptor_installed",
            stage="startup",
            outcome="installed",
            raw_handler_count=result.raw_handler_count,
        )
        return client

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
            name = await self._resolver.resolve_user_name(uid)
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

        if event in {"add", "invite", "join", "joined", "joinbylink", "join_by_link", "joinedbylink"}:
            if event in {"joinbylink", "join_by_link", "joinedbylink"}:
                if rendered_users:
                    return f"Присоединились по ссылке: {rendered_users}"
                # Имя присоединившегося может прийти через sender
                name = actor
                if not name and sender_id:
                    name = await self._resolver.resolve_user_name(sender_id)
                if name:
                    return f"Присоединился по ссылке: {name}"
                return "Участник присоединился по ссылке"
            if rendered_users:
                return f"Добавлены участники: {rendered_users}"
            return "В чат добавлен участник"

        if event in {"leave", "left", "exit"}:
            if actor:
                return f"{actor} вышел(а) из чата"
            if sender_id:
                resolved_actor = await self._resolver.resolve_user_name(sender_id)
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

    def _attachment_log_summary(self, attachments: list["MaxAttachment"]) -> list[dict[str, object]]:
        return [
            {
                "kind": attachment.kind,
                "source_type": attachment.source_type,
                "filename": sanitize_path(attachment.filename or attachment.local_path),
                "duration": attachment.duration,
                "width": attachment.width,
                "height": attachment.height,
            }
            for attachment in attachments
        ]

    def _attachment_failure_log_summary(
        self,
        failures: list["MaxAttachmentFailure"],
    ) -> list[dict[str, object]]:
        return [
            {
                "kind": failure.kind,
                "source_type": failure.source_type,
                "filename": sanitize_path(failure.filename),
                "index": failure.index,
                "reason": failure.reason,
                "retryable": failure.retryable,
                "reference_kind": failure.reference_kind,
            }
            for failure in failures
        ]

    def _attachment_kind_for_type(self, atype: str) -> str:
        if atype == "PHOTO":
            return "photo"
        if atype == "VIDEO":
            return "video"
        if atype == "AUDIO":
            return "audio"
        if atype == "FILE":
            return "document"
        return atype.lower() or "unknown"

    def _build_attachment_failure(
        self,
        *,
        atype: str,
        raw_type: str,
        attach,
        index: int,
        filename: Optional[str],
        media_chat_id: str,
        media_msg_id: str,
        reason: str = "download_failed",
    ) -> MaxAttachmentFailure:
        retryable = False
        reference_kind = None
        reference_id = None
        if atype == "VIDEO":
            video_id = (
                getattr(attach, "video_id", None)
                or getattr(attach, "videoId", None)
                or getattr(attach, "id", None)
            )
            if video_id is not None:
                retryable = True
                reference_kind = "video_id"
                reference_id = str(video_id)
        elif atype == "AUDIO":
            audio_id = getattr(attach, "audio_id", None) or getattr(attach, "audioId", None)
            file_id = getattr(attach, "file_id", None) or getattr(attach, "fileId", None)
            if audio_id is not None:
                retryable = True
                reference_kind = "audio_id"
                reference_id = str(audio_id)
            elif file_id is not None:
                retryable = True
                reference_kind = "file_id"
                reference_id = str(file_id)

        return MaxAttachmentFailure(
            kind=self._attachment_kind_for_type(atype),
            source_type=raw_type,
            filename=filename,
            index=index,
            reason=reason,
            retryable=retryable,
            media_chat_id=str(media_chat_id) if media_chat_id is not None else None,
            media_msg_id=str(media_msg_id) if media_msg_id is not None else None,
            reference_kind=reference_kind,
            reference_id=reference_id,
            duration=self._media._duration_seconds(
                getattr(attach, "duration", None),
                kind=self._attachment_kind_for_type(atype),
            ),
            width=getattr(attach, "width", None),
            height=getattr(attach, "height", None),
        )

    def _should_skip_empty_event(self, message_type: Optional[str], text: Optional[str],
                                 attachments: list["MaxAttachment"],
                                 rendered_texts: list[str], reaction_info,
                                 attachment_failures: list["MaxAttachmentFailure"] | None = None) -> bool:
        if text or attachments or rendered_texts or attachment_failures:
            return False

        normalized_type = str(message_type or "").upper()
        if reaction_info is not None:
            return True

        return normalized_type in {"", "TEXT", "USER"}

    def _is_channel_metadata_only_event(self, message_type: Optional[str], message, content_message) -> bool:
        normalized_type = str(message_type or "").upper()
        if normalized_type not in _CHANNEL_METADATA_MESSAGE_TYPES:
            return False

        fields = set(self._raw_payload._object_field_names(message))
        fields.update(self._raw_payload._object_field_names(content_message))
        if not fields:
            return False
        if fields - _CHANNEL_METADATA_ONLY_FIELDS:
            return False
        return bool(fields & _CHANNEL_METADATA_MARKER_FIELDS)

    async def _handle_raw_message(self, message):
        """Конвертируем raw MAX Message → MaxMessage и вызываем handlers.

        MAX client message view fields:
          .id         — int message id
          .chat_id    — int (положительный = DM, отрицательный = группа)
          .sender     — int user_id отправителя (не объект!)
          .text       — str
          .attaches   — list вложений
        """
        try:
            raw_msg_id = str(getattr(message, "id", None) or "")
            chat_id = str(getattr(message, "chat_id", "") or "")
            if (
                raw_msg_id
                and chat_id
                and not getattr(message, "_from_raw_unwrapped", False)
                and self._raw_payload._consume_raw_unwrapped_message(chat_id, raw_msg_id)
            ):
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.skipped",
                    direction="inbound",
                    stage="received",
                    outcome="skipped",
                    reason="raw_unwrapped_duplicate",
                    max_chat_id=chat_id,
                    max_msg_id=raw_msg_id,
                )
                return

            forwarded = self._raw_payload._extract_forwarded_payload(message)
            content_message = forwarded.message if forwarded else message

            text = self._normalize_text(
                getattr(content_message, "text", None)
                or getattr(message, "text", None)
                or None
            )
            message_type = str(
                getattr(content_message, "type", None)
                or getattr(message, "type", None)
                or ""
            ) or None
            status = str(getattr(message, "status", None) or "").upper() or None
            reaction_info = (
                getattr(message, "reactionInfo", None)
                or getattr(message, "reaction_info", None)
                or getattr(content_message, "reactionInfo", None)
                or getattr(content_message, "reaction_info", None)
            )
            msg_id = f"{raw_msg_id}:{status}" if raw_msg_id and status else raw_msg_id
            media_chat_id = (
                forwarded.chat_id
                if forwarded
                else getattr(message, "_forward_source_chat_id", None)
            ) or chat_id
            media_msg_id = (
                forwarded.msg_id
                if forwarded
                else getattr(message, "_forward_source_msg_id", None)
            ) or raw_msg_id

            # Отправитель: message.sender — это int
            sender_int = getattr(message, "sender", None)
            sender_id  = str(sender_int) if sender_int is not None else None
            reply_to_msg_id = self._raw_payload._extract_reply_to_msg_id(message)
            flow_id = build_max_flow_id(chat_id, msg_id or raw_msg_id)

            if not raw_msg_id or not chat_id:
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.skipped",
                    stage="received",
                    outcome="skipped",
                    reason="missing_identifiers",
                )
                return

            if is_probable_client_cid(chat_id):
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="received",
                    outcome="skipped",
                    reason="probable_client_cid_chat_id",
                    max_msg_id=msg_id,
                    message_type=message_type,
                    message_fields=self._media._safe_attachment_field_names(message),
                    content_fields=self._media._safe_attachment_field_names(content_message),
                    **self._raw_payload._safe_message_structure_summary(content_message),
                )
                return

            is_own = bool(self._own_id and sender_id == self._own_id)
            if is_own:
                if self._runtime._consume_expected_outbound_id(chat_id, raw_msg_id):
                    log_event(
                        logger,
                        logging.DEBUG,
                        "max.inbound.skipped",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="received",
                        outcome="skipped",
                        reason="expected_echo",
                        max_chat_id=chat_id,
                        max_msg_id=raw_msg_id,
                    )
                    return
                pending = self._runtime._claim_pending_outbound_ack(chat_id, text, reply_to_msg_id)
                if pending:
                    if not pending.future.done():
                        pending.future.set_result(raw_msg_id)
                    log_event(
                        logger,
                        logging.DEBUG,
                        "max.inbound.skipped",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="received",
                        outcome="skipped",
                        reason="acknowledged_echo",
                        max_chat_id=chat_id,
                        max_msg_id=raw_msg_id,
                    )
                    return

            # DM: chat_id > 0 (личная переписка), группа/канал: chat_id < 0
            try:
                chat_id_int = int(chat_id)
                is_dm = chat_id_int > 0
            except (ValueError, TypeError):
                is_dm = not chat_id.startswith("-")

            # Название чата: для групп ищем в кеше клиента.
            chat_title = None
            if not is_dm and self._client:
                try:
                    chat_obj = next(
                        (
                            c
                            for c in self._client.group_chats_snapshot()
                            if c.id == chat_id_int
                        ),
                        None,
                    )
                    if chat_obj:
                        chat_title = getattr(chat_obj, "title", None)
                except Exception:
                    pass

            attaches = getattr(content_message, "attaches", None) or []
            attach_list = attaches if isinstance(attaches, list) else [attaches]
            raw_attachment_types = [
                self._media._attachment_type_name(attach)
                for attach in attach_list
                if attach is not None
            ]
            attachment_types = [
                self._media._normalize_attachment_type(atype)
                for atype in raw_attachment_types
                if atype
            ]

            log_event(
                logger,
                logging.INFO,
                "max.inbound.received",
                flow_id=flow_id,
                direction="inbound",
                stage="received",
                outcome="accepted",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                is_dm=is_dm,
                is_own=is_own,
                message_type=message_type,
                status=status,
                attachment_types=attachment_types,
                has_text=bool(text),
            )

            has_raw_attachments = any(attach is not None for attach in attach_list)
            recoverable_empty_type = str(message_type or "").upper() in {"", "TEXT", "USER"}
            if (
                not text
                and not has_raw_attachments
                and not reaction_info
                and not status
                and recoverable_empty_type
                and not getattr(message, "_from_empty_recovery", False)
            ):
                self._raw_payload._log_typed_empty_message(
                    flow_id=flow_id,
                    message=message,
                    content_message=content_message,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    reaction_info=reaction_info,
                )
                recovered = await self._voice_recovery._recover_empty_message_from_recent_history(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    flow_id=flow_id,
                )
                if recovered is not None:
                    await self._handle_raw_message(recovered)
                    return
                if self._voice_recovery._schedule_empty_recovery_cache_wait(
                    chat_id=chat_id,
                    raw_msg_id=raw_msg_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    flow_id=flow_id,
                ):
                    return
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="empty_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    has_reaction_info=bool(reaction_info),
                )
                return

            if text or has_raw_attachments:
                self._voice_recovery._forget_pending_empty_recovery(
                    chat_id,
                    raw_msg_id,
                    flow_id=flow_id,
                    reason="content_arrived",
                )

            # Имя отправителя: кеш + live fallback (важно для групповых чатов).
            # Для пустых событий recovery уже выполнен выше, чтобы не тратить
            # активный MAX socket на CONTACT_INFO перед CHAT_HISTORY.
            sender_name = None
            if sender_id:
                sender_name = await self._resolver.resolve_user_name(sender_id)

            # own_id сохраняем в msg для фильтрации в BridgeCore
            # (не фильтруем здесь — bridge решает сам)

            # Вложения в MAX client message view.
            attachments: list[MaxAttachment] = []
            attachment_failures: list[MaxAttachmentFailure] = []
            rendered_texts: list[str] = []
            media_index = 0
            for attach in attach_list:
                if attach is None:
                    continue
                raw_type = self._media._attachment_type_name(attach)
                atype = self._media._normalize_attachment_type(raw_type)
                if atype in {"PHOTO", "VIDEO", "AUDIO", "FILE"}:
                    filename_hint = self._media._attachment_filename(attach)
                    attachment = await self._media._download_attachment(
                        media_chat_id,
                        media_msg_id,
                        attach,
                        index=media_index,
                        flow_id=flow_id,
                    )
                    media_index += 1
                    if attachment:
                        attachments.append(attachment)
                    else:
                        attachment_failures.append(
                            self._build_attachment_failure(
                                atype=atype,
                                raw_type=raw_type,
                                attach=attach,
                                index=media_index - 1,
                                filename=filename_hint,
                                media_chat_id=media_chat_id,
                                media_msg_id=media_msg_id,
                            )
                        )
                    continue

                if atype == "CONTROL":
                    rendered = await self._render_control_attach(attach, sender_id, sender_name)
                elif atype == "CONTACT":
                    rendered = self._render_contact_attach(attach)
                elif atype == "STICKER":
                    rendered = self._render_sticker_attach(attach)
                else:
                    rendered = f"[Вложение MAX: {raw_type.lower()}]" if raw_type else None

                if rendered:
                    rendered_texts.append(rendered)

            if status == "EDITED":
                rendered_texts.insert(0, "[Сообщение отредактировано]")
            elif status == "REMOVED":
                rendered_texts = ["[Сообщение удалено]"]

            if self._should_skip_empty_event(
                message_type,
                text,
                attachments,
                rendered_texts,
                reaction_info,
                attachment_failures,
            ):
                self._raw_payload._log_typed_empty_message(
                    flow_id=flow_id,
                    message=message,
                    content_message=content_message,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    message_type=message_type,
                    reaction_info=reaction_info,
                )
                if (
                    not reaction_info
                    and not getattr(message, "_from_empty_recovery", False)
                ):
                    recovered = await self._voice_recovery._recover_empty_message_from_recent_history(
                        chat_id=chat_id,
                        raw_msg_id=raw_msg_id,
                        flow_id=flow_id,
                    )
                    if recovered is not None:
                        await self._handle_raw_message(recovered)
                        return
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="empty_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    has_reaction_info=bool(reaction_info),
                )
                return

            if (
                not text
                and not attachments
                and not rendered_texts
                and not attachment_failures
                and self._is_channel_metadata_only_event(message_type, message, content_message)
            ):
                fields = sorted(
                    set(self._raw_payload._object_field_names(message))
                    | set(self._raw_payload._object_field_names(content_message))
                )
                log_event(
                    logger,
                    logging.INFO,
                    "max.inbound.skipped",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="skipped",
                    reason="channel_metadata_only_event",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    message_type=message_type,
                    metadata_fields=fields,
                )
                return

            if not text and not attachments and not rendered_texts and message_type:
                if message_type.upper() not in {"TEXT", "USER"}:
                    rendered_texts.append(
                        self._raw_payload._render_unknown_message_details(
                            message=message,
                            content_message=content_message,
                            message_type=message_type,
                            status=status,
                            raw_attachment_types=raw_attachment_types,
                            forwarded=forwarded,
                        )
                    )

            log_event(
                logger,
                logging.INFO,
                "max.inbound.normalized",
                flow_id=flow_id,
                direction="inbound",
                stage="normalize",
                outcome="ready",
                max_chat_id=chat_id,
                max_msg_id=msg_id,
                attachments=self._attachment_log_summary(attachments),
                attachment_failures=self._attachment_failure_log_summary(attachment_failures),
                failed_attachment_count=len(attachment_failures),
                attachment_types=attachment_types,
                rendered_count=len(rendered_texts),
                has_text=bool(text),
            )
            if text:
                log_event(
                    logger,
                    logging.DEBUG,
                    "max.inbound.preview",
                    flow_id=flow_id,
                    direction="inbound",
                    stage="normalize",
                    outcome="ready",
                    max_chat_id=chat_id,
                    max_msg_id=msg_id,
                    preview=text,
                )

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
                attachment_failures=attachment_failures,
            )

            for handler in self._handlers:
                try:
                    await handler(mx_msg)
                except Exception as e:
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.inbound.handler_failed",
                        flow_id=flow_id,
                        direction="inbound",
                        stage="dispatch",
                        outcome="failed",
                        max_chat_id=chat_id,
                        max_msg_id=msg_id,
                        error=str(e),
                    )

        except Exception as e:
            log_event(
                logger,
                logging.ERROR,
                "max.inbound.failed",
                stage="received",
                outcome="failed",
                reason="message_parse_failed",
                error=str(e),
            )
