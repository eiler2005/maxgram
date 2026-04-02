import os

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

    assert cfg.bridge.default_mode == "readonly"
    assert len(cfg.chats) == 1
    assert cfg.chats[0].max_chat_id == "-70000000000001"
    assert cfg.chats[0].title == "Локальный чат"
    assert cfg.chats[0].mode == "disabled"
