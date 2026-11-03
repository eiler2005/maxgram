import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from src.adapters.max.adapter import MaxAdapter
from src.adapters.max.ports import MaxChatView
from src.adapters.tg.adapter import TelegramAdapter
from src.bridge.contracts import TelegramResponse, TelegramInlineButton
from src.bridge.core import BridgeCore
from src.bridge.join import MaxJoinService
from src.db.repository import Repository
from tests.fakes.fake_max_backend import FakeMaxBackend
from tests.integration.test_bridge_end_to_end import IntegrationConfig, _wait_until
from tests.test_tg_adapter import _make_message


class Panel:
    def __init__(self):
        self.message_id = 501
        self.message_thread_id = None
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class Bot:
    def __init__(self):
        self.topics = []
        self.sent = []

    async def create_forum_topic(self, **kwargs):
        await asyncio.sleep(0)
        self.topics.append(kwargs)
        return SimpleNamespace(message_thread_id=55 + len(self.topics))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=600 + len(self.sent))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["paste", "command", "caption", "text_link"])
async def test_owner_join_end_to_end_and_first_max_post_reuse_topic(tmp_path, entry):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    backend = FakeMaxBackend()
    joins = []

    async def join(link):
        joins.append(link)
        return MaxChatView(id=-100, title="Example channel", type="CHANNEL", status="ACTIVE")

    backend.client.join_chat_by_link = join
    backend.client.group_chats[-100] = MaxChatView(id=-100, title="Example channel")
    max_adapter = MaxAdapter(
        phone="+70000000000", data_dir=str(tmp_path), session_name="session.db",
        tmp_dir=str(tmp_path / "tmp"), backend=backend,
    )
    tg = TelegramAdapter("token", owner_id=1, forum_group_id=-100123)
    bot = Bot()
    tg._bot = bot
    cfg = IntegrationConfig(
        telegram=SimpleNamespace(owner_id=1),
        bridge=SimpleNamespace(max_file_size_mb=50),
        content=SimpleNamespace(
            placeholder_unsupported="[unsupported: {type}]",
            placeholder_file_too_large="[too large: {filename}]",
            forward_voice=True, forward_documents=True, forward_photos=True,
        ),
        health=SimpleNamespace(reminder_interval_hours=4),
    )
    bridge = BridgeCore(cfg, repo, max_adapter, tg)
    task = asyncio.create_task(max_adapter.start())
    try:
        await _wait_until(max_adapter.is_ready)
        text = "/join@bridge https://max.ru/example_channel" if entry == "command" else "https://max.ru/example_channel"
        message = _make_message(user_id=1, group_id=1, text=text, topic_id=None)
        if entry == "caption":
            message.text = None
            message.caption = "Переслано: https://max.ru/example_channel"
        elif entry == "text_link":
            message.text = "Название канала"
            message.entities = [SimpleNamespace(url="https://max.ru/example_channel")]
        replies = []
        panel = Panel()

        async def reply(text, **kwargs):
            replies.append((text, kwargs))
            return panel

        message.reply = reply
        await tg._dispatch_incoming_message(message)
        assert len(replies) == 1
        assert joins == []
        markup = replies[0][1]["reply_markup"]
        data = markup.inline_keyboard[0][0].callback_data
        answers = []

        async def ack(text=None, **kwargs):
            answers.append(text)

        cb = SimpleNamespace(data=data, from_user=SimpleNamespace(id=1), message=panel, answer=ack)
        # A first MAX post can race the join confirmation. Both use one topic lock.
        await asyncio.gather(
            tg._dispatch_callback_query(cb),
            backend.client.emit_text_message(chat_id=-100, msg_id=71, text="New post"),
        )
        assert len(bot.topics) == 1
        assert joins == ["https://max.ru/example_channel"]
        binding = await repo.get_binding("-100")
        assert binding.tg_topic_id == 56
        assert bot.sent[0]["message_thread_id"] == 56
        assert "New post" in bot.sent[0]["text"]
        assert panel.edits[-1][1]["reply_markup"].inline_keyboard[0][0].url == "https://t.me/c/123/56"
        assert answers == ["Выполняю…"]
        help_text = await bridge._status.build_help_message()
        assert "/join" in help_text
        assert "15 минут" in help_text
        assert "одобрения" in help_text
        assert "бот сообщит об ошибке" in help_text
    finally:
        await max_adapter.close()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner, chat, topic, command, expected", [
    (1, 1, None, False, "join"),
    (1, -100123, None, True, "join"),
    (1, -100123, 1, True, "join"),
    (2, -100123, None, True, "ignore"),
    (2, 2, None, False, "ignore"),
    (1, -999, None, True, "ignore"),
    (1, -100123, 55, True, "reject"),
    (1, -100123, 55, False, "forward"),
])
async def test_join_input_scope_does_not_steal_regular_topic_messages(owner, chat, topic, command, expected):
    tg = TelegramAdapter("token", owner_id=1, forum_group_id=-100123)
    called = []
    replies = []

    async def join(text):
        called.append(text)
        return TelegramResponse("Confirm", [TelegramInlineButton("Join", callback_data="max_join_confirm:test")])

    async def reply(text, **kwargs):
        replies.append(text)

    async def forward(*args):
        called.append("forward")

    tg.on_arg_command("join", join)
    tg.on_reply(forward)
    text = ("/join " if command else "") + "https://max.ru/example_channel"
    message = _make_message(user_id=owner, group_id=chat, text=text, topic_id=topic)
    message.reply = reply
    await tg._dispatch_incoming_message(message)
    if expected == "join":
        assert len(called) == 1
        assert replies == ["Confirm"]
    elif expected == "forward":
        assert called == ["forward"]
    elif expected == "reject":
        assert called == []
        assert "General" in replies[0]
    else:
        assert called == replies == []


