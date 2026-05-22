import sqlite3

from src.adapters.max_session_store import MaxSessionStore


def create_session_db(path, *, token="token-value", device_id="d" * 32):
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE auth (token VARCHAR, device_id CHAR(32) PRIMARY KEY NOT NULL)")
        con.execute("INSERT INTO auth (token, device_id) VALUES (?, ?)", (token, device_id))
        con.commit()
    finally:
        con.close()


def read_auth_shape(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT length(token), length(device_id) FROM auth").fetchall()
    finally:
        con.close()


def read_token(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT token FROM auth").fetchone()[0]
    finally:
        con.close()


def corrupt_session_header_like_prod(path):
    with open(path, "r+b") as f:
        for offset, value in (
            (28, 50_528_275),
            (32, 3_548_323_811),
            (36, 3_778_712_627),
            (40, 926_788_063),
            (44, 4_086_426_381),
            (48, 2_940_270_336),
        ):
            f.seek(offset)
            f.write(value.to_bytes(4, "big"))


def test_backup_current_writes_valid_snapshot_and_skips_unchanged(tmp_path):
    session = tmp_path / "session.db"
    create_session_db(session, token="secret-token")
    store = MaxSessionStore(tmp_path, "session.db")

    first = store.backup_current(reason="connected")
    second = store.backup_current(reason="connected")

    assert first.action == "backed_up"
    assert first.backup_path is not None
    assert store.validate(first.backup_path).ok
    assert read_auth_shape(first.backup_path) == [(12, 32)]
    assert second.action == "skipped"
    assert second.reason == "unchanged"


def test_recover_if_needed_repairs_header_corruption_without_losing_token(tmp_path):
    session = tmp_path / "session.db"
    create_session_db(session, token="current-token")
    corrupt_session_header_like_prod(session)
    store = MaxSessionStore(tmp_path, "session.db")

    assert not store.validate().ok
    outcome = store.recover_if_needed()

    assert outcome.action == "repaired"
    assert outcome.backup_path is not None
    assert outcome.backup_path.exists()
    assert store.validate().ok
    assert read_auth_shape(session) == [(13, 32)]
    assert read_token(session) == "current-token"


def test_recover_if_needed_restores_latest_valid_snapshot(tmp_path):
    session = tmp_path / "session.db"
    create_session_db(session, token="backup-token")
    store = MaxSessionStore(tmp_path, "session.db")
    backup = store.backup_current(reason="connected")
    assert backup.action == "backed_up"

    session.write_bytes(b"not a sqlite database")
    outcome = store.recover_if_needed()

    assert outcome.action == "restored"
    assert outcome.source_path == backup.backup_path
    assert store.validate().ok
    assert read_token(session) == "backup-token"
