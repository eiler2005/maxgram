from .conftest import *  # noqa: F403


@pytest.mark.asyncio
async def test_collect_recovery_snapshot_captures_access_metadata_without_messages(tmp_path):
    (tmp_path / "session").write_bytes(b"session bytes")
    adapter = AdapterHarness(
        phone="+79991234567",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = RecoveryClient()
    adapter._own_id = "100"

    snapshot = await adapter.collect_recovery_snapshot()

    assert snapshot.max_user_id == "100"
    assert snapshot.masked_phone != "+79991234567"
    assert snapshot.session_fingerprint_hash is not None
    by_id = {chat.max_chat_id: chat for chat in snapshot.chats}
    assert by_id["-1"].title == "Enriched group"
    assert by_id["-1"].chat_kind == "group"
    assert by_id["-1"].invite_link == "https://max.ru/join/example"
    assert by_id["-1"].owner_name == "Group Owner"
    assert by_id["-1"].admin_contacts == [
        {"user_id": "500", "name": "Group Owner"},
        {"user_id": "501", "name": "Group Admin"},
    ]
    assert by_id["-2"].chat_kind == "channel"
    assert by_id["300"].chat_kind == "dm"
    assert by_id["300"].dm_partner_user_id == "300"
    assert by_id["300"].dm_partner_name == "DM Partner"
    by_user = {contact.max_user_id: contact for contact in snapshot.contacts}
    assert list(by_user) == ["300"]
    assert by_user["300"].display_name == "DM Partner"
    assert by_user["300"].current_dm_chat_id == "300"
    assert by_user["300"].source == "dialog"
    assert "100" not in by_user
    assert "999" not in by_user


@pytest.mark.asyncio
async def test_typed_empty_message_recovers_audio_from_recent_history(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    recovered_message = SimpleNamespace(
        id=106,
        chat_id=28093080,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="AUDIO",
                audio_id=84,
                url="https://audio.example.test/recovered.ogg",
                duration=12,
                wave="abc",
                token="secret-token",
            )
        ],
        link=None,
    )

    class HistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})
            self.history_calls = []

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            self.history_calls.append((chat_id, from_time, forward, backward))
            return [recovered_message]

    client = HistoryClient()
    adapter._client = client
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=106,
                chat_id=28093080,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 12, None, None, "AUDIO")
    ]
    assert client.history_calls
    assert client.history_calls[0][0] == 28093080
    assert client.history_calls[0][2:] == (0, 10)
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "recovered"
        and event.get("attachment_types") == ["AUDIO"]
        for event in events
    )
    assert not any(
        event.get("event") == "max.inbound.skipped"
        and event.get("max_msg_id") == "106"
        for event in events
    )


@pytest.mark.asyncio
async def test_typed_empty_message_checks_history_before_live_name_lookup(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    recovered_message = SimpleNamespace(
        id=109,
        chat_id=28093080,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="AUDIO",
                audio_id=84,
                url="https://audio.example.test/recovered.ogg",
                duration=12,
            )
        ],
        link=None,
    )

    class HistoryBeforeNameClient(LookupClient):
        def __init__(self):
            super().__init__()
            self.call_order = []

        async def get_users(self, user_ids: list[int]):
            self.call_order.append("get_users")
            return []

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            self.call_order.append("fetch_history")
            return [recovered_message]

    client = HistoryBeforeNameClient()
    adapter._client = client
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    await adapter._handle_raw_message(
        SimpleNamespace(
            id=109,
            chat_id=28093080,
            sender=7001,
            text="",
            type="USER",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert len(received) == 1
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 12, None, None, "AUDIO")
    ]
    assert client.call_order[0] == "fetch_history"


