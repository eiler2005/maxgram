"""Operation admission and draining for a single MAX connection generation."""
from __future__ import annotations

import asyncio


class EgressOperationInterrupted(RuntimeError):
    """An admitted operation was interrupted; its remote outcome is unknown."""


class OperationGate:
    def __init__(self):
        self.open = asyncio.Event()
        self.open.set()
        self.tasks: set[asyncio.Task] = set()
        self.generation = 0

    async def run(self, function, *args, generation=None, **kwargs):
        await self.open.wait()
        if generation is not None and generation != self.generation:
            raise RuntimeError("MAX connection generation expired")
        task = asyncio.create_task(function(*args, **kwargs))
        self.tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise EgressOperationInterrupted("MAX operation interrupted; outcome unknown") from None
        finally:
            self.tasks.discard(task)

    async def drain(self, timeout=30):
        self.open.clear()
        tasks = set(self.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=5)
                if pending:
                    raise TimeoutError("MAX operation drain timed out")


class GuardedClientPort:
    """Delegate the backend port, guarding asynchronous operations only."""
    def __init__(self, client, gate):
        self._delegate = client
        self._gate = gate
        self._generation = gate.generation

    @property
    def logger(self):
        return self._delegate.logger

    @property
    def is_connected(self):
        return self._delegate.is_connected

    def prepare_startup(self, *args, **kwargs):
        return self._delegate.prepare_startup(*args, **kwargs)

    def install_interactive_ping(self, *args, **kwargs):
        return self._delegate.install_interactive_ping(*args, **kwargs)

    def install_raw_message_interceptor(self, *args, **kwargs):
        return self._delegate.install_raw_message_interceptor(*args, **kwargs)

    def register_start_handler(self, *args, **kwargs):
        return self._delegate.register_start_handler(*args, **kwargs)

    def register_raw_receive_handler(self, *args, **kwargs):
        return self._delegate.register_raw_receive_handler(*args, **kwargs)

    def register_message_handler(self, *args, **kwargs):
        return self._delegate.register_message_handler(*args, **kwargs)

    def register_message_edit_handler(self, *args, **kwargs):
        return self._delegate.register_message_edit_handler(*args, **kwargs)

    def register_message_delete_handler(self, *args, **kwargs):
        return self._delegate.register_message_delete_handler(*args, **kwargs)

    def register_typing_handler(self, *args, **kwargs):
        return self._delegate.register_typing_handler(*args, **kwargs)

    def register_message_read_handler(self, *args, **kwargs):
        return self._delegate.register_message_read_handler(*args, **kwargs)

    def register_presence_handler(self, *args, **kwargs):
        return self._delegate.register_presence_handler(*args, **kwargs)

    def register_reaction_update_handler(self, *args, **kwargs):
        return self._delegate.register_reaction_update_handler(*args, **kwargs)

    def register_disconnect_handler(self, *args, **kwargs):
        return self._delegate.register_disconnect_handler(*args, **kwargs)

    async def get_message(self, *args, **kwargs):
        return await self._gate.run(self._delegate.get_message, *args, generation=self._generation, **kwargs)

    async def get_messages(self, *args, **kwargs):
        return await self._gate.run(self._delegate.get_messages, *args, generation=self._generation, **kwargs)

    async def start(self, *args, **kwargs):
        return await self._delegate.start(*args, **kwargs)

    async def close(self, *args, **kwargs):
        return await self._delegate.close(*args, **kwargs)

    def own_user_id(self, *args, **kwargs):
        return self._delegate.own_user_id(*args, **kwargs)

    def cached_user(self, *args, **kwargs):
        return self._delegate.cached_user(*args, **kwargs)

    async def load_users(self, *args, **kwargs):
        return await self._gate.run(self._delegate.load_users, *args, generation=self._generation, **kwargs)

    def contacts_snapshot(self, *args, **kwargs):
        return self._delegate.contacts_snapshot(*args, **kwargs)

    def users_cache_snapshot(self, *args, **kwargs):
        return self._delegate.users_cache_snapshot(*args, **kwargs)

    async def import_contacts(self, *args, **kwargs):
        return await self._gate.run(self._delegate.import_contacts, *args, generation=self._generation, **kwargs)

    def dm_chat_id_for_user(self, *args, **kwargs):
        return self._delegate.dm_chat_id_for_user(*args, **kwargs)

    def dialogs_snapshot(self, *args, **kwargs):
        return self._delegate.dialogs_snapshot(*args, **kwargs)

    def group_chats_snapshot(self, *args, **kwargs):
        return self._delegate.group_chats_snapshot(*args, **kwargs)

    def channels_snapshot(self, *args, **kwargs):
        return self._delegate.channels_snapshot(*args, **kwargs)

    async def join_chat_by_link(self, *args, **kwargs):
        return await self._gate.run(self._delegate.join_chat_by_link, *args, generation=self._generation, **kwargs)

    async def chat(self, *args, **kwargs):
        return await self._gate.run(self._delegate.chat, *args, generation=self._generation, **kwargs)

    def dialog_last_message(self, *args, **kwargs):
        return self._delegate.dialog_last_message(*args, **kwargs)

    async def send_outbound_message(self, *args, **kwargs):
        return await self._gate.run(self._delegate.send_outbound_message, *args, generation=self._generation, **kwargs)

    async def raw_request(self, *args, **kwargs):
        return await self._gate.run(self._delegate.raw_request, *args, generation=self._generation, **kwargs)

    async def file_url(self, *args, **kwargs):
        return await self._gate.run(self._delegate.file_url, *args, generation=self._generation, **kwargs)

    async def video_url(self, *args, **kwargs):
        return await self._gate.run(self._delegate.video_url, *args, generation=self._generation, **kwargs)

    async def video_payload(self, *args, **kwargs):
        return await self._gate.run(self._delegate.video_payload, *args, generation=self._generation, **kwargs)

    async def raw_history_payload(self, *args, **kwargs):
        return await self._gate.run(self._delegate.raw_history_payload, *args, generation=self._generation, **kwargs)

    async def history_messages(self, *args, **kwargs):
        return await self._gate.run(self._delegate.history_messages, *args, generation=self._generation, **kwargs)


class EgressSelection:
    """Shared profile reference: new HTTP attempts read the current profile."""
    def __init__(self, profile):
        self.profile = profile

    @property
    def http_client_options(self):
        return self.profile.http_client_options
