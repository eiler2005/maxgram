import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pymax.exceptions import ApiError

from src.adapters.max.backends.pymax.client_adapter import PymaxClientAdapter
from src.bridge.contracts import MaxJoinedChat, TelegramCallbackAction, TelegramResponse
from src.bridge.join import MaxJoinService, CONFIRM, CANCEL
from src.bridge.max_links import extract_max_links, normalize_max_link
from src.db.repository import Repository, ChatBinding


@pytest.mark.parametrize("text, expected", [
    ("max.ru/example_channel", "https://max.ru/example_channel"),
    ("https://MAX.RU/join/abc-123_", "https://max.ru/join/abc-123_"),
    ("http://www.max.ru/channel/?utm_source=test#post", "https://max.ru/channel"),
    ("https://max.ru/channel).", "https://max.ru/channel"),
])
def test_normalize_max_link(text, expected):
    assert normalize_max_link(text) == expected


@pytest.mark.parametrize("text", [
    "https://max.ru.evil.test/channel", "https://evil.test/max.ru/channel",
    "https://user@max.ru/channel", "https://max.ru:443/channel",
    "https://max.ru/join/", "https://max.ru/../channel", "https://max.ru/",
    "javascript://max.ru/channel", "https://max.ru/%2Fchannel",
    "https://max.ru/settings", "https://max.ru/" + "a" * 2100,
])
def test_normalize_max_link_rejects_non_channel_urls(text):
    assert normalize_max_link(text) is None


def test_extract_max_links_from_forwarded_text_deduplicates_and_skips_nested_external_url():
    text = (
        "Приглашение https://max.ru/join/abc?utm_source=tg\n"
        "max.ru/join/abc https://evil.test/?next=https://max.ru/unwanted"
        " evil.max.ru/unwanted evil-max.ru/unwanted someone@max.ru/unwanted"
    )
    assert extract_max_links(text) == ["https://max.ru/join/abc"]


class JoinMax:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.result = MaxJoinedChat("-100", "Example channel", "CHANNEL")
        self.error = None

    def is_ready(self):
        return self.ready

    async def join_chat_by_link(self, link):
        self.calls.append(link)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.result


@pytest_asyncio.fixture
async def joins(tmp_path):
    repo = Repository(str(tmp_path / "join.db"))
    await repo.connect()
    max_adapter = JoinMax()
    created = []
    scans = []

    async def ensure_topic(msg):
        binding = await repo.get_binding(msg.chat_id)
        if binding:
            return binding.tg_topic_id
        topic_id = 50 + len(created)
        created.append(msg.chat_id)
        await repo.save_binding(ChatBinding(
            max_chat_id=msg.chat_id, tg_topic_id=topic_id,
            title=msg.chat_title, mode="active", created_at=int(time.time()),
        ))
        return topic_id

    service = MaxJoinService(
        repo=repo, max_adapter=max_adapter, owner_id=1,
        ensure_topic=ensure_topic,
        topic_link=lambda topic_id: f"https://t.me/c/123/{topic_id}",
        schedule_scan=scans.append,
    )
    yield SimpleNamespace(repo=repo, max=max_adapter, service=service, created=created, scans=scans)
    await repo.close()


def callback(response, action=CONFIRM, user_id=1):
    action_id = response.buttons[0].callback_data.split(":", 1)[1]
    return TelegramCallbackAction(action=action, action_id=action_id, user_id=user_id)


@pytest.mark.asyncio
async def test_join_prepare_requires_confirmation_and_keeps_url_out_of_callback(joins):
    response = await joins.service.prepare("https://max.ru/join/private_invite")
    assert isinstance(response, TelegramResponse)
    assert joins.max.calls == []
    assert len(response.buttons) == 2
    assert all(len(button.callback_data.encode()) <= 64 for button in response.buttons)
    assert all("private_invite" not in button.callback_data for button in response.buttons)
    record = await joins.repo.get_callback_action(callback(response).action_id)
    assert json.loads(record.payload_json) == {"url": "https://max.ru/join/private_invite"}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "https://evil.test/channel", "max.ru/one max.ru/two"])
