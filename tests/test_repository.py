import json

import aiosqlite
import pytest

from src.db.repository import (
    ChatBinding,
    KnownUser,
    MessageRecord,
    PendingInboundMessage,
    PendingMediaDownload,
    PendingOutboundMessage,
    Repository,
)
from src.db.migrations import apply_migrations


@pytest.mark.asyncio
async def test_schema_migrations_apply_fresh_and_are_idempotent(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "bridge.db"))
    db.row_factory = aiosqlite.Row
    try:
        await apply_migrations(db)
        await apply_migrations(db)

        async with db.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ) as cur:
            rows = await cur.fetchall()
        assert [(row["version"], row["name"]) for row in rows] == [
            (1, "baseline_schema"),
            (2, "pending_outbound_messages"),
            (3, "pending_inbound_messages"),
        ]

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message_map'"
        ) as cur:
            row = await cur.fetchone()
        assert row["name"] == "message_map"
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'pending_outbound_messages'"
        ) as cur:
            row = await cur.fetchone()
        assert row["name"] == "pending_outbound_messages"
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'pending_inbound_messages'"
        ) as cur:
            row = await cur.fetchone()
        assert row["name"] == "pending_inbound_messages"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_migrations_baseline_existing_db(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "existing.db"))
    db.row_factory = aiosqlite.Row
    try:
        await db.execute(
            """
            CREATE TABLE chat_bindings (
                max_chat_id TEXT PRIMARY KEY,
                tg_topic_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL
            )
            """
        )
        await db.commit()

        await apply_migrations(db)

        async with db.execute(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ) as cur:
            row = await cur.fetchone()
        assert row["version"] == 1

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'delivery_log'"
        ) as cur:
            table = await cur.fetchone()
        assert table["name"] == "delivery_log"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_save_message_upserts_tg_fields(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.save_message(
            MessageRecord(
                max_msg_id="m1",
                max_chat_id="chat-1",
                tg_msg_id=None,
                tg_topic_id=None,
                direction="inbound",
                created_at=1,
            )
        )

        await repo.save_message(
            MessageRecord(
                max_msg_id="m1",
                max_chat_id="chat-1",
                tg_msg_id=777,
                tg_topic_id=12,
                direction="inbound",
                created_at=2,
            )
        )

        assert await repo.get_max_msg_id_by_tg(777) == "m1"
        assert await repo.get_max_msg_id_by_tg(999) is None

        async with repo._db.execute(
            "SELECT tg_msg_id, tg_topic_id, direction FROM message_map WHERE max_msg_id = ? AND max_chat_id = ?",
            ("m1", "chat-1"),
        ) as cur:
            row = await cur.fetchone()

        assert row["tg_msg_id"] == 777
        assert row["tg_topic_id"] == 12
        assert row["direction"] == "inbound"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_pending_outbound_lifecycle_clears_text_after_delivery(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        job_id = await repo.enqueue_pending_outbound(
            PendingOutboundMessage(
                tg_topic_id=99,
                tg_msg_id=777,
                max_chat_id="123",
                reply_to_max_id="mx-reply",
                text="temporary plaintext",
                attempts=3,
                next_attempt_at=1,
                last_error="Not connected to the server",
            )
        )
        due = await repo.get_due_pending_outbound(now=2)
        assert [job.id for job in due] == [job_id]
        assert due[0].text == "temporary plaintext"

        assert await repo.lease_pending_outbound(job_id, lease_until=100, now=2) is True
        await repo.mark_pending_outbound_delivered(job_id, max_msg_id="mx-out", now=3)

        async with repo._db.execute(
            "SELECT status, text, delivered_max_msg_id FROM pending_outbound_messages "
            "WHERE id = ?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["status"] == "delivered"
        assert row["text"] is None
        assert row["delivered_max_msg_id"] == "mx-out"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_pending_inbound_lifecycle_clears_text_after_delivery(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        job_id = await repo.enqueue_pending_inbound(
            PendingInboundMessage(
                max_chat_id="123",
                max_msg_id="mx-in",
                tg_topic_id=99,
                text="temporary plaintext",
                attempts=1,
                next_attempt_at=1,
                last_error="TelegramNetworkError: connection reset",
            )
        )
        due = await repo.get_due_pending_inbound(now=2)
        assert [job.id for job in due] == [job_id]
        assert due[0].text == "temporary plaintext"

        assert await repo.lease_pending_inbound(job_id, lease_until=100, now=2) is True
        await repo.mark_pending_inbound_delivered(job_id, tg_msg_id=777, now=3)

        async with repo._db.execute(
            "SELECT status, text, delivered_tg_msg_id FROM pending_inbound_messages "
            "WHERE id = ?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row["status"] == "delivered"
        assert row["text"] is None
        assert row["delivered_tg_msg_id"] == 777
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_tg_reply_mapping_resolves_delayed_media_message(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.save_tg_reply_mapping(
            888,
            "chat-1",
            "m-delayed",
            12,
            source="pending_media",
        )

        assert await repo.get_max_msg_id_by_tg(888) == "m-delayed"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_registry_snapshot_report_and_export_are_idempotent(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        first_account = await repo.upsert_max_account_generation(
            max_user_id="100",
            masked_phone="+7******1234",
            session_fingerprint_hash="hash-a",
        )
        second_account = await repo.upsert_max_account_generation(
            max_user_id="200",
            masked_phone="+7******5678",
            session_fingerprint_hash="hash-b",
        )
        assert first_account["migration_required"] is False
        assert second_account["migration_required"] is True
        assert second_account["previous_max_user_id"] == "100"

        invite_link = "https://max.ru/join/example"
        result = await repo.upsert_recovery_snapshot(
            [
                {
                    "registry_key": "tg_topic:77",
                    "tg_topic_id": 77,
                    "title": "VIP group",
                    "old_max_chat_id": "-old",
                    "current_max_chat_id": "-old",
                    "chat_kind": "group",
                    "mode": "active",
                    "access_type": "LINK",
                    "invite_link": invite_link,
                    "owner_user_id": "500",
                    "owner_name": "Owner",
                    "admin_contacts": [{"user_id": "501", "name": "Admin"}],
                    "participant_count": 12,
                    "recovery_status": "visible",
                }
            ],
            reason="unit_test",
        )
        again = await repo.upsert_recovery_snapshot([
            {
                "registry_key": "tg_topic:77",
                "tg_topic_id": 77,
                "title": "VIP group",
                "old_max_chat_id": "-old",
                "current_max_chat_id": "-old",
                "chat_kind": "group",
                "mode": "active",
                "recovery_status": "visible",
            }
        ])

        assert result["scanned"] == 1
        assert result["inserted"] == 1
        assert result["status_changed"] == 0
        assert again["scanned"] == 1
        assert again["inserted"] == 0
        assert again["status_changed"] == 0

        report = await repo.get_recovery_report()
        assert report["stats"]["total"] == 1
        assert report["stats"]["restored"] == 1
        assert report["stats"]["last_scan_at"] is not None

        entry = await repo.get_recovery_entry_by_topic(77)
        assert entry.invite_link == invite_link
        assert "Admin" in entry.admin_contacts_json

        async with repo._db.execute(
            "SELECT details_json FROM chat_recovery_events WHERE event_type = 'scan' ORDER BY id ASC LIMIT 1"
        ) as cur:
            event = await cur.fetchone()
        details = json.loads(event["details_json"])
        assert details["reason"] == "unit_test"
        assert details["has_invite_link"] is True
        assert invite_link not in event["details_json"]

        export = await repo.export_recovery_registry()
        assert export["entries"][0]["admin_contacts"] == [{"name": "Admin", "user_id": "501"}]
        assert export["entries"][0]["last_scan_at"] is not None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_remap_preserves_topic_and_updates_binding(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.save_binding(ChatBinding("old-chat", 77, "Client", "active", 1))
        await repo.upsert_recovery_snapshot([
            {
                "registry_key": "tg_topic:77",
                "tg_topic_id": 77,
                "title": "Client",
                "old_max_chat_id": "old-chat",
                "current_max_chat_id": "old-chat",
                "chat_kind": "dm",
                "mode": "active",
                "recovery_status": "needs_invite",
            },
            {
                "registry_key": "max_chat:new-chat",
                "title": "Client",
                "current_max_chat_id": "new-chat",
                "chat_kind": "dm",
                "mode": "active",
                "recovery_status": "unmapped",
            },
        ])

        binding = await repo.remap_recovery_topic(77, "new-chat")

        assert binding.max_chat_id == "new-chat"
        assert binding.tg_topic_id == 77
        assert (await repo.get_binding_by_topic(77)).max_chat_id == "new-chat"
        entry = await repo.get_recovery_entry_by_topic(77)
        assert entry.current_max_chat_id == "new-chat"
        assert entry.old_max_chat_id == "old-chat"
        assert entry.recovery_status == "remapped"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_dm_contact_recovery_snapshot_upsert_export_and_privacy(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        async with repo._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'dm_contact_recovery_registry'"
        ) as cur:
            table = await cur.fetchone()
        assert table["name"] == "dm_contact_recovery_registry"

        contact = {
            "max_user_id": "300",
            "display_name": "DM Partner",
            "old_dm_chat_id": "300",
            "current_dm_chat_id": "300",
            "tg_topic_id": 77,
            "source": "dialog",
            "recovery_status": "visible",
            "phone": "+79990000000",
            "message_text": "secret",
            "raw_payload": {"token": "raw"},
        }
        first = await repo.upsert_dm_contact_recovery_snapshot([contact], reason="unit_test")
        again = await repo.upsert_dm_contact_recovery_snapshot([contact], reason="unit_test")

        assert first == {"scanned": 1, "inserted": 1, "status_changed": 0}
        assert again == {"scanned": 1, "inserted": 0, "status_changed": 0}

        entries = await repo.list_dm_contact_recovery_entries()
        assert len(entries) == 1
        assert entries[0].max_user_id == "300"
        assert entries[0].display_name == "DM Partner"
        assert entries[0].last_scan_at is not None

        report = await repo.get_recovery_report()
        assert report["stats"]["dm_contacts"] == 1
        assert report["stats"]["dm_contacts_linked"] == 1
        assert report["stats"]["dm_contacts_needs_remap"] == 0
        assert report["stats"]["dm_contacts_last_scan_at"] is not None

        export = await repo.export_recovery_registry()
        assert export["dm_contacts"][0]["max_user_id"] == "300"
        assert export["dm_contacts"][0]["display_name"] == "DM Partner"
        serialized = json.dumps(export, ensure_ascii=False)
        assert "+79990000000" not in serialized
        assert "secret" not in serialized
        assert "raw" not in serialized

        async with repo._db.execute(
            "SELECT details_json FROM chat_recovery_events WHERE event_type = 'dm_contact_scan' ORDER BY id ASC LIMIT 1"
        ) as cur:
            event = await cur.fetchone()
        assert "DM Partner" not in event["details_json"]
        details = json.loads(event["details_json"])
        assert details == {
            "has_tg_topic": True,
            "reason": "unit_test",
            "source": "dialog",
            "status": "visible",
        }
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_snapshot_reports_status_change_deltas(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.upsert_recovery_snapshot([
            {
                "registry_key": "tg_topic:77",
                "tg_topic_id": 77,
                "title": "Client",
                "old_max_chat_id": "old-chat",
                "current_max_chat_id": "old-chat",
                "chat_kind": "dm",
                "mode": "active",
                "recovery_status": "visible",
            },
        ])

        changed = await repo.upsert_recovery_snapshot(
            [
                {
                    "registry_key": "tg_topic:77",
                    "tg_topic_id": 77,
                    "title": "Client",
                    "old_max_chat_id": "old-chat",
                    "current_max_chat_id": "old-chat",
                    "chat_kind": "dm",
                    "mode": "active",
                    "recovery_status": "needs_invite",
                },
            ],
            reason="weekly",
        )

        assert changed["scanned"] == 1
        assert changed["inserted"] == 0
        assert changed["status_changed"] == 1
        assert changed["needs_invite"] == 1
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_find_phantom_topic_bindings_requires_duplicate_real_delivery(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.save_binding(ChatBinding("1779274610031001", 1564, "Чат 1779274610031001", "active", 1))
        await repo.save_binding(ChatBinding("-70638114166223", 336, "Happy School", "active", 1))
        await repo.save_binding(ChatBinding("1779279999999999", 1565, "Чат 1779279999999999", "active", 1))
        await repo.save_message(MessageRecord("m1", "1779274610031001", 1565, 1564, "inbound", 1))
        await repo.save_message(MessageRecord("m1", "-70638114166223", 1566, 336, "inbound", 1))
        await repo.save_message(MessageRecord("m2", "1779279999999999", 1567, 1565, "inbound", 1))

        phantoms = await repo.find_phantom_topic_bindings()

        assert [binding.max_chat_id for binding in phantoms] == ["1779274610031001"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_pending_media_queue_lifecycle_is_idempotent(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        job = PendingMediaDownload(
            max_chat_id="chat-1",
            max_msg_id="m1",
            tg_topic_id=12,
            attachment_index=4,
            kind="video",
            source_type="VIDEO",
            media_chat_id="chat-1",
            media_msg_id="m1",
            reference_kind="video_id",
            reference_id="555",
            duration=10,
            width=640,
            height=360,
            next_attempt_at=10,
        )
        first_id = await repo.enqueue_pending_media(job)
        second_id = await repo.enqueue_pending_media(job)

        assert first_id == second_id
        exact = await repo.find_active_pending_media(
            max_chat_id="chat-1",
            max_msg_id="m1",
            attachment_index=4,
            kind="video",
        )
        assert exact is not None
        assert exact.id == first_id
        by_reference = await repo.find_active_pending_media_by_reference(
            media_chat_id="chat-1",
            media_msg_id="m1",
            attachment_index=4,
            kind="video",
            reference_kind="video_id",
            reference_id="555",
        )
        assert by_reference is not None
        assert by_reference.id == first_id
        due = await repo.get_due_pending_media(now=10)
        assert len(due) == 1
        assert due[0].reference_kind == "video_id"
        assert due[0].reference_id == "555"

        assert await repo.lease_pending_media(first_id, lease_until=100, now=10) is True
        assert await repo.lease_pending_media(first_id, lease_until=100, now=10) is False

        await repo.mark_pending_media_retry(first_id, error="download_failed", next_attempt_at=70, now=11)
        due = await repo.get_due_pending_media(now=69)
        assert due == []
        due = await repo.get_due_pending_media(now=70)
        assert len(due) == 1
        assert due[0].attempts == 1
        assert due[0].status == "retry"

        await repo.mark_pending_media_delivered(first_id, tg_msg_id=999, now=80)
        stats = await repo.count_pending_media()
        assert stats == {"pending_count": 0, "oldest_created_at": None}
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_get_chat_activity_map_since_groups_by_chat(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()

    try:
        await repo.save_message(
            MessageRecord(
                max_msg_id="m1",
                max_chat_id="chat-1",
                tg_msg_id=1,
                tg_topic_id=11,
                direction="inbound",
                created_at=100,
            )
        )
        await repo.save_message(
            MessageRecord(
                max_msg_id="m2",
                max_chat_id="chat-1",
                tg_msg_id=2,
                tg_topic_id=11,
                direction="outbound",
                created_at=101,
            )
        )
        await repo.save_message(
            MessageRecord(
                max_msg_id="m3",
                max_chat_id="chat-2",
                tg_msg_id=3,
                tg_topic_id=12,
                direction="inbound",
                created_at=102,
            )
        )

        result = await repo.get_chat_activity_map_since(99)

        assert result["chat-1"] == {"inbound": 1, "outbound": 1, "total": 2}
        assert result["chat-2"] == {"inbound": 1, "outbound": 0, "total": 1}
    finally:
        await repo.close()


# ── known_users ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_and_find_user_by_name(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        await repo.save_user("111", "Татьяна Геннадиевна Ладина")
        result = await repo.find_user_by_name("Татьяна Геннадиевна Ладина")
        assert result == "111"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_find_user_case_insensitive(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        await repo.save_user("222", "Мария Иванова")
        assert await repo.find_user_by_name("мария иванова") == "222"
        assert await repo.find_user_by_name("МАРИЯ ИВАНОВА") == "222"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_save_user_upserts_name(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        await repo.save_user("333", "Старое Имя")
        await repo.save_user("333", "Новое Имя")
        assert await repo.find_user_by_name("Новое Имя") == "333"
        assert await repo.find_user_by_name("Старое Имя") is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_find_user_returns_none_when_not_found(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    try:
        result = await repo.find_user_by_name("Несуществующий Человек")
        assert result is None
    finally:
        await repo.close()
