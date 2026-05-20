import pytest

from src.db.repository import ChatBinding, MessageRecord, PendingMediaDownload, Repository, KnownUser


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
