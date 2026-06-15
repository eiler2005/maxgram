"""
Data access facade — public Repository API for SQLite.

Subdomain SQL lives in src.db.repos.*; this module keeps compatibility for
existing imports and public method names.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite

from .migrations import apply_migrations
from .repos.bindings import BindingsRepo
from .repos.delivery import DeliveryRepo
from .repos.generations import GenerationsRepo
from .repos.pending_inbound import PendingInboundRepo
from .repos.messages import MessagesRepo
from .repos.pending_media import PendingMediaRepo
from .repos.pending_outbound import PendingOutboundRepo
from .repos.recovery import RecoveryRepo
from .repos.users import UsersRepo
from .types import (
    ChatBinding,
    ChatRecoveryEntry,
    DmContactRecoveryEntry,
    KnownUser,
    MessageRecord,
    PendingInboundMessage,
    PendingMediaDownload,
    PendingOutboundMessage,
    TgReplyMapping,
)


class Repository:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._transaction_depth = 0
        get_db = lambda: self._db
        should_autocommit = lambda: self._transaction_depth == 0
        self._bindings = BindingsRepo(get_db, should_autocommit)
        self._messages = MessagesRepo(get_db, should_autocommit)
        self._recovery = RecoveryRepo(
            get_db,
            self._bindings.remap_binding_by_topic,
            should_autocommit,
        )
        self._generations = GenerationsRepo(
            get_db,
            self._recovery._log_recovery_event,
            should_autocommit,
        )
        self._pending_media = PendingMediaRepo(get_db, should_autocommit)
        self._pending_inbound = PendingInboundRepo(get_db, should_autocommit)
        self._pending_outbound = PendingOutboundRepo(get_db, should_autocommit)
        self._delivery = DeliveryRepo(get_db, should_autocommit)
        self._users = UsersRepo(get_db, should_autocommit)

    async def connect(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await apply_migrations(self._db)

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["Repository"]:
        if self._db is None:
            raise RuntimeError("Repository is not connected")
        if self._transaction_depth:
            raise RuntimeError("Nested repository transactions are not supported")

        await self._db.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield self
        except Exception:
            await self._db.rollback()
            raise
        else:
            await self._db.commit()
        finally:
            self._transaction_depth = 0

    # ── ChatBinding ────────────────────────────────────────────────────────

    async def get_binding(self, max_chat_id: str) -> Optional[ChatBinding]:
        return await self._bindings.get_binding(max_chat_id)

    async def get_binding_by_topic(self, tg_topic_id: int) -> Optional[ChatBinding]:
        return await self._bindings.get_binding_by_topic(tg_topic_id)

    async def save_binding(self, binding: ChatBinding):
        await self._bindings.save_binding(binding)

    async def update_mode(self, max_chat_id: str, mode: str):
        await self._bindings.update_mode(max_chat_id, mode)

    async def update_title(self, max_chat_id: str, title: str):
        await self._bindings.update_title(max_chat_id, title)

    async def remap_binding_by_topic(self, tg_topic_id: int, new_max_chat_id: str) -> Optional[ChatBinding]:
        return await self._bindings.remap_binding_by_topic(tg_topic_id, new_max_chat_id)

    async def find_phantom_topic_bindings(self) -> list[ChatBinding]:
        return await self._bindings.find_phantom_topic_bindings()

    async def list_bindings(self) -> list[ChatBinding]:
        return await self._bindings.list_bindings()

    # ── MessageMap / TgReplyMap ────────────────────────────────────────────

    async def is_duplicate(self, max_msg_id: str, max_chat_id: str) -> bool:
        return await self._messages.is_duplicate(max_msg_id, max_chat_id)

    async def save_message(self, record: MessageRecord):
        await self._messages.save_message(record)

    async def get_tg_msg_by_max(self, max_chat_id: str, max_msg_id: str) -> Optional[int]:
        return await self._messages.get_tg_msg_by_max(max_chat_id, max_msg_id)

    async def get_max_msg_id_by_tg(self, tg_msg_id: int) -> Optional[str]:
        return await self._messages.get_max_msg_id_by_tg(tg_msg_id)

    async def get_tg_reply_mapping(self, tg_msg_id: int) -> Optional[TgReplyMapping]:
        return await self._messages.get_tg_reply_mapping(tg_msg_id)

    async def save_tg_reply_mapping(
        self,
        tg_msg_id: int,
        max_chat_id: str,
        max_msg_id: str,
        tg_topic_id: Optional[int],
        *,
        source: str,
        commit: bool = True,
    ):
        await self._messages.save_tg_reply_mapping(
            tg_msg_id,
            max_chat_id,
            max_msg_id,
            tg_topic_id,
            source=source,
            commit=commit,
        )

    # ── MAX account recovery registry ──────────────────────────────────────

    async def upsert_max_account_generation(
        self,
        *,
        max_user_id: str,
        masked_phone: Optional[str],
        session_fingerprint_hash: Optional[str],
    ) -> dict[str, object]:
        return await self._generations.upsert_max_account_generation(
            max_user_id=max_user_id,
            masked_phone=masked_phone,
            session_fingerprint_hash=session_fingerprint_hash,
        )

    async def upsert_recovery_snapshot(
        self,
        entries: list[dict[str, object]],
        *,
        reason: str = "scan",
    ) -> dict[str, int]:
        return await self._recovery.upsert_recovery_snapshot(entries, reason=reason)

    async def list_recovery_entries(self) -> list[ChatRecoveryEntry]:
        return await self._recovery.list_recovery_entries()

    async def upsert_dm_contact_recovery_snapshot(
        self,
        contacts: list[dict[str, object]],
        *,
        reason: str = "scan",
    ) -> dict[str, int]:
        return await self._recovery.upsert_dm_contact_recovery_snapshot(
            contacts,
            reason=reason,
        )

    async def list_dm_contact_recovery_entries(self) -> list[DmContactRecoveryEntry]:
        return await self._recovery.list_dm_contact_recovery_entries()

    async def get_recovery_entry_by_topic(self, tg_topic_id: int) -> Optional[ChatRecoveryEntry]:
        return await self._recovery.get_recovery_entry_by_topic(tg_topic_id)

    async def update_recovery_entry(self, tg_topic_id: int, fields: dict[str, object]) -> Optional[ChatRecoveryEntry]:
        return await self._recovery.update_recovery_entry(tg_topic_id, fields)

    async def remap_recovery_topic(self, tg_topic_id: int, new_max_chat_id: str) -> Optional[ChatBinding]:
        return await self._recovery.remap_recovery_topic(tg_topic_id, new_max_chat_id)

    async def get_recovery_report(self) -> dict[str, object]:
        return await self._recovery.get_recovery_report()

    async def export_recovery_registry(self) -> dict[str, object]:
        return await self._recovery.export_recovery_registry()

    # ── PendingMediaDownloads ──────────────────────────────────────────────

    async def find_active_pending_media(
        self,
        *,
        max_chat_id: str,
        max_msg_id: str,
        attachment_index: int,
        kind: str,
    ) -> Optional[PendingMediaDownload]:
        return await self._pending_media.find_active_pending_media(
            max_chat_id=max_chat_id,
            max_msg_id=max_msg_id,
            attachment_index=attachment_index,
            kind=kind,
        )

    async def find_active_pending_media_by_reference(
        self,
        *,
        media_chat_id: str,
        media_msg_id: str,
        attachment_index: int,
        kind: str,
        reference_kind: str,
        reference_id: str,
    ) -> Optional[PendingMediaDownload]:
        return await self._pending_media.find_active_pending_media_by_reference(
            media_chat_id=media_chat_id,
            media_msg_id=media_msg_id,
            attachment_index=attachment_index,
            kind=kind,
            reference_kind=reference_kind,
            reference_id=reference_id,
        )

    async def enqueue_pending_media(self, job: PendingMediaDownload) -> int:
        return await self._pending_media.enqueue_pending_media(job)

    async def get_due_pending_media(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 5,
    ) -> list[PendingMediaDownload]:
        return await self._pending_media.get_due_pending_media(now=now, limit=limit)

    async def lease_pending_media(
        self,
        job_id: int,
        *,
        lease_until: int,
        now: Optional[int] = None,
    ) -> bool:
        return await self._pending_media.lease_pending_media(
            job_id,
            lease_until=lease_until,
            now=now,
        )

    async def mark_pending_media_retry(
        self,
        job_id: int,
        *,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ):
        await self._pending_media.mark_pending_media_retry(
            job_id,
            error=error,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    async def mark_pending_media_delivered(
        self,
        job_id: int,
        *,
        tg_msg_id: int,
        now: Optional[int] = None,
    ):
        await self._pending_media.mark_pending_media_delivered(
            job_id,
            tg_msg_id=tg_msg_id,
            now=now,
        )

    async def mark_pending_media_failed(
        self,
        job_id: int,
        *,
        error: str,
        now: Optional[int] = None,
    ):
        await self._pending_media.mark_pending_media_failed(job_id, error=error, now=now)

    async def count_pending_media(self) -> dict[str, Optional[int]]:
        return await self._pending_media.count_pending_media()

    # ── PendingInboundMessages ────────────────────────────────────────────

    async def enqueue_pending_inbound(self, job: PendingInboundMessage) -> int:
        return await self._pending_inbound.enqueue_pending_inbound(job)

    async def get_due_pending_inbound(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 5,
    ) -> list[PendingInboundMessage]:
        return await self._pending_inbound.get_due_pending_inbound(now=now, limit=limit)

    async def lease_pending_inbound(
        self,
        job_id: int,
        *,
        lease_until: int,
        now: Optional[int] = None,
    ) -> bool:
        return await self._pending_inbound.lease_pending_inbound(
            job_id,
            lease_until=lease_until,
            now=now,
        )

    async def mark_pending_inbound_retry(
        self,
        job_id: int,
        *,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ):
        await self._pending_inbound.mark_pending_inbound_retry(
            job_id,
            error=error,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    async def mark_pending_inbound_delivered(
        self,
        job_id: int,
        *,
        tg_msg_id: int,
        now: Optional[int] = None,
    ):
        await self._pending_inbound.mark_pending_inbound_delivered(
            job_id,
            tg_msg_id=tg_msg_id,
            now=now,
        )

    async def mark_pending_inbound_failed(
        self,
        job_id: int,
        *,
        error: str,
        now: Optional[int] = None,
    ):
        await self._pending_inbound.mark_pending_inbound_failed(job_id, error=error, now=now)

    async def expire_pending_inbound(
        self,
        *,
        older_than_seconds: int,
        now: Optional[int] = None,
    ) -> int:
        return await self._pending_inbound.expire_pending_inbound(
            older_than_seconds=older_than_seconds,
            now=now,
        )

    async def count_pending_inbound(self) -> dict[str, Optional[int]]:
        return await self._pending_inbound.count_pending_inbound()

    # ── PendingOutboundMessages ───────────────────────────────────────────

    async def enqueue_pending_outbound(self, job: PendingOutboundMessage) -> int:
        return await self._pending_outbound.enqueue_pending_outbound(job)

    async def get_due_pending_outbound(
        self,
        *,
        now: Optional[int] = None,
        limit: int = 5,
    ) -> list[PendingOutboundMessage]:
        return await self._pending_outbound.get_due_pending_outbound(now=now, limit=limit)

    async def lease_pending_outbound(
        self,
        job_id: int,
        *,
        lease_until: int,
        now: Optional[int] = None,
    ) -> bool:
        return await self._pending_outbound.lease_pending_outbound(
            job_id,
            lease_until=lease_until,
            now=now,
        )

    async def mark_pending_outbound_retry(
        self,
        job_id: int,
        *,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ):
        await self._pending_outbound.mark_pending_outbound_retry(
            job_id,
            error=error,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    async def mark_pending_outbound_delivered(
        self,
        job_id: int,
        *,
        max_msg_id: str,
        now: Optional[int] = None,
    ):
        await self._pending_outbound.mark_pending_outbound_delivered(
            job_id,
            max_msg_id=max_msg_id,
            now=now,
        )

    async def mark_pending_outbound_failed(
        self,
        job_id: int,
        *,
        error: str,
        now: Optional[int] = None,
    ):
        await self._pending_outbound.mark_pending_outbound_failed(job_id, error=error, now=now)

    async def expire_pending_outbound(
        self,
        *,
        older_than_seconds: int,
        now: Optional[int] = None,
    ) -> int:
        return await self._pending_outbound.expire_pending_outbound(
            older_than_seconds=older_than_seconds,
            now=now,
        )

    async def count_pending_outbound(self) -> dict[str, Optional[int]]:
        return await self._pending_outbound.count_pending_outbound()

    # ── DeliveryLog / Stats / Retention ────────────────────────────────────

    async def log_delivery(self, max_msg_id: str, max_chat_id: str,
                           direction: str, status: str, error: str = None,
                           attempts: int = 1):
        await self._delivery.log_delivery(
            max_msg_id,
            max_chat_id,
            direction,
            status,
            error,
            attempts,
        )

    async def get_failed_messages(self, limit: int = 50) -> list[dict]:
        return await self._delivery.get_failed_messages(limit)

    async def count_messages_since(self, since_ts: int) -> dict[str, int]:
        return await self._delivery.count_messages_since(since_ts)

    async def count_deliveries_since(self, since_ts: int) -> dict[str, int]:
        return await self._delivery.count_deliveries_since(since_ts)

    async def get_chat_activity_since(self, since_ts: int,
                                      limit: int = 10) -> list[dict]:
        return await self._delivery.get_chat_activity_since(since_ts, limit)

    async def get_chat_activity_map_since(self, since_ts: int) -> dict[str, dict[str, int]]:
        return await self._delivery.get_chat_activity_map_since(since_ts)

    async def cleanup_old_messages(self, older_than_days: int):
        await self._delivery.cleanup_old_messages(older_than_days)

    async def cleanup_old_logs(self, older_than_days: int):
        await self._delivery.cleanup_old_logs(older_than_days)

    # ── KnownUsers ────────────────────────────────────────────────────────

    async def save_user(self, user_id: str, display_name: str):
        await self._users.save_user(user_id, display_name)

    async def find_user_by_name(self, display_name: str) -> Optional[str]:
        return await self._users.find_user_by_name(display_name)
