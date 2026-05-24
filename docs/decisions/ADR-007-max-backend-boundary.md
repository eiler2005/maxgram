# ADR-007: MAX Backend Boundary

Date: 2026-05-22

Updated: 2026-05-24 for PyMax 2 backend redesign.

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
- `src/adapters/max/ports.py` defines the typed internal client port and DTOs
  that operation services consume.
- `PymaxClientAdapter` is a thin `MaxClientPort` facade over the PyMax backend
  package. PyMax 2 wiring is split into focused internal modules:
  `client_factory.py`, `login.py`, `session_store.py`, `transport.py`,
  `events.py`, `raw_gateway.py`, `models.py` and `media.py`.
- `login.py` owns tolerant PyMax 2 login validation: unsupported attachment
  variants in initial sync are stripped before upstream `LoginResponse`
  validation, without changing bridge contracts.
- PyMax 2 session compatibility is isolated in `session_store.py`: it imports
  legacy PyMax 1 `auth(token, device_id)` into the PyMax 2 `sessions` schema
  once, without exposing token data outside the backend boundary.
- Existing PyMax 1 `SocketMaxClient` sessions are DESKTOP sessions, so
  `client_factory.py` pins a DESKTOP user-agent and sync overrides for the
  migration path instead of leaking that compatibility concern outward.
- Private PyMax 2 calls such as `client._app.invoke(...)` are isolated inside
  the backend raw gateway. Native `on_raw()` replaces the old private
  message-notification patch.
- `MaxAdapter` lazily creates `PymaxBackend` by default and also has an
  internal backend injection point for tests and future replacement work.
- MAX operation services (`send`, `events`, `media`, `recovery`, `resolve`,
  `voice_recovery`, `lifecycle`) use typed ports and shared runtime state, not
  direct library imports or pymax client shape.
- Service dependencies are wired explicitly through internal `*Deps` objects;
  there is no service registry or service-level dynamic `__getattr__`.
- Compatibility imports and public `MaxAdapter` methods stay unchanged.

## Consequences

- Replacing `pymax` later means implementing another backend package and wiring
  it into `MaxAdapter`, while `BridgeCore`, `src/bridge/contracts.py` and MAX
  operation services remain unchanged.
- PyMax 2 routers, raw frames, transport and Pydantic models stay inside
  `src/adapters/max/backends/pymax/`; operation services continue consuming
  `MaxClientPort` and DTOs.
- Tests enforce no mixin-based `MaxAdapter` inheritance, no service registry /
  service `__getattr__`, no service dependency on full `MaxAdapter`, and no
  `pymax` imports outside `src/adapters/max/backends/pymax/`.
- Additional service-boundary tests enforce that operation services do not call
  pymax-private/client-shape attrs such as `_send_and_wait`,
  `_handle_message_notifications`, `fetch_history`, `get_file_by_id`,
  `contacts/dialogs/chats/_users` directly.
- Adapter tests use harness/fake service dependencies instead of subclassing
  the real `MaxAdapter` to override private methods.
