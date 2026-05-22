from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .state import MaxRuntimeState


@dataclass
class MaxServiceRegistry:
    state: MaxRuntimeState
    overrides: dict[str, object] | None = None
    runtime: object | None = None
    raw_payload: object | None = None
    voice_recovery: object | None = None
    events: object | None = None
    send: object | None = None
    resolver: object | None = None
    media: object | None = None
    recovery: object | None = None
    lifecycle: object | None = None

    def services(self) -> Iterable[object]:
        return (
            service
            for service in (
                self.runtime,
                self.raw_payload,
                self.voice_recovery,
                self.events,
                self.send,
                self.resolver,
                self.media,
                self.recovery,
                self.lifecycle,
            )
            if service is not None
        )

    def resolve(self, name: str, *, skip: object | None = None):
        if self.overrides and name in self.overrides:
            return self.overrides[name]
        if self.state.has_attr(name):
            return self.state.get_attr(name)
        for service in self.services():
            if service is skip:
                continue
            if name in getattr(service, "__dict__", {}):
                return getattr(service, name)
            if getattr(type(service), name, None) is not None:
                return getattr(service, name)
        raise AttributeError(name)


class MaxService:
    def __init__(self, state: MaxRuntimeState, services: MaxServiceRegistry):
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_services", services)

    def __getattr__(self, name: str):
        services = object.__getattribute__(self, "_services")
        return services.resolve(name, skip=self)

    def __setattr__(self, name: str, value):
        state = object.__getattribute__(self, "_state")
        if state.set_attr(name, value):
            return
        object.__setattr__(self, name, value)
