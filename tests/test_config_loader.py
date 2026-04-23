import os
from pathlib import Path

from src.config.loader import load_config


def test_load_config_merges_optional_local_override(tmp_path, monkeypatch):
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "config.local.yaml"

    base_path.write_text(
        """
telegram:
  bot_token: "${TG_BOT_TOKEN}"
  owner_id: "${TG_OWNER_ID}"
  forum_group_id: "${TG_FORUM_GROUP_ID}"

max:
  phone: "${MAX_PHONE}"

storage:
  db_filename: "bridge.db"
  tmp_dirname: "tmp"

bridge:
  forward_all: true
  default_mode: "active"

content: {}

chats: []
""".strip(),
        encoding="utf-8",
    )

    local_path.write_text(
        """
bridge:
  default_mode: "readonly"

chats:
  - max_chat_id: "-70000000000001"
    title: "Локальный чат"
    mode: "disabled"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("TG_BOT_TOKEN", "123456:token")
    monkeypatch.setenv("TG_OWNER_ID", "123")
    monkeypatch.setenv("TG_FORUM_GROUP_ID", "-1001234567890")
    monkeypatch.setenv("MAX_PHONE", "+79990000000")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_LOCAL_PATH", str(local_path))

    cfg = load_config(str(base_path))

    assert cfg.telegram.ops_topic_id is None
    assert cfg.health.reminder_interval_hours == 4
    assert cfg.health.heartbeat_interval_seconds == 30
    assert cfg.health.worker_restart_backoff_seconds == 5
    assert cfg.bridge.default_mode == "readonly"
    assert len(cfg.chats) == 1
    assert cfg.chats[0].max_chat_id == "-70000000000001"
    assert cfg.chats[0].title == "Локальный чат"
    assert cfg.chats[0].mode == "disabled"


def test_load_config_reads_secrets_from_dotenv_secrets(tmp_path, monkeypatch):
    base_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    secrets_path = tmp_path / ".env.secrets"

    base_path.write_text(
        """
telegram:
  bot_token: "${TG_BOT_TOKEN}"
  owner_id: "${TG_OWNER_ID}"
  forum_group_id: "${TG_FORUM_GROUP_ID}"

max:
  phone: "${MAX_PHONE}"

storage:
  db_filename: "bridge.db"
  tmp_dirname: "tmp"

bridge:
  forward_all: true
  default_mode: "active"

content: {}

chats: []
""".strip(),
        encoding="utf-8",
    )

    env_path.write_text("DATA_DIR=./data-from-env\n", encoding="utf-8")
    secrets_path.write_text(
        "\n".join(
            (
                "TG_BOT_TOKEN=987654:token",
                "TG_OWNER_ID=456",
                "TG_FORUM_GROUP_ID=-1009876543210",
                "MAX_PHONE=+79991112233",
            )
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_OWNER_ID", raising=False)
    monkeypatch.delenv("TG_FORUM_GROUP_ID", raising=False)
    monkeypatch.delenv("MAX_PHONE", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("CONFIG_LOCAL_PATH", raising=False)

    cfg = load_config(str(base_path))

    assert cfg.telegram.bot_token == "987654:token"
    assert cfg.telegram.owner_id == 456
    assert cfg.telegram.forum_group_id == -1009876543210
    assert cfg.max.phone == "+79991112233"
    assert cfg.storage.data_dir == Path("data-from-env")
    assert (tmp_path / "data-from-env").exists()
