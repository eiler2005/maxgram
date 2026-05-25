from .conftest import *  # noqa: F403


@pytest.mark.asyncio
async def test_resolve_user_name_uses_contacts_cache_before_live_lookup(tmp_path):
    class ContactClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.contacts = [SimpleNamespace(id=99577134, names=[SimpleNamespace(first_name="Елена", last_name="", name="Елена")])]
            self.live_calls = 0

        async def get_users(self, user_ids: list[int]):
            self.live_calls += 1
            return []

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = ContactClient()
    adapter._client = client

    assert await adapter.resolve_user_name("99577134") == "Елена"
    assert client.live_calls == 0


@pytest.mark.asyncio
async def test_resolve_user_name_live_lookup_has_short_timeout(tmp_path, monkeypatch):
    class SlowClient(LookupClient):
        async def get_users(self, user_ids: list[int]):
            await asyncio.sleep(10)
            return []

    async def fake_wait_for(coro, timeout):
        assert timeout == 5
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr("src.adapters.max_adapter.asyncio.wait_for", fake_wait_for)

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = SlowClient()

    assert await adapter.resolve_user_name("99577134") is None


@pytest.mark.asyncio
async def test_resolve_user_name_negative_cache_suppresses_repeated_live_lookup(tmp_path):
    class FailingClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.live_calls = 0

        async def get_users(self, user_ids: list[int]):
            self.live_calls += 1
            raise ValueError("bad user payload")

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = FailingClient()
    adapter._client = client

    assert await adapter.resolve_user_name("99577134") is None
    assert await adapter.resolve_user_name("99577134") is None
    assert client.live_calls == 1

    adapter._adapter._resolver._negative_user_lookup_until[99577134] = 0
    assert await adapter.resolve_user_name("99577134") is None
    assert client.live_calls == 2
