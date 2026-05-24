import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bridge import media_retry as bridge_media_retry
from src.bridge import background as bridge_background
from src.bridge.contracts import (
    MaxAttachment,
    MaxAttachmentFailure,
    MaxMessage,
    MaxRecoveryChatSnapshot,
    MaxRecoveryContactSnapshot,
    MaxRecoverySnapshot,
)
from src.bridge.core import BridgeCore
from src.db.repository import ChatBinding, PendingMediaDownload, Repository
from src.runtime.health import RuntimeHealthStore, Severity


class DummyRepo:
    def __init__(self):
        self.bindings = []
        self.activity_map = {}
        self.binding_by_chat = {}
        self.saved_users: dict[str, str] = {}   # user_id → name
        self._find_user_result: str | None = None
        self.delivery_logs = []
        self.pending_media = []
        self.reply_mappings = {}
        self.pending_stats = {"pending_count": 0, "oldest_created_at": None}
        self.duplicates: set[tuple[str, str]] = set()
        self.phantom_bindings = []

    async def get_binding_by_topic(self, tg_topic_id: int):
        return SimpleNamespace(max_chat_id="-70000000000003", tg_topic_id=tg_topic_id, mode="active")

    async def get_binding(self, max_chat_id: str):
        return self.binding_by_chat.get(max_chat_id)

    async def get_max_msg_id_by_tg(self, tg_msg_id: int):
        value = self.reply_mappings.get(tg_msg_id, "mx-reply-1")
        return getattr(value, "max_msg_id", value)

    async def get_tg_reply_mapping(self, tg_msg_id: int):
        value = self.reply_mappings.get(tg_msg_id, "mx-reply-1")
        if value is None:
            return None
        if hasattr(value, "max_chat_id"):
            return value
        return SimpleNamespace(
            tg_msg_id=tg_msg_id,
            max_chat_id="-70000000000003",
            max_msg_id=value,
            tg_topic_id=99,
            source="test",
            created_at=1,
        )

    async def save_message(self, record):
        self.saved_record = record

    async def save_binding(self, binding):
        self.binding_by_chat[binding.max_chat_id] = binding

    async def update_title(self, max_chat_id: str, title: str):
        binding = self.binding_by_chat.get(max_chat_id)
        if binding is not None:
            binding.title = title

    async def update_mode(self, max_chat_id: str, mode: str):
        binding = self.binding_by_chat.get(max_chat_id)
        if binding is not None:
            binding.mode = mode

    async def find_phantom_topic_bindings(self):
        return list(self.phantom_bindings)

    async def log_delivery(self, *args, **kwargs):
        self.delivery_logs.append((args, kwargs))
        self.logged = (args, kwargs)

    async def enqueue_pending_media(self, job):
        for existing in self.pending_media:
            if (
                existing.max_chat_id == job.max_chat_id
                and existing.max_msg_id == job.max_msg_id
                and existing.attachment_index == job.attachment_index
                and existing.kind == job.kind
            ):
                return existing.id
        job.id = len(self.pending_media) + 1
        self.pending_media.append(job)
        return job.id

    async def find_active_pending_media(self, *, max_chat_id: str, max_msg_id: str,
                                        attachment_index: int, kind: str):
        for job in self.pending_media:
            if (
                job.max_chat_id == max_chat_id
                and job.max_msg_id == max_msg_id
                and job.attachment_index == attachment_index
                and job.kind == kind
                and job.status in {"pending", "retry", "leased"}
            ):
                return job
        return None

    async def find_active_pending_media_by_reference(self, *, media_chat_id: str,
                                                     media_msg_id: str,
                                                     attachment_index: int,
                                                     kind: str,
                                                     reference_kind: str,
                                                     reference_id: str):
        for job in self.pending_media:
            if (
                job.media_chat_id == media_chat_id
                and job.media_msg_id == media_msg_id
                and job.attachment_index == attachment_index
                and job.kind == kind
                and job.reference_kind == reference_kind
                and job.reference_id == reference_id
                and job.status in {"pending", "retry", "leased"}
            ):
                return job
        return None

    async def get_due_pending_media(self, *, now=None, limit=5):
        return [
            job for job in self.pending_media
            if job.status in {"pending", "retry", "leased"}
        ][:limit]

    async def lease_pending_media(self, job_id: int, *, lease_until: int, now=None):
        for job in self.pending_media:
            if job.id == job_id:
                job.status = "leased"
                job.lease_until = lease_until
                return True
        return False

    async def mark_pending_media_retry(self, job_id: int, *, error: str, next_attempt_at: int, now=None):
        for job in self.pending_media:
            if job.id == job_id:
                job.status = "retry"
                job.attempts += 1
                job.last_error = error
                job.next_attempt_at = next_attempt_at
                job.lease_until = None

    async def mark_pending_media_delivered(self, job_id: int, *, tg_msg_id: int, now=None):
        for job in self.pending_media:
            if job.id == job_id:
                job.status = "delivered"
                job.attempts += 1
                job.delivered_tg_msg_id = tg_msg_id
                job.lease_until = None

    async def mark_pending_media_failed(self, job_id: int, *, error: str, now=None):
        for job in self.pending_media:
            if job.id == job_id:
                job.status = "failed"
                job.attempts += 1
                job.last_error = error
                job.lease_until = None

    async def save_tg_reply_mapping(self, tg_msg_id: int, max_chat_id: str, max_msg_id: str,
                                    tg_topic_id: int | None, *, source: str, commit: bool = True):
        self.reply_mappings[tg_msg_id] = max_msg_id

    async def count_pending_media(self):
        return self.pending_stats

    async def list_bindings(self):
        return self.bindings

    async def get_chat_activity_map_since(self, since_ts: int):
        return self.activity_map

    async def save_user(self, user_id: str, display_name: str):
        self.saved_users[user_id] = display_name

    async def find_user_by_name(self, display_name: str) -> str | None:
        return self._find_user_result

    async def is_duplicate(self, max_msg_id: str, max_chat_id: str) -> bool:
        return (max_msg_id, max_chat_id) in self.duplicates


class DummyMax:
    def __init__(self):
        self._find_user_result: str | None = None
        self._last_outbound_error: str | None = None
        self._last_outbound_attempts: int = 0
        self.video_reference_result = None
        self.video_reference_calls = []
        self.audio_reference_result = None
        self.audio_reference_calls = []
        self.replay_calls = []
        self.empty_stats = {"pending_count": 0, "oldest_created_at": None}
        self.start_handlers = []
        self.issue_handlers = []
        self.last_issue = None
        self.last_connected_at = None
        self.egress_status = None
        self.last_egress_probe = None
        self.ready = True

    def on_message(self, handler):
        self.handler = handler

    def on_start(self, handler):
        self.start_handlers.append(handler)

    def on_issue(self, handler):
        self.issue_handlers.append(handler)

    def is_ready(self):
        return self.ready

    def get_own_id(self) -> str | None:
        return "999"

    def get_dm_partner_id(self, chat_id: str) -> str | None:
        return None

    def find_user_by_name(self, name: str) -> str | None:
        return self._find_user_result

    async def resolve_user_name(self, user_id: str):
        return None

    async def resolve_chat_title(self, chat_id: str):
        return None

    async def send_message(self, chat_id: str, text: str, reply_to_msg_id=None,
                           media_path=None, media_type=None, flow_id=None):
        self.sent = (chat_id, text, reply_to_msg_id, flow_id)
        return "mx-out-1"

    async def download_video_reference(self, **kwargs):
        self.video_reference_calls.append(kwargs)
        return self.video_reference_result

    async def download_audio_reference(self, **kwargs):
        self.audio_reference_calls.append(kwargs)
        return self.audio_reference_result

    async def replay_recent_history(self, chat_id: str, *, limit: int = 30, since_ts=None, flow_id=None):
        self.replay_calls.append((chat_id, limit, since_ts, flow_id))
        return 0

    async def collect_recovery_snapshot(self):
        return MaxRecoverySnapshot(
            max_user_id=None,
            masked_phone=None,
            session_fingerprint_hash=None,
        )

    def get_pending_empty_recovery_stats(self):
        return self.empty_stats

    def get_last_outbound_error(self) -> str | None:
        return self._last_outbound_error

    def get_last_outbound_attempts(self) -> int:
        return self._last_outbound_attempts

    def get_last_issue(self):
        return self.last_issue

    def get_last_connected_at(self):
        return self.last_connected_at

    def get_egress_status(self):
        return self.egress_status

    def get_last_egress_probe(self):
        return self.last_egress_probe

    async def probe_egress(self):
        return self.last_egress_probe