@pytest.mark.asyncio
async def test_join_non_owner_callback_is_rejected_before_handler():
    tg = TelegramAdapter("token", owner_id=1, forum_group_id=-100123)
    calls = []

    async def handler(action):
        calls.append(action)

    async def answer(text=None, **kwargs):
        calls.append(text)

    tg.on_callback_action(handler)
    await tg._dispatch_callback_query(SimpleNamespace(
        data="max_join_confirm:test", from_user=SimpleNamespace(id=2),
        message=Panel(), answer=answer,
    ))
    assert calls == ["Только владелец bridge"]


@pytest.mark.asyncio
async def test_join_offline_response_preserves_confirmation_keyboard():
    tg = TelegramAdapter("token", owner_id=1, forum_group_id=-100123)
    panel = Panel()

    async def handler(action):
        return "MAX сейчас не подключён"

    async def answer(text=None, **kwargs):
        pass

    tg.on_callback_action(handler)
    await tg._dispatch_callback_query(SimpleNamespace(
        data="max_join_confirm:test", from_user=SimpleNamespace(id=1), message=panel, answer=answer,
    ))
    assert panel.edits == []
    assert panel.answers[0][0] == "MAX сейчас не подключён"


@pytest.mark.asyncio
@pytest.mark.parametrize("text, rejected", [
    ("https://example.org/news", True),
    ("https://max.ru.evil.test/join/token", True),
    ("https://evil.test/?next=https://max.ru/join/token", True),
    ("https://max.ru/", True),
    ("max.ru", True),
    ("https://max.ru/join/", True),
    ("https://max.ru/channel/123", True),
    ("ftp://max.ru/join/token", True),
    ("https://user@max.ru/join/token", True),
    ("Привет", False),
    ("someone@max.ru", False),
])
async def test_owner_dm_invalid_link_feedback_has_no_join_side_effects(tmp_path, text, rejected):
    repo = Repository(str(tmp_path / "invalid-link.db"))
    await repo.connect()

    async def unexpected_topic(msg):
        raise AssertionError("Invalid input must not create a topic")

    service = MaxJoinService(
        repo=repo, max_adapter=SimpleNamespace(), owner_id=1,
        ensure_topic=unexpected_topic, topic_link=lambda _: None,
        schedule_scan=lambda _: None,
    )
    tg = TelegramAdapter("token", owner_id=1, forum_group_id=-100123)
    tg.on_arg_command("join", service.prepare)
    message = _make_message(user_id=1, group_id=1, text=text, topic_id=None)
    replies = []

    async def reply(text, **kwargs):
        replies.append((text, kwargs))

    message.reply = reply
    try:
        await tg._dispatch_incoming_message(message)
        if rejected:
            assert len(replies) == 1
            assert "не подходит для вступления" in replies[0][0]
            assert replies[0][1]["reply_markup"] is None
        else:
            assert replies == []
        async with repo._db.execute("SELECT COUNT(*) FROM telegram_callback_actions") as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert await repo.list_bindings() == []
    finally:
        await repo.close()