@pytest.mark.asyncio
async def test_typed_empty_message_recovers_audio_from_raw_history_cache(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")
    adapter._client = LookupClient(users={7001: make_user("Вита")})

    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    raw_history_event = {
        "opcode": 49,
        "payload": {
            "messages": [
                {
                    "chatId": 195509792,
                    "id": 110,
                    "sender": 7001,
                    "text": "",
                    "type": "USER",
                    "attaches": [
                        {
                            "_type": "AUDIO",
                            "audioId": 84,
                            "url": "https://audio.example.test/secret.ogg",
                            "duration": 12,
                            "wave": "abc",
                            "token": "secret-token",
                            "text": "secret text",
                        }
                    ],
                }
            ]
        },
    }

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_receive(raw_history_event)
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=110,
                chat_id=195509792,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

    assert len(received) == 1
    assert received[0].chat_id == "195509792"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 12, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/secret.ogg",
            "audio_195509792_110",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "recovered"
        and event.get("reason") == "raw_history_cache_match"
        and event.get("attachment_types") == ["AUDIO"]
        for event in events
    )
    assert "secret-token" not in caplog.text
    assert "secret text" not in caplog.text
    assert "https://audio.example.test/secret.ogg" not in caplog.text


@pytest.mark.asyncio
async def test_raw_history_with_only_cid_is_not_cached_without_expected_context(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    adapter._client = LookupClient(users={7001: make_user("Вита")})

    await adapter._handle_raw_receive(
        {
            "opcode": 49,
            "payload": {
                "messages": [
                    {
                        "cid": 1779268162669013,
                        "id": 116606118527662695,
                        "sender": 7001,
                        "type": "USER",
                        "attaches": [
                            {
                                "_type": "AUDIO",
                                "audioId": 85,
                                "url": "https://audio.example.test/secret.ogg",
                                "duration": 9,
                            }
                        ],
                    }
                ]
            },
        }
    )

    assert adapter._raw_history_messages == {}


@pytest.mark.asyncio
async def test_typed_empty_message_recovers_audio_from_raw_send_and_wait_history(
    tmp_path,
    caplog,
):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class RawHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Людмила")})
            self.fetch_history_calls = 0

        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "cid": 1779274610031001,
                            "id": 116605798165273695,
                            "sender": 7001,
                            "time": 1779263269000,
                            "type": "USER",
                            "text": "",
                            "audio": {
                                "audioId": 91,
                                "url": "https://audio.example.test/ludmila.ogg",
                                "duration": 20,
                                "wave": "abc",
                                "token": "secret-token",
                            },
                        }
                    ]
                }
            }

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            self.fetch_history_calls += 1
            return []

    client = RawHistoryClient()
    adapter._client = client
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=116605798165273695,
                chat_id=200056208,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

    assert client.fetch_history_calls == 0
    assert len(received) == 1
    assert received[0].chat_id == "200056208"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 20, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/ludmila.ogg",
            "audio_200056208_116605798165273695",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]
    assert "secret-token" not in caplog.text
    assert "https://audio.example.test/ludmila.ogg" not in caplog.text


@pytest.mark.asyncio
async def test_typed_empty_message_recovers_top_level_audio_from_raw_history(
    tmp_path,
):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class RawHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Людмила")})

        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "cid": 1779274610031001,
                            "id": 116605798165273695,
                            "sender": 7001,
                            "time": 1779263269000,
                            "type": "AUDIO",
                            "audioId": 91,
                            "url": "https://audio.example.test/top-level.ogg",
                            "duration": 20,
                            "wave": "abc",
                        }
                    ]
                }
            }

    adapter._client = RawHistoryClient()
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    await adapter._handle_raw_message(
        SimpleNamespace(
            id=116605798165273695,
            chat_id=200056208,
            sender=7001,
            text="",
            type="USER",
            status=None,
            attaches=[],
            link=None,
        )
    )

    assert len(received) == 1
    assert received[0].chat_id == "200056208"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 20, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/top-level.ogg",
            "audio_200056208_116605798165273695",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_replay_recent_history_reclassifies_unsupported_nested_audio(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class RawHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Людмила")})

        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "cid": 1779274610031002,
                            "id": 116605799957888782,
                            "sender": 7001,
                            "time": 1779263296000,
                            "type": "USER",
                            "attaches": [
                                {
                                    "_type": "UNSUPPORTED",
                                    "payload": {
                                        "audioId": 92,
                                        "url": "https://audio.example.test/nested.ogg",
                                        "duration": 9,
                                        "wave": "abc",
                                    },
                                }
                            ],
                        }
                    ]
                }
            }

    adapter._client = RawHistoryClient()
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    replayed = await adapter.replay_recent_history(
        "200056208",
        limit=30,
        since_ts=0,
    )

    assert replayed == 1
    assert received[0].chat_id == "200056208"
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    ]
    assert adapter.url_downloads == [
        (
            "https://audio.example.test/nested.ogg",
            "audio_200056208_116605799957888782",
            None,
            ".ogg",
            "audio",
            "direct_url",
        )
    ]


