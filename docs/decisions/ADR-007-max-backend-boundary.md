# ADR-007: MAX Backend Boundary

Date: 2026-05-22

## Context

The first MAX adapter split removed some file-level weight but still used mixin
inheritance over one shared `self`. That made `pymax` replacement harder than
the architecture promised: code was visually split, but behavior still lived in
one implicit object.

## Decision

`MaxAdapter` is now a public facade composed from operation services and an
internal backend boundary:

- `src/adapters/max/backends/base.py` defines the internal `MaxBackend` shape.
- `src/adapters/max/backends/pymax/` contains the current `PymaxBackend` and is
  the only place allowed to import `pymax`.
- `MaxAdapter` lazily creates `PymaxBackend` by default and also has an
  internal backend injection point for tests and future replacement work.
- MAX operation services (`send`, `events`, `media`, `recovery`, `resolve`,
  `voice_recovery`, `lifecycle`) use backend operations and shared runtime
  state, not direct library imports.
- Service dependencies are wired explicitly through internal `*Deps` objects;
  there is no service registry or service-level dynamic `__getattr__`.
- Compatibility imports and public `MaxAdapter` methods stay unchanged.

## Consequences

- Replacing `pymax` later means implementing another backend package and wiring
  it into `MaxAdapter`, while `BridgeCore` and `src/bridge/contracts.py` remain
  unchanged.
- Tests enforce no mixin-based `MaxAdapter` inheritance, no service registry /
  service `__getattr__`, no service dependency on full `MaxAdapter`, and no
  `pymax` imports outside `src/adapters/max/backends/pymax/`.
- Adapter tests use harness/fake service dependencies instead of subclassing
  the real `MaxAdapter` to override private methods.
