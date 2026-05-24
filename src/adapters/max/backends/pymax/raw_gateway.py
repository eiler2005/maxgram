from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

from pymax.protocol import Command, Opcode

from ...ports import MaxRawInterceptorResult, RawReceiveHandler
from .models import MaxRawFrame, normalize_frame


class PymaxRawGateway:
    """Raw frame ingress/requests over PyMax 2, isolated from the port facade."""

    def __init__(self, client) -> None:
        self._client = client

    def _handler_count(self) -> int:
        return int(getattr(self._client, "_maxtg_raw_handler_count", 0) or 0)

    def _opcode(self, name: str, default: int | None = None):
        value = getattr(Opcode, name, None)
        if value is not None:
            return value
        if default is None:
            return None
        return SimpleNamespace(value=default, name=name)

    def install_raw_handler(self, handler: RawReceiveHandler) -> MaxRawInterceptorResult:
        if getattr(self._client, "_maxtg_raw_interceptor_installed", False):
            return MaxRawInterceptorResult(
                installed=True,
                raw_handler_count=self._handler_count(),
            )

        register = getattr(self._client, "on_raw", None)
        if register is None:
            return MaxRawInterceptorResult(
                installed=False,
                reason="client_has_no_raw_handler",
            )

        async def wrapped(frame, _client=None):
            result = handler(normalize_frame(frame))
            if inspect.isawaitable(result):
                await result

        register()(wrapped)
        self._client._maxtg_raw_interceptor_installed = True
        self._client._maxtg_raw_handler_count = 1
        return MaxRawInterceptorResult(installed=True, raw_handler_count=1)

    async def request(
        self,
        *,
        opcode_name: str,
        payload: dict[str, Any],
        default_opcode: int | None = None,
        timeout: int | float | None = None,
        cmd: int | None = None,
    ) -> dict[str, Any] | None:
        opcode = self._opcode(opcode_name, default_opcode)
        if opcode is None:
            return None

        app = getattr(self._client, "_app", None)
        invoke = getattr(app, "invoke", None)
        if invoke is None:
            return None

        opcode_value = int(getattr(opcode, "value", opcode))
        response = await invoke(
            opcode=opcode_value,
            payload=payload,
            cmd=Command.REQUEST if cmd is None else cmd,
            timeout=timeout,
        )
        return MaxRawFrame.from_object(response).to_dict()
