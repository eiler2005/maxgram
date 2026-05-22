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
- `src/adapters/max/ports.py` defines the typed internal client port and DTOs
  that operation services consume.
- `PymaxClientAdapter` wraps `SocketMaxClient`, hides private pymax calls and
  converts pymax messages/users/chats/dialogs/attachments into internal views.
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
- Tests enforce no mixin-based `MaxAdapter` inheritance, no service registry /
  service `__getattr__`, no service dependency on full `MaxAdapter`, and no
  `pymax` imports outside `src/adapters/max/backends/pymax/`.
- Additional service-boundary tests enforce that operation services do not call
  pymax-private/client-shape attrs such as `_send_and_wait`,
  `_handle_message_notifications`, `fetch_history`, `get_file_by_id`,
  `contacts/dialogs/chats/_users` directly.
- Adapter tests use harness/fake service dependencies instead of subclassing
  the real `MaxAdapter` to override private methods.
