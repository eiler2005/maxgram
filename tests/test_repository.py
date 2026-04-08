import pytest

from src.db.repository import MessageRecord, Repository, KnownUser


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
        await repo.save_user("222", "Марина Ермилова")
        assert await repo.find_user_by_name("марина ермилова") == "222"
        assert await repo.find_user_by_name("МАРИНА ЕРМИЛОВА") == "222"
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
