from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional

from . import users as max_users
from .deps import RecoveryDeps
from ...bridge.contracts import (
    MaxRecoveryChatSnapshot,
    MaxRecoveryContactSnapshot,
    MaxRecoverySnapshot,
)
from ...logging_utils import log_event, mask_phone, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")


class MaxRecoveryService:
    def __init__(self, deps: RecoveryDeps):
        self._deps = deps

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    @property
    def _phone(self):
        return self._deps.phone

    @property
    def _data_dir(self):
        return self._deps.data_dir

    @property
    def _session_name(self):
        return self._deps.session_name

    @property
    def _session_store(self):
        return self._deps.session_store

    @property
    def _resolver(self):
        return self._deps.resolver

    def get_session_fingerprint_hash(self) -> Optional[str]:
        session_path = Path(self._data_dir) / self._session_name
        if not session_path.exists():
            return None
        digest = hashlib.sha256()
        try:
            with session_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    def _enum_value(self, value) -> Optional[str]:
        return max_users.enum_value(value)

    def _extract_user_id(self, user_obj) -> Optional[str]:
        return max_users.extract_user_id(user_obj)

    def _iter_userish(self, value):
        yield from max_users.iter_userish(
            value,
            extract_user_name=self._resolver._extract_user_name,
        )

    async def _resolve_recovery_user_name(self, user_id: Optional[str], user_obj=None) -> Optional[str]:
        direct_name = self._resolver._extract_user_name(user_obj)
        if direct_name:
            return direct_name
        if not self._client or not user_id:
            return None
        try:
            cached = self._client.get_cached_user(int(user_id))
            cached_name = self._resolver._extract_user_name(cached)
            if cached_name:
                return cached_name
        except Exception:
            pass
        return await self._resolver.resolve_user_name(str(user_id))

    def _normalize_recovery_chat_kind(self, chat_obj) -> str:
        return max_users.normalize_recovery_chat_kind(chat_obj)

    def _chat_title(self, chat_obj, fallback: str) -> str:
        return max_users.chat_title(chat_obj, fallback)

    def _dialog_partner_id(self, dialog_obj, own_id: Optional[str]) -> Optional[str]:
        return max_users.dialog_partner_id(
            dialog_obj,
            own_id,
            extract_user_name=self._resolver._extract_user_name,
        )

    async def _recovery_snapshot_for_chat(self, chat_obj) -> Optional[MaxRecoveryChatSnapshot]:
        chat_id = getattr(chat_obj, "id", None)
        if chat_id is None:
            return None
        chat_id_str = str(chat_id)
        enriched = chat_obj
        if self._client:
            try:
                enriched_obj = await asyncio.wait_for(self._client.get_chat(int(chat_id)), timeout=5)
                if enriched_obj is not None:
                    enriched = enriched_obj
            except Exception:
                enriched = chat_obj

        owner_obj = getattr(enriched, "owner", None)
        owner_user_id = self._extract_user_id(owner_obj)
        owner_name = await self._resolve_recovery_user_name(owner_user_id, owner_obj)

        admin_contacts: list[dict[str, str]] = []
        seen_admin_ids: set[str] = set()
        for source in (
            owner_obj,
            getattr(enriched, "admins", None),
            getattr(enriched, "admin_participants", None),
        ):
            for admin_obj in self._iter_userish(source):
                admin_id = self._extract_user_id(admin_obj)
                if not admin_id or admin_id in seen_admin_ids:
                    continue
                admin_name = await self._resolve_recovery_user_name(admin_id, admin_obj)
                admin_contacts.append({"user_id": admin_id, "name": admin_name or ""})
                seen_admin_ids.add(admin_id)

        access_type = self._enum_value(getattr(enriched, "access", None))
        if access_type is None:
            access_type = self._enum_value(getattr(enriched, "access_type", None))
        participant_count = getattr(enriched, "participants_count", None)
        try:
            participant_count = int(participant_count) if participant_count is not None else None
        except (TypeError, ValueError):
            participant_count = None

        return MaxRecoveryChatSnapshot(
            max_chat_id=chat_id_str,
            title=self._chat_title(enriched, f"Чат {chat_id_str}"),
            chat_kind=self._normalize_recovery_chat_kind(enriched),
            access_type=access_type,
            invite_link=getattr(enriched, "link", None) or getattr(enriched, "invite_link", None),
            owner_user_id=owner_user_id,
            owner_name=owner_name,
            admin_contacts=admin_contacts,
            participant_count=participant_count,
        )

    async def _recovery_snapshot_for_dialog(
        self,
        dialog_obj,
        own_id: Optional[str],
    ) -> Optional[MaxRecoveryChatSnapshot]:
        chat_id = getattr(dialog_obj, "id", None)
        if chat_id is None:
            return None
        chat_id_str = str(chat_id)
        partner_id = self._dialog_partner_id(dialog_obj, own_id)
        partner_name = await self._resolve_recovery_user_name(partner_id)
        title = self._chat_title(dialog_obj, partner_name or f"DM {partner_id or chat_id_str}")
        return MaxRecoveryChatSnapshot(
            max_chat_id=chat_id_str,
            title=title,
            chat_kind="dm",
            dm_partner_user_id=partner_id,
            dm_partner_name=partner_name,
            participant_count=2 if partner_id else None,
        )

    async def _recovery_contact_snapshot_for_dialog(
        self,
        dialog_obj,
        own_id: Optional[str],
    ) -> Optional[MaxRecoveryContactSnapshot]:
        chat_id = getattr(dialog_obj, "id", None)
        partner_id = self._dialog_partner_id(dialog_obj, own_id)
        if chat_id is None or not partner_id:
            return None
        partner_name = await self._resolve_recovery_user_name(partner_id)
        display_name = self._chat_title(dialog_obj, partner_name or f"DM {partner_id}")
        return MaxRecoveryContactSnapshot(
            max_user_id=partner_id,
            display_name=display_name,
            old_dm_chat_id=str(chat_id),
            current_dm_chat_id=str(chat_id),
            source="dialog",
            recovery_status="visible",
        )

    async def collect_recovery_snapshot(self) -> MaxRecoverySnapshot:
        """Collect account/chat recovery metadata without message contents."""
        own_id = self._own_id
        try:
            me = getattr(self._client, "me", None) if self._client else None
            if me is not None:
                own_id = str(getattr(me, "id", None) or own_id or "") or None
        except Exception:
            pass

        snapshot = MaxRecoverySnapshot(
            max_user_id=own_id,
            masked_phone=mask_phone(self._phone),
            session_fingerprint_hash=self.get_session_fingerprint_hash(),
        )
        if not self._client:
            return snapshot

        seen: set[str] = set()
        chat_sources = [
            *(getattr(self._client, "chats", None) or []),
            *(getattr(self._client, "channels", None) or []),
        ]
        for chat_obj in chat_sources:
            item = await self._recovery_snapshot_for_chat(chat_obj)
            if item and item.max_chat_id not in seen:
                snapshot.chats.append(item)
                seen.add(item.max_chat_id)

        seen_contacts: set[str] = set()
        for dialog_obj in getattr(self._client, "dialogs", None) or []:
            item = await self._recovery_snapshot_for_dialog(dialog_obj, own_id)
            if item and item.max_chat_id not in seen:
                snapshot.chats.append(item)
                seen.add(item.max_chat_id)
            contact = await self._recovery_contact_snapshot_for_dialog(dialog_obj, own_id)
            if contact and contact.max_user_id not in seen_contacts:
                snapshot.contacts.append(contact)
                seen_contacts.add(contact.max_user_id)

        return snapshot

    def _recover_session_if_needed(self, *, first_connect: bool):
        outcome = self._session_store.recover_if_needed()
        if outcome.action in {"ok", "missing", "failed"}:
            if outcome.action == "failed":
                log_event(
                    logger,
                    logging.WARNING,
                    "max.session.recovery_unavailable",
                    stage="startup" if first_connect else "runtime",
                    outcome="skipped",
                    reason=outcome.reason,
                )
            return outcome

        log_event(
            logger,
            logging.INFO,
            "max.session.recovered",
            stage="startup" if first_connect else "runtime",
            outcome=outcome.action,
            reason=outcome.reason,
            backup_path=sanitize_path(str(outcome.backup_path)) if outcome.backup_path else None,
            source_path=sanitize_path(str(outcome.source_path)) if outcome.source_path else None,
        )
        return outcome

    def _backup_session_snapshot(self, *, first_connect: bool):
        outcome = self._session_store.backup_current(reason="connected")
        if outcome.action != "backed_up":
            return outcome

        log_event(
            logger,
            logging.INFO,
            "max.session.backed_up",
            stage="startup" if first_connect else "runtime",
            outcome="backed_up",
            backup_path=sanitize_path(str(outcome.backup_path)) if outcome.backup_path else None,
        )
        return outcome