async def test_join_prepare_bad_or_ambiguous_input_creates_no_action(joins, text):
    response = await joins.service.prepare(text)
    assert response.buttons == []
    assert joins.max.calls == []
    async with joins.repo._db.execute("SELECT COUNT(*) FROM telegram_callback_actions") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_join_confirm_creates_topic_and_clears_private_payload(joins):
    response = await joins.service.prepare("https://max.ru/example_channel")
    cb = callback(response)
    result = await joins.service.handle(cb)
    assert "подключён" in result.text
    assert result.buttons[0].url == "https://t.me/c/123/50"
    assert joins.created == ["-100"]
    assert joins.max.calls == ["https://max.ru/example_channel"]
    record = await joins.repo.get_callback_action(cb.action_id)
    assert record.status == "used"
    assert record.payload_json == "{}"
    assert joins.scans == ["max_join_button"]


@pytest.mark.asyncio
async def test_join_cancel_prevents_later_confirmation(joins):
    response = await joins.service.prepare("max.ru/example_channel")
    await joins.service.handle(callback(response, CANCEL))
    await joins.service.handle(callback(response))
    assert joins.max.calls == []
    assert joins.created == []
    record = await joins.repo.get_callback_action(callback(response).action_id)
    assert record.last_error == "cancelled"
    assert record.payload_json == "{}"


@pytest.mark.asyncio
async def test_join_double_click_and_new_request_do_not_duplicate_join_or_topic(joins):
    response = await joins.service.prepare("max.ru/example_channel")
    await asyncio.gather(*(joins.service.handle(callback(response)) for _ in range(3)))
    response2 = await joins.service.prepare("max.ru/example_channel")
    await joins.service.handle(callback(response2))
    assert joins.max.calls == ["https://max.ru/example_channel"]
    assert joins.created == ["-100"]


@pytest.mark.asyncio
async def test_join_confirm_cancel_race_has_only_one_outcome(joins):
    response = await joins.service.prepare("max.ru/example_channel")
    await asyncio.gather(
        joins.service.handle(callback(response)),
        joins.service.handle(callback(response, CANCEL)),
    )
    record = await joins.repo.get_callback_action(callback(response).action_id)
    assert record.payload_json == "{}"
    assert len(joins.max.calls) <= 1
    assert (record.status == "used") == (len(joins.max.calls) == 1)


@pytest.mark.asyncio
async def test_join_topic_failure_can_be_retried_without_duplicate_join(joins):
    original = joins.service._ensure_topic

    async def fail_topic(msg):
        return None

    joins.service._ensure_topic = fail_topic
    result = await joins.service.handle(callback(await joins.service.prepare("max.ru/example_channel")))
    assert "В MAX вступили" in result.text
    assert "создать не удалось" in result.text
    assert joins.created == []
    joins.service._ensure_topic = original
    result = await joins.service.handle(callback(await joins.service.prepare("max.ru/example_channel")))
    assert result.buttons
    assert len(joins.max.calls) == 1


@pytest.mark.asyncio
async def test_join_success_cache_expires_before_rechecking_membership(joins, monkeypatch):
    await joins.service.handle(callback(await joins.service.prepare("max.ru/example_channel")))
    monkeypatch.setattr("src.bridge.join.JOIN_CACHE_SECONDS", 0)
    await joins.service.handle(callback(await joins.service.prepare("max.ru/example_channel")))
    assert len(joins.max.calls) == 2
    assert joins.created == ["-100"]


@pytest.mark.asyncio
async def test_join_timeout_consumes_button_without_automatic_replay(joins, monkeypatch):
    async def blocked(link):
        joins.max.calls.append(link)
        await asyncio.Event().wait()

    joins.max.join_chat_by_link = blocked
    monkeypatch.setattr("src.bridge.join.JOIN_TIMEOUT_SECONDS", 0.01)
    cb = callback(await joins.service.prepare("max.ru/join/private_invite"))
    result = await joins.service.handle(cb)
    assert "подтвердить" in result.text
    assert "Проверьте /chats" in result.text
    assert (await joins.repo.get_callback_action(cb.action_id)).payload_json == "{}"
    await joins.service.handle(cb)
    assert len(joins.max.calls) == 1
    assert joins.created == []


@pytest.mark.asyncio
async def test_join_rejects_personal_profile_as_channel(joins):
    joins.max.result.chat_kind = "DIALOG"
    result = await joins.service.handle(callback(await joins.service.prepare("max.ru/example_user")))
    assert "недействительна" in result.text
    assert joins.created == []


@pytest.mark.asyncio
async def test_join_non_owner_cannot_use_confirmation(joins):
    response = await joins.service.prepare("max.ru/example_channel")
    assert await joins.service.handle(callback(response, user_id=2)) == "Только владелец bridge"
    assert joins.max.calls == []
    record = await joins.repo.get_callback_action(callback(response).action_id)
    assert record.status == "pending"


