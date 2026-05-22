"""
MAX session.db validation, backups, and local recovery.

The auth token stays inside SQLite files only. This module never logs or returns
token values; diagnostics use row/field shape and file names.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_HEADER_SIZE = 100
SESSION_BACKUP_DIRNAME = "session_backups"
SESSION_BACKUP_RETENTION = 8


@dataclass(frozen=True)
class SessionDbValidation:
    ok: bool
    reason: str
    auth_rows: int = 0
    auth_shape: tuple[tuple[int | None, int | None], ...] = ()


@dataclass(frozen=True)
class SessionRecoveryOutcome:
    action: str
    reason: str
    backup_path: Path | None = None
    source_path: Path | None = None


class MaxSessionStore:
    def __init__(
        self,
        data_dir: str | Path,
        session_name: str,
        *,
        backup_dirname: str = SESSION_BACKUP_DIRNAME,
        retention: int = SESSION_BACKUP_RETENTION,
    ):
        self.data_dir = Path(data_dir)
        self.session_name = Path(session_name).name
        self.session_path = self.data_dir / self.session_name
        self.backup_dir = self.data_dir / backup_dirname
        self.retention = max(1, int(retention))

    def validate(self, path: Path | None = None) -> SessionDbValidation:
        target = path or self.session_path
        if not target.exists():
            return SessionDbValidation(ok=False, reason="missing")
        try:
            if target.stat().st_size < SQLITE_HEADER_SIZE:
                return SessionDbValidation(ok=False, reason="short_file")
            with target.open("rb") as f:
                header = f.read(SQLITE_HEADER_SIZE)
            if header[: len(SQLITE_MAGIC)] != SQLITE_MAGIC:
                return SessionDbValidation(ok=False, reason="bad_sqlite_magic")
        except OSError as e:
            return SessionDbValidation(ok=False, reason=f"read_error:{e.__class__.__name__}")

        con = None
        try:
            con = sqlite3.connect(self._readonly_uri(target), uri=True)
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return SessionDbValidation(ok=False, reason="integrity_check_failed")

            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "auth" not in tables:
                return SessionDbValidation(ok=False, reason="missing_auth_table")

            columns = {row[1] for row in con.execute("PRAGMA table_info(auth)")}
            if not {"token", "device_id"}.issubset(columns):
                return SessionDbValidation(ok=False, reason="bad_auth_schema")

            rows = tuple(
                (row[0], row[1])
                for row in con.execute("SELECT length(token), length(device_id) FROM auth")
            )
        except sqlite3.Error as e:
            return SessionDbValidation(ok=False, reason=f"sqlite_error:{e.__class__.__name__}")
        finally:
            if con is not None:
                con.close()

        if len(rows) != 1 or rows[0][0] is None or rows[0][0] <= 0 or rows[0][1] != 32:
            return SessionDbValidation(
                ok=False,
                reason="bad_auth_shape",
                auth_rows=len(rows),
                auth_shape=rows,
            )
        return SessionDbValidation(
            ok=True,
            reason="ok",
            auth_rows=len(rows),
            auth_shape=rows,
        )

    def recover_if_needed(self) -> SessionRecoveryOutcome:
        validation = self.validate()
        if validation.ok:
            return SessionRecoveryOutcome(action="ok", reason="valid")

        if validation.reason != "missing":
            repaired = self._build_repaired_clean_copy()
            if repaired is not None:
                corrupt_backup = self._archive_current("corrupt")
                self._replace_session_file(repaired)
                self._prune_backups()
                return SessionRecoveryOutcome(
                    action="repaired",
                    reason=validation.reason,
                    backup_path=corrupt_backup,
                )

        restored = self._restore_latest_valid_backup()
        if restored is not None:
            return SessionRecoveryOutcome(
                action="restored",
                reason=validation.reason,
                source_path=restored,
            )

        if validation.reason == "missing":
            return SessionRecoveryOutcome(action="missing", reason="no_session_file")

        return SessionRecoveryOutcome(action="failed", reason=validation.reason)

    def backup_current(self, *, reason: str = "connected") -> SessionRecoveryOutcome:
        validation = self.validate()
        if not validation.ok:
            return SessionRecoveryOutcome(action="skipped", reason=validation.reason)

        latest = self._latest_valid_backup()
        if latest is not None and self._auth_digest(latest) == self._auth_digest(self.session_path):
            return SessionRecoveryOutcome(action="skipped", reason="unchanged", source_path=latest)

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.chmod(0o700)
        timestamp = self._timestamp()
        safe_reason = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in reason)
        target = self.backup_dir / f"{self.session_name}.snapshot-{timestamp}-{safe_reason}.db"
        tmp = self.backup_dir / f".{target.name}.tmp"
        try:
            self._write_clean_copy(self.session_path, tmp)
            os.chmod(tmp, 0o600)
            tmp_validation = self.validate(tmp)
            if not tmp_validation.ok:
                return SessionRecoveryOutcome(action="skipped", reason=tmp_validation.reason)
            os.replace(tmp, target)
        finally:
            self._unlink_if_exists(tmp)
        self._prune_backups()
        return SessionRecoveryOutcome(action="backed_up", reason="ok", backup_path=target)

    def _readonly_uri(self, path: Path) -> str:
        resolved = path.resolve()
        return f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"

    def _snapshot_paths(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(
            self.backup_dir.glob(f"{self.session_name}.snapshot-*.db"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )

    def _latest_valid_backup(self) -> Path | None:
        for path in reversed(self._snapshot_paths()):
            if self.validate(path).ok:
                return path
        return None

    def _restore_latest_valid_backup(self) -> Path | None:
        source = self._latest_valid_backup()
        if source is None:
            return None
        self._archive_current("unreadable")
        clean = self.data_dir / f".{self.session_name}.restore-{self._timestamp()}.clean"
        try:
            self._write_clean_copy(source, clean)
            if not self.validate(clean).ok:
                return None
            self._replace_session_file(clean)
            return source
        finally:
            self._unlink_if_exists(clean)

    def _archive_current(self, label: str) -> Path | None:
        if not self.session_path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.chmod(0o700)
        archive = self.backup_dir / f"{self.session_name}.{label}-{self._timestamp()}.db"
        shutil.copy2(self.session_path, archive)
        os.chmod(archive, 0o600)
        return archive

    def _build_repaired_clean_copy(self) -> Path | None:
        try:
            with self.session_path.open("rb") as f:
                header = f.read(SQLITE_HEADER_SIZE)
            if header[: len(SQLITE_MAGIC)] != SQLITE_MAGIC:
                return None
            page_size = int.from_bytes(header[16:18], "big")
            if page_size == 1:
                page_size = 65536
            file_size = self.session_path.stat().st_size
            if page_size <= 0 or file_size % page_size != 0:
                return None
            db_pages = file_size // page_size
        except OSError:
            return None

        timestamp = self._timestamp()
        patched = self.data_dir / f".{self.session_name}.repair-{timestamp}.patched"
        clean = self.data_dir / f".{self.session_name}.repair-{timestamp}.clean"
        try:
            shutil.copy2(self.session_path, patched)
            with patched.open("r+b") as f:
                for offset, value in (
                    (28, db_pages),
                    (32, 0),
                    (36, 0),
                    (40, 1),
                    (44, 4),
                    (48, 0),
                ):
                    f.seek(offset)
                    f.write(int(value).to_bytes(4, "big"))

            if not self.validate(patched).ok:
                return None
            self._write_clean_copy(patched, clean)
            if not self.validate(clean).ok:
                return None
            return clean
        except (OSError, sqlite3.Error):
            return None
        finally:
            self._unlink_if_exists(patched)

    def _write_clean_copy(self, source: Path, target: Path) -> None:
        self._unlink_if_exists(target)
        src = sqlite3.connect(self._readonly_uri(source), uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def _replace_session_file(self, clean: Path) -> None:
        current_mode = 0o600
        if self.session_path.exists():
            current_mode = stat.S_IMODE(self.session_path.stat().st_mode)
        os.chmod(clean, current_mode)
        os.replace(clean, self.session_path)

    def _prune_backups(self) -> None:
        snapshots = self._snapshot_paths()
        for path in snapshots[: max(0, len(snapshots) - self.retention)]:
            self._unlink_if_exists(path)

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _auth_digest(self, path: Path) -> str | None:
        con = None
        try:
            con = sqlite3.connect(self._readonly_uri(path), uri=True)
            row = con.execute("SELECT token, device_id FROM auth").fetchone()
        except sqlite3.Error:
            return None
        finally:
            if con is not None:
                con.close()
        if row is None:
            return None
        digest = hashlib.sha256()
        digest.update(str(row[0] or "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(row[1] or "").encode("utf-8"))
        return digest.hexdigest()

    def _timestamp(self) -> str:
        return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{time.time_ns() % 1_000_000_000:09d}"

    def _unlink_if_exists(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
