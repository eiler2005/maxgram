"""
SQLite схема для bridge state.
Всё состояние — только здесь. Никаких in-memory кешей критических данных.
"""

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Связь: MAX чат ↔ Telegram топик
CREATE TABLE IF NOT EXISTS chat_bindings (
    max_chat_id     TEXT PRIMARY KEY,
    tg_topic_id     INTEGER NOT NULL,
    title           TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'active',  -- active | readonly | disabled
    created_at      INTEGER NOT NULL  -- unix timestamp
);

-- Связь: MAX message_id ↔ Telegram message_id (дедупликация + reply routing)
CREATE TABLE IF NOT EXISTS message_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    max_msg_id      TEXT NOT NULL,
    max_chat_id     TEXT NOT NULL,
    tg_msg_id       INTEGER,
    tg_topic_id     INTEGER,
    direction       TEXT NOT NULL,   -- inbound | outbound
    created_at      INTEGER NOT NULL,
    UNIQUE(max_msg_id, max_chat_id)
);

-- Лог доставки (только meta, без текста сообщений)
CREATE TABLE IF NOT EXISTS delivery_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    max_msg_id      TEXT NOT NULL,
    max_chat_id     TEXT NOT NULL,
    direction       TEXT NOT NULL,
    status          TEXT NOT NULL,   -- pending | delivered | failed
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    last_attempt_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_map_max ON message_map(max_msg_id, max_chat_id);
CREATE INDEX IF NOT EXISTS idx_message_map_tg  ON message_map(tg_msg_id);
CREATE INDEX IF NOT EXISTS idx_delivery_status ON delivery_log(status, last_attempt_at);
CREATE INDEX IF NOT EXISTS idx_delivery_created ON delivery_log(created_at);
CREATE INDEX IF NOT EXISTS idx_message_created  ON message_map(created_at);
"""
