from types import SimpleNamespace

import pytest

from src.adapters.tg_adapter import TelegramAdapter


def _make_message(*, user_id: int, group_id: int, text: str, topic_id: int = 100,
                  is_bot: bool = False, reply_to_id: int | None = None,
                  first_name: str = "Марина", last_name: str = "Ермилова",
                  username: str | None = None):
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
        text=text,
        caption=None,
        message_thread_id=topic_id,
        reply_to_message=reply_to,
        reply=None,
    )


@pytest.mark.asyncio
async def test_dispatch_incoming_message_accepts_non_owner_group_member():
    adapter = TelegramAdapter("token", owner_id=1, forum_group_id=-100)
    calls = []

    async def handler(topic_id, text, reply_to_tg_id, sender_name):
        calls.append((topic_id, text, reply_to_tg_id, sender_name))

    adapter.on_reply(handler)

    message = _make_message(
        user_id=2,
        group_id=-100,
        text="Проверка связи",
        topic_id=555,
        reply_to_id=777,
    )

    await adapter._dispatch_incoming_message(message)

    assert calls == [(555, "Проверка связи", 777, "Марина Ермилова")]


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
