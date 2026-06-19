from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken

from .ports import MaxContactImportEntry, MaxUserView

CONTACTS_KEY_ENV = "MAX_RECOVERY_CONTACTS_KEY"
CONTACTS_SNAPSHOT_FILENAME = "recovery_contacts.enc.json"
CONTACTS_SNAPSHOT_SCHEMA = 1


class RecoveryContactsError(RuntimeError):
    """Base error for encrypted recovery contacts operations."""


class RecoveryContactsKeyMissing(RecoveryContactsError):
    """Encryption key is not configured."""


class RecoveryContactsSnapshotMissing(RecoveryContactsError):
    """Encrypted contacts snapshot file is missing."""


class RecoveryContactsSnapshotExists(RecoveryContactsError):
    """Encrypted contacts snapshot already exists and --force was not used."""


class RecoveryContactsSnapshotInvalid(RecoveryContactsError):
    """Encrypted contacts snapshot cannot be parsed or decrypted."""


def snapshot_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / CONTACTS_SNAPSHOT_FILENAME


def account_hash(value: object | None) -> str | None:
    if value is None:
        return None
    return sha256(str(value).encode("utf-8")).hexdigest()


def recovery_contacts_key() -> bytes:
    value = os.environ.get(CONTACTS_KEY_ENV, "").strip()
    if not value:
        raise RecoveryContactsKeyMissing(f"{CONTACTS_KEY_ENV} is not configured")
    return value.encode("ascii")


def _fernet(key: bytes | None = None) -> Fernet:
    try:
        return Fernet(key or recovery_contacts_key())
    except (ValueError, TypeError) as exc:
        raise RecoveryContactsSnapshotInvalid("invalid recovery contacts key") from exc


def _normalize_phone(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == "+")
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        digits = "+" + "".join(ch for ch in cleaned[1:] if ch.isdigit())
    else:
        digits = "+" + "".join(ch for ch in cleaned if ch.isdigit())
    return digits if len(digits) > 1 else None


def _name_parts(user: MaxUserView) -> tuple[str, str | None]:
    first = str(user.first_name or user.name or "").strip()
    last = str(user.last_name or "").strip()
    if first:
        return first, last or None
    display = str(user.display_name or "").strip()
    if display:
        parts = display.split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else None
    fallback = str(user.id or "MAX contact").strip() or "MAX contact"
    return fallback, None


def contact_from_user(user: MaxUserView) -> MaxContactImportEntry | None:
    phone = _normalize_phone(getattr(user, "phone", None))
    if phone is None:
        return None
    first_name, last_name = _name_parts(user)
    return MaxContactImportEntry(
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )


def contacts_from_users(
    users: Iterable[MaxUserView],
) -> tuple[list[MaxContactImportEntry], int, int]:
    contacts: list[MaxContactImportEntry] = []
    seen_phones: set[str] = set()
    total = 0
    skipped_without_phone = 0
    for user in users:
        total += 1
        contact = contact_from_user(user)
        if contact is None:
            skipped_without_phone += 1
            continue
        if contact.phone in seen_phones:
            continue
        contacts.append(contact)
        seen_phones.add(contact.phone)
    return contacts, total, skipped_without_phone


