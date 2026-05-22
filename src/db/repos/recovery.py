"""MAX account and chat recovery registry repository."""

import time
from collections.abc import Awaitable, Callable
from typing import Optional

from .base import BaseRepo
from ..types import (
    ChatBinding,
    ChatRecoveryEntry,
    DmContactRecoveryEntry,
    _json_compact,
    _json_loads,
)


RemapBinding = Callable[[int, str], Awaitable[Optional[ChatBinding]]]


class RecoveryRepo(BaseRepo):
    def __init__(self, get_db, remap_binding_by_topic: RemapBinding):
        super().__init__(get_db)
        self._remap_binding_by_topic = remap_binding_by_topic

    async def upsert_max_account_generation(
        self,
        *,
        max_user_id: str,
        masked_phone: Optional[str],
        session_fingerprint_hash: Optional[str],
    ) -> dict[str, object]:
        now = int(time.time())
        async with self._db.execute(
            "SELECT * FROM max_account_generations WHERE status = 'active' ORDER BY last_seen_at DESC LIMIT 1"
        ) as cur:
            active = await cur.fetchone()

        previous_max_user_id = active["max_user_id"] if active else None
        migration_required = bool(previous_max_user_id and previous_max_user_id != max_user_id)
        if migration_required:
            await self._db.execute(
                "UPDATE max_account_generations SET status = 'retired' WHERE status = 'active' AND max_user_id != ?",
                (max_user_id,),
            )

        await self._db.execute(
            """INSERT INTO max_account_generations
               (max_user_id, masked_phone, session_fingerprint_hash, status, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, 'active', ?, ?)
               ON CONFLICT(max_user_id) DO UPDATE SET
                 masked_phone = excluded.masked_phone,
                 session_fingerprint_hash = excluded.session_fingerprint_hash,
                 status = 'active',
                 last_seen_at = excluded.last_seen_at""",
            (max_user_id, masked_phone, session_fingerprint_hash, now, now),
        )
        await self._log_recovery_event(
            registry_key=None,
            tg_topic_id=None,
            event_type="account_migration_required" if migration_required else "account_seen",
            details={
                "max_user_id": max_user_id,
                "previous_max_user_id": previous_max_user_id,
            },
            commit=False,
        )
        await self._db.commit()
        return {
            "migration_required": migration_required,
            "previous_max_user_id": previous_max_user_id,
            "max_user_id": max_user_id,
        }

    def _recovery_entry_from_row(self, row) -> ChatRecoveryEntry:
        return ChatRecoveryEntry(**dict(row))

    def _dm_contact_entry_from_row(self, row) -> DmContactRecoveryEntry:
        return DmContactRecoveryEntry(**dict(row))

    async def _log_recovery_event(
        self,
        *,
        registry_key: Optional[str],
        tg_topic_id: Optional[int],
        event_type: str,
        details: Optional[dict[str, object]] = None,
        commit: bool = True,
    ):
        await self._db.execute(
            """INSERT INTO chat_recovery_events
               (registry_key, tg_topic_id, event_type, details_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                registry_key,
                tg_topic_id,
                event_type,
                _json_compact(details or {}),
                int(time.time()),
            ),
        )
        if commit:
            await self._db.commit()

    async def upsert_recovery_snapshot(
        self,
        entries: list[dict[str, object]],
        *,
        reason: str = "scan",
    ) -> dict[str, int]:
        now = int(time.time())
        scanned = 0
        inserted = 0
        status_changed = 0
        unmapped = 0
        needs_invite = 0
        manual_admin_required = 0
        for entry in entries:
            registry_key = str(entry["registry_key"])
            tg_topic_id = entry.get("tg_topic_id")
            title = str(entry.get("title") or registry_key)
            admin_contacts = entry.get("admin_contacts")
            if isinstance(admin_contacts, str):
                admin_contacts_json = admin_contacts
            else:
                admin_contacts_json = _json_compact(admin_contacts or [])
            recovery_status = str(entry.get("recovery_status") or "visible")

            async with self._db.execute(
                "SELECT recovery_status FROM chat_recovery_registry WHERE registry_key = ?",
                (registry_key,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                inserted += 1
            elif existing["recovery_status"] != recovery_status:
                status_changed += 1
            if tg_topic_id is None and recovery_status != "remapped":
                unmapped += 1
            if recovery_status in {"needs_invite", "account_migration_required"}:
                needs_invite += 1
            if recovery_status == "manual_admin_required":
                manual_admin_required += 1

            await self._db.execute(
                """INSERT INTO chat_recovery_registry
                   (registry_key, tg_topic_id, title, old_max_chat_id, current_max_chat_id,
                    chat_kind, mode, priority, access_type, invite_link, owner_user_id,
                    owner_name, admin_contacts_json, dm_partner_user_id, dm_partner_name,
                    participant_count, manual_note, recovery_status, first_seen_at,
                    last_seen_at, last_scan_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(registry_key) DO UPDATE SET
                     tg_topic_id = COALESCE(excluded.tg_topic_id, chat_recovery_registry.tg_topic_id),
                     title = excluded.title,
                     old_max_chat_id = COALESCE(
                       chat_recovery_registry.old_max_chat_id,
                       chat_recovery_registry.current_max_chat_id,
                       excluded.old_max_chat_id,
                       excluded.current_max_chat_id
                     ),
                     current_max_chat_id = COALESCE(excluded.current_max_chat_id, chat_recovery_registry.current_max_chat_id),
                     chat_kind = excluded.chat_kind,
                     mode = excluded.mode,
                     access_type = COALESCE(excluded.access_type, chat_recovery_registry.access_type),
                     invite_link = COALESCE(excluded.invite_link, chat_recovery_registry.invite_link),
                     owner_user_id = COALESCE(excluded.owner_user_id, chat_recovery_registry.owner_user_id),
                     owner_name = COALESCE(excluded.owner_name, chat_recovery_registry.owner_name),
                     admin_contacts_json = CASE
                       WHEN excluded.admin_contacts_json != '[]' THEN excluded.admin_contacts_json
                       ELSE chat_recovery_registry.admin_contacts_json
                     END,
                     dm_partner_user_id = COALESCE(excluded.dm_partner_user_id, chat_recovery_registry.dm_partner_user_id),
                     dm_partner_name = COALESCE(excluded.dm_partner_name, chat_recovery_registry.dm_partner_name),
                     participant_count = COALESCE(excluded.participant_count, chat_recovery_registry.participant_count),
                     recovery_status = excluded.recovery_status,
                     last_seen_at = excluded.last_seen_at,
                     last_scan_at = excluded.last_scan_at""",
                (
                    registry_key,
                    tg_topic_id,
                    title,
                    entry.get("old_max_chat_id"),
                    entry.get("current_max_chat_id"),
                    str(entry.get("chat_kind") or "unknown"),
                    str(entry.get("mode") or "active"),
                    int(entry.get("priority") or 0),
                    entry.get("access_type"),
                    entry.get("invite_link"),
                    entry.get("owner_user_id"),
                    entry.get("owner_name"),
                    admin_contacts_json,
                    entry.get("dm_partner_user_id"),
                    entry.get("dm_partner_name"),
                    entry.get("participant_count"),
                    entry.get("manual_note"),
                    recovery_status,
                    now,
                    now,
                    now,
                ),
            )
            await self._log_recovery_event(
                registry_key=registry_key,
                tg_topic_id=int(tg_topic_id) if tg_topic_id is not None else None,
                event_type="scan",
                details={
                    "reason": reason,
                    "status": recovery_status,
                    "chat_kind": str(entry.get("chat_kind") or "unknown"),
                    "has_invite_link": bool(entry.get("invite_link")),
                },
                commit=False,
            )
            scanned += 1

        await self._db.commit()
        return {
            "scanned": scanned,
            "inserted": inserted,
            "status_changed": status_changed,
            "unmapped": unmapped,
            "needs_invite": needs_invite,
            "manual_admin_required": manual_admin_required,
        }

    async def list_recovery_entries(self) -> list[ChatRecoveryEntry]:
        async with self._db.execute(
            """SELECT * FROM chat_recovery_registry
               ORDER BY COALESCE(tg_topic_id, 999999999), title COLLATE NOCASE"""
        ) as cur:
            rows = await cur.fetchall()
        return [self._recovery_entry_from_row(row) for row in rows]

    async def upsert_dm_contact_recovery_snapshot(
        self,
        contacts: list[dict[str, object]],
        *,
        reason: str = "scan",
    ) -> dict[str, int]:
        now = int(time.time())
        scanned = 0
        inserted = 0
        status_changed = 0
        for contact in contacts:
            max_user_id = str(contact.get("max_user_id") or "").strip()
            if not max_user_id:
                continue
            display_name = str(contact.get("display_name") or max_user_id).strip() or max_user_id
            recovery_status = str(contact.get("recovery_status") or "visible")
            tg_topic_id = contact.get("tg_topic_id")
            if tg_topic_id is not None:
                tg_topic_id = int(tg_topic_id)

            async with self._db.execute(
                "SELECT recovery_status FROM dm_contact_recovery_registry WHERE max_user_id = ?",
                (max_user_id,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                inserted += 1
            elif existing["recovery_status"] != recovery_status:
                status_changed += 1

            await self._db.execute(
                """INSERT INTO dm_contact_recovery_registry
                   (max_user_id, display_name, old_dm_chat_id, current_dm_chat_id,
                    tg_topic_id, source, recovery_status, first_seen_at, last_seen_at,
                    last_scan_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(max_user_id) DO UPDATE SET
                     display_name = excluded.display_name,
                     old_dm_chat_id = COALESCE(
                       dm_contact_recovery_registry.old_dm_chat_id,
                       dm_contact_recovery_registry.current_dm_chat_id,
                       excluded.old_dm_chat_id,
                       excluded.current_dm_chat_id
                     ),
                     current_dm_chat_id = COALESCE(
                       excluded.current_dm_chat_id,
                       dm_contact_recovery_registry.current_dm_chat_id
                     ),
                     tg_topic_id = COALESCE(
                       excluded.tg_topic_id,
                       dm_contact_recovery_registry.tg_topic_id
                     ),
                     source = excluded.source,
                     recovery_status = excluded.recovery_status,
                     last_seen_at = excluded.last_seen_at,
                     last_scan_at = excluded.last_scan_at""",
                (
                    max_user_id,
                    display_name,
                    contact.get("old_dm_chat_id"),
                    contact.get("current_dm_chat_id"),
                    tg_topic_id,
                    str(contact.get("source") or "dialog"),
                    recovery_status,
                    now,
                    now,
                    now,
                ),
            )
            await self._log_recovery_event(
                registry_key=f"dm_contact:{max_user_id}",
                tg_topic_id=tg_topic_id,
                event_type="dm_contact_scan",
                details={
                    "reason": reason,
                    "source": str(contact.get("source") or "dialog"),
                    "status": recovery_status,
                    "has_tg_topic": tg_topic_id is not None,
                },
                commit=False,
            )
            scanned += 1

        await self._db.commit()
        return {
            "scanned": scanned,
            "inserted": inserted,
            "status_changed": status_changed,
        }

    async def list_dm_contact_recovery_entries(self) -> list[DmContactRecoveryEntry]:
        async with self._db.execute(
            """SELECT * FROM dm_contact_recovery_registry
               ORDER BY COALESCE(tg_topic_id, 999999999), display_name COLLATE NOCASE"""
        ) as cur:
            rows = await cur.fetchall()
        return [self._dm_contact_entry_from_row(row) for row in rows]

    async def get_recovery_entry_by_topic(self, tg_topic_id: int) -> Optional[ChatRecoveryEntry]:
        async with self._db.execute(
            "SELECT * FROM chat_recovery_registry WHERE tg_topic_id = ?",
            (tg_topic_id,),
        ) as cur:
            row = await cur.fetchone()
            return self._recovery_entry_from_row(row) if row else None

    async def update_recovery_entry(self, tg_topic_id: int, fields: dict[str, object]) -> Optional[ChatRecoveryEntry]:
        allowed = {
            "priority",
            "access_type",
            "invite_link",
            "owner_user_id",
            "owner_name",
            "admin_contacts_json",
            "dm_partner_user_id",
            "dm_partner_name",
            "manual_note",
            "recovery_status",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return await self.get_recovery_entry_by_topic(tg_topic_id)

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [int(time.time()), tg_topic_id]
        await self._db.execute(
            f"UPDATE chat_recovery_registry SET {assignments}, last_seen_at = ? WHERE tg_topic_id = ?",
            values,
        )
        await self._log_recovery_event(
            registry_key=f"tg_topic:{tg_topic_id}",
            tg_topic_id=tg_topic_id,
            event_type="manual_update",
            details={"fields": sorted(updates.keys())},
            commit=False,
        )
        await self._db.commit()
        return await self.get_recovery_entry_by_topic(tg_topic_id)

    async def remap_recovery_topic(self, tg_topic_id: int, new_max_chat_id: str) -> Optional[ChatBinding]:
        before = await self.get_recovery_entry_by_topic(tg_topic_id)
        binding = await self._remap_binding_by_topic(tg_topic_id, new_max_chat_id)
        if binding is None:
            return None

        await self._db.execute(
            """UPDATE chat_recovery_registry
               SET old_max_chat_id = COALESCE(old_max_chat_id, current_max_chat_id),
                   current_max_chat_id = ?,
                   recovery_status = 'remapped',
                   last_seen_at = ?
               WHERE tg_topic_id = ?""",
            (new_max_chat_id, int(time.time()), tg_topic_id),
        )
        await self._db.execute(
            """UPDATE chat_recovery_registry
               SET recovery_status = 'remapped'
               WHERE registry_key = ?""",
            (f"max_chat:{new_max_chat_id}",),
        )
        await self._db.execute(
            """UPDATE dm_contact_recovery_registry
               SET old_dm_chat_id = COALESCE(old_dm_chat_id, current_dm_chat_id),
                   current_dm_chat_id = ?,
                   recovery_status = 'remapped',
                   last_seen_at = ?
               WHERE tg_topic_id = ?""",
            (new_max_chat_id, int(time.time()), tg_topic_id),
        )
        await self._log_recovery_event(
            registry_key=f"tg_topic:{tg_topic_id}",
            tg_topic_id=tg_topic_id,
            event_type="remap",
            details={
                "old_max_chat_id": before.current_max_chat_id if before else None,
                "new_max_chat_id": new_max_chat_id,
            },
            commit=False,
        )
        await self._db.commit()
        return binding

    async def get_recovery_report(self) -> dict[str, object]:
        entries = await self.list_recovery_entries()
        dm_contacts = await self.list_dm_contact_recovery_entries()
        chat_last_scan_at = max((entry.last_scan_at or 0 for entry in entries), default=0) or None
        dm_contacts_last_scan_at = max(
            (entry.last_scan_at or 0 for entry in dm_contacts),
            default=0,
        ) or None
        stats = {
            "total": len(entries),
            "topics": sum(1 for entry in entries if entry.tg_topic_id is not None),
            "restored": 0,
            "needs_invite": 0,
            "joinable_by_link": 0,
            "manual_admin_required": 0,
            "unmapped": 0,
            "last_scan_at": max(
                timestamp or 0
                for timestamp in (chat_last_scan_at, dm_contacts_last_scan_at)
            ) or None,
            "dm_contacts": len(dm_contacts),
            "dm_contacts_linked": sum(
                1 for entry in dm_contacts if entry.tg_topic_id is not None
            ),
            "dm_contacts_needs_remap": sum(
                1
                for entry in dm_contacts
                if entry.recovery_status in {
                    "needs_contact",
                    "needs_remap",
                    "account_migration_required",
                }
                or not entry.current_dm_chat_id
            ),
            "dm_contacts_last_scan_at": dm_contacts_last_scan_at,
        }
        for entry in entries:
            status = entry.recovery_status
            admins = _json_loads(entry.admin_contacts_json, [])
            if entry.tg_topic_id is None and status != "remapped":
                stats["unmapped"] += 1
            if status in {"visible", "remapped", "tracked"} and entry.current_max_chat_id:
                stats["restored"] += 1
            if entry.invite_link and status not in {"visible", "remapped"}:
                stats["joinable_by_link"] += 1
            if admins and not entry.invite_link and status not in {"visible", "remapped"}:
                stats["manual_admin_required"] += 1
            if status in {"needs_invite", "account_migration_required"}:
                stats["needs_invite"] += 1
        return {"stats": stats, "entries": entries, "dm_contacts": dm_contacts}

    async def export_recovery_registry(self) -> dict[str, object]:
        async with self._db.execute(
            "SELECT * FROM max_account_generations ORDER BY last_seen_at DESC"
        ) as cur:
            accounts = [dict(row) for row in await cur.fetchall()]
        entries = []
        for entry in await self.list_recovery_entries():
            data = dict(entry.__dict__)
            data["admin_contacts"] = _json_loads(data.pop("admin_contacts_json"), [])
            entries.append(data)
        async with self._db.execute(
            "SELECT * FROM chat_recovery_events ORDER BY created_at DESC LIMIT 500"
        ) as cur:
            events = []
            for row in await cur.fetchall():
                event = dict(row)
                event["details"] = _json_loads(event.pop("details_json"), {})
                events.append(event)
        return {
            "exported_at": int(time.time()),
            "accounts": accounts,
            "entries": entries,
            "dm_contacts": [
                dict(entry.__dict__)
                for entry in await self.list_dm_contact_recovery_entries()
            ],
            "events": events,
        }