class DummyRecoveryMax(DummyMax):
    def __init__(self, snapshot: MaxRecoverySnapshot, *, snapshot_delay: float = 0):
        super().__init__()
        self.snapshot = snapshot
        self.snapshot_delay = snapshot_delay
        self.snapshot_calls = 0
        self.start_handlers = []

    def on_start(self, handler):
        self.start_handlers.append(handler)

    async def collect_recovery_snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_delay:
            await asyncio.sleep(self.snapshot_delay)
        return self.snapshot


class DummyTelegram:
    def __init__(self):
        self.calls = []
        self.commands = {}
        self.arg_commands = {}
        self.arg_command_options = {}
        self.fail_voice = False
        self.delete_topic_result = True
        self.close_topic_result = True

    def on_reply(self, handler):
        self.handler = handler

    def on_command(self, cmd: str, handler):
        self.commands[cmd] = handler

    def on_arg_command(self, cmd: str, handler, **kwargs):
        self.arg_commands[cmd] = handler
        self.arg_command_options[cmd] = kwargs

    async def send_photo(self, topic_id, path, caption="", flow_id=None):
        self.calls.append(("photo", caption))
        return 1

    async def send_document(self, topic_id, path, caption="", filename="", flow_id=None):
        self.calls.append(("document", caption, filename))
        return 2

    async def send_video(self, topic_id, path, caption="", filename="", duration=None, width=None, height=None, flow_id=None):
        self.calls.append(("video", caption, filename, duration, width, height))
        return 3

    async def send_audio(self, topic_id, path, caption="", filename="", duration=None, flow_id=None):
        self.calls.append(("audio", caption, filename, duration))
        return 4

    async def send_voice(self, topic_id, path, caption="", duration=None, flow_id=None):
        self.calls.append(("voice", caption, duration))
        if self.fail_voice:
            return None
        return 6

    async def send_text(self, topic_id, text, reply_to_msg_id=None, flow_id=None):
        self.calls.append(("text", text))
        return 5

    async def send_notification(self, text):
        self.calls.append(("notification", text))

    async def send_owner_document(self, path: str, caption: str = "", filename: str = ""):
        self.calls.append(("owner_document", Path(path).name, caption, filename))
        return True

    async def create_topic(self, title, flow_id=None):
        self.calls.append(("create_topic", title, flow_id))
        return 101

    async def rename_topic(self, topic_id, title, flow_id=None):
        self.calls.append(("rename_topic", topic_id, title, flow_id))

    async def delete_topic(self, topic_id, flow_id=None):
        self.calls.append(("delete_topic", topic_id, flow_id))
        return self.delete_topic_result

    async def close_topic(self, topic_id, flow_id=None):
        self.calls.append(("close_topic", topic_id, flow_id))
        return self.close_topic_result


class DummyConfig(SimpleNamespace):
    def get_chat_title(self, max_chat_id: str):
        titles = getattr(self, "_chat_titles", {})
        return titles.get(max_chat_id)

    def get_chat_mode(self, max_chat_id: str):
        modes = getattr(self, "_chat_modes", {})
        return modes.get(max_chat_id, "active")


def make_bridge(repo=None, max_adapter=None, tg_adapter=None):
    return BridgeCore(
        config=DummyConfig(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
                forward_voice=True,
                forward_documents=True,
                forward_photos=True,
            ),
            health=SimpleNamespace(reminder_interval_hours=4),
        ),
        repo=repo or DummyRepo(),
        max_adapter=max_adapter or DummyMax(),
        tg_adapter=tg_adapter or DummyTelegram(),
    )


async def process_pending_media_for_bridge(bridge: BridgeCore, job: PendingMediaDownload):
    await bridge_media_retry.process_pending_media_download(
        cfg=bridge._cfg,
        repo=bridge._repo,
        max_adapter=bridge._max,
        tg=bridge._tg,
        job=job,
    )


def test_command_dispatcher_registers_expected_commands():
    tg = DummyTelegram()
    bridge = make_bridge(tg_adapter=tg)

    assert set(tg.commands) == {"status", "chats", "help"}
    assert set(tg.arg_commands) == {"dm", "recovery"}
    assert tg.commands["status"] == bridge._status.build_status_message
    assert tg.arg_commands["dm"] == bridge._commands.handle_dm
    assert tg.arg_commands["recovery"] == bridge._recovery.handle_command
    assert tg.arg_command_options["dm"] == {"allow_group_general": True}


@pytest.mark.asyncio
async def test_on_max_message_skips_probable_client_cid_chat_id():
    repo = DummyRepo()
    tg = DummyTelegram()
    bridge = make_bridge(repo=repo, tg_adapter=tg)

    await bridge._on_max_message(
        MaxMessage(
            msg_id="116606540857475456",
            chat_id="1779274610031001",
            chat_title=None,
            sender_id="7001",
            sender_name="Ирина",
            text="hello",
            attachments=[],
            attachment_types=[],
            rendered_texts=[],
            message_type="USER",
            status=None,
            is_dm=True,
            is_own=False,
            raw=SimpleNamespace(),
        )
    )

    assert tg.calls == []
    assert repo.binding_by_chat == {}


@pytest.mark.asyncio
async def test_cleanup_phantom_topics_deletes_then_disables_binding():
    repo = DummyRepo()
    tg = DummyTelegram()
    binding = SimpleNamespace(
        max_chat_id="1779274610031001",
        tg_topic_id=1564,
        title="Чат 1779274610031001",
        mode="active",
        created_at=1,
    )
    repo.phantom_bindings = [binding]
    repo.binding_by_chat[binding.max_chat_id] = binding
    bridge = make_bridge(repo=repo, tg_adapter=tg)

    stats = await bridge.cleanup_phantom_topics()

    assert stats == {"found": 1, "deleted": 1, "closed": 0, "disabled": 1}
    assert ("delete_topic", 1564, "mx:1779274610031001:phantom-cleanup") in tg.calls
    assert binding.mode == "disabled"
    assert binding.title == "[deleted phantom] Чат 1779274610031001"


@pytest.mark.asyncio
async def test_cleanup_phantom_topics_falls_back_to_close():
    repo = DummyRepo()
    tg = DummyTelegram()
    tg.delete_topic_result = False
    binding = SimpleNamespace(
        max_chat_id="1779274610031001",
        tg_topic_id=1564,
        title="Чат 1779274610031001",
        mode="active",
        created_at=1,
    )
    repo.phantom_bindings = [binding]
    repo.binding_by_chat[binding.max_chat_id] = binding
    bridge = make_bridge(repo=repo, tg_adapter=tg)

    stats = await bridge.cleanup_phantom_topics()

    assert stats == {"found": 1, "deleted": 0, "closed": 1, "disabled": 1}
    assert ("close_topic", 1564, "mx:1779274610031001:phantom-cleanup") in tg.calls
    assert binding.mode == "disabled"


