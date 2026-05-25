"""Small backend-swap demo without MAX credentials.

Run from repo root:
    python examples/swap_max_backend.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.max.adapter import MaxAdapter
from tests.fakes.fake_max_backend import FakeMaxBackend


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        backend = FakeMaxBackend()
        adapter = MaxAdapter(
            phone="+70000000000",
            data_dir=tmp,
            session_name="session.db",
            tmp_dir=tmp,
            backend=backend,
        )
        received = []

        async def handle_message(message):
            received.append(message)

        adapter.on_message(handle_message)

        task = asyncio.create_task(adapter.start())
        try:
            for _ in range(100):
                if adapter.is_ready():
                    break
                await asyncio.sleep(0.01)
            if not adapter.is_ready():
                raise RuntimeError("fake backend did not start")

            await backend.client.emit_text_message(text="sample text")
            sent_id = await adapter.send_message("-70000000000003", "reply sample")

            print(f"received={len(received)}")
            print(f"sent_id={sent_id}")
            print(f"backend_capture={backend.client.sent_messages[-1]['text']}")
        finally:
            await adapter.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    asyncio.run(main())
