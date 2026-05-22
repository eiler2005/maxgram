from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import users as max_users
from .deps import ResolveDeps

logger = logging.getLogger("src.adapters.max_adapter")


class MaxResolveService:
    def __init__(self, deps: ResolveDeps):
        self._deps = deps

    @property
    def _client(self):
        return self._deps.connection.client

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    async def resolve_user_name(self, user_id: str) -> Optional[str]:
        """Получить имя пользователя по ID (для DM чатов без названия).
        Сначала пробует кеш (не требует сокета), затем live-запрос.
        """
        if not self._client:
            return None
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return None

        # 1. Из кеша (синхронно, всегда доступно после sync)
        try:
            cached = self._client.get_cached_user(user_id_int)
            if cached:
                name = self._extract_user_name(cached)
                if name:
                    logger.debug("resolve_user_name (cache) user_id=%s → %r", user_id, name)
                    return name
        except Exception as e:
            logger.debug("get_cached_user failed user_id=%s: %s", user_id, e)

        for source_name, users in (
            ("contacts", getattr(self._client, "contacts", []) or []),
            ("users_cache", (getattr(self._client, "_users", {}) or {}).values()),
        ):
            try:
                for user in users:
                    if str(getattr(user, "id", "") or "") != str(user_id_int):
                        continue
                    name = self._extract_user_name(user)
                    if name:
                        logger.debug(
                            "resolve_user_name (%s) user_id=%s → %r",
                            source_name,
                            user_id,
                            name,
                        )
                        return name
            except Exception as e:
                logger.debug("resolve_user_name %s lookup failed user_id=%s: %s", source_name, user_id, e)

        # 2. Live-запрос (требует активного сокета)
        try:
            users = await asyncio.wait_for(self._client.get_users([user_id_int]), timeout=5)
            if users:
                name = self._extract_user_name(users[0])
                logger.debug("resolve_user_name (live) user_id=%s → %r", user_id, name)
                return name or None
        except asyncio.TimeoutError:
            logger.warning("resolve_user_name timed out user_id=%s", user_id)
        except Exception as e:
            logger.warning("resolve_user_name failed user_id=%s: %s", user_id, e)
        return None

    async def resolve_chat_title(self, chat_id: str) -> Optional[str]:
        """Получить название группового чата по ID.
        Сначала пробует локальный кеш pymax, затем live-запрос к MAX API.
        """
        if not self._client:
            return None

        try:
            chat_id_int = int(chat_id)
        except (TypeError, ValueError):
            return None

        if chat_id_int > 0:
            return None

        try:
            chat_obj = next(
                (chat for chat in getattr(self._client, "chats", []) if getattr(chat, "id", None) == chat_id_int),
                None,
            )
            if chat_obj:
                title = getattr(chat_obj, "title", None) or getattr(chat_obj, "name", None)
                if title:
                    logger.debug("resolve_chat_title (cache) chat_id=%s -> %r", chat_id, title)
                    return title
        except Exception as e:
            logger.debug("resolve_chat_title cache failed chat_id=%s: %s", chat_id, e)

        try:
            chat_obj = await self._client.get_chat(chat_id_int)
            if chat_obj:
                title = getattr(chat_obj, "title", None) or getattr(chat_obj, "name", None)
                if title:
                    logger.debug("resolve_chat_title (live) chat_id=%s -> %r", chat_id, title)
                    return title
        except Exception as e:
            logger.warning("resolve_chat_title failed chat_id=%s: %s", chat_id, e)

        return None

    def get_own_id(self) -> Optional[str]:
        """Вернуть ID нашего MAX аккаунта (для фильтрации собственных сообщений)."""
        return self._own_id

    def find_user_by_name(self, name: str) -> Optional[str]:
        """Найти user_id по отображаемому имени (регистронезависимо).

        Поиск в трёх источниках (от быстрого к более широкому):
          1. client.contacts — контакты, загруженные при sync.
          2. Кеш участников известных DM-диалогов (client.dialogs).
          3. client._users — все пользователи, чьи имена были резолвнуты
             в этой сессии (каждый отправитель любого сообщения в известные чаты).

        Возвращает str(user_id) или None если не найден.
        Если несколько пользователей с одинаковым именем — возвращает первого.
        """
        if not self._client:
            return None
        name_lower = name.strip().lower()

        # 1. Контакты из sync
        for contact in getattr(self._client, "contacts", []):
            contact_name = self._extract_user_name(contact)
            if contact_name and contact_name.strip().lower() == name_lower:
                return str(contact.id)

        # 2. Участники известных DM-диалогов через user cache
        own_id = self._own_id
        for dialog in getattr(self._client, "dialogs", []):
            for pid in (dialog.participants or {}):
                if str(pid) == own_id:
                    continue
                try:
                    user = self._client.get_cached_user(int(pid))
                    if user:
                        user_name = self._extract_user_name(user)
                        if user_name and user_name.strip().lower() == name_lower:
                            return str(pid)
                except Exception:
                    pass

        # 3. Полный кеш пользователей сессии (_users): все отправители всех
        #    сообщений, прошедших через bridge (группы + DM).
        users_cache: dict = getattr(self._client, "_users", {})
        for uid, user in users_cache.items():
            if str(uid) == own_id:
                continue
            try:
                user_name = self._extract_user_name(user)
                if user_name and user_name.strip().lower() == name_lower:
                    return str(uid)
            except Exception:
                pass

        return None

    def get_dm_partner_id(self, chat_id: str) -> Optional[str]:
        """Для DM-чата вернуть user_id СОБЕСЕДНИКА (не нашего аккаунта).

        Использует кеш dialogs из pymax (populated при sync).
        Нужен когда наш аккаунт инициировал чат: в этом случае chat_id может
        совпадать с own_id, и resolve_user_name(chat_id) вернёт наше имя.
        Возвращает None если диалог не найден или собеседник не определён.
        """
        if not self._client or not self._own_id:
            return None
        try:
            chat_id_int = int(chat_id)
            dialog = next(
                (d for d in getattr(self._client, "dialogs", []) if d.id == chat_id_int),
                None,
            )
            if dialog:
                for pid in (dialog.participants or {}):
                    if str(pid) != self._own_id:
                        return str(pid)
        except Exception:
            pass
        return None

    def _extract_user_name(self, user_obj) -> Optional[str]:
        """Извлечь имя из pymax User/Contact/Names объекта."""
        if user_obj is None:
            return None
        # User/Contact имеют .names: list[Names], где Names.name, first_name, last_name
        names_list = getattr(user_obj, "names", None)
        if names_list:
            n = names_list[0]
            first = getattr(n, "first_name", None) or getattr(n, "name", None) or ""
            last  = getattr(n, "last_name", None) or ""
            return f"{first} {last}".strip() or None
        # Fallback: прямые атрибуты (для других объектов)
        first = getattr(user_obj, "first_name", None) or getattr(user_obj, "name", None) or ""
        last  = getattr(user_obj, "last_name", None) or ""
        return f"{first} {last}".strip() or None
