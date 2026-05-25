from __future__ import annotations

from hypothesis import given, settings, strategies as st

from src.adapters.max import payload as max_payload
from src.adapters.max.ports import MaxClientMessage
from src.adapters.max.raw.inspection import AttachmentInspectorProxy
from src.adapters.max.raw.parser import RawPayloadParser


SAFE_TEXT = st.text(
    alphabet=list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-"),
    min_size=1,
    max_size=30,
).filter(lambda value: bool(value.strip()))

SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-10_000, max_value=10_000),
    st.text(
        alphabet=list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-"),
        max_size=20,
    ),
)

MIXED_KEY = st.one_of(
    st.sampled_from(
        [
            "chatId",
            "chat_id",
            "messageId",
            "message_id",
            "msgId",
            "sender",
            "safeField",
            "_private",
            "token",
            "rawPayload",
            "text",
            "url",
        ]
    ),
    st.integers(min_value=-3, max_value=3),
    st.tuples(st.sampled_from(["array", "key", "chatId"]), st.integers(min_value=0, max_value=3)),
)

NESTED_PAYLOAD = st.recursive(
    SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(MIXED_KEY, children, max_size=5),
    ),
    max_leaves=25,
)

ATTACHMENT = st.fixed_dictionaries(
    {
        "type": st.sampled_from(["AUDIO", "VIDEO", "PHOTO", "FILE"]),
        "fileId": st.integers(min_value=1, max_value=999_999),
        "duration": st.integers(min_value=0, max_value=600),
    }
)


@given(value=SCALAR)
@settings(max_examples=50)
def test_payload_value_normalizes_case_and_underscore_aliases(value):
    assert max_payload.payload_value({"chat_id": value}, "chatId") == value
    assert max_payload.payload_value({"messageId": value}, "message_id") == value
    assert max_payload.payload_value({"audio_id": value}, "audioId") == value


@given(payload=NESTED_PAYLOAD)
@settings(max_examples=100)
def test_safe_field_paths_never_emit_private_or_unsafe_fields(payload):
    wrapper = {
        "root": payload,
        "_private": "redacted-value",
        "token": "redacted-value",
        "rawPayload": "redacted-value",
        "message": {"text": "redacted-value", "safeField": payload},
        ("array", "key"): {"url": "redacted-value", "safeField": payload},
    }

    paths = max_payload.safe_field_paths(wrapper, max_depth=4, max_items=80)

    assert len(paths) == len(set(paths))
    for path in paths:
        lowered = path.lower()
        assert "token" not in lowered
        assert "url" not in lowered
        assert "text" not in lowered
        assert "raw" not in lowered
        assert "._" not in path
        assert not path.startswith("_")


@given(
    chat_key=st.sampled_from(["chatId", "chat_id"]),
    id_key=st.sampled_from(["id", "messageId", "message_id", "msgId"]),
    msg_id=st.integers(min_value=1, max_value=999_999),
    sender=st.integers(min_value=1, max_value=999_999),
    text=SAFE_TEXT,
    attachments=st.lists(ATTACHMENT, max_size=2),
)
@settings(max_examples=100)
def test_raw_regular_message_round_trips_mixed_alias_payloads(
    chat_key,
    id_key,
    msg_id,
    sender,
    text,
    attachments,
):
    parser = RawPayloadParser(attachments=AttachmentInspectorProxy(lambda: None))
    chat_id = -70000000000001
    payload = {
        chat_key: chat_id,
        "message": {
            id_key: msg_id,
            "sender": sender,
            "text": text,
            "type": "USER",
            "attaches": attachments,
        },
    }

    message = parser._build_raw_regular_message(payload)

    assert isinstance(message, MaxClientMessage)
    assert str(message.id) == str(msg_id)
    assert str(message.chat_id) == str(chat_id)
    assert str(message.sender) == str(sender)
    assert message.text == text
    assert message._from_raw_unwrapped is True
    assert len(message.attaches or []) == len(attachments)