@pytest.mark.asyncio
async def test_dm_history_sweep_replays_active_direct_chats(monkeypatch):
    repo = DummyRepo()
    max_adapter = DummyMax()
    repo.bindings = [
        SimpleNamespace(max_chat_id="200056208", tg_topic_id=1, title="Людмила", mode="active"),
        SimpleNamespace(max_chat_id="-70638114166223", tg_topic_id=2, title="Happy School", mode="active"),
        SimpleNamespace(max_chat_id="1779274610031001", tg_topic_id=3, title="Чат 1779274610031001", mode="active"),
        SimpleNamespace(max_chat_id="28093080", tg_topic_id=4, title="Вик", mode="disabled"),
    ]
    bridge = make_bridge(repo=repo, max_adapter=max_adapter)

    async def stop_after_first_sleep(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr("src.bridge.background.asyncio.sleep", stop_after_first_sleep)
    with pytest.raises(asyncio.CancelledError):
        await bridge.run_dm_history_sweep(poll_interval=120, limit=30, backfill_seconds=48 * 60 * 60)

    assert len(max_adapter.replay_calls) == 1
    chat_id, limit, since_ts, flow_id = max_adapter.replay_calls[0]
    assert chat_id == "200056208"
    assert limit == 30
    assert since_ts is not None
    assert flow_id == "mx:200056208:history-sweep"


@pytest.mark.asyncio
async def test_dm_history_sweep_skips_until_max_is_ready(monkeypatch):
    repo = DummyRepo()
    max_adapter = DummyMax()
    max_adapter.ready = False
    repo.bindings = [
        SimpleNamespace(max_chat_id="200056208", tg_topic_id=1, title="Людмила", mode="active"),
    ]
    bridge = make_bridge(repo=repo, max_adapter=max_adapter)

    async def stop_after_first_sleep(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr("src.bridge.background.asyncio.sleep", stop_after_first_sleep)
    with pytest.raises(asyncio.CancelledError):
        await bridge.run_dm_history_sweep(poll_interval=120, limit=30, backfill_seconds=48 * 60 * 60)

    assert max_adapter.replay_calls == []


@pytest.mark.asyncio
async def test_forward_to_telegram_sends_media_then_rendered_system_text(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    video_path = Path(tmp_path) / "clip.mp4"
    video_path.write_bytes(b"1234")

    msg = MaxMessage(
        msg_id="42",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text=None,
        attachments=[MaxAttachment("video", str(video_path), "clip.mp4", 7, 640, 360, "VIDEO")],
        attachment_types=["VIDEO"],
        rendered_texts=["Участник вышел из чата"],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 3
    assert tg_adapter.calls == [
        ("video", "[Тестовый Пользователь]", "clip.mp4", 7, 640, 360),
        ("text", "Участник вышел из чата"),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_sends_voice_note_for_voice_source(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    voice_path = Path(tmp_path) / "voice.ogg"
    voice_path.write_bytes(b"OggSvoice")

    msg = MaxMessage(
        msg_id="44",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text="",
        attachments=[MaxAttachment("audio", str(voice_path), "voice.ogg", 3, None, None, "VOICE")],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 6
    assert tg_adapter.calls == [
        ("voice", "[Тестовый Пользователь]", 3),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_sends_audio_source_as_voice_in_dm(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                forward_voice=True,
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    voice_path = Path(tmp_path) / "voice.ogg"
    voice_path.write_bytes(b"OggSvoice")

    msg = MaxMessage(
        msg_id="45",
        chat_id="28093080",
        chat_title="Вик Мултык",
        sender_id="10",
        sender_name="Вик Мултык",
        text="",
        attachments=[MaxAttachment("audio", str(voice_path), "voice.ogg", 13, None, None, "AUDIO")],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 6
    assert tg_adapter.calls == [
        ("voice", "", 13),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_falls_back_to_audio_when_voice_send_fails(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    tg_adapter.fail_voice = True
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                forward_voice=True,
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    voice_path = Path(tmp_path) / "voice.ogg"
    voice_path.write_bytes(b"OggSvoice")

    msg = MaxMessage(
        msg_id="46",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text="",
        attachments=[MaxAttachment("audio", str(voice_path), "voice.ogg", 5, None, None, "AUDIO")],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 4
    assert tg_adapter.calls == [
        ("voice", "[Тестовый Пользователь]", 5),
        ("audio", "[Тестовый Пользователь]", "voice.ogg", 5),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_respects_disabled_voice_forwarding(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                forward_voice=False,
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    voice_path = Path(tmp_path) / "voice.ogg"
    voice_path.write_bytes(b"OggSvoice")

    msg = MaxMessage(
        msg_id="47",
        chat_id="28093080",
        chat_title="Вик Мултык",
        sender_id="10",
        sender_name="Вик Мултык",
        text="",
        attachments=[MaxAttachment("audio", str(voice_path), "voice.ogg", 13, None, None, "AUDIO")],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 5
    assert tg_adapter.calls == [
        ("text", "[unsupported: AUDIO]"),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_uses_rendered_text_without_media(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="43",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text=None,
        attachments=[],
        attachment_types=["CONTROL"],
        rendered_texts=["Тестовый Пользователь вышел(а) из чата"],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 5
    assert tg_adapter.calls == [
        ("text", "Тестовый Пользователь вышел(а) из чата"),
    ]


@pytest.mark.asyncio
async def test_forward_to_telegram_reports_failed_attachment_download(tmp_path):
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=DummyRepo(),
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="45",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text=None,
        attachments=[],
        attachment_types=["VIDEO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
        attachment_failures=[
            MaxAttachmentFailure(
                kind="video",
                source_type="VIDEO",
                filename=None,
                index=0,
                reason="download_failed",
            )
        ],
    )

    result = await bridge._forward_to_telegram(msg, topic_id=99)

    assert result == 5
    assert tg_adapter.calls == [
        ("text", "⚠️ Не удалось скачать вложение MAX: video #1"),
    ]


@pytest.mark.asyncio
async def test_on_max_message_enqueues_retryable_video_failure(tmp_path):
    repo = DummyRepo()
    repo.binding_by_chat["-70000000000003"] = SimpleNamespace(
        max_chat_id="-70000000000003",
        tg_topic_id=99,
        title="Тестовая группа",
        mode="active",
    )
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    photo_path = Path(tmp_path) / "photo.jpg"
    photo_path.write_bytes(b"\xff\xd8\xff")
    msg = MaxMessage(
        msg_id="mx-video-1",
        chat_id="-70000000000003",
        chat_title="Тестовая группа",
        sender_id="10",
        sender_name="Тестовый Пользователь",
        text="",
        attachments=[MaxAttachment("photo", str(photo_path), "photo.jpg", None, 10, 10, "PHOTO")],
        attachment_types=["PHOTO", "VIDEO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
        attachment_failures=[
            MaxAttachmentFailure(
                kind="video",
                source_type="VIDEO",
                filename=None,
                index=4,
                reason="download_failed",
                retryable=True,
                media_chat_id="-70000000000003",
                media_msg_id="mx-video-1",
                reference_kind="video_id",
                reference_id="555",
                duration=10,
                width=640,
                height=360,
            )
        ],
    )

    await bridge._on_max_message(msg)

    assert tg_adapter.calls == [
        ("photo", "[Тестовый Пользователь]"),
        ("text", "⏳ Видео MAX #5 докачивается и будет дослано позже"),
    ]
    assert len(repo.pending_media) == 1
    job = repo.pending_media[0]
    assert job.reference_kind == "video_id"
    assert job.reference_id == "555"
    assert job.tg_topic_id == 99
    assert job.attachment_index == 4
    assert "http" not in str(job)
    assert "token" not in str(job).lower()


@pytest.mark.asyncio
async def test_on_max_message_enqueues_retryable_audio_failure():
    repo = DummyRepo()
    repo.binding_by_chat["200056208"] = SimpleNamespace(
        max_chat_id="200056208",
        tg_topic_id=1372,
        title="Людмила",
        mode="active",
    )
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    msg = MaxMessage(
        msg_id="116605799957888782",
        chat_id="200056208",
        chat_title=None,
        sender_id="7001",
        sender_name="Людмила",
        text="",
        attachments=[],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
        attachment_failures=[
            MaxAttachmentFailure(
                kind="audio",
                source_type="AUDIO",
                filename=None,
                index=0,
                reason="download_failed",
                retryable=True,
                media_chat_id="200056208",
                media_msg_id="116605799957888782",
                reference_kind="audio_id",
                reference_id="92",
                duration=9,
            )
        ],
    )

    await bridge._on_max_message(msg)

    assert tg_adapter.calls == [
        ("text", "⏳ Голосовое MAX #1 докачивается и будет дослано позже"),
    ]
    assert len(repo.pending_media) == 1
    job = repo.pending_media[0]
    assert job.kind == "audio"
    assert job.reference_kind == "audio_id"
    assert job.reference_id == "92"


@pytest.mark.asyncio
async def test_existing_pending_audio_failure_does_not_duplicate_placeholder():
    repo = DummyRepo()
    repo.binding_by_chat["200056208"] = SimpleNamespace(
        max_chat_id="200056208",
        tg_topic_id=1372,
        title="Елена",
        mode="active",
    )
    repo.pending_media.append(
        PendingMediaDownload(
            id=7,
            max_chat_id="200056208",
            max_msg_id="116605799957888782",
            tg_topic_id=1372,
            attachment_index=0,
            kind="audio",
            source_type="AUDIO",
            media_chat_id="200056208",
            media_msg_id="116605799957888782",
            reference_kind="audio_id",
            reference_id="92",
            status="retry",
        )
    )
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    msg = MaxMessage(
        msg_id="116605799957888782:USER",
        chat_id="200056208",
        chat_title=None,
        sender_id="7001",
        sender_name="Елена",
        text="",
        attachments=[],
        attachment_types=["AUDIO"],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
        attachment_failures=[
            MaxAttachmentFailure(
                kind="audio",
                source_type="AUDIO",
                filename=None,
                index=0,
                reason="download_failed",
                retryable=True,
                media_chat_id="200056208",
                media_msg_id="116605799957888782",
                reference_kind="audio_id",
                reference_id="92",
                duration=9,
            )
        ],
    )

    await bridge._on_max_message(msg)

    assert tg_adapter.calls == []
    assert len(repo.pending_media) == 1
    assert repo.pending_media[0].id == 7
    assert repo.delivery_logs[-1][0][3] == "partial"
    assert repo.delivery_logs[-1][0][4] == "attachment_download_pending_duplicate"


@pytest.mark.asyncio
async def test_pending_media_worker_delivers_video_and_maps_reply(tmp_path):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    video_path = Path(tmp_path) / "retry.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    max_adapter.video_reference_result = MaxAttachment(
        "video",
        str(video_path),
        "retry.mp4",
        10,
        640,
        360,
        "VIDEO",
    )
    job = PendingMediaDownload(
        id=1,
        max_chat_id="-70000000000003",
        max_msg_id="mx-video-1",
        tg_topic_id=99,
        attachment_index=4,
        kind="video",
        source_type="VIDEO",
        media_chat_id="-70000000000003",
        media_msg_id="mx-video-1",
        reference_kind="video_id",
        reference_id="555",
        status="leased",
    )
    repo.pending_media.append(job)

    await process_pending_media_for_bridge(bridge, job)

    assert tg_adapter.calls == [
        ("video", "Докачанное видео MAX #5", "retry.mp4", 10, 640, 360),
    ]
    assert repo.reply_mappings[3] == "mx-video-1"
    assert job.status == "delivered"
    assert job.delivered_tg_msg_id == 3
    assert not video_path.exists()


@pytest.mark.asyncio
async def test_pending_media_worker_delivers_audio_as_voice_and_maps_reply(tmp_path):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    voice_path = Path(tmp_path) / "retry.ogg"
    voice_path.write_bytes(b"OggS")
    max_adapter.audio_reference_result = MaxAttachment(
        "audio",
        str(voice_path),
        "retry.ogg",
        9,
        None,
        None,
        "AUDIO",
    )
    job = PendingMediaDownload(
        id=1,
        max_chat_id="200056208",
        max_msg_id="116605799957888782",
        tg_topic_id=1372,
        attachment_index=0,
        kind="audio",
        source_type="AUDIO",
        media_chat_id="200056208",
        media_msg_id="116605799957888782",
        reference_kind="audio_id",
        reference_id="92",
        status="leased",
    )
    repo.pending_media.append(job)

    await process_pending_media_for_bridge(bridge, job)

    assert tg_adapter.calls == [
        ("voice", "Докачанное голосовое MAX #1", 9),
    ]
    assert repo.reply_mappings[6] == "116605799957888782"
    assert job.status == "delivered"
    assert job.delivered_tg_msg_id == 6
    assert not voice_path.exists()
    assert max_adapter.audio_reference_calls[0]["reference_kind"] == "audio_id"


@pytest.mark.asyncio
async def test_pending_media_worker_reschedules_download_failure():
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    job = PendingMediaDownload(
        id=1,
        max_chat_id="-70000000000003",
        max_msg_id="mx-video-1",
        tg_topic_id=99,
        attachment_index=4,
        kind="video",
        source_type="VIDEO",
        media_chat_id="-70000000000003",
        media_msg_id="mx-video-1",
        reference_kind="video_id",
        reference_id="555",
        status="leased",
    )
    repo.pending_media.append(job)

    await process_pending_media_for_bridge(bridge, job)

    assert job.status == "retry"
    assert job.attempts == 1
    assert job.last_error == "download_failed"
    assert job.next_attempt_at > 0


@pytest.mark.asyncio
async def test_pending_media_worker_marks_missing_reference_terminal():
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    job = PendingMediaDownload(
        id=1,
        max_chat_id="-70000000000003",
        max_msg_id="mx-video-1",
        tg_topic_id=99,
        attachment_index=4,
        kind="video",
        source_type="VIDEO",
        media_chat_id="-70000000000003",
        media_msg_id="mx-video-1",
        reference_kind="video_id",
        reference_id="",
        status="leased",
    )
    repo.pending_media.append(job)

    await process_pending_media_for_bridge(bridge, job)

    assert job.status == "failed"
    assert job.last_error == "missing_stable_media_reference"


@pytest.mark.asyncio
async def test_on_tg_reply_to_delayed_video_uses_original_max_message():
    repo = DummyRepo()
    repo.reply_mappings[777] = "mx-video-1"
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=778,
        text="Ответ на позднее видео",
        reply_to_tg_msg_id=777,
        sender_name="Мария Иванова",
    )

    assert max_adapter.sent == (
        "-70000000000003",
        "[Мария Иванова]\nОтвет на позднее видео",
        "mx-video-1",
        "tg:99:778",
    )


@pytest.mark.asyncio
async def test_on_tg_reply_prefixes_sender_name_for_max():
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=555,
        text="Проверка связи",
        reply_to_tg_msg_id=123,
        sender_name="Мария Иванова",
    )

    assert max_adapter.sent == (
        "-70000000000003",
        "[Мария Иванова]\nПроверка связи",
        "mx-reply-1",
        "tg:99:555",
    )


@pytest.mark.asyncio
async def test_on_tg_reply_after_remap_skips_stale_reply_to_max_id():
    repo = DummyRepo()
    repo.reply_mappings[123] = SimpleNamespace(
        tg_msg_id=123,
        max_chat_id="-old-chat",
        max_msg_id="old-max-message",
        tg_topic_id=99,
        source="message_map",
        created_at=1,
    )
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=555,
        text="После remap",
        reply_to_tg_msg_id=123,
        sender_name="Мария Иванова",
    )

    assert max_adapter.sent == (
        "-70000000000003",
        "[Мария Иванова]\nПосле remap",
        None,
        "tg:99:555",
    )


@pytest.mark.asyncio
async def test_on_tg_reply_rejects_too_large_media(tmp_path):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=0.000001),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    media_path = Path(tmp_path) / "huge.bin"
    media_path.write_bytes(b"0123456789")

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=555,
        text="",
        reply_to_tg_msg_id=None,
        sender_name="Мария Иванова",
        media_path=str(media_path),
        media_type="document",
    )

    assert not hasattr(max_adapter, "sent")
    assert tg_adapter.calls == [
        ("text", "🚫 [too large: huge.bin] (лимит: 1e-06MB)"),
    ]


@pytest.mark.asyncio
async def test_get_or_create_topic_resolves_group_title_via_live_max_lookup():
    class GroupAwareMax(DummyMax):
        async def resolve_chat_title(self, chat_id: str):
            assert chat_id == "-70243447272944"
            return "2104 ПН 16:40 Scratch Jr"

    repo = DummyRepo()
    max_adapter = GroupAwareMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=DummyConfig(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="42",
        chat_id="-70243447272944",
        chat_title=None,
        sender_id="10",
        sender_name="Наталья Ростовцева",
        text="Тест",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    topic_id = await bridge._get_or_create_topic(msg, flow_id="mx:-70243447272944:42")

    assert topic_id == 101
    assert tg_adapter.calls == [
        ("create_topic", "2104 ПН 16:40 Scratch Jr", "mx:-70243447272944:42"),
    ]
    assert repo.binding_by_chat["-70243447272944"].title == "2104 ПН 16:40 Scratch Jr"


@pytest.mark.asyncio
async def test_get_or_create_topic_prefers_dm_sender_name_for_title():
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=DummyConfig(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="42",
        chat_id="208748958",
        chat_title=None,
        sender_id="99577134",
        sender_name="Елена",
        text="ок",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
    )

    topic_id = await bridge._get_or_create_topic(msg, flow_id="mx:208748958:42")

    assert topic_id == 101
    assert tg_adapter.calls == [
        ("create_topic", "Елена", "mx:208748958:42"),
    ]
    assert repo.binding_by_chat["208748958"].title == "Елена"


@pytest.mark.asyncio
async def test_get_or_create_topic_uses_dm_sender_id_before_chat_id():
    class SenderAwareMax(DummyMax):
        def __init__(self):
            super().__init__()
            self.resolved_user_ids = []

        async def resolve_user_name(self, user_id: str):
            self.resolved_user_ids.append(user_id)
            if user_id == "99577134":
                return "Елена"
            return None

    repo = DummyRepo()
    max_adapter = SenderAwareMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=DummyConfig(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    msg = MaxMessage(
        msg_id="42",
        chat_id="208748958",
        chat_title=None,
        sender_id="99577134",
        sender_name=None,
        text="ок",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="USER",
        status=None,
        is_dm=True,
        is_own=False,
        raw=None,
    )

    topic_id = await bridge._get_or_create_topic(msg, flow_id="mx:208748958:42")

    assert topic_id == 101
    assert max_adapter.resolved_user_ids == ["99577134"]
    assert tg_adapter.calls == [
        ("create_topic", "Елена", "mx:208748958:42"),
    ]


@pytest.mark.asyncio
async def test_build_chats_message_lists_topics_with_activity():
    repo = DummyRepo()
    repo.bindings = [
        SimpleNamespace(max_chat_id="-1", tg_topic_id=101, title="Школьный чат", mode="active", created_at=1),
        SimpleNamespace(max_chat_id="-2", tg_topic_id=102, title="Секция", mode="readonly", created_at=2),
    ]
    repo.activity_map = {
        "-1": {"inbound": 3, "outbound": 1, "total": 4},
        "-2": {"inbound": 0, "outbound": 2, "total": 2},
    }

    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=DummyMax(),
        tg_adapter=DummyTelegram(),
    )

    text = await bridge._status.build_chats_message(period_hours=24)

    assert "🗂 Чаты: 2 (активных: 1)" in text
    assert "✅ #101 Школьный чат · ↓3 ↑1" in text
    assert "🔒 #102 Секция · ↓0 ↑2" in text


@pytest.mark.asyncio
async def test_build_status_message_includes_max_issue_summary():
    class IssueAwareMax(DummyMax):
        def is_ready(self):
            return False

        def get_last_connected_at(self):
            return 1776962052

        def get_last_issue(self):
            return SimpleNamespace(
                summary="MAX сессия недействительна, нужна повторная авторизация",
                requires_reauth=True,
            )

    repo = DummyRepo()

    async def count_messages_since(_since):
        return {"inbound": 0, "outbound": 0}

    async def count_deliveries_since(_since):
        return {}

    async def get_chat_activity_since(_since, limit=10):
        return []

    repo.count_messages_since = count_messages_since
    repo.count_deliveries_since = count_deliveries_since
    repo.get_chat_activity_since = get_chat_activity_since
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    bridge = _make_bridge(repo=repo, max_adapter=IssueAwareMax())

    text = await bridge._status.build_status_message(period_hours=4)

    assert "⚠️ Проблема MAX" in text
    assert "MAX сессия недействительна, нужна повторная авторизация" in text
    assert "Требуется: reauth по SMS" in text


@pytest.mark.asyncio
async def test_build_status_message_includes_manual_direct_egress_warning():
    class DirectEgressMax(DummyMax):
        def get_egress_status(self):
            return {
                "max_egress_active": "hetzner_direct",
                "max_egress_label": "прямой Hetzner VPS (ручной аварийный режим)",
                "warning": "MAX uses non-RU direct egress",
            }

    repo = DummyRepo()

    async def count_messages_since(_since):
        return {"inbound": 0, "outbound": 0}

    async def count_deliveries_since(_since):
        return {}

    async def get_chat_activity_since(_since, limit=10):
        return []

    repo.count_messages_since = count_messages_since
    repo.count_deliveries_since = count_deliveries_since
    repo.get_chat_activity_since = get_chat_activity_since
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    bridge = _make_bridge(repo=repo, max_adapter=DirectEgressMax())

    text = await bridge._status.build_status_message(period_hours=4)

    assert "MAX egress: hetzner_direct" in text
    assert "прямой Hetzner VPS" in text
    assert "MAX uses non-RU direct egress" in text


@pytest.mark.asyncio
async def test_build_status_message_describes_home_router_egress():
    class HomeRouterEgressMax(DummyMax):
        def get_egress_status(self):
            return {
                "max_egress_active": "home_ru_proxy",
                "max_egress_label": "роутерный РФ Channel M",
            }

    repo = DummyRepo()

    async def count_messages_since(_since):
        return {"inbound": 0, "outbound": 0}

    async def count_deliveries_since(_since):
        return {}

    async def get_chat_activity_since(_since, limit=10):
        return []

    repo.count_messages_since = count_messages_since
    repo.count_deliveries_since = count_deliveries_since
    repo.get_chat_activity_since = get_chat_activity_since
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    bridge = _make_bridge(repo=repo, max_adapter=HomeRouterEgressMax())

    text = await bridge._status.build_status_message(period_hours=4)

    assert "MAX egress: home_ru_proxy" in text
    assert "роутерный РФ Channel M" in text


@pytest.mark.asyncio
async def test_build_status_message_includes_safe_egress_probe():
    max_adapter = DummyMax()
    max_adapter.egress_status = {
        "max_egress_active": "home_ru_proxy",
        "max_egress_label": "роутерный РФ Channel M",
    }
    max_adapter.last_egress_probe = {
        "ok": False,
        "stage": "http_connect",
        "latency_ms": 321,
        "checked_at": int(time.time()),
        "error": "MaxEgressUnavailable: MAX egress proxy CONNECT failed",
    }

    repo = DummyRepo()
    repo.count_messages_since = lambda _since: asyncio.sleep(0, result={"inbound": 0, "outbound": 0})
    repo.count_deliveries_since = lambda _since: asyncio.sleep(0, result={})
    repo.get_chat_activity_since = lambda _since, limit=10: asyncio.sleep(0, result=[])
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    bridge = _make_bridge(repo=repo, max_adapter=max_adapter)

    text = await bridge._status.build_status_message(period_hours=4)

    assert "MAX egress probe: ❌ http_connect, 321ms" in text
    assert "CONNECT failed" in text
    assert "user:pass" not in text


@pytest.mark.asyncio
async def test_build_status_message_refreshes_home_proxy_egress_probe():
    class RefreshingProbeMax(DummyMax):
        def __init__(self):
            super().__init__()
            self.egress_status = {
                "max_egress_active": "home_ru_proxy",
                "max_egress_label": "роутерный РФ Channel M",
            }
            self.last_egress_probe = {
                "ok": False,
                "stage": "proxy_tcp",
                "checked_at": int(time.time()) - 900,
                "error": "ConnectionRefusedError: [Errno 111] Connection refused",
            }
            self.probe_calls = 0

        async def probe_egress(self):
            self.probe_calls += 1
            self.last_egress_probe = {
                "ok": True,
                "stage": "target_tls",
                "latency_ms": 122,
                "checked_at": int(time.time()),
            }
            return self.last_egress_probe

    max_adapter = RefreshingProbeMax()
    repo = DummyRepo()
    repo.count_messages_since = lambda _since: asyncio.sleep(0, result={"inbound": 0, "outbound": 0})
    repo.count_deliveries_since = lambda _since: asyncio.sleep(0, result={})
    repo.get_chat_activity_since = lambda _since, limit=10: asyncio.sleep(0, result=[])
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    bridge = _make_bridge(repo=repo, max_adapter=max_adapter)

    text = await bridge._status.build_status_message(period_hours=4)

    assert max_adapter.probe_calls == 1
    assert "MAX egress probe: ✅ target_tls, 122ms" in text
    assert "Connection refused" not in text


@pytest.mark.asyncio
async def test_build_status_message_uses_shared_health_snapshot(tmp_path):
    class OfflineMax(DummyMax):
        def is_ready(self):
            return False

        def get_last_connected_at(self):
            return 1776962052

    repo = DummyRepo()

    async def count_messages_since(_since):
        return {"inbound": 1, "outbound": 2}

    async def count_deliveries_since(_since):
        return {}

    async def get_chat_activity_since(_since, limit=10):
        return []

    repo.count_messages_since = count_messages_since
    repo.count_deliveries_since = count_deliveries_since
    repo.get_chat_activity_since = get_chat_activity_since
    repo.list_bindings = lambda: asyncio.sleep(0, result=[])

    health = RuntimeHealthStore(tmp_path)
    await health.mark_healthy("runtime", summary="Worker running", notify=False)
    await health.report_issue(
        "max_link",
        code="session_invalid",
        summary="MAX сессия недействительна",
        raw_cause="Invalid token",
        severity=Severity.CRITICAL,
        requires_reauth=True,
    )

    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=OfflineMax(),
        tg_adapter=DummyTelegram(),
        health_store=health,
    )

    text = await bridge._status.build_status_message(period_hours=4)

    assert "🩺 Runtime Health" in text
    assert "MAX сессия недействительна" in text
    assert "Требуется: reauth по SMS" in text


@pytest.mark.asyncio
async def test_watchdog_sends_gap_notice_after_reconnect():
    class WatchdogMax(DummyMax):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def is_ready(self):
            self.calls += 1
            if self.calls == 1:
                return False
            return True

    repo = DummyRepo()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=WatchdogMax(),
        tg_adapter=tg_adapter,
    )

    task = asyncio.create_task(
        bridge.run_max_watchdog(alert_after_seconds=0, check_interval=0)
    )
    try:
        for _ in range(100):
            if len([c for c in tg_adapter.calls if c[0] == "notification"]) >= 3:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    notifications = [c[1] for c in tg_adapter.calls if c[0] == "notification"]
    assert any("MAX недоступен уже" in text for text in notifications)
    assert any("Возможен пропуск сообщений MAX" in text for text in notifications)
    assert any("MAX восстановлен" in text for text in notifications)


@pytest.mark.asyncio
async def test_max_watchdog_reports_egress_down_without_restart(tmp_path):
    class OfflineHomeProxyMax(DummyMax):
        def __init__(self):
            super().__init__()
            self.egress_status = {"max_egress_active": "home_ru_proxy"}
            self.probe_calls = 0

        def is_ready(self):
            return False

        async def probe_egress(self):
            self.probe_calls += 1
            self.last_egress_probe = {
                "ok": False,
                "stage": "http_connect",
                "error": "MaxEgressUnavailable: CONNECT failed",
            }
            return self.last_egress_probe

    max_adapter = OfflineHomeProxyMax()
    health = RuntimeHealthStore(tmp_path)
    restarts = []

    task = asyncio.create_task(
        bridge_background.run_max_watchdog(
            max_adapter=max_adapter,
            health=health,
            send_ops_notification=lambda _text: asyncio.sleep(0),
            emit_health_alert=lambda _change: asyncio.sleep(0),
            alert_after_seconds=999,
            check_interval=0,
            egress_probe_interval=0,
            self_heal_grace_seconds=0,
            self_heal_state_path=tmp_path / "self-heal.json",
            restart_process=lambda reason: restarts.append(reason),
        )
    )
    try:
        for _ in range(50):
            if max_adapter.probe_calls:
                break
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    snapshot = await health.get_snapshot()
    issue = snapshot.subsystems["max_link"].issue
    assert issue is not None
    assert issue.code == "max_egress_unavailable"
    assert restarts == []


@pytest.mark.asyncio
async def test_max_watchdog_restarts_once_when_proxy_ok_but_max_stays_offline(tmp_path):
    class OfflineHomeProxyMax(DummyMax):
        def __init__(self):
            super().__init__()
            self.egress_status = {"max_egress_active": "home_ru_proxy"}

        def is_ready(self):
            return False

        async def probe_egress(self):
            self.last_egress_probe = {
                "ok": True,
                "stage": "target_tls",
                "latency_ms": 12,
            }
            return self.last_egress_probe

    class RestartRequested(RuntimeError):
        pass

    state_path = tmp_path / "self-heal.json"
    max_adapter = OfflineHomeProxyMax()

    with pytest.raises(RestartRequested):
        await bridge_background.run_max_watchdog(
            max_adapter=max_adapter,
            health=None,
            send_ops_notification=lambda _text: asyncio.sleep(0),
            emit_health_alert=lambda _change: asyncio.sleep(0),
            alert_after_seconds=999,
            check_interval=0,
            egress_probe_interval=0,
            self_heal_grace_seconds=0,
            self_heal_state_path=state_path,
            self_heal_restart_cooldown_seconds=3600,
            restart_process=lambda _reason: (_ for _ in ()).throw(RestartRequested()),
        )

    assert state_path.exists()

    suppressed_restarts = []
    task = asyncio.create_task(
        bridge_background.run_max_watchdog(
            max_adapter=max_adapter,
            health=None,
            send_ops_notification=lambda _text: asyncio.sleep(0),
            emit_health_alert=lambda _change: asyncio.sleep(0),
            alert_after_seconds=999,
            check_interval=0,
            egress_probe_interval=0,
            self_heal_grace_seconds=0,
            self_heal_state_path=state_path,
            self_heal_restart_cooldown_seconds=3600,
            restart_process=lambda reason: suppressed_restarts.append(reason),
        )
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert suppressed_restarts == []


@pytest.mark.asyncio
async def test_on_tg_reply_logs_forward_completion(caplog):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=SimpleNamespace(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )

    with caplog.at_level(logging.INFO, logger="src.bridge.core"):
        await bridge._on_tg_reply(
            topic_id=99,
            tg_msg_id=777,
            text="Проверка логов",
            reply_to_tg_msg_id=123,
            sender_name="Мария Иванова",
        )

    events = [getattr(record, "event_fields", {}) for record in caplog.records]
    assert any(
        event.get("event") == "bridge.outbound.forward_finished"
        and event.get("outcome") == "delivered"
        for event in events
    )


@pytest.mark.asyncio
async def test_on_tg_reply_logs_failed_delivery_with_max_error():
    class FailingMax(DummyMax):
        async def send_message(self, chat_id: str, text: str, reply_to_msg_id=None,
                               media_path=None, media_type=None, flow_id=None):
            self.sent = (chat_id, text, reply_to_msg_id, flow_id)
            self._last_outbound_error = "Socket is not connected"
            self._last_outbound_attempts = 3
            return None

    repo = DummyRepo()
    max_adapter = FailingMax()
    tg_adapter = DummyTelegram()
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=888,
        text="Проверка ошибки",
        reply_to_tg_msg_id=None,
        sender_name="Мария Иванова",
    )

    assert tg_adapter.calls[-1] == ("text", "❌ Не удалось отправить сообщение в MAX")
    args, kwargs = repo.delivery_logs[-1]
    assert args[0] == "out_fail:99:888"
    assert args[1] == "-70000000000003"
    assert args[2] == "outbound"
    assert args[3] == "failed"
    assert args[4] == "Socket is not connected (attempts=3)"
    assert kwargs["attempts"] == 3


@pytest.mark.asyncio
async def test_on_tg_reply_reports_safe_pymax_sequence_overflow_error():
    class FailingMax(DummyMax):
        async def send_message(self, chat_id: str, text: str, reply_to_msg_id=None,
                               media_path=None, media_type=None, flow_id=None):
            self.sent = (chat_id, text, reply_to_msg_id, flow_id)
            self._last_outbound_error = "pymax_tcp_sequence_overflow: PyMax TCP seq exceeded 255"
            self._last_outbound_attempts = 1
            return None

    repo = DummyRepo()
    max_adapter = FailingMax()
    tg_adapter = DummyTelegram()
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=890,
        text="do not leak this text",
        reply_to_tg_msg_id=None,
        sender_name="Мария Иванова",
    )

    assert tg_adapter.calls[-1] == (
        "text",
        "❌ Не удалось отправить сообщение в MAX (MAX transport: pymax_tcp_sequence_overflow)",
    )
    args, kwargs = repo.delivery_logs[-1]
    assert args[0] == "out_fail:99:890"
    assert args[3] == "failed"
    assert args[4] == "pymax_tcp_sequence_overflow: PyMax TCP seq exceeded 255"
    assert "do not leak this text" not in str(repo.delivery_logs)
    assert kwargs["attempts"] == 1


@pytest.mark.asyncio
async def test_on_tg_reply_logs_too_large_outbound_failure(tmp_path):
    repo = DummyRepo()
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)
    bridge._cfg.bridge.max_file_size_mb = 0.000001

    media_path = Path(tmp_path) / "huge.bin"
    media_path.write_bytes(b"0123456789")

    await bridge._on_tg_reply(
        topic_id=99,
        tg_msg_id=889,
        text="",
        reply_to_tg_msg_id=None,
        sender_name="Мария Иванова",
        media_path=str(media_path),
        media_type="document",
    )

    args, kwargs = repo.delivery_logs[-1]
    assert args[0] == "out_fail:99:889"
    assert args[1] == "-70000000000003"
    assert args[2] == "outbound"
    assert args[3] == "failed"
    assert args[4] == "too_large:huge.bin"
    assert kwargs["attempts"] == 1


# ---------------------------------------------------------------------------
# MaxAdapter._fix_filename_encoding — cp1251-as-latin-1 mojibake
# ---------------------------------------------------------------------------

def test_fix_filename_encoding_fixes_cyrillic_mojibake():
    from src.adapters.max_adapter import MaxAdapter
    # "Вальс из к/ф Маскарад - Арам Хачатурян.mp3" stored as cp1251, read as latin-1
    garbled = "Âàëüñ èç ê/ô Ìàñêàðàä - Àðàì Õà÷àòóðÿí.mp3"
    fixed = MaxAdapter._fix_filename_encoding(garbled)
    assert fixed == "Вальс из к/ф Маскарад - Арам Хачатурян.mp3"


def test_fix_filename_encoding_leaves_ascii_unchanged():
    from src.adapters.max_adapter import MaxAdapter
    assert MaxAdapter._fix_filename_encoding("audio_track.ogg") == "audio_track.ogg"


def test_fix_filename_encoding_leaves_proper_utf8_unchanged():
    from src.adapters.max_adapter import MaxAdapter
    # Already correct UTF-8 Cyrillic — encode("latin-1") raises, original returned
    name = "Вальс.mp3"
    assert MaxAdapter._fix_filename_encoding(name) == name


# ---------------------------------------------------------------------------
# /dm command
# ---------------------------------------------------------------------------

def _make_bridge(repo=None, max_adapter=None, tg_adapter=None):
    return BridgeCore(
        config=DummyConfig(
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo or DummyRepo(),
        max_adapter=max_adapter or DummyMax(),
        tg_adapter=tg_adapter or DummyTelegram(),
    )


@pytest.mark.asyncio
async def test_cmd_recovery_scan_report_set_remap_and_export(tmp_path, caplog):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    await repo.save_binding(ChatBinding("-old-chat", 77, "Client group", "active", 1))
    snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash",
        chats=[
            MaxRecoveryChatSnapshot(
                max_chat_id="-old-chat",
                title="Client group",
                chat_kind="group",
                access_type="LINK",
                invite_link="https://max.ru/join/example",
                admin_contacts=[{"user_id": "501", "name": "Admin"}],
                participant_count=7,
            ),
            MaxRecoveryChatSnapshot(
                max_chat_id="-new-chat",
                title="New visible group",
                chat_kind="group",
                participant_count=3,
            ),
        ],
    )
    max_adapter = DummyRecoveryMax(snapshot)
    tg_adapter = DummyTelegram()
    bridge = BridgeCore(
        config=DummyConfig(
            storage=SimpleNamespace(tmp_dir=tmp_path),
            bridge=SimpleNamespace(max_file_size_mb=50),
            content=SimpleNamespace(
                placeholder_unsupported="[unsupported: {type}]",
                placeholder_file_too_large="[too large: {filename}]",
            ),
        ),
        repo=repo,
        max_adapter=max_adapter,
        tg_adapter=tg_adapter,
    )
    assert asyncio.iscoroutinefunction(max_adapter.start_handlers[0])

    try:
        with caplog.at_level(logging.INFO, logger="src.bridge.core"):
            scan = await bridge._recovery.handle_command("scan")
            report = await bridge._recovery.handle_command("report")
            set_result = await bridge._recovery.handle_command(
                'set 77 priority=9 note="call admin" status=manual_admin_required admin="Admin:501"'
            )
            remap = await bridge._recovery.handle_command("remap 77 -new-chat")
            post_remap_report = await bridge._recovery.handle_command("report")
            export = await bridge._recovery.handle_command("export")

        assert "snapshot обновлён" in scan
        assert "Свежесть snapshot:" in report
        assert "unmapped MAX: 1" in report
        assert "https://max.ru/join/example" not in report
        assert not any(
            "https://max.ru/join/example" in str(getattr(record, "event_fields", {}))
            for record in caplog.records
        )
        assert "обновлена" in set_result
        assert "теперь отправляет" in remap
        assert "unmapped MAX: 0" in post_remap_report
        assert "Export отправлен" in export
        assert any(call[0] == "owner_document" for call in tg_adapter.calls)
        assert (await repo.get_binding_by_topic(77)).max_chat_id == "-new-chat"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_new_binding_recovery_scan_is_async_and_does_not_delay_forwarding(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash",
        chats=[
            MaxRecoveryChatSnapshot(
                max_chat_id="-async-chat",
                title="Async group",
                chat_kind="group",
            ),
        ],
        contacts=[
            MaxRecoveryContactSnapshot(
                max_user_id="300",
                display_name="DM Partner",
                old_dm_chat_id="300",
                current_dm_chat_id="300",
            ),
        ],
    )
    max_adapter = DummyRecoveryMax(snapshot, snapshot_delay=0.05)
    tg_adapter = DummyTelegram()
    bridge = make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)
    bridge._recovery._event_scan_delays["new_binding"] = 0.05

    try:
        await bridge._on_max_message(
            MaxMessage(
                msg_id="mx-async-1",
                chat_id="-async-chat",
                chat_title="Async group",
                sender_id="10",
                sender_name="Мария",
                text="hello",
                attachments=[],
                attachment_types=[],
                rendered_texts=[],
                message_type="USER",
                status=None,
                is_dm=False,
                is_own=False,
                raw=SimpleNamespace(secret="raw payload must not be inspected"),
            )
        )

        assert ("create_topic", "Async group", "mx:-async-chat:mx-async-1") in tg_adapter.calls
        assert ("text", "[Мария] hello") in tg_adapter.calls
        assert max_adapter.snapshot_calls == 0

        await asyncio.wait_for(bridge._recovery._event_scan_task, timeout=1)
        assert max_adapter.snapshot_calls == 1
        report = await repo.get_recovery_report()
        assert report["stats"]["total"] == 1
        assert report["stats"]["dm_contacts"] == 1
    finally:
        task = bridge._recovery._event_scan_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_control_events_debounce_into_one_recovery_scan(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    await repo.save_binding(ChatBinding("-control-chat", 77, "Control group", "active", 1))
    snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash",
        chats=[
            MaxRecoveryChatSnapshot(
                max_chat_id="-control-chat",
                title="Control group",
                chat_kind="group",
            ),
        ],
    )
    max_adapter = DummyRecoveryMax(snapshot)
    tg_adapter = DummyTelegram()
    bridge = make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)
    bridge._recovery._event_scan_delays["control_event"] = 0.05
    bridge._recovery._event_scan_cooldowns["control_event"] = 0

    try:
        for index in range(2):
            await bridge._on_max_message(
                MaxMessage(
                    msg_id=f"mx-control-{index}",
                    chat_id="-control-chat",
                    chat_title="Control group",
                    sender_id="10",
                    sender_name="Мария",
                    text=None,
                    attachments=[],
                    attachment_types=["CONTROL"],
                    rendered_texts=["Control event"],
                    message_type="USER",
                    status=None,
                    is_dm=False,
                    is_own=False,
                    raw=SimpleNamespace(payload={"token": "secret"}),
                )
            )

        assert max_adapter.snapshot_calls == 0
        await asyncio.wait_for(bridge._recovery._event_scan_task, timeout=1)
        assert max_adapter.snapshot_calls == 1
    finally:
        task = bridge._recovery._event_scan_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_auto_changes_are_summarized_in_status_not_notified(tmp_path, caplog):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    secret_link = "https://max.ru/join/secret-token"
    snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash",
        chats=[
            MaxRecoveryChatSnapshot(
                max_chat_id="-secret-chat",
                title="Secret Client Room",
                chat_kind="group",
                access_type="LINK",
                invite_link=secret_link,
                admin_contacts=[{"user_id": "501", "name": "Admin"}],
                participant_count=3,
            ),
        ],
    )
    max_adapter = DummyRecoveryMax(snapshot)
    tg_adapter = DummyTelegram()
    bridge = make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)

    try:
        with caplog.at_level(logging.INFO, logger="src.bridge.core"):
            result = await bridge._recovery.safe_scan(reason="control_event", notify=False)
            await bridge._recovery.maybe_notify_changes(reason="control_event", result=result)
            await bridge._recovery.maybe_notify_changes(reason="control_event", result=result)

        notifications = [call[1] for call in tg_adapter.calls if call[0] == "notification"]
        assert notifications == []

        status = await bridge._status.build_status_message(period_hours=4)
        assert "🧭 MAX recovery snapshot" in status
        assert "unmapped: 1" in status
        assert "invite/admin: 0/0" in status
        assert "/recovery report" in status

        redacted_text = "\n".join([status, *notifications])
        assert secret_link not in redacted_text
        assert "Secret Client Room" not in redacted_text
        assert "+7" not in redacted_text

        logged = "\n".join(str(getattr(record, "event_fields", {})) for record in caplog.records)
        assert "periodic_status_summary" in logged
        assert secret_link not in logged
        assert "Secret Client Room" not in logged
        assert "raw payload" not in logged
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_account_migration_notification_is_redacted_and_deduped(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    first_snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash-1",
        chats=[],
    )
    second_snapshot = MaxRecoverySnapshot(
        max_user_id="200",
        masked_phone="+7******9999",
        session_fingerprint_hash="hash-2",
        chats=[],
    )
    max_adapter = DummyRecoveryMax(first_snapshot)
    tg_adapter = DummyTelegram()
    bridge = make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)

    try:
        await bridge._recovery.safe_scan(reason="max_connect", notify=False)
        max_adapter.snapshot = second_snapshot
        result = await bridge._recovery.safe_scan(reason="max_connect", notify=False)
        await bridge._recovery.maybe_notify_changes(reason="max_connect", result=result)
        await bridge._recovery.maybe_notify_changes(reason="max_connect", result=result)

        notifications = [call[1] for call in tg_adapter.calls if call[0] == "notification"]
        assert len(notifications) == 1
        assert "MAX account migration required" in notifications[0]
        assert "MAX recovery snapshot изменился" not in notifications[0]
        assert "/recovery report" in notifications[0]
        assert "+7" not in notifications[0]
        assert "hash" not in notifications[0]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_recovery_scan_updates_dm_contact_registry_and_report(tmp_path):
    repo = Repository(str(tmp_path / "bridge.db"))
    await repo.connect()
    await repo.save_binding(ChatBinding("300", 77, "DM Partner", "active", 1))
    snapshot = MaxRecoverySnapshot(
        max_user_id="100",
        masked_phone="+7******1234",
        session_fingerprint_hash="hash",
        chats=[
            MaxRecoveryChatSnapshot(
                max_chat_id="300",
                title="DM Partner",
                chat_kind="dm",
                dm_partner_user_id="300",
                dm_partner_name="DM Partner",
            ),
        ],
        contacts=[
            MaxRecoveryContactSnapshot(
                max_user_id="300",
                display_name="DM Partner",
                old_dm_chat_id="300",
                current_dm_chat_id="300",
            ),
        ],
    )
    bridge = make_bridge(
        repo=repo,
        max_adapter=DummyRecoveryMax(snapshot),
        tg_adapter=DummyTelegram(),
    )

    try:
        scan = await bridge._recovery.handle_command("scan")
        report = await bridge._recovery.handle_command("report")
        export = await repo.export_recovery_registry()

        assert "DM contacts: 1" in scan
        assert "DM contacts: 1 · linked topics: 1 · needs contact/remap: 0" in report
        assert "DM Partner" not in report
        assert export["dm_contacts"] == [
            {
                "max_user_id": "300",
                "display_name": "DM Partner",
                "old_dm_chat_id": "300",
                "current_dm_chat_id": "300",
                "tg_topic_id": 77,
                "source": "dialog",
                "recovery_status": "visible",
                "first_seen_at": export["dm_contacts"][0]["first_seen_at"],
                "last_seen_at": export["dm_contacts"][0]["last_seen_at"],
                "last_scan_at": export["dm_contacts"][0]["last_scan_at"],
            }
        ]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cmd_dm_finds_user_in_db_and_sends():
    repo = DummyRepo()
    max_adapter = DummyMax()

    async def find_by_name_specific(name):
        return "12345" if name == "Татьяна Геннадиевна Ладина" else None

    repo.find_user_by_name = find_by_name_specific
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter)

    result = await bridge._commands.handle_dm("Татьяна Геннадиевна Ладина Добрый день!")

    assert "✅" in result
    assert "Татьяна Геннадиевна Ладина" in result
    assert max_adapter.sent[0] == "12345"
    assert max_adapter.sent[1] == "Добрый день!"


@pytest.mark.asyncio
async def test_cmd_dm_falls_back_to_pymax_cache_when_db_empty():
    repo = DummyRepo()
    repo._find_user_result = None  # DB miss
    max_adapter = DummyMax()
    max_adapter._find_user_result = "99999"  # pymax cache hit
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter)

    result = await bridge._commands.handle_dm("Мария Иванова привет")

    assert "✅" in result
    assert max_adapter.sent[0] == "99999"
    assert max_adapter.sent[1] == "привет"


@pytest.mark.asyncio
async def test_cmd_dm_returns_error_when_user_not_found():
    bridge = _make_bridge()  # both DB and pymax return None

    result = await bridge._commands.handle_dm("Несуществующий Человек текст")

    assert "❌" in result
    assert "не найден" in result


@pytest.mark.asyncio
async def test_cmd_dm_returns_usage_hint_when_no_args():
    bridge = _make_bridge()

    result = await bridge._commands.handle_dm("")
    assert "Формат" in result

    result = await bridge._commands.handle_dm("ОдноСлово")
    assert "Формат" in result


@pytest.mark.asyncio
async def test_cmd_dm_tries_longest_name_prefix_first():
    """3-word name match should win over 1-word match."""
    repo = DummyRepo()
    max_adapter = DummyMax()

    call_log = []

    async def find_by_name_db(name):
        call_log.append(("db", name))
        if name == "Татьяна Геннадиевна Ладина":
            return "777"
        return None

    repo.find_user_by_name = find_by_name_db
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter)

    result = await bridge._commands.handle_dm("Татьяна Геннадиевна Ладина вечер")

    # Input is 4 words: min(4, 4-1)=3, so first attempt IS the 3-word name
    assert call_log[0] == ("db", "Татьяна Геннадиевна Ладина")
    assert max_adapter.sent[0] == "777"
    assert max_adapter.sent[1] == "вечер"


# ---------------------------------------------------------------------------
# Sender persistence in _on_max_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_on_max_message_persists_sender_to_db():
    repo = DummyRepo()
    repo.binding_by_chat = {}
    max_adapter = DummyMax()
    tg_adapter = DummyTelegram()
    bridge = _make_bridge(repo=repo, max_adapter=max_adapter, tg_adapter=tg_adapter)

    msg = MaxMessage(
        msg_id="m1",
        chat_id="-70000000000003",
        chat_title="Хор Гармония",
        sender_id="42",
        sender_name="Татьяна Геннадиевна Ладина",
        text="Добрый день",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="TEXT",
        status=None,
        is_dm=False,
        is_own=False,
        raw=None,
    )

    await bridge._on_max_message(msg)

    assert repo.saved_users.get("42") == "Татьяна Геннадиевна Ладина"


@pytest.mark.asyncio
async def test_on_max_message_does_not_persist_own_sender():
    """Own messages (is_own=True) must not be saved as known_users."""
    repo = DummyRepo()
    bridge = _make_bridge(repo=repo)

    msg = MaxMessage(
        msg_id="m2",
        chat_id="-70000000000003",
        chat_title="Хор Гармония",
        sender_id="999",
        sender_name="Мария Иванова",
        text="Сообщение",
        attachments=[],
        attachment_types=[],
        rendered_texts=[],
        message_type="TEXT",
        status=None,
        is_dm=False,
        is_own=True,
        raw=None,
    )

    await bridge._on_max_message(msg)

    assert "999" not in repo.saved_users
