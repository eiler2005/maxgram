import logging
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramAPIError

from src.adapters.tg_adapter import TelegramAdapter
from src.runtime.health import RuntimeHealthStore


def _make_message(*, user_id: int, group_id: int, text: str, topic_id: int = 100,
                  is_bot: bool = False, reply_to_id: int | None = None,
                  first_name: str = "Мария", last_name: str = "Иванова",
                  username: str | None = None, message_id: int = 321):
    reply_to = None
    if reply_to_id is not None:
        reply_to = SimpleNamespace(message_id=reply_to_id)

    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            is_bot=is_bot,
            first_name=first_name,
            last_name=last_name,
            username=username,
        ),
        chat=SimpleNamespace(id=group_id),
        message_id=message_id,
        text=text,
        caption=None,
        message_thread_id=topic_id,
        reply_to_message=reply_to,
        reply=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
    )


@pytest.mark.asyncio
async def test_dispatch_incoming_message_accepts_non_owner_group_member():
    adapter = TelegramAdapter("token", owner_id=1, forum_group_id=-100)
    calls = []

    async def handler(topic_id, tg_msg_id, text, reply_to_tg_id, sender_name, media_path=None, media_type=None):
        calls.append((topic_id, tg_msg_id, text, reply_to_tg_id, sender_name))

    adapter.on_reply(handler)

    message = _make_message(
        user_id=2,
        group_id=-100,
        text="Проверка связи",
        topic_id=555,
        reply_to_id=777,
    )

    await adapter._dispatch_incoming_message(message)

    assert calls == [(555, 321, "Проверка связи", 777, "Мария Иванова")]


@pytest.mark.asyncio
async def test_dispatch_incoming_message_ignores_non_owner_commands():
    adapter = TelegramAdapter("token", owner_id=1, forum_group_id=-100)
    handled = []

    async def fake_handle_command(message):
        handled.append(message.text)

    adapter._handle_command = fake_handle_command

    message = _make_message(
        user_id=2,
        group_id=-100,
        text="/status",
        topic_id=555,
    )

    await adapter._dispatch_incoming_message(message)

    assert handled == []


class FakeRetryBot:
    def __init__(self):
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise TelegramAPIError(method="sendMessage", message="boom")
        return SimpleNamespace(message_id=88)


class FakeSystemBot:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(message_id=outcome)


@pytest.mark.asyncio
async def test_tg_retry_logs_retry_and_success(caplog):
    adapter = TelegramAdapter("token", owner_id=1, forum_group_id=-100)
    adapter._bot = FakeRetryBot()

    with caplog.at_level(logging.INFO, logger="src.adapters.tg_adapter"):
        msg_id = await adapter.send_text(555, "Привет", flow_id="mx:-1:42")

    assert msg_id == 88
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(event.get("event") == "tg.outbound.retry" for event in events)
    assert any(event.get("event") == "tg.outbound.sent" for event in events)


@pytest.mark.asyncio
async def test_send_system_notification_fans_out_to_dm_and_ops_topic(tmp_path):
    health = RuntimeHealthStore(tmp_path)
    adapter = TelegramAdapter(
        "token",
        owner_id=1,
        forum_group_id=-100,
        ops_topic_id=777,
        outbox_store=health.outbox,
        health_store=health,
    )
    adapter._bot = FakeSystemBot([11, 22])

    ok = await adapter.send_system_notification("Runtime degraded")

    assert ok is True
    assert adapter._bot.calls == [
        {"chat_id": 1, "text": "Runtime degraded"},
        {"chat_id": -100, "text": "Runtime degraded", "message_thread_id": 777},
    ]
    assert await health.outbox.size() == 0


@pytest.mark.asyncio
async def test_send_system_notification_queues_failed_target_and_flushes_outbox(tmp_path):
    health = RuntimeHealthStore(tmp_path)
    adapter = TelegramAdapter(
        "token",
        owner_id=1,
        forum_group_id=-100,
        ops_topic_id=777,
        outbox_store=health.outbox,
        health_store=health,
    )
    adapter._bot = FakeSystemBot(
        [
            11,
            TelegramAPIError(method="sendMessage", message="boom"),
            TelegramAPIError(method="sendMessage", message="boom"),
            TelegramAPIError(method="sendMessage", message="boom"),
        ]
    )

    ok = await adapter.send_system_notification("Need attention")

    assert ok is False
    assert await health.outbox.size() == 1

    adapter._bot = FakeSystemBot([22])
    flushed = await adapter.flush_notification_outbox()

    assert flushed == 1
    assert await health.outbox.size() == 0