@pytest.mark.asyncio
async def test_replay_recent_history_pre_dedups_known_messages_but_keeps_pending(tmp_path):
    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    class RawHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Людмила")})

        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "cid": 1779274610031002,
                            "id": 116605799957888782,
                            "sender": 7001,
                            "time": 1779263296000,
                            "type": "USER",
                            "text": "known",
                        },
                        {
                            "cid": 1779274610031003,
                            "id": 116605799957888783,
                            "sender": 7001,
                            "time": 1779263297000,
                            "type": "USER",
                            "text": "pending",
                        },
                    ]
                }
            }

    adapter._client = RawHistoryClient()
    adapter._remember_pending_empty_recovery(
        chat_id="200056208",
        raw_msg_id="116605799957888783",
        msg_id="116605799957888783",
        message_type="USER",
        flow_id="mx:200056208:pending",
    )
    received = []

    async def handler(msg):
        received.append(msg)

    async def is_known_message(_chat_id, _msg_id):
        return True

    adapter.on_message(handler)
    replayed = await adapter.replay_recent_history(
        "200056208",
        limit=30,
        since_ts=0,
        is_known_message=is_known_message,
    )

    assert replayed == 1
    assert [msg.msg_id for msg in received] == ["116605799957888783"]


@pytest.mark.asyncio
async def test_replay_recent_history_uses_requested_dm_chat_for_cid_only_payload(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    class RawHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Людмила")})

        async def _send_and_wait(self, opcode, payload, timeout=10):
            return {
                "payload": {
                    "messages": [
                        {
                            "cid": 1779274610031001,
                            "id": 116605798165273695,
                            "sender": 7001,
                            "time": 1779263269000,
                            "type": "USER",
                            "text": "Вы придете?",
                        },
                        {
                            "cid": 1779274610031002,
                            "id": 116605799957888782,
                            "sender": 7001,
                            "time": 1779263296000,
                            "type": "USER",
                            "voice": {
                                "_type": "VOICE",
                                "audioId": 92,
                                "url": "https://audio.example.test/replay.ogg",
                                "duration": 9,
                            },
                        },
                    ]
                }
            }

    adapter._client = RawHistoryClient()
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    replayed = await adapter.replay_recent_history(
        "200056208",
        limit=30,
        since_ts=0,
    )

    assert replayed == 2
    assert [msg.chat_id for msg in received] == ["200056208", "200056208"]
    assert received[0].text == "Вы придете?"
    assert received[1].attachment_types == ["AUDIO"]
    assert received[1].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    ]


@pytest.mark.asyncio
async def test_typed_empty_message_uses_raw_history_after_fetch_socket_error(
    tmp_path,
    monkeypatch,
    caplog,
):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("src.adapters.max_adapter.asyncio.sleep", no_sleep)

    raw_history_event = {
        "opcode": 49,
        "payload": {
            "messages": [
                {
                    "cid": 195509792,
                    "id": 111,
                    "sender": 7001,
                    "type": "USER",
                    "attaches": [
                        {
                            "_type": "AUDIO",
                            "audioId": 85,
                            "url": "https://audio.example.test/recovered.ogg",
                            "duration": 9,
                        }
                    ],
                }
            ]
        },
    }

    class SocketHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})
            self.history_calls = 0

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            self.history_calls += 1
            await adapter._handle_raw_receive(raw_history_event)
            raise RuntimeError("Send and wait failed (socket)")

    client = SocketHistoryClient()
    adapter._client = client
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=111,
                chat_id=195509792,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

    assert client.history_calls == 1
    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 9, None, None, "AUDIO")
    ]
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "recovered"
        and event.get("reason") == "raw_history_cache_after_fetch_error"
        for event in events
    )
    assert not any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "failed"
        and event.get("reason") == "recent_history_failed"
        for event in events
    )


