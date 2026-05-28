from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymax.api.response import parse_payload_list, require_payload_item_model
from pymax.api.users.enums import ContactAction, UserPayloadKey
from pymax.api.users.payloads import (
    ContactActionPayload,
    FetchContactsPayload,
    SearchByPhonePayload,
)
from pymax.api.users.service import UserService
from pymax.protocol import Opcode
from pymax.types.domain import User


def sanitize_user_payload(payload: dict[Any, Any]) -> dict[Any, Any]:
    """Normalize MAX user shapes that PyMax 2.1.x models still type too tightly."""
    return _sanitize_user_value(deepcopy(payload))


def _sanitize_user_value(value):
    if isinstance(value, dict):
        sanitized = {key: _sanitize_user_value(item) for key, item in value.items()}
        gender = sanitized.get("gender")
        if gender is not None and not isinstance(gender, str):
            sanitized.pop("gender", None)
        _normalize_web_app(sanitized, "webApp")
        _normalize_web_app(sanitized, "web_app")
        return sanitized
    if isinstance(value, list):
        return [_sanitize_user_value(item) for item in value]
    return value


def _normalize_web_app(value: dict[Any, Any], key: str) -> None:
    web_app = value.get(key)
    if web_app is None or isinstance(web_app, dict):
        return
    if isinstance(web_app, str):
        value[key] = {"url": web_app}
        return
    value.pop(key, None)


class BridgeUserService(UserService):
    """User service that tolerates production MAX payload drift."""

    async def fetch_users(self, user_ids: list[int]) -> list[User]:
        frame = FetchContactsPayload(contact_ids=user_ids)
        response = await self.app.invoke(Opcode.CONTACT_INFO, frame.to_payload())
        if response.payload is not None:
            response.payload = sanitize_user_payload(response.payload)

        users = [
            self._cache_user(user)
            for user in parse_payload_list(response, UserPayloadKey.CONTACTS, User)
        ]
        return users

    async def search_by_phone(self, phone: str) -> User:
        frame = SearchByPhonePayload(phone=phone)
        response = await self.app.invoke(
            Opcode.CONTACT_INFO_BY_PHONE,
            frame.to_payload(),
        )
        if response.payload is not None:
            response.payload = sanitize_user_payload(response.payload)
        contact = require_payload_item_model(response, UserPayloadKey.CONTACT, User)
        return self._cache_user(contact)

    async def add_contact(self, contact_id: int) -> User:
        response = await self._contact_action(
            ContactActionPayload(
                contact_id=contact_id,
                action=ContactAction.ADD,
            )
        )
        if response.payload is not None:
            response.payload = sanitize_user_payload(response.payload)
        contact = require_payload_item_model(response, UserPayloadKey.CONTACT, User)
        return self._cache_user(contact)
