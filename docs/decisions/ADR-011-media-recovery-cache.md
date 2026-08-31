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
- Cache-only `FILE` recovery first tries the encrypted direct URL hint, then
  its stable `fileId`. The typed PyMax file API must return an HTTP(S) URL;
  otherwise the backend performs the constrained raw `FILE_DOWNLOAD` fallback
  with only `chatId`, `messageId` and `fileId`. Forwarded/cache jobs retry the
  source coordinates and then the receiving wrapper coordinates.
- Video recovery uses public PyMax 2.4.1 `get_video_by_id()` first and keeps raw
  `VIDEO_PLAY` only as a backend fallback. After the immediate failure it makes
  six deferred attempts every 180 seconds, then emits a terminal warning.
  Existing photo/audio stable-reference backoff remains unchanged.

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
- A video placeholder has a bounded 18-minute lifetime instead of promising an
  indefinite delivery "in a couple of minutes". A failed job remains as
  metadata for diagnostics and can be reset only by exact id after a backup.
- The recovery regression matrix covers safe typed/raw URL selection, direct
  URL → stable reference fallback for files, source/wrapper fallback, durable
  retry after a double miss, exact video retry exhaustion and per-media-part
  duplicate suppression. New media recovery paths must extend this matrix.
