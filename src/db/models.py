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
    status          TEXT NOT NULL,   -- pending | delivered | partial | failed
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    last_attempt_at INTEGER NOT NULL
);

-- Дополнительные TG message_id, которые отвечают исходному MAX message_id.
-- Нужны для медиа, досланных позже отдельным сообщением.
CREATE TABLE IF NOT EXISTS tg_reply_map (
    tg_msg_id       INTEGER PRIMARY KEY,
    max_chat_id     TEXT NOT NULL,
    max_msg_id      TEXT NOT NULL,
    tg_topic_id     INTEGER,
    source          TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);

-- Durable retry для MAX-медиа, которое не удалось скачать сразу.
-- Хранится только meta: без текста сообщений, signed URL, token или raw payload.
CREATE TABLE IF NOT EXISTS pending_media_downloads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    max_chat_id      TEXT NOT NULL,
    max_msg_id       TEXT NOT NULL,
    tg_topic_id      INTEGER NOT NULL,
    attachment_index INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    source_type      TEXT,
    media_chat_id    TEXT NOT NULL,
    media_msg_id     TEXT NOT NULL,
    reference_kind   TEXT NOT NULL,
    reference_id     TEXT NOT NULL,
    filename         TEXT,
    duration         INTEGER,
    width            INTEGER,
    height           INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    next_attempt_at  INTEGER NOT NULL,
    last_attempt_at  INTEGER,
    lease_until      INTEGER,
    last_error       TEXT,
    delivered_tg_msg_id INTEGER,
    delivered_at     INTEGER,
    UNIQUE(max_chat_id, max_msg_id, attachment_index, kind)
);

-- Известные пользователи MAX (name ↔ user_id, для /dm поиска)
CREATE TABLE IF NOT EXISTS known_users (
    max_user_id  TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_known_users_name ON known_users(display_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_message_map_max ON message_map(max_msg_id, max_chat_id);
CREATE INDEX IF NOT EXISTS idx_message_map_tg  ON message_map(tg_msg_id);
CREATE INDEX IF NOT EXISTS idx_tg_reply_map_tg ON tg_reply_map(tg_msg_id);
CREATE INDEX IF NOT EXISTS idx_delivery_status ON delivery_log(status, last_attempt_at);
CREATE INDEX IF NOT EXISTS idx_delivery_created ON delivery_log(created_at);
CREATE INDEX IF NOT EXISTS idx_message_created  ON message_map(created_at);
CREATE INDEX IF NOT EXISTS idx_pending_media_status_due
  ON pending_media_downloads(status, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_pending_media_source
  ON pending_media_downloads(max_chat_id, max_msg_id);
"""
