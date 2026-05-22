from types import SimpleNamespace

from src.adapters.max import errors as max_errors
from src.adapters.max import payload as max_payload
from src.adapters.max import users as max_users
from src.adapters.max.media import downloader as max_downloader
from src.adapters.max.media import ua as max_ua


def test_max_ua_mapping_selects_chrome_android_profile():
    headers, src_ag, family = max_ua.download_client_profile_for_url(
        "https://cdn.example.test/file?srcAg=CHROME_ANDROID"
    )

    assert headers == {"User-Agent": max_ua.MAX_CDN_ANDROID_CHROME_USER_AGENT}
    assert src_ag == "CHROME_ANDROID"
    assert family == "chrome_android"


def test_payload_helpers_match_case_and_strip_unsafe_fields():
    payload = {
        "chatId": 42,
        "message": {
            "safe": {"child": 1},
            "downloadUrl": "https://secret.example.test/file",
            "token": "secret",
        },
    }

    assert max_payload.payload_value(payload, "chat_id") == 42
    assert max_payload.payload_value(payload, "CHATID") == 42
    assert max_payload.safe_payload_error_code({"error": {"code": "audio.not.ready"}}) == "audio.not.ready"
    assert max_payload.safe_field_paths(payload) == [
        "chatId",
        "message",
        "message.safe",
        "message.safe.child",
    ]


def test_error_classification_is_pymax_free():
    corrupt = max_errors.classify_runtime_error(
        RuntimeError("sqlite3.DatabaseError: database disk image is malformed")
    )
    invalid = max_errors.classify_runtime_error(RuntimeError("Invalid token"))

    assert corrupt is not None
    assert corrupt.kind == "session_corrupt"
    assert corrupt.requires_reauth is True
    assert invalid is not None
    assert invalid.kind == "session_invalid"


def test_users_and_downloader_helpers_are_plain_object_based():
    dialog = SimpleNamespace(participants=[SimpleNamespace(id="own"), SimpleNamespace(account_id="peer")])
    video_url = max_downloader.extract_video_url(
        {"preview": "https://cdn.example.test/thumb.jpg", "streams": {"360": "https://cdn.example.test/v.mp4"}}
    )

    assert max_users.dialog_partner_id(dialog, "own", extract_user_name=lambda _value: None) == "peer"
    assert max_downloader.fix_filename_encoding("plain.txt") == "plain.txt"
    assert max_downloader.build_filename("voice", None, "https://cdn.example.test/path/audio", "audio/ogg") == "voice.oga"
    assert video_url == "https://cdn.example.test/v.mp4"
