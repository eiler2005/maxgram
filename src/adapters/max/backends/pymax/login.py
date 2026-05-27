from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymax.api.auth.payloads import SyncPayload, WebSyncPayload
from pymax.api.auth.service import AuthService
from pymax.api.session.enums import DeviceType
from pymax.protocol import Opcode
from pymax.types.domain.attachments.enums import AttachmentType
from pymax.types.domain.login import LoginResponse


SUPPORTED_ATTACHMENT_TYPES = {item.value for item in AttachmentType}


def sanitize_login_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop attachment variants unknown to PyMax 2 before login validation."""
    return _sanitize_value(deepcopy(payload))


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
        login_response = LoginResponse.model_validate(
            sanitize_login_payload(response.payload)
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
        login_response = LoginResponse.model_validate(
            sanitize_login_payload(response.payload)
        )
        await self._update_session(login_response)
        return login_response
