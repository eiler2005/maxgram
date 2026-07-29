# ADR-011: Encrypted TTL Cache For Failed MAX Media Recovery

Date: 2026-07-29

MAX can deliver live attachments as `UNSUPPORTED` or with incomplete media
references. If immediate download and history/exact lookup fail, old messages
may become unrecoverable because the bridge intentionally does not keep full
raw payloads, signed URLs, message text or media files.

## Decision

- Keep `pending_media_downloads` as the durable retry lifecycle table.
- Add `media_recovery_cache` as a separate short-lived cache for problematic
  media only: `UNSUPPORTED`, download failures, partial deliveries and
  cache-only retry paths.
- Store stable references (`audioId`, `fileId`, `photoId`, `videoId`),
  filename, dimensions and duration as metadata.
- Store volatile URL/payload hints only as Fernet ciphertext using
  `MAX_RECOVERY_CONTACTS_KEY`; if the key is missing or invalid, keep metadata
  only and do not persist plaintext payload.
- Default TTL is 48 hours via `bridge.media_recovery_cache_ttl_hours`.
  Existing cleanup runs every 30 minutes and purges expired rows by
  `expires_at`.
- Retry workers first try stable MAX references/history paths. If they fail,
  they may read encrypted cached hints and pass them back into the MAX adapter
  media downloader. Logs include only cache metadata, never URL/payload.

## Rejected Alternatives

- Persist full raw MAX payloads: rejected because they can include message text,
  arbitrary private fields and URLs.
- Persist signed URLs in plaintext: rejected because query strings may carry
  credentials and are not needed outside short recovery windows.
- Keep cache rows forever: rejected because the value decays quickly while DB
  privacy and size cost grows over time.
- Cache every successful attachment: rejected because already delivered media
  has no recovery need and would expand the privacy exception.

## Consequences

- Fresh problematic media has a bounded recovery window even if later MAX
  history/exact lookup stops returning the original payload.
- Older failures that predate this cache remain unrecoverable unless MAX still
  returns the message through history/exact lookup.
- Operators should verify `bridge.media_recovery_cache.*` events and
  `media_recovery_cache.expires_at` when investigating media retry misses.
