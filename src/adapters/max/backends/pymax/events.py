from __future__ import annotations

import inspect

from ...ports import ClientMessageHandler
from .models import client_message


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


class PymaxEventRouter:
    """Adapts PyMax 2 handler signatures to bridge callbacks."""

    def __init__(self, client) -> None:
        self._client = client

    def register_start_handler(self, handler) -> None:
        async def wrapped(_client):
            return await _maybe_await(handler())

        self._client.on_start()(wrapped)

    def register_message_handler(self, handler: ClientMessageHandler) -> None:
        self._client.on_message()(self._wrap_message_handler(handler))

    def register_message_edit_handler(self, handler: ClientMessageHandler) -> None:
        self._client.on_message_edit()(self._wrap_message_handler(handler))

    def register_message_delete_handler(self, handler: ClientMessageHandler) -> None:
        self._client.on_message_delete()(self._wrap_message_handler(handler))

    def _wrap_message_handler(self, handler: ClientMessageHandler):
        async def wrapped(message, _client):
            return await handler(client_message(message))

        return wrapped
