from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from pymax.api.auth.payloads import SyncPayload, WebSyncPayload
from pymax.api.auth.service import AuthService
from pymax.api.session.enums import DeviceType
from pymax.protocol import Opcode
from pymax.types.domain.attachments.enums import AttachmentType
from pymax.types.domain.login import LoginResponse

from src.logging_utils import log_event


SUPPORTED_ATTACHMENT_TYPES = {item.value for item in AttachmentType}
logger = logging.getLogger("src.adapters.max_adapter")


class PymaxPayloadValidationError(RuntimeError):
    """Safe wrapper for PyMax model drift without raw MAX payload in the message."""

    def __init__(
        self,
        *,
        model: str,
        errors: list[dict[str, Any]],
        repaired: bool = False,
    ) -> None:
        self.model = model
        self.errors = errors
        self.repaired = repaired
        paths = _validation_error_paths(errors)
        paths_preview = ", ".join(paths[:6]) or "unknown"
        if len(paths) > 6:
            paths_preview = f"{paths_preview}, +{len(paths) - 6} more"
        super().__init__(
            "pymax payload validation failed: "
            f"model={model}; error_count={len(errors)}; "
            f"repaired={str(repaired).lower()}; paths={paths_preview}"
        )


@dataclass
class _LoginPayloadRepair:
    dropped_last_messages: int = 0
    dropped_messages: int = 0
    nulled_contacts: int = 0
    validation_paths: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(
            (
                self.dropped_last_messages,
                self.dropped_messages,
                self.nulled_contacts,
            )
        )


