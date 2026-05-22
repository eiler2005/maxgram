# ADR-009: MAX adapter service boundaries without god base class

Date: 2026-05-22

## Status

Accepted

## Context

`MaxAdapter` had already moved away from mixins into operation services, but those services still inherited a shared `ExplicitMaxService` base. The base exposed state and cross-service calls through private forwarder methods, which made dependencies implicit: for example, event handling could call media/raw/recovery helpers without declaring those collaborators in its own code.

At the same time, raw payload handling and media downloading had grown into large multi-responsibility modules. This made pymax quirks harder to isolate and made type checking less useful.

## Decision

- Keep `MaxAdapter` as the public facade and composition point.
- Split raw payload behavior behind `raw_payload.py` into parser/normalizer, raw-history cache/fetch, empty-message candidate recovery and telemetry helpers.
- Move generic CDN HTTP download/retry/resume/content validation into `media/downloader.py`; keep `media/attachments.py` focused on MAX attachment strategy and `MaxAttachment` construction.
- Remove `ExplicitMaxService`; each operation service owns its explicit deps object and calls required collaborators directly.

## Consequences

- Public bridge contracts and compatibility import paths stay unchanged.
- Cross-service coupling is now visible in service deps wiring instead of inherited private methods.
- Architectural tests guard against reintroducing `ExplicitMaxService`, dynamic registries, `__getattr__` service lookup or pymax imports outside the backend boundary.
