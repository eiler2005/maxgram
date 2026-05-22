# ADR-008: BridgeCore Coordinator Split And Scoped Quality Gates

## Status

Accepted.

## Context

`BridgeCore` had already delegated parts of forwarding, topics, replies and
recovery, but it still owned formatter, parser, media retry and recovery
scheduler state. The line count was less important than the semantic issue:
the class still mixed runtime wiring with subdomain behavior.

The repository also had only a very light repo-wide lint/type gate. Turning on
strict linting across the whole codebase at once produced hundreds of existing
findings, so the useful step is a scoped gate around the refactored boundary.

## Decision

- Keep `BridgeCore` as the runtime coordinator: dependencies, stats, callback
  registration and background entrypoints.
- Move operator status rendering to `bridge/status.py`.
- Move durable media retry enqueue/find/worker behavior to `bridge/media_retry.py`.
- Move recovery scan task state, debounce/cooldown and notification digest to
  `bridge/recovery/scheduler.py`.
- Move Telegram command registration to `bridge/commands/dispatcher.py`.
- Add a scoped CI gate for `src/bridge`: ruff bug/simplification checks and
  strict-ish mypy for the touched coordinator boundary.

## Consequences

- `core.py` no longer contains status/recovery formatter/parser methods,
  recovery scheduler state or media retry business logic.
- Existing behavior remains covered through `BridgeCore` integration tests and
  focused boundary tests.
- Repo-wide strict lint/mypy remains a separate cleanup effort; this ADR only
  locks the bridge coordinator boundary.
