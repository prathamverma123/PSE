-- Personal Search Engine — SQLite schema.
-- Everything lives on this machine; nothing is transmitted anywhere.

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,          -- file path OR url
    source       TEXT NOT NULL DEFAULT 'manual',-- manual | crawl | url
    doc_type     TEXT NOT NULL DEFAULT 'other',
    title        TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    ctime        REAL NOT NULL DEFAULT 0,
    added_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    category     TEXT NOT NULL DEFAULT 'uncategorized',  -- banned | unbanned | uncategorized | custom
    content_hash TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_documents_doc_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS ix_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS ix_documents_mtime ON documents(mtime);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS document_tags (
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, tag_id)
);

CREATE INDEX IF NOT EXISTS ix_document_tags_tag ON document_tags(tag_id);

CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    query_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    api_token     TEXT UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- directory | url
    target      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | done | error
    total       INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    indexed     INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS content_cache (
    path       TEXT PRIMARY KEY,
    content    BLOB,
    encrypted  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);
