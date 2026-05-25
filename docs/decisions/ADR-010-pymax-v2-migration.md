# ADR-010: PyMax v2 Migration Through Backend Adapter

Date: 2026-05-25

## Status

Accepted

## Context

The bridge originally grew around the PyMax v1 client shape. PyMax v2
(`maxapi-python` 2.x) changed import paths, connection/session internals and
payload classes enough that direct use from bridge code would make future
upstream drift risky.

The long migration journal is kept in
[docs/migration-pymax-2.0.md](../migration-pymax-2.0.md).

## Decision

- Keep `BridgeCore` and bridge modules dependent only on transport-neutral
  contracts.
- Keep all PyMax imports and private client-shape adaptation under
  `src/adapters/max/backends/pymax/`.
- Treat PyMax v2 as the active backend through `PymaxBackend` and typed client
  ports/DTOs; PyMax v1 reconnect/OOM handling remains historical rationale.
- Guard the migration with surface-pin tests for the actual PyMax v2 import
  surface and a fake-backend integration test that proves `MaxBackend` can be
  swapped without PyMax.

## Rejected Alternatives

- Import PyMax directly from bridge/core modules: rejected because it would make
  routing logic depend on upstream library internals.
- Keep a dual PyMax v1/v2 runtime switch: rejected because it increases
  operational paths without a current production need.
- Replace the backend boundary with a generic plugin system: rejected because a
  single explicit backend interface is enough for this bridge.

## Consequences

- Upgrading `maxapi-python` requires reviewing only the backend adapter surface
  plus the pinned import tests.
- External reviewers can verify the architecture claim by running the
  fake-backend integration test and `examples/swap_max_backend.py`.
- Any new PyMax private-attribute access should be centralized in the backend
  adapter, not spread through bridge/core logic.
