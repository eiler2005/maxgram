# ADR-005: MAX Account Migration Recovery Registry

## Status

Accepted — 2026-05-22

## Context

MAX phone number migration is not a normal in-app operation for this bridge. If the owner loses access to the current MAX account and reauthorizes with another phone number, the bridge should treat it as a new MAX account.

The important continuity target is Telegram-side continuity: existing Telegram forum topics should remain the operator UI for the same real-world conversations. MAX-side message history and private/admin-only chat membership cannot be cloned by the bridge. Those chats may require an admin invite or a valid invite link.

Privacy constraints remain unchanged:

- Do not store message text.
- Do not store raw MAX payloads.
- Do not store media URLs, signed URLs, or tokens.
- Do not expose invite links or manual notes in normal logs or group-visible reports.

## Decision

Add a SQLite-backed recovery registry inside `data/bridge.db`.

Persist:

- `max_account_generations`: `max_user_id`, masked phone, session fingerprint hash, `active|retired|lost`, first/last seen timestamps.
- `chat_recovery_registry`: stable `registry_key` (`tg_topic:<topic_id>` or `max_chat:<chat_id>`), topic id, old/current MAX chat id, title, chat kind, mode, priority, access type, invite link, owner/admin metadata, DM partner metadata, participant count, manual note, recovery status, `last_scan_at`.
- `chat_recovery_events`: append-only scan/set/remap/account-change audit with compact metadata only.

Add `MaxAdapter.collect_recovery_snapshot()` to collect metadata from `client.chats`, `client.channels`, `client.dialogs`, and `get_chat()`.

Add owner-only Telegram commands:

- `/recovery scan`
- `/recovery report`
- `/recovery export`
- `/recovery set <topic_id> key=value ...`
- `/recovery remap <topic_id> <new_max_chat_id>`

Run safe recovery scans after successful MAX connect/reconnect and weekly thereafter. Also run event-driven snapshots with hybrid triggers:

- `new_binding`: after a new Telegram topic binding is created for a MAX chat; high-priority scan after a short delay.
- `title_changed`: after fallback topic title resolution or explicit title update; short debounce.
- `control_event`: when normalized MAX metadata reports a `CONTROL` message/attachment type; lower-priority scan with cooldown.

Event-driven scans are scheduled in `BridgeCore` via background `asyncio` tasks. Message forwarding, topic creation, and title updates must not wait for snapshot collection. Multiple events collapse into one scan task.

Show snapshot freshness via `last_scan_at`.

Use important-only notifications for automatic scans. Notify owner/ops only for meaningful changes: new registry rows, unmapped MAX chats, invite/admin-required states, or account migration. Notification text contains aggregate counts/statuses and points to `/recovery report`; it must not contain invite links, manual notes, phone numbers, message text, titles, raw MAX fields, media URLs, signed URLs, or tokens. Identical notification digests may be deduplicated in memory for about 24 hours.

When a remap changes a topic from an old MAX chat to a new MAX chat, preserve the Telegram topic and old `message_map`. If the operator replies to an old Telegram message whose mapped MAX message belongs to the old chat id, send the new MAX message without `reply_to_msg_id`.

## Consequences

Benefits:

- The operator has a complete recovery checklist before losing the old account.
- Private/admin-only chats can be described with admin contacts, notes, and invite links.
- Existing Telegram topics survive MAX account migration.
- Backups of `data/` include recovery metadata automatically.

Tradeoffs:

- The registry stores sensitive operational metadata such as invite links and admin notes. It must stay in `data/`, with the same protection model as `bridge.db` and `session.db`.
- V1 does not auto-join chats or mass-invite the new account. Human/admin action is still required.
- V1 does not auto-remap or guess matches from title similarity. The operator still decides using `/recovery report`, invites/links, and `/recovery remap`.
- Recovery export is owner-DM only and should not be pasted into group chats.

## Verification

Covered by tests for:

- SQLite migrations/upsert/report/export/remap idempotency.
- Snapshot collection from fake group/channel/DM objects.
- Event-driven scheduler: new bindings do not delay forwarding, CONTROL events debounce into one scan, and important-only notifications are redacted/deduplicated.
- Owner-only `/recovery` command flow.
- Telegram command allowlist: `/dm` public in General, `/recovery` owner-only.
- Stale reply mapping after remap.
- Privacy: reports/logs do not include invite links, notes, phone numbers, message text, or raw MAX payloads.
