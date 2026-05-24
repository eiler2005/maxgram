from __future__ import annotations

from pymax.session import SessionStore
from pymax.session.models import SessionInfo


class BridgeSessionStore(SessionStore):
    """PyMax 2 session store with one-time import from PyMax 1 auth table."""

    def __init__(
        self,
        work_dir: str,
        db_name: str,
        *,
        phone: str,
        import_legacy: bool = True,
    ) -> None:
        super().__init__(work_dir, db_name)
        self._bridge_phone = phone
        self._import_legacy = import_legacy

    async def load_session(self) -> SessionInfo | None:
        session = await super().load_session()
        if session is not None:
            return session

        if not self._import_legacy:
            return None

        session = await self._load_legacy_auth_session()
        if session is None:
            return None

        await self.save_session(session)
        return session

    async def clear_sessions(self) -> None:
        conn = await self._get_connection()
        await conn.execute("DELETE FROM sessions")
        await conn.commit()

    async def _load_legacy_auth_session(self) -> SessionInfo | None:
        conn = await self._get_connection()
        async with conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'auth'
            """
        ) as cursor:
            if await cursor.fetchone() is None:
                return None

        async with conn.execute("PRAGMA table_info(auth)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if not {"token", "device_id"}.issubset(columns):
            return None

        async with conn.execute(
            """
            SELECT token, device_id
            FROM auth
            WHERE token IS NOT NULL AND token != ''
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None

        return SessionInfo(
            token=row["token"],
            device_id=row["device_id"],
            phone=self._bridge_phone,
            mt_instance_id="",
        )
