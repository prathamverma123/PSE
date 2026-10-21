"""SQLite access layer for the personal search engine (schema in schema.sql)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from config import load_config

_local = threading.local()
_write_lock = threading.RLock()

CONFIG = load_config()
DB_PATH = CONFIG["DB_PATH"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Per-thread connection with WAL enabled; foreign keys ON."""
    key = db_path or DB_PATH
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == key:
        return conn
    if conn is not None:
        conn.close()
    Path(key).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit: every write is durable at once,
    # so a separate connection (backup API, CLI, another tool) always sees
    # the same data as this one.
    conn = sqlite3.connect(key, timeout=30.0, check_same_thread=False,
                            isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    _local.path = key
    return conn


@contextmanager
def db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        with _write_lock:
            conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db(db_path: str | None = None) -> None:
    schema = Path(__file__).resolve().parent / "schema.sql"
    sql = schema.read_text(encoding="utf-8")
    conn = connect(db_path)
    with _write_lock:
        conn.executescript(sql)
        conn.commit()


# ---------------------------------------------------------------- documents

def upsert_document(doc: dict, conn: sqlite3.Connection | None = None) -> int:
    """Insert or update a document row (keyed by unique path). Returns id."""
    c = conn or connect()
    now = _now()
    with _write_lock:
        cur = c.execute(
            """
            INSERT INTO documents (path, source, doc_type, title, size, mtime,
                                   ctime, added_at, updated_at, category, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                source=excluded.source,
                doc_type=excluded.doc_type,
                title=excluded.title,
                size=excluded.size,
                mtime=excluded.mtime,
                ctime=excluded.ctime,
                updated_at=excluded.updated_at,
                category=CASE WHEN documents.category='uncategorized'
                              THEN documents.category ELSE documents.category END,
                content_hash=excluded.content_hash
            """,
            (doc["path"], doc.get("source", "manual"), doc.get("doc_type", "other"),
             doc.get("title", ""), int(doc.get("size", 0)), float(doc.get("mtime", 0)),
             float(doc.get("ctime", 0)), now, now,
             doc.get("category", "uncategorized"), doc.get("content_hash", "")),
        )
        return cur.lastrowid if cur.lastrowid else c.execute(
            "SELECT id FROM documents WHERE path=?", (doc["path"],)
        ).fetchone()["id"]


def get_document(doc_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()


def get_document_by_path(path: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()


def list_documents(category: str | None = None, doc_type: str | None = None,
                   tag: str | None = None, q: str | None = None,
                   limit: int = 200, offset: int = 0,
                   conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    c = conn or connect()
    sql = ("SELECT d.* FROM documents d")
    where, params = [], []
    if tag:
        sql += " JOIN document_tags dt ON dt.doc_id=d.id JOIN tags t ON t.id=dt.tag_id"
        where.append("t.name = ?")
        params.append(tag)
    if category:
        where.append("d.category = ?")
        params.append(category)
    if doc_type:
        where.append("d.doc_type = ?")
        params.append(doc_type)
    if q:
        where.append("(d.title LIKE ? OR d.path LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    if tag:
        sql = sql.replace("SELECT d.* FROM documents d", "SELECT DISTINCT d.* FROM documents d", 1)
    sql += " ORDER BY d.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return c.execute(sql, params).fetchall()


def count_documents(conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    return c.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]


def delete_document(doc_id: int, conn: sqlite3.Connection | None = None) -> bool:
    c = conn or connect()
    with _write_lock:
        cur = c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        return cur.rowcount > 0


def set_category(doc_id: int, category: str, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute("UPDATE documents SET category=?, updated_at=? WHERE id=?",
                  (category, _now(), doc_id))


def orphan_tags(conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM document_tags)")


# ---------------------------------------------------------------- tags

def get_or_create_tag(name: str, conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    name = name.strip().lower()
    with _write_lock:
        row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = c.execute("INSERT INTO tags (name) VALUES (?)", (name,))
        return cur.lastrowid


def add_tag(doc_id: int, name: str, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    tag_id = get_or_create_tag(name, c)
    with _write_lock:
        c.execute("INSERT OR IGNORE INTO document_tags (doc_id, tag_id) VALUES (?, ?)",
                  (doc_id, tag_id))


def remove_tag(doc_id: int, name: str, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute(
            "DELETE FROM document_tags WHERE doc_id=? AND tag_id="
            "(SELECT id FROM tags WHERE name=?)", (doc_id, name.strip().lower()))
        orphan_tags(c)


def tags_for_document(doc_id: int, conn: sqlite3.Connection | None = None) -> list[str]:
    c = conn or connect()
    rows = c.execute(
        "SELECT t.name FROM tags t JOIN document_tags dt ON dt.tag_id=t.id "
        "WHERE dt.doc_id=? ORDER BY t.name", (doc_id,)).fetchall()
    return [r["name"] for r in rows]


def set_tags(doc_id: int, names: list[str], conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute("DELETE FROM document_tags WHERE doc_id=?", (doc_id,))
        for name in names:
            tag_id = get_or_create_tag(name, c)
            c.execute("INSERT OR IGNORE INTO document_tags (doc_id, tag_id) VALUES (?, ?)",
                      (doc_id, tag_id))
        orphan_tags(c)


def all_tags_with_counts(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    c = conn or connect()
    return c.execute(
        "SELECT t.name, COUNT(dt.doc_id) AS uses FROM tags t "
        "LEFT JOIN document_tags dt ON dt.tag_id=t.id "
        "GROUP BY t.id ORDER BY uses DESC, t.name").fetchall()


# ----------------------------------------------------------- saved searches

def save_search(name: str, params: dict, conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    with _write_lock:
        cur = c.execute("INSERT INTO saved_searches (name, query_json) VALUES (?, ?)",
                        (name, json.dumps(params)))
        return cur.lastrowid


def list_saved_searches(conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    c = conn or connect()
    return c.execute("SELECT * FROM saved_searches ORDER BY id DESC").fetchall()


def get_saved_search(search_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM saved_searches WHERE id=?", (search_id,)).fetchone()


def delete_saved_search(search_id: int, conn: sqlite3.Connection | None = None) -> bool:
    c = conn or connect()
    with _write_lock:
        cur = c.execute("DELETE FROM saved_searches WHERE id=?", (search_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- users

def create_user(username: str, password: str, conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    with _write_lock:
        cur = c.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)))
        return cur.lastrowid


def set_password(username: str, password: str, conn: sqlite3.Connection | None = None) -> bool:
    c = conn or connect()
    with _write_lock:
        cur = c.execute("UPDATE users SET password_hash=? WHERE username=?",
                        (generate_password_hash(password), username))
        return cur.rowcount > 0


def count_users(conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def get_user_by_id(user_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_name(username: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def verify_user(username: str, password: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    row = get_user_by_name(username, conn)
    if row and check_password_hash(row["password_hash"], password):
        return row
    return None


def set_api_token(user_id: int, token: str | None, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute("UPDATE users SET api_token=? WHERE id=?", (token, user_id))


def get_user_by_token(token: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM users WHERE api_token=?", (token,)).fetchone()


# ---------------------------------------------------------------- crawl jobs

def create_job(kind: str, target: str, conn: sqlite3.Connection | None = None) -> int:
    c = conn or connect()
    with _write_lock:
        cur = c.execute("INSERT INTO crawl_jobs (kind, target) VALUES (?, ?)", (kind, target))
        return cur.lastrowid


def update_job(job_id: int, *, total: int | None = None, processed: int | None = None,
               indexed: int | None = None, skipped: int | None = None,
               errors: int | None = None, status: str | None = None,
               message: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    sets, params = [], []
    if total is not None:
        sets.append("total=?"); params.append(total)
    if processed is not None:
        sets.append("processed=?"); params.append(processed)
    if indexed is not None:
        sets.append("indexed=?"); params.append(indexed)
    if skipped is not None:
        sets.append("skipped=?"); params.append(skipped)
    if errors is not None:
        sets.append("errors=?"); params.append(errors)
    if message is not None:
        sets.append("message=?"); params.append(message[:500])
    if status is not None:
        sets.append("status=?"); params.append(status)
        if status in ("done", "error"):
            sets.append("finished_at=?"); params.append(_now())
    if not sets:
        return
    params.append(job_id)
    with _write_lock:
        c.execute(f"UPDATE crawl_jobs SET {', '.join(sets)} WHERE id=?", params)


def get_job(job_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    c = conn or connect()
    return c.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()


def latest_jobs(limit: int = 10, conn: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    c = conn or connect()
    return c.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ------------------------------------------------------------- content cache

def cache_content(path: str, text: str, encrypted: bool, conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    blob = text.encode("utf-8", "replace")
    with _write_lock:
        c.execute(
            "INSERT INTO content_cache (path, content, encrypted, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET content=excluded.content, "
            "encrypted=excluded.encrypted, updated_at=excluded.updated_at",
            (path, blob, 1 if encrypted else 0, _now()))


def read_cache(path: str, encrypted_reader=None, conn: sqlite3.Connection | None = None) -> str | None:
    c = conn or connect()
    row = c.execute("SELECT content, encrypted FROM content_cache WHERE path=?",
                    (path,)).fetchone()
    if not row:
        return None
    blob = row["content"]
    if row["encrypted"]:
        if encrypted_reader is None:
            return None
        blob = encrypted_reader(blob)
    try:
        return blob.decode("utf-8", "replace")
    except AttributeError:
        return None


def clear_cache(conn: sqlite3.Connection | None = None) -> None:
    c = conn or connect()
    with _write_lock:
        c.execute("DELETE FROM content_cache")


def dump_stats(conn: sqlite3.Connection | None = None) -> dict:
    c = conn or connect()
    stats = {"documents": count_documents(c)}
    stats["by_type"] = {r["doc_type"]: r["n"] for r in c.execute(
        "SELECT doc_type, COUNT(*) AS n FROM documents GROUP BY doc_type")}
    stats["by_category"] = {r["category"]: r["n"] for r in c.execute(
        "SELECT category, COUNT(*) AS n FROM documents GROUP BY category")}
    stats["tags"] = {r["name"]: r["uses"] for r in all_tags_with_counts(c)}
    return stats


init_db()
