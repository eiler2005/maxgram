"""media_recovery_cache repository."""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .base import BaseRepo
from ..types import MediaRecoveryCacheEntry

MEDIA_RECOVERY_CACHE_KEY_ENV = "MAX_RECOVERY_CONTACTS_KEY"
MEDIA_RECOVERY_CACHE_SCHEMA = 1
MEDIA_RECOVERY_CACHE_CIPHER = "fernet"


def _fernet_from_env() -> Fernet | None:
    value = os.environ.get(MEDIA_RECOVERY_CACHE_KEY_ENV, "").strip()
    if not value:
        return None
    try:
        return Fernet(value.encode("ascii"))
    except (TypeError, ValueError):
        return None


def _encrypt_payload(payload: dict[str, object] | None) -> str | None:
    if not payload:
        return None
    fernet = _fernet_from_env()
    if fernet is None:
        return None
    wrapper = {
        "schema": MEDIA_RECOVERY_CACHE_SCHEMA,
        "payload": payload,
    }
    plaintext = json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return fernet.encrypt(plaintext).decode("ascii")


def _decrypt_payload(ciphertext: str | None) -> dict[str, object] | None:
    if not ciphertext:
        return None
    fernet = _fernet_from_env()
    if fernet is None:
        return None
    try:
        plaintext = fernet.decrypt(ciphertext.encode("ascii"))
        wrapper = json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(wrapper, dict) or wrapper.get("schema") != MEDIA_RECOVERY_CACHE_SCHEMA:
        return None
    payload = wrapper.get("payload")
    return payload if isinstance(payload, dict) else None


class MediaRecoveryCacheRepo(BaseRepo):
    def _entry_from_row(self, row) -> MediaRecoveryCacheEntry:
        return MediaRecoveryCacheEntry(**dict(row))

    async def save_media_recovery_cache(
        self,
        *,
        max_chat_id: str,
        max_msg_id: str,
        attachment_index: int,
        kind: str,
        source_type: Optional[str] = None,
        media_chat_id: Optional[str] = None,
        media_msg_id: Optional[str] = None,
        reference_kind: Optional[str] = None,
        reference_id: Optional[str] = None,
        filename: Optional[str] = None,
        duration: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        payload: Optional[dict[str, object]] = None,
        ttl_seconds: int = 48 * 60 * 60,
        now: Optional[int] = None,
    ) -> bool:
        now = int(time.time()) if now is None else now
        ttl_seconds = max(60, int(ttl_seconds))
        expires_at = now + ttl_seconds
        ciphertext = _encrypt_payload(payload)
        cipher = MEDIA_RECOVERY_CACHE_CIPHER if ciphertext else None
        await self._db.execute(
            """INSERT INTO media_recovery_cache
               (max_chat_id, max_msg_id, attachment_index, kind, source_type,
                media_chat_id, media_msg_id, reference_kind, reference_id,
                filename, duration, width, height, payload_cipher,
                payload_ciphertext, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(max_chat_id, max_msg_id, attachment_index, kind)
               DO UPDATE SET
                 source_type = excluded.source_type,
                 media_chat_id = excluded.media_chat_id,
                 media_msg_id = excluded.media_msg_id,
                 reference_kind = excluded.reference_kind,
                 reference_id = excluded.reference_id,
                 filename = excluded.filename,
                 duration = excluded.duration,
                 width = excluded.width,
                 height = excluded.height,
                 payload_cipher = COALESCE(excluded.payload_cipher, media_recovery_cache.payload_cipher),
                 payload_ciphertext = COALESCE(excluded.payload_ciphertext, media_recovery_cache.payload_ciphertext),
                 updated_at = excluded.updated_at,
                 expires_at = excluded.expires_at""",
            (
                max_chat_id,
                max_msg_id,
                attachment_index,
                kind,
                source_type,
                media_chat_id,
                media_msg_id,
                reference_kind,
                reference_id,
                filename,
                duration,
                width,
                height,
                cipher,
                ciphertext,
                now,
                now,
                expires_at,
            ),
        )
        await self._commit()
        return bool(ciphertext)

    async def get_media_recovery_cache_payload(
        self,
        *,
        max_chat_id: str,
        max_msg_id: str,
        attachment_index: int,
        kind: str,
        now: Optional[int] = None,
    ) -> dict[str, object] | None:
        now = int(time.time()) if now is None else now
        async with self._db.execute(
            """SELECT payload_cipher, payload_ciphertext FROM media_recovery_cache
               WHERE max_chat_id = ? AND max_msg_id = ?
                 AND attachment_index = ? AND kind = ?
                 AND expires_at > ?
               LIMIT 1""",
            (max_chat_id, max_msg_id, attachment_index, kind, now),
        ) as cur:
            row = await cur.fetchone()
        if not row or row["payload_cipher"] != MEDIA_RECOVERY_CACHE_CIPHER:
            return None
        return _decrypt_payload(row["payload_ciphertext"])

    async def get_media_recovery_cache_entry(
        self,
        *,
        max_chat_id: str,
        max_msg_id: str,
        attachment_index: int,
        kind: str,
    ) -> MediaRecoveryCacheEntry | None:
        async with self._db.execute(
            """SELECT * FROM media_recovery_cache
               WHERE max_chat_id = ? AND max_msg_id = ?
                 AND attachment_index = ? AND kind = ?
               LIMIT 1""",
            (max_chat_id, max_msg_id, attachment_index, kind),
        ) as cur:
            row = await cur.fetchone()
        return self._entry_from_row(row) if row else None

    async def purge_expired_media_recovery_cache(
        self,
        *,
        now: Optional[int] = None,
    ) -> int:
        now = int(time.time()) if now is None else now
        cursor = await self._db.execute(
            "DELETE FROM media_recovery_cache WHERE expires_at <= ?",
            (now,),
        )
        await self._commit()
        return int(cursor.rowcount or 0)
