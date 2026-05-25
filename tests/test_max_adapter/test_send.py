from .conftest import *  # noqa: F403

from src.adapters.max import send as max_send_module


class EchoAckClient(LookupClient):
    def __init__(self, adapter):
        super().__init__()
        self._adapter = adapter

    async def send_message(self, **kwargs):
        async def emit_echo():
            await asyncio.sleep(0.01)
            await self._adapter._handle_raw_message(
                SimpleNamespace(
                    id=4242,
                    chat_id=kwargs["chat_id"],
                    sender=161361072,
                    text=kwargs["text"],
                    type="USER",
                    status=None,
                    attaches=[],
                    link=None,
                )
            )

        asyncio.create_task(emit_echo())
        return {"payload": {"accepted": True}}


@pytest.mark.asyncio
async def test_send_message_waits_for_echo_ack_when_pymax_does_not_return_id(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._own_id = "161361072"
    adapter._client = EchoAckClient(adapter)

    received = []

    async def handler(msg):
        received.append(msg.msg_id)

    adapter.on_message(handler)

    msg_id = await adapter.send_message("123456789", "тест исходящего сообщения")

    assert msg_id == "4242"
    assert received == []


class DirectIdClient(LookupClient):
    async def send_message(self, **kwargs):
        return SimpleNamespace(id=31337)


@pytest.mark.asyncio
async def test_own_echo_is_suppressed_when_send_message_returns_real_id(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._own_id = "161361072"
    adapter._client = DirectIdClient()

    received = []

    async def handler(msg):
        received.append(msg.msg_id)

    adapter.on_message(handler)

    msg_id = await adapter.send_message("123456789", "тест")
    assert msg_id == "31337"

    await adapter._handle_raw_message(
        SimpleNamespace(
            id=31337,
            chat_id=123456789,
            sender=161361072,
            text="тест",
            type="USER",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert received == []


class FlakyRetryClient(LookupClient):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class HangingClient(LookupClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_send_message_retries_retryable_transport_error_and_succeeds(tmp_path, monkeypatch, caplog):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._client = FlakyRetryClient(
        [
            RuntimeError("Socket is not connected"),
            SimpleNamespace(id=4243),
        ]
    )

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        msg_id = await adapter.send_message("123456789", "тест")

    assert msg_id == "4243"
    assert adapter._client.calls == 2
    assert adapter.get_last_outbound_error() is None
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(event.get("event") == "max.outbound.retry" for event in events)
    assert any(event.get("event") == "max.outbound.sent" and event.get("attempt") == 2 for event in events)


@pytest.mark.asyncio
async def test_send_message_timeout_flows_through_retry_layer(tmp_path, monkeypatch):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._client = HangingClient()

    original_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 3600:
            await original_sleep(delay)
        return None

    monkeypatch.setattr(max_send_module, "DEFAULT_OPERATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    msg_id = await adapter.send_message("123456789", "тест")

    assert msg_id is None
    assert adapter._client.calls == 3
    assert "timed out" in (adapter.get_last_outbound_error() or "")


@pytest.mark.asyncio
async def test_send_message_exposes_final_error_after_retries(tmp_path, monkeypatch):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    adapter._started = True
    adapter._client = FlakyRetryClient(
        [
            RuntimeError("Socket is not connected"),
            RuntimeError("Socket is not connected"),
            RuntimeError("Socket is not connected"),
        ]
    )

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    msg_id = await adapter.send_message("123456789", "тест")

    assert msg_id is None
    assert adapter._client.calls == 3
    assert adapter.get_last_outbound_error() == "Socket is not connected"
    assert adapter.get_last_outbound_attempts() == 3


@pytest.mark.asyncio
async def test_send_message_sanitizes_pymax_sequence_overflow_error(tmp_path, caplog):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._started = True
    adapter._client = FlakyRetryClient(
        [struct.error("'B' format requires 0 <= number <= 255")]
    )

    with caplog.at_level(logging.ERROR, logger="src.adapters.max_adapter"):
        msg_id = await adapter.send_message("123456789", "secret text")

    assert msg_id is None
    assert adapter._client.calls == 1
    assert (
        adapter.get_last_outbound_error()
        == "pymax_tcp_sequence_overflow: PyMax TCP seq exceeded 255"
    )
    assert adapter.get_last_outbound_attempts() == 1
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.outbound.failed"
        and event.get("error") == "pymax_tcp_sequence_overflow: PyMax TCP seq exceeded 255"
        and event.get("retryable") is False
        for event in events
    )
    assert "secret text" not in caplog.text


class FakeSocketNotConnectedError(Exception):
    pass


class PingClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.is_connected = True
        self.close_calls = 0
        self.send_calls = 0
        self.logger = logging.getLogger(f"tests.max_adapter.ping.{id(self)}")

    async def _send_and_wait(self, **kwargs):
        self.send_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if not self.outcomes:
            self.is_connected = False
        return outcome

    async def close(self):
        self.close_calls += 1
        self.is_connected = False

    async def raw_request(self, **kwargs):
        return await self._send_and_wait(**kwargs)


class StartClient:
    def __init__(self):
        self.raw_handlers = []

    async def _sync(self):
        return None

    async def _login(self):
        return None

    def prepare_startup(self, error_handler):
        for attr_name in ("_sync", "_login"):
            original = getattr(self, attr_name)

            async def wrapped(*args, __original=original, **kwargs):
                try:
                    return await __original(*args, **kwargs)
                except Exception as exc:
                    await error_handler(exc)
                    raise

            wrapped._maxtg_wrapped = True
            setattr(self, attr_name, wrapped)

    def install_raw_message_interceptor(self, _handler):
        return MaxRawInterceptorResult(installed=False, reason="client_has_no_message_notification_handler")

    def install_interactive_ping(self, ping_loop):
        self.ping_loop = ping_loop

    def register_start_handler(self, handler):
        self.start_handler = handler

    def register_raw_receive_handler(self, handler):
        self.raw_handlers.append(handler)
        return len(self.raw_handlers)

    def register_message_handler(self, handler):
        self.message_handler = handler

    def register_message_edit_handler(self, handler):
        self.message_edit_handler = handler

    def register_message_delete_handler(self, handler):
        self.message_delete_handler = handler

    def own_user_id(self):
        return None

    async def start(self):
        raise RuntimeError("test-stop")


class LifecycleBackend:
    def __init__(self, client):
        self.client = client

    def create_client(self):
        return self.client


@pytest.mark.asyncio
async def test_start_path_logs_masked_phone_without_name_error(tmp_path, monkeypatch, caplog):
    adapter = AdapterHarness(
        phone="+79991234567",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    async def fake_make_client():
        return StartClient()

    async def stop_sleep(_delay):
        raise asyncio.CancelledError()

    adapter._make_client = fake_make_client
    monkeypatch.setattr(asyncio, "sleep", stop_sleep)

    with caplog.at_level(logging.ERROR, logger="src.adapters.max_adapter"):
        with pytest.raises(asyncio.CancelledError):
            await adapter.start()

    assert "mask_phone" not in caplog.text


@pytest.mark.asyncio
async def test_make_client_wraps_startup_stage_errors_with_runtime_capture(tmp_path):
    class StageFailingClient(StartClient):
        async def _sync(self):
            raise RuntimeError("sync-boom")

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
        backend=LifecycleBackend(StageFailingClient()),
    )

    client = await adapter._make_client()

    assert getattr(client._sync, "_maxtg_wrapped", False) is True
    with pytest.raises(RuntimeError, match="sync-boom"):
        await client._sync()
    assert adapter.get_last_start_error() == "sync-boom"


def test_is_ready_tracks_underlying_transport_state(tmp_path):
    class TransportClient:
        is_connected = True

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    client = TransportClient()

    adapter._started = True
    adapter._client = client
    assert adapter.is_ready() is True

    client.is_connected = False
    assert adapter.is_ready() is False


@pytest.mark.asyncio
async def test_failfast_ping_closes_client_after_consecutive_failures(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    client = PingClient([RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])

    ping_loop = adapter._build_failfast_interactive_ping(
        client,
        ping_interval=0,
        failure_limit=3,
        ping_opcode=object(),
        disconnect_error=FakeSocketNotConnectedError,
    )

    await ping_loop()

    assert client.send_calls == 3
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_failfast_ping_resets_failure_counter_after_success(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    client = PingClient(
        [
            RuntimeError("boom"),
            {"ok": True},
            RuntimeError("boom"),
            FakeSocketNotConnectedError(),
        ]
    )

    ping_loop = adapter._build_failfast_interactive_ping(
        client,
        ping_interval=0,
        failure_limit=2,
        ping_opcode=object(),
        disconnect_error=FakeSocketNotConnectedError,
    )

    await ping_loop()

    assert client.send_calls == 4
    assert client.close_calls == 0


def test_classify_runtime_error_marks_corrupt_session_as_reauth(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    issue = adapter._classify_runtime_error(RuntimeError("sqlite3.OperationalError: unsupported file format"))

    assert issue is not None
    assert issue.kind == "session_corrupt"
    assert issue.requires_reauth is True


def test_classify_runtime_error_marks_malformed_session_as_reauth(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    issue = adapter._classify_runtime_error(RuntimeError("sqlite3.DatabaseError: database disk image is malformed"))

    assert issue is not None
    assert issue.kind == "session_corrupt"
    assert issue.requires_reauth is True


def test_classify_runtime_error_uses_exception_context_for_logout_all(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))

    try:
        try:
            raise RuntimeError("FAIL_LOGOUT_ALL [login.token]")
        except RuntimeError:
            raise OSError("[SSL: APPLICATION_DATA_AFTER_CLOSE_NOTIFY] application data after close notify")
    except OSError as exc:
        issue = adapter._classify_runtime_error(exc)

    assert issue is not None
    assert issue.kind == "session_invalid"
    assert issue.requires_reauth is True


@pytest.mark.asyncio
async def test_emit_runtime_issue_notifies_only_once_per_signature(tmp_path):
    adapter = AdapterHarness(phone="+7", data_dir=str(tmp_path), session_name="session", tmp_dir=str(tmp_path / "tmp"))
    seen = []

    async def handler(issue):
        seen.append((issue.kind, issue.summary))

    adapter.on_issue(handler)
    issue = adapter._remember_runtime_issue(
        adapter._classify_runtime_error(RuntimeError("Invalid token"))  # type: ignore[arg-type]
    )

    await adapter._emit_runtime_issue(issue)
    await adapter._emit_runtime_issue(issue)

    assert seen == [("session_invalid", "MAX сессия недействительна, нужна повторная авторизация")]
