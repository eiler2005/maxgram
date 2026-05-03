"""
Загрузка конфига из config.yaml + optional config.local.yaml + .env переменных.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


@dataclass
class TelegramConfig:
    bot_token: str
    owner_id: int
    forum_group_id: int
    ops_topic_id: Optional[int] = None


@dataclass
class MaxConfig:
    phone: str
    session_filename: str = "session.db"


@dataclass
class StorageConfig:
    data_dir: Path
    db_filename: str = "bridge.db"
    tmp_dirname: str = "tmp"

    @property
    def db_path(self) -> str:
        return str(self.data_dir / self.db_filename)

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / self.tmp_dirname

    @property
    def session_path(self) -> str:
        return str(self.data_dir)


@dataclass
class HealthConfig:
    reminder_interval_hours: int = 4
    heartbeat_interval_seconds: int = 30
    worker_restart_backoff_seconds: int = 5


@dataclass
class ContentConfig:
    forward_photos: bool = True
    forward_documents: bool = True
    forward_voice: bool = False
    forward_stickers: bool = False
    placeholder_unsupported: str = "[Неподдерживаемый тип: {type}]"
    placeholder_file_too_large: str = "[Файл слишком большой: {filename}]"


@dataclass
class BridgeConfig:
    forward_all: bool = True
    default_mode: str = "active"
    file_retention_hours: int = 1
    message_retention_days: int = 30
    log_retention_days: int = 7
    max_file_size_mb: int = 50


@dataclass
class ChatConfig:
    max_chat_id: str
    title: str
    mode: str = "active"


@dataclass
class AppConfig:
    telegram: TelegramConfig
    max: MaxConfig
    storage: StorageConfig
    health: HealthConfig
    bridge: BridgeConfig
    content: ContentConfig
    chats: list[ChatConfig] = field(default_factory=list)

    def get_chat_mode(self, max_chat_id: str) -> str:
        """Режим для конкретного чата (из конфига или default)."""
        for c in self.chats:
            if c.max_chat_id == str(max_chat_id):
                return c.mode
        return self.bridge.default_mode

    def get_chat_title(self, max_chat_id: str) -> Optional[str]:
        """Предустановленное название чата из конфига."""
        for c in self.chats:
            if c.max_chat_id == str(max_chat_id):
                return c.title
        return None


def _resolve_env(value) -> str:
    """Заменяет ${VAR} → os.environ[VAR]."""
    if not isinstance(value, str):
        return value
    pattern = re.compile(r'\$\{([^}]+)\}')
    def replacer(m):
        var = m.group(1)
        val = os.environ.get(var, "")
        if not val:
            raise ValueError(f"Env variable {var!r} is not set (required in config.yaml)")
        return val
    return pattern.sub(replacer, value)


def _resolve_optional_int(value) -> Optional[int]:
    if value is None:
        return None
    rendered = _resolve_env(str(value)).strip()
    if not rendered:
        return None
    return int(rendered)


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно объединяет словари. override имеет приоритет."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
            continue
        merged[key] = value
    return merged


def load_config(config_path: str = "config.yaml") -> AppConfig:
    config_root = Path(config_path).resolve().parent
    load_dotenv(config_root / ".env")
    load_dotenv(config_root / ".env.secrets", override=True)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    local_config_path = Path(os.environ.get("CONFIG_LOCAL_PATH", "config.local.yaml"))
    if local_config_path.exists():
        with open(local_config_path, "r", encoding="utf-8") as f:
            local_raw = yaml.safe_load(f) or {}
        raw = _deep_merge(raw or {}, local_raw)

    tg_raw = raw.get("telegram", {})
    tg = TelegramConfig(
        bot_token=_resolve_env(tg_raw["bot_token"]),
        owner_id=int(_resolve_env(str(tg_raw["owner_id"]))),
        forum_group_id=int(_resolve_env(str(tg_raw["forum_group_id"]))),
        ops_topic_id=_resolve_optional_int(tg_raw.get("ops_topic_id")),
    )

    max_raw = raw.get("max", {})
    mx = MaxConfig(
        phone=_resolve_env(max_raw.get("phone", "${MAX_PHONE}")),
            session_filename=max_raw.get("session_filename", "session.db"),
    )

    stor_raw = raw.get("storage", {})
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    stor = StorageConfig(
        data_dir=data_dir,
        db_filename=stor_raw.get("db_filename", "bridge.db"),
        tmp_dirname=stor_raw.get("tmp_dirname", "tmp"),
    )
    stor.tmp_dir.mkdir(parents=True, exist_ok=True)

    health_raw = raw.get("health", {})
    health = HealthConfig(
        reminder_interval_hours=int(health_raw.get("reminder_interval_hours", 4)),
        heartbeat_interval_seconds=int(health_raw.get("heartbeat_interval_seconds", 30)),
        worker_restart_backoff_seconds=int(health_raw.get("worker_restart_backoff_seconds", 5)),
    )

    br_raw = raw.get("bridge", {})
    br = BridgeConfig(
        forward_all=br_raw.get("forward_all", True),
        default_mode=br_raw.get("default_mode", "active"),
        file_retention_hours=br_raw.get("file_retention_hours", 1),
        message_retention_days=br_raw.get("message_retention_days", 30),
        log_retention_days=br_raw.get("log_retention_days", 7),
        max_file_size_mb=br_raw.get("max_file_size_mb", 50),
    )

    ct_raw = raw.get("content", {})
    ct = ContentConfig(
        forward_photos=ct_raw.get("forward_photos", True),
        forward_documents=ct_raw.get("forward_documents", True),
        forward_voice=ct_raw.get("forward_voice", False),
        forward_stickers=ct_raw.get("forward_stickers", False),
        placeholder_unsupported=ct_raw.get("placeholder_unsupported", "[Неподдерживаемый тип: {type}]"),
        placeholder_file_too_large=ct_raw.get("placeholder_file_too_large", "[Файл слишком большой: {filename}]"),
    )

    chats = [
        ChatConfig(
            max_chat_id=str(c["max_chat_id"]),
            title=c.get("title", str(c["max_chat_id"])),
            mode=c.get("mode", br.default_mode),
        )
        for c in raw.get("chats", [])
    ]

    return AppConfig(
        telegram=tg,
        max=mx,
        storage=stor,
        health=health,
        bridge=br,
        content=ct,
        chats=chats,
    )