def sanitize_login_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop attachment variants unknown to PyMax 2 before login validation."""
    return _sanitize_value(deepcopy(payload))


def validate_login_response(
    payload: dict[str, Any],
    *,
    session_token: str | None = None,
) -> LoginResponse:
    """Validate login response while tolerating non-critical PyMax model drift."""
    sanitized = sanitize_login_payload(payload)
    filled_token = _fill_missing_token_from_session(
        sanitized,
        session_token=session_token,
    )
    try:
        response = LoginResponse.model_validate(sanitized)
    except ValidationError as exc:
        repaired, repair = _repair_login_payload_for_validation(sanitized, exc)
        if not repair.changed:
            _log_login_validation_failed(exc, repaired=False)
            raise PymaxPayloadValidationError(
                model="LoginResponse",
                errors=_safe_validation_errors(exc),
            ) from exc
    else:
        if filled_token:
            _log_login_payload_repaired(
                dropped_last_messages=0,
                dropped_messages=0,
                nulled_contacts=0,
                filled_token_from_session=True,
                validation_paths=[],
            )
        return response

    _log_login_payload_repaired(
        dropped_last_messages=repair.dropped_last_messages,
        dropped_messages=repair.dropped_messages,
        nulled_contacts=repair.nulled_contacts,
        filled_token_from_session=filled_token,
        validation_paths=repair.validation_paths[:8],
    )
    try:
        return LoginResponse.model_validate(repaired)
    except ValidationError as exc:
        _log_login_validation_failed(exc, repaired=True)
        raise PymaxPayloadValidationError(
            model="LoginResponse",
            errors=_safe_validation_errors(exc),
            repaired=True,
        ) from exc


def _sanitize_value(value):
    if isinstance(value, dict):
        sanitized = {key: _sanitize_value(item) for key, item in value.items()}
        attaches = sanitized.get("attaches")
        if isinstance(attaches, list):
            sanitized["attaches"] = [
                item
                for item in attaches
                if not _is_unsupported_attachment(item)
            ]
        attributes = sanitized.get("attributes")
        if isinstance(attributes, dict) and _is_message_element(sanitized):
            if "url" not in attributes:
                sanitized.pop("attributes", None)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _is_unsupported_attachment(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    raw_type = value.get("type", value.get("_type"))
    return isinstance(raw_type, str) and raw_type not in SUPPORTED_ATTACHMENT_TYPES


def _is_message_element(value: dict[str, Any]) -> bool:
    return isinstance(value.get("type"), str) and isinstance(value.get("length"), int)


def _fill_missing_token_from_session(
    payload: dict[str, Any],
    *,
    session_token: str | None,
) -> bool:
    if not session_token:
        return False
    token = payload.get("token")
    if isinstance(token, str) and token:
        return False
    payload["token"] = session_token
    return True


def _safe_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return exc.errors(include_url=False, include_input=False)


def _validation_error_paths(errors: list[dict[str, Any]]) -> list[str]:
    return [_format_validation_loc(error.get("loc", ())) for error in errors]


def _format_validation_loc(loc: object) -> str:
    if not isinstance(loc, tuple):
        return "unknown"
    parts: list[str] = []
    for index, item in enumerate(loc):
        if loc and loc[0] == "messages" and index == 1:
            parts.append("*")
        elif isinstance(item, int):
            parts.append("#")
        else:
            parts.append(str(item))
    return ".".join(parts) or "unknown"


def _repair_login_payload_for_validation(
    payload: dict[str, Any],
    exc: ValidationError,
) -> tuple[dict[str, Any], _LoginPayloadRepair]:
    repaired = deepcopy(payload)
    repair = _LoginPayloadRepair(
        validation_paths=_validation_error_paths(_safe_validation_errors(exc))
    )
    last_message_indexes: set[int] = set()
    message_indexes: dict[object, set[int]] = {}
    contact_indexes: set[int] = set()

    for error in _safe_validation_errors(exc):
        loc = error.get("loc", ())
        if not isinstance(loc, tuple):
            continue
        if _is_last_message_validation_loc(loc):
            last_message_indexes.add(loc[1])
        elif _is_message_list_validation_loc(loc):
            message_indexes.setdefault(loc[1], set()).add(loc[2])
        elif _is_contact_validation_loc(loc):
            contact_indexes.add(loc[1])

    chats = repaired.get("chats")
    if isinstance(chats, list):
        for index in sorted(last_message_indexes):
            if 0 <= index < len(chats) and isinstance(chats[index], dict):
                if chats[index].pop("lastMessage", None) is not None:
                    repair.dropped_last_messages += 1

    messages = repaired.get("messages")
    if isinstance(messages, dict):
        for key, indexes in message_indexes.items():
            message_list = _get_message_list(messages, key)
            if not isinstance(message_list, list):
                continue
            for index in sorted(indexes, reverse=True):
                if 0 <= index < len(message_list):
                    del message_list[index]
                    repair.dropped_messages += 1

    contacts = repaired.get("contacts")
    if isinstance(contacts, list):
        for index in sorted(contact_indexes):
            if 0 <= index < len(contacts) and contacts[index] is not None:
                contacts[index] = None
                repair.nulled_contacts += 1

    return repaired, repair


def _is_last_message_validation_loc(loc: tuple[object, ...]) -> bool:
    return (
        len(loc) >= 3
        and loc[0] == "chats"
        and isinstance(loc[1], int)
        and loc[2] == "lastMessage"
    )


def _is_message_list_validation_loc(loc: tuple[object, ...]) -> bool:
    return (
        len(loc) >= 3
        and loc[0] == "messages"
        and isinstance(loc[2], int)
    )


def _is_contact_validation_loc(loc: tuple[object, ...]) -> bool:
    return len(loc) >= 2 and loc[0] == "contacts" and isinstance(loc[1], int)


def _get_message_list(messages: dict[object, Any], key: object) -> object:
    if key in messages:
        return messages[key]
    if isinstance(key, int) and str(key) in messages:
        return messages[str(key)]
    if isinstance(key, str):
        try:
            int_key = int(key)
        except ValueError:
            return None
        return messages.get(int_key)
    return None


def _log_login_validation_failed(exc: ValidationError, *, repaired: bool) -> None:
    errors = _safe_validation_errors(exc)
    log_event(
        logger,
        logging.ERROR,
        "max.pymax.payload_validation_failed",
        stage="login",
        outcome="failed",
        model="LoginResponse",
        repaired=repaired,
        error_count=len(errors),
        validation_paths=_validation_error_paths(errors)[:8],
    )


def _log_login_payload_repaired(
    *,
    dropped_last_messages: int,
    dropped_messages: int,
    nulled_contacts: int,
    filled_token_from_session: bool,
    validation_paths: list[str],
) -> None:
    log_event(
        logger,
        logging.WARNING,
        "max.pymax.payload_repaired",
        stage="login",
        outcome="repaired",
        model="LoginResponse",
        dropped_last_messages=dropped_last_messages,
        dropped_messages=dropped_messages,
        nulled_contacts=nulled_contacts,
        filled_token_from_session=filled_token_from_session,
        validation_paths=validation_paths,
    )


class BridgeAuthService(AuthService):
    """Auth service that tolerates server attachment variants missing upstream."""

    async def login(self, user_agent):
        if user_agent.device_type == DeviceType.WEB:
            return await self.web_login()
        return await self.mobile_login()

    async def mobile_login(self) -> LoginResponse:
        session = self.app.session
        if session is None:
            raise RuntimeError("No session available for login")

        sync = self.app.config.sync.resolve(session.sync)
        frame = SyncPayload.from_sync_state(
            user_agent=self.app.config.device.user_agent,
            token=session.token,
            sync=sync,
        )
        response = await self.app.invoke(Opcode.LOGIN, frame.to_payload())
        login_response = validate_login_response(
            response.payload,
            session_token=session.token,
        )
        await self._update_session(login_response)
        return login_response

    async def web_login(self) -> LoginResponse:
        session = self.app.session
        if session is None:
            raise RuntimeError("No session available for login")

        sync = self.app.config.sync.resolve(session.sync)
        frame = WebSyncPayload.from_sync_state(
            token=session.token,
            sync=sync,
        )
        response = await self.app.invoke(Opcode.LOGIN, frame.to_payload())
        login_response = validate_login_response(
            response.payload,
            session_token=session.token,
        )
        await self._update_session(login_response)
        return login_response
