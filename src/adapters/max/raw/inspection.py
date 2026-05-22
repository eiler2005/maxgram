from __future__ import annotations

from typing import Callable, Protocol

from .. import payload as max_payload


class AttachmentInspector(Protocol):
    def attachment_type_name(self, attach) -> str: ...
    def normalize_attachment_type(self, atype: str) -> str: ...
    def safe_attachment_field_names(self, attach) -> list[str]: ...


def _attachment_type_name(attach) -> str:
    atype = getattr(attach, "type", None)
    if atype is None:
        return ""
    return str(getattr(atype, "value", atype)).upper()


def _normalize_attachment_type(atype: str) -> str:
    if not atype:
        return ""
    upper = str(atype).upper()
    if upper.startswith(("PHOTO", "IMAGE")):
        return "PHOTO"
    if upper.startswith("VIDEO"):
        return "VIDEO"
    if upper.startswith(("AUDIO", "VOICE")):
        return "AUDIO"
    if upper.startswith(("FILE", "DOCUMENT", "DOC")):
        return "FILE"
    return upper


def _safe_attachment_field_names(attach) -> list[str]:
    try:
        names = vars(attach).keys()
    except TypeError:
        names = (
            name
            for name in dir(attach)
            if not name.startswith("_") and not callable(getattr(attach, name, None))
        )
    return sorted(name for name in names if max_payload.is_safe_field_name(name))


class AttachmentInspectorProxy:
    """Narrow raw-payload view over media attachment helpers.

    Raw parsing only needs type/name-safe inspection. Keeping this as a tiny
    protocol-shaped proxy prevents raw payload recovery from depending on the
    full media service surface.
    """

    def __init__(self, media_factory: Callable[[], object | None]):
        self._media_factory = media_factory

    def attachment_type_name(self, attach) -> str:
        media = self._media_factory()
        method = getattr(media, "_attachment_type_name", None)
        if callable(method):
            return method(attach)
        return _attachment_type_name(attach)

    def normalize_attachment_type(self, atype: str) -> str:
        media = self._media_factory()
        method = getattr(media, "_normalize_attachment_type", None)
        if callable(method):
            return method(atype)
        return _normalize_attachment_type(atype)

    def safe_attachment_field_names(self, attach) -> list[str]:
        media = self._media_factory()
        method = getattr(media, "_safe_attachment_field_names", None)
        if callable(method):
            return method(attach)
        return _safe_attachment_field_names(attach)
