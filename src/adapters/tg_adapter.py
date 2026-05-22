"""Compatibility import for the Telegram adapter."""

from .tg import ReplyHandler, TelegramAdapter, TelegramNotifier

__all__ = ["ReplyHandler", "TelegramAdapter", "TelegramNotifier"]