@pytest.mark.asyncio
async def test_typed_empty_message_waits_for_delayed_raw_history_cache(
    tmp_path,
    monkeypatch,
    caplog,
):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")
    monkeypatch.setattr("src.adapters.max_adapter.MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS", 1)
    monkeypatch.setattr("src.adapters.max_adapter.MAX_EMPTY_RECOVERY_CACHE_POLL_SECONDS", 0.01)

    class EmptyHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            return []

    adapter._client = EmptyHistoryClient()
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._handle_raw_message(
            SimpleNamespace(
                id=112,
                chat_id=195509792,
                sender=7001,
                text="",
                type="USER",
                status=None,
                attaches=[],
                link=None,
            )
        )

        assert received == []
        assert adapter._pending_empty_recovery_tasks

        await adapter._handle_raw_receive(
            {
                "opcode": 49,
                "payload": {
                    "messages": [
                        {
                            "cid": 195509792,
                            "id": 112,
                            "sender": 7001,
                            "type": "USER",
                            "attaches": [
                                {
                                    "_type": "AUDIO",
                                    "audioId": 86,
                                    "url": "https://audio.example.test/delayed.ogg",
                                    "duration": 7,
                                }
                            ],
                        }
                    ]
                },
            }
        )
        await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 7, None, None, "AUDIO")
    ]
    assert not adapter._pending_empty_recovery_tasks
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "queued"
        and event.get("reason") == "raw_history_cache_wait"
        for event in events
    )
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "recovered"
        and event.get("reason") == "raw_history_cache_delayed_match"
        for event in events
    )


@pytest.mark.asyncio
async def test_pending_empty_recovery_worker_delivers_late_audio(tmp_path, caplog):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )
    local_path = str(tmp_path / "tmp" / "voice.ogg")
    adapter.url_result = (local_path, "voice.ogg")

    recovered_message = SimpleNamespace(
        id=113,
        chat_id=195509792,
        sender=7001,
        text="",
        type="USER",
        status=None,
        attaches=[
            SimpleNamespace(
                type="AUDIO",
                audio_id=87,
                url="https://audio.example.test/late.ogg",
                duration=6,
            )
        ],
        link=None,
    )

    class LateHistoryClient(LookupClient):
        def __init__(self):
            super().__init__(users={7001: make_user("Вита")})

        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            return [recovered_message]

    adapter._client = LateHistoryClient()
    received = []

    async def handler(msg):
        received.append(msg)

    adapter.on_message(handler)
    adapter._remember_pending_empty_recovery(
        chat_id="195509792",
        raw_msg_id="113",
        msg_id="113",
        message_type="USER",
        flow_id="mx:195509792:113",
    )
    job = dict(adapter._pending_empty_recoveries["195509792:113"])

    with caplog.at_level(logging.INFO, logger="src.adapters.max_adapter"):
        await adapter._attempt_pending_empty_recovery(job)

    assert len(received) == 1
    assert received[0].attachment_types == ["AUDIO"]
    assert received[0].attachments == [
        MaxAttachment("audio", local_path, "voice.ogg", 6, None, None, "AUDIO")
    ]
    assert adapter._pending_empty_recoveries == {}
    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "retry"
        and event.get("reason") == "durable_history_retry"
        for event in events
    )
    assert any(
        event.get("event") == "max.inbound.empty_recovery"
        and event.get("outcome") == "completed"
        and event.get("reason") in {"durable_history_recovered", "content_arrived"}
        for event in events
    )