@pytest.mark.asyncio
async def test_join_offline_keeps_button_pending_for_retry(joins):
    response = await joins.service.prepare("max.ru/example_channel")
    joins.max.ready = False
    result = await joins.service.handle(callback(response))
    assert "не подключён" in result
    record = await joins.repo.get_callback_action(callback(response).action_id)
    assert record.status == "pending"
    joins.max.ready = True
    await joins.service.handle(callback(response))
    assert len(joins.max.calls) == 1


@pytest.mark.asyncio
async def test_join_expired_confirmation_is_not_executed_and_payload_is_cleared(joins):
    response = await joins.service.prepare("max.ru/join/private_invite")
    cb = callback(response)
    await joins.repo._db.execute("UPDATE telegram_callback_actions SET created_at = 1 WHERE id = ?", (cb.action_id,))
    await joins.repo._db.commit()
    result = await joins.service.handle(cb)
    assert "устарела" in result.text
    assert joins.max.calls == []
    assert (await joins.repo.get_callback_action(cb.action_id)).payload_json == "{}"


@pytest.mark.asyncio
async def test_join_new_request_purges_expired_private_payload(joins):
    response = await joins.service.prepare("max.ru/join/private_invite")
    cb = callback(response)
    await joins.repo._db.execute("UPDATE telegram_callback_actions SET created_at = 1 WHERE id = ?", (cb.action_id,))
    await joins.repo._db.commit()
    await joins.service.prepare("max.ru/example_channel")
    record = await joins.repo.get_callback_action(cb.action_id)
    assert record.payload_json == "{}"
    assert record.last_error == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id, status", [(None, "joined"), ("0", "joined"), ("1000000000000", "joined"), ("-100", "pending")])
async def test_join_does_not_create_topic_before_confirmed_membership(joins, chat_id, status):
    joins.max.result = MaxJoinedChat(chat_id, "Example", "CHANNEL", join_status=status)
    response = await joins.service.prepare("max.ru/example_channel")
    result = await joins.service.handle(callback(response))
    assert joins.created == []
    assert result.buttons == []
    if status == "pending":
        assert "Ожидаем одобрения" in result.text
    else:
        assert "подтвердить" in result.text


@pytest.mark.asyncio
async def test_join_reuses_existing_binding_after_restart(joins):
    await joins.repo.save_binding(ChatBinding("-100", 77, "Existing", "active", int(time.time())))
    joins.max.result.join_status = "already_member"
    response = await joins.service.prepare("max.ru/example_channel")
    result = await joins.service.handle(callback(response))
    assert joins.created == []
    assert result.buttons[0].url.endswith("/77")


@pytest.mark.asyncio
async def test_join_timeout_or_error_does_not_leak_private_link(joins, caplog):
    joins.max.error = RuntimeError("secret https://max.ru/join/private_invite")
    response = await joins.service.prepare("max.ru/join/private_invite")
    result = await joins.service.handle(callback(response))
    assert "secret" not in result.text + caplog.text
    assert "private_invite" not in result.text + caplog.text
    assert joins.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code, expected", [
    ("chat.join.pending", "pending"), ("chat.access.denied", "denied"),
    ("invite.expired", "invalid_link"), ("chat.already.member", "already_member"),
])
async def test_backend_classifies_join_without_retrying_other_endpoint(code, expected):
    calls = []

    async def join_group(link):
        calls.append(link)
        raise ApiError(opcode=33, error=code, message="https://max.ru/join/private_invite")

    adapter = PymaxClientAdapter(SimpleNamespace(join_group=join_group))
    if expected == "pending":
        assert (await adapter.join_chat_by_link("https://max.ru/join/test")).join_status == expected
    else:
        from src.bridge.errors import MaxJoinError
        with pytest.raises(MaxJoinError) as error:
            await adapter.join_chat_by_link("https://max.ru/join/test")
        assert error.value.code == expected
        assert "private_invite" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_backend_reuses_known_membership_without_join_request():
    raw = SimpleNamespace(chats=[SimpleNamespace(
        id=-100, title="Existing", link="https://max.ru/example_channel", status="ACTIVE"
    )])
    joined = await PymaxClientAdapter(raw).join_chat_by_link("max.ru/example_channel")
    assert joined.id == -100
    assert joined.join_status == "already_member"