def write_encrypted_snapshot(
    *,
    path: Path,
    contacts: list[MaxContactImportEntry],
    source_account_id: object | None,
    total_seen: int,
    skipped_without_phone: int,
    force: bool = False,
    key: bytes | None = None,
) -> dict[str, object]:
    if path.exists() and not force:
        raise RecoveryContactsSnapshotExists("recovery contacts snapshot already exists")
    created_at = int(time.time())
    source_hash = account_hash(source_account_id)
    payload = {
        "schema": CONTACTS_SNAPSHOT_SCHEMA,
        "created_at": created_at,
        "source_account_id": str(source_account_id) if source_account_id is not None else None,
        "source_account_hash": source_hash,
        "contacts": [asdict(contact) for contact in contacts],
        "counts": {
            "total_seen": total_seen,
            "contacts_with_phone": len(contacts),
            "skipped_without_phone": skipped_without_phone,
        },
    }
    token = _fernet(key).encrypt(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    wrapper = {
        "schema": CONTACTS_SNAPSHOT_SCHEMA,
        "cipher": "fernet",
        "created_at": created_at,
        "source_account_hash": source_hash,
        "contact_count": len(contacts),
        "total_seen": total_seen,
        "skipped_without_phone": skipped_without_phone,
        "token": token.decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp_path.replace(path)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return wrapper


def _read_wrapper(path: Path) -> dict[str, object]:
    if not path.exists():
        raise RecoveryContactsSnapshotMissing("recovery contacts snapshot is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryContactsSnapshotInvalid("invalid recovery contacts snapshot") from exc
    if not isinstance(data, dict) or data.get("schema") != CONTACTS_SNAPSHOT_SCHEMA:
        raise RecoveryContactsSnapshotInvalid("unsupported recovery contacts snapshot")
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise RecoveryContactsSnapshotInvalid("invalid recovery contacts token")
    return data


def read_encrypted_snapshot(path: Path, *, key: bytes | None = None) -> dict[str, object]:
    wrapper = _read_wrapper(path)
    try:
        plaintext = _fernet(key).decrypt(str(wrapper["token"]).encode("ascii"))
        payload = json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryContactsSnapshotInvalid("cannot decrypt recovery contacts snapshot") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CONTACTS_SNAPSHOT_SCHEMA:
        raise RecoveryContactsSnapshotInvalid("unsupported recovery contacts payload")
    contacts = payload.get("contacts")
    if not isinstance(contacts, list):
        raise RecoveryContactsSnapshotInvalid("invalid recovery contacts payload")
    return payload


def contacts_from_payload(payload: dict[str, object]) -> list[MaxContactImportEntry]:
    contacts: list[MaxContactImportEntry] = []
    raw_contacts = payload.get("contacts")
    if not isinstance(raw_contacts, list):
        raise RecoveryContactsSnapshotInvalid("invalid recovery contacts payload")
    for item in raw_contacts:
        if not isinstance(item, dict):
            raise RecoveryContactsSnapshotInvalid("invalid recovery contacts item")
        phone = _normalize_phone(item.get("phone"))
        first_name = str(item.get("first_name") or "").strip()
        last_name_raw = item.get("last_name")
        last_name = str(last_name_raw).strip() if last_name_raw is not None else None
        if not phone or not first_name:
            raise RecoveryContactsSnapshotInvalid("invalid recovery contacts item")
        contacts.append(
            MaxContactImportEntry(
                phone=phone,
                first_name=first_name,
                last_name=last_name or None,
            )
        )
    return contacts


def snapshot_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "key_configured": bool(os.environ.get(CONTACTS_KEY_ENV, "").strip()),
        }
    try:
        wrapper = _read_wrapper(path)
    except RecoveryContactsError as exc:
        return {
            "exists": True,
            "path": str(path),
            "valid": False,
            "error": exc.__class__.__name__,
            "key_configured": bool(os.environ.get(CONTACTS_KEY_ENV, "").strip()),
        }
    decryptable = None
    key_configured = bool(os.environ.get(CONTACTS_KEY_ENV, "").strip())
    if key_configured:
        try:
            read_encrypted_snapshot(path)
            decryptable = True
        except RecoveryContactsError:
            decryptable = False
    return {
        "exists": True,
        "path": str(path),
        "valid": True,
        "decryptable": decryptable,
        "key_configured": key_configured,
        "created_at": wrapper.get("created_at"),
        "source_account_hash": wrapper.get("source_account_hash"),
        "contact_count": wrapper.get("contact_count"),
        "total_seen": wrapper.get("total_seen"),
        "skipped_without_phone": wrapper.get("skipped_without_phone"),
    }