def test_pending_empty_recovery_load_retries_soon_after_restart(tmp_path):
    now = int(time.time())
    state = [
        {
            "chat_id": "200056208",
            "raw_msg_id": "116605798165273695",
            "msg_id": "116605798165273695",
            "message_type": "USER",
            "attempts": 9,
            "created_at": now - 3600,
            "updated_at": now - 60,
            "next_attempt_at": now + 6 * 60 * 60,
            "last_error": "history_message_not_found_or_empty",
        }
    ]
    (tmp_path / "pending_empty_recoveries.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    adapter = AdapterHarness(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    job = adapter._pending_empty_recoveries["200056208:116605798165273695"]
    assert job["attempts"] == 9
    assert int(job["next_attempt_at"]) <= now + MAX_EMPTY_RECOVERY_CACHE_WAIT_SECONDS


@pytest.mark.asyncio
async def test_pending_empty_recovery_worker_reschedules_empty_history(tmp_path):
    adapter = CapturingAttachmentDownloadAdapter(
        phone="+7",
        data_dir=str(tmp_path),
        session_name="session",
        tmp_dir=str(tmp_path / "tmp"),
    )

    class EmptyHistoryClient(LookupClient):
        async def fetch_history(self, chat_id, from_time=None, forward=0, backward=200):
            return [
                SimpleNamespace(
                    id=114,
                    chat_id=195509792,
                    sender=7001,
                    text="",
                    type="USER",
                    status=None,
                    attaches=[],
                    link=None,
                )
            ]

    adapter._client = EmptyHistoryClient()
    adapter._remember_pending_empty_recovery(
        chat_id="195509792",
        raw_msg_id="114",
        msg_id="114",
        message_type="USER",
        flow_id="mx:195509792:114",
    )
    job = dict(adapter._pending_empty_recoveries["195509792:114"])

    await adapter._attempt_pending_empty_recovery(job)

    pending = adapter._pending_empty_recoveries["195509792:114"]
    assert pending["attempts"] == 1
    assert pending["last_error"] == "history_message_not_found_or_empty"
    assert pending["next_attempt_at"] > int(time.time())


@pytest.mark.asyncio
async def test_precise_get_message_is_tried_before_history_sweep(tmp_path):
    """PyMax 2.2.0 get_message() is attempted first; history sweep is skipped when it succeeds."""
    from src.adapters.max.voice_recovery import MaxVoiceRecoveryService
    from src.adapters.max.deps import VoiceRecoveryDeps
    from src.adapters.max.state import ConnectionState, RawHistoryState, EmptyRecoveryState

    history_calls = []
    precise_calls = []

    class PreciseClient:
        async def get_message(self, *, chat_id, message_id):
            precise_calls.append((chat_id, message_id))
            return SimpleNamespace(
                id=message_id,
                chat_id=chat_id,
                sender=101,
                type="USER",
                text="voice",
                attaches=[SimpleNamespace(type="AUDIO", url="https://cdn.example/audio.ogg")],
            )

        async def history_messages(self, **kwargs):
            history_calls.append(kwargs)
            return []

    class FakeRawPayload:
        def _get_cached_raw_history_message(self, *a): return None
        def _remember_expected_raw_history_message(self, *a): pass
        async def _fetch_raw_history_payload(self, **kw): return None
        def _find_raw_history_message_dict(self, *a): return None
        def _prepare_empty_recovery_candidate(self, obj, **kw): return obj
        def log_typed_empty_message(self, **kw): pass

    conn = ConnectionState()
    conn.client = PreciseClient()

    deps = VoiceRecoveryDeps(
        connection=conn,
        raw_history=RawHistoryState(),
        empty_recovery=EmptyRecoveryState(),
        data_dir=str(tmp_path),
        raw_payload=FakeRawPayload(),
    )
    svc = MaxVoiceRecoveryService(deps)

    result = await svc._recover_empty_message_from_recent_history(
        chat_id="-100",
        raw_msg_id="9001",
        flow_id="test",
    )

    assert precise_calls == [(-100, 9001)], "get_message must be called with int args"
    assert history_calls == [], "history sweep must not fire when get_message returns a result"
    assert result is not None
