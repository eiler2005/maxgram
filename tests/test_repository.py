import pytest

from src.db.repository import MessageRecord, Repository


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
