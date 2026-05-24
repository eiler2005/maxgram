from __future__ import annotations

import asyncio
import contextlib
import ssl

from pymax.auth import ConsoleSmsCodeProvider, SmsAuthFlow

from ...network import MaxEgressProfile
from .client_factory import create_pymax_client
from .session_store import BridgeSessionStore


async def clear_saved_sessions(*, data_dir: str, session_name: str, phone: str) -> None:
    store = BridgeSessionStore(data_dir, session_name, phone=phone, import_legacy=False)
    try:
        await store.clear_sessions()
    finally:
        await store.close()


async def reauthorize_with_console(
    *,
    phone: str,
    data_dir: str,
    session_name: str,
    egress: MaxEgressProfile | None = None,
    clear_session: bool = True,
) -> None:
    """Refresh MAX session DB with PyMax SMS/2FA console auth."""
    if clear_session:
        await clear_saved_sessions(
            data_dir=data_dir,
            session_name=session_name,
            phone=phone,
        )

    started = asyncio.Event()
    auth_flow = SmsAuthFlow(ConsoleSmsCodeProvider())
    client = create_pymax_client(
        phone=phone,
        data_dir=data_dir,
        session_name=session_name,
        egress=egress,
        auth_flow=auth_flow,
        import_legacy_session=False,
    )

    @client.on_start()
    def _stop_after_success(_client):
        started.set()
        task = asyncio.create_task(_close_after_success(_client))
        task.add_done_callback(_ignore_close_error)

    try:
        await client.start()
    except (asyncio.CancelledError, ssl.SSLError):
        if not started.is_set():
            raise
    finally:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await client.close()

    if not started.is_set():
        raise RuntimeError("MAX reauth did not reach a successful login")


async def _close_after_success(client) -> None:
    await asyncio.sleep(0)
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await client.stop()


def _ignore_close_error(task: asyncio.Task[None]) -> None:
    with contextlib.suppress(Exception, asyncio.CancelledError):
        task.result()
