import json

import pytest
from cryptography.fernet import Fernet

from src.adapters.max.ports import MaxContactImportEntry, MaxUserView
from src.adapters.max.recovery_contacts import (
    CONTACTS_KEY_ENV,
    RecoveryContactsKeyMissing,
    RecoveryContactsSnapshotInvalid,
    contacts_from_payload,
    contacts_from_users,
    read_encrypted_snapshot,
    snapshot_status,
    write_encrypted_snapshot,
)


def test_recovery_contacts_snapshot_encrypts_payload_and_uses_0600(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv(CONTACTS_KEY_ENV, key.decode("ascii"))
    path = tmp_path / "recovery_contacts.enc.json"
    phone = "+79990000000"

    wrapper = write_encrypted_snapshot(
        path=path,
        contacts=[
            MaxContactImportEntry(
                phone=phone,
                first_name="Ada",
                last_name="Lovelace",
            )
        ],
        source_account_id="100",
        total_seen=2,
        skipped_without_phone=1,
    )
    raw = path.read_text(encoding="utf-8")
    payload = read_encrypted_snapshot(path)
    contacts = contacts_from_payload(payload)
    status = snapshot_status(path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert phone not in raw
    assert "Ada" not in raw
    assert wrapper["contact_count"] == 1
    assert payload["source_account_id"] == "100"
    assert contacts == [
        MaxContactImportEntry(
            phone=phone,
            first_name="Ada",
            last_name="Lovelace",
        )
    ]
    assert status["decryptable"] is True
    assert status["contact_count"] == 1


def test_recovery_contacts_snapshot_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv(CONTACTS_KEY_ENV, raising=False)

    with pytest.raises(RecoveryContactsKeyMissing):
        write_encrypted_snapshot(
            path=tmp_path / "recovery_contacts.enc.json",
            contacts=[],
            source_account_id=None,
            total_seen=0,
            skipped_without_phone=0,
        )


def test_recovery_contacts_snapshot_rejects_corrupt_ciphertext(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv(CONTACTS_KEY_ENV, key.decode("ascii"))
    path = tmp_path / "recovery_contacts.enc.json"
    write_encrypted_snapshot(
        path=path,
        contacts=[
            MaxContactImportEntry(
                phone="+79990000000",
                first_name="Ada",
            )
        ],
        source_account_id="100",
        total_seen=1,
        skipped_without_phone=0,
    )
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["token"] = wrapper["token"][:-6] + "broken"
    path.write_text(json.dumps(wrapper), encoding="utf-8")

    with pytest.raises(RecoveryContactsSnapshotInvalid):
        read_encrypted_snapshot(path)

    assert snapshot_status(path)["decryptable"] is False


def test_recovery_contacts_from_users_filters_missing_phone_and_deduplicates():
    users = [
        MaxUserView(id=1, first_name="Ada", phone="+7 999 000-00-00"),
        MaxUserView(id=2, first_name="No Phone"),
        MaxUserView(id=3, first_name="Duplicate", phone="+79990000000"),
    ]

    contacts, total_seen, skipped_without_phone = contacts_from_users(users)

    assert total_seen == 3
    assert skipped_without_phone == 1
    assert contacts == [
        MaxContactImportEntry(
            phone="+79990000000",
            first_name="Ada",
        )
    ]
