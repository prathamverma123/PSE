"""Whoosh full-text index for the personal search engine.

Fields: path, title, content, doc_type, category, tags, mtime, size, id.
The index lives entirely in INDEX_DIR on your machine.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from whoosh import highlight
from whoosh.fields import DATETIME, ID, NUMERIC, TEXT, Schema
from whoosh.filedb.filestore import FileStorage
from whoosh.index import Index, open_dir
from whoosh.qparser import MultifieldParser, QueryParser
from whoosh.query import And, DateRange, Every, Term
from whoosh.writing import AsyncWriter

import database
from config import load_config

log = logging.getLogger("pse.index")

CONFIG = load_config()
INDEX_DIR = Path(CONFIG["INDEX_DIR"])
_write_lock = threading.RLock()

SCHEMA = Schema(
    id=NUMERIC(stored=True, numtype=int),
    path=ID(stored=True, unique=True),
    title=TEXT(stored=True),
    content=TEXT(stored=True),
    doc_type=ID(stored=True),
    category=ID(stored=True),
    tags=TEXT(stored=True),
    mtime=NUMERIC(stored=True, numtype=float),
    modified=DATETIME(stored=True),   # same as mtime, as a real datetime for DateRange
    size=NUMERIC(stored=True, numtype=int),
    added_at=TEXT(stored=True),
)

# A highlighter that wraps hits in <mark> for the result previews.
class MarkFormatter(highlight.HtmlFormatter):
    """HtmlFormatter with a <mark> template — no CSS classes needed."""
    template = '<mark>%(t)s</mark>'


_hl = highlight.Highlighter()
_hl.fragmenter = highlight.ContextFragmenter(maxchars=180, surround=60)
_hl.formatter = MarkFormatter(between=" … ")


def _open_index(create: bool = False) -> Index:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return open_dir(str(INDEX_DIR), indexname="pse")
    except (OSError, Exception) as e:  # noqa: BLE001 - index may not exist yet
        if "NoIndexFound" in type(e).__name__ or not INDEX_DIR.glob("pse*"):
            pass
    # Whoosh's open_dir raises variously; fall back to creating it.
    from whoosh.index import create_in
    return create_in(str(INDEX_DIR), SCHEMA, indexname="pse")


def _storage_exists() -> bool:
    storage = FileStorage(str(INDEX_DIR))
    return storage.index_exists(indexname="pse")


def get_index() -> Index:
    if _storage_exists():
        return open_dir(str(INDEX_DIR), indexname="pse")
    from whoosh.index import create_in
    return create_in(str(INDEX_DIR), SCHEMA, indexname="pse")


def rebuild_index() -> None:
    """Drop and recreate the empty index (documents stay in SQLite)."""
    with _write_lock:
        if INDEX_DIR.exists():
            shutil.rmtree(INDEX_DIR, ignore_errors=True)
        get_index()


def wipe_index_and_db() -> None:
    """Remove the index and all document rows/tags (nuclear option)."""
    with _write_lock:
        if INDEX_DIR.exists():
            shutil.rmtree(INDEX_DIR, ignore_errors=True)
        get_index()
        c = database.connect()
        c.execute("DELETE FROM document_tags")
        c.execute("DELETE FROM documents")
        c.commit()


# ------------------------------------------------------------------ helpers

def _as_datetime(ts: float):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def normalize_query(query: str) -> str:
    """Make a raw query string safe for Whoosh's default parser.

    Supports plain words (ANDed), quoted phrases, and prefix wildcards
    (foo*). Anything else is escaped so user input can't blow up the query.
    """
    query = (query or "").strip()
    if not query:
        return ""
    tokens = re.findall(r'"[^"]*"|\S+', query)
    out = []
    for tok in tokens:
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            out.append(tok)
        elif tok.endswith("*"):
            stem = tok[:-1]
            out.append(re.escape(stem) + "*")
        else:
            # keep AND/OR/NOT as-is, escape the rest
            if tok.upper() in ("AND", "OR", "NOT"):
                out.append(tok.upper())
            else:
                out.append(re.escape(tok))
    return " ".join(out) if out else ""


# ------------------------------------------------------------ CRUD wrappers

def _doc_fields(doc: dict) -> dict:
    """Map an application-level doc dict onto the Whoosh schema."""
    ts = float(doc.get("mtime") or 0)
    modified = _as_datetime(ts) or datetime.utcfromtimestamp(0)
    return {
        "id": int(doc.get("id") or 0),
        "path": doc["path"],
        "title": doc.get("title", ""),
        "content": doc.get("content", ""),
        "doc_type": doc.get("doc_type", "other"),
        "category": doc.get("category", "uncategorized"),
        "tags": ", ".join(doc.get("tags", [])),
        "mtime": ts,
        "modified": modified,
        "size": int(doc.get("size") or 0),
        "added_at": doc.get("added_at", ""),
    }


def add_document(doc: dict) -> None:
    """doc keys: path, title, content, doc_type, category, tags, mtime, size, id."""
    with _write_lock:
        ix = get_index()
        with AsyncWriter(ix) as w:
            w.add_document(**_doc_fields(doc))


def update_document(doc: dict) -> None:
    with _write_lock:
        ix = get_index()
        with AsyncWriter(ix) as w:
            w.delete_by_term("path", doc["path"])
            w.add_document(**_doc_fields(doc))


def delete_document(path: str) -> None:
    with _write_lock:
        ix = get_index()
        with AsyncWriter(ix) as w:
            w.delete_by_term("path", path)


def delete_all() -> None:
    with _write_lock:
        ix = get_index()
        with AsyncWriter(ix) as w:
            w.delete_all()


# -------------------------------------------------------------------- search

def search(query: str, *, doc_type: str | None = None, category: str | None = None,
           tag: str | None = None, date_from: str | None = None,
           date_to: str | None = None, path_prefix: str | None = None,
           page: int = 1, page_size: int | None = None,
           include_banned: bool = True,
           highlight_terms: list[str] | None = None) -> dict:
    """Run a search; returns dict(results, total, page, page_size, ...).

    Every filter is optional. ``highlight_terms`` additionally marks up the
    snippet with <mark> tags for the result previews.
    """
    page = max(1, int(page or 1))
    page_size = int(page_size or CONFIG["PAGE_SIZE"])
    page_size = max(1, min(page_size, 200))

    ix = get_index()
    searcher = ix.searcher()

    nq = normalize_query(query)
    if nq:
        parser = QueryParser("content", schema=ix.schema)
        try:
            q = parser.parse(nq)
        except Exception:  # noqa: BLE001 - fall back to a term-only query
            q = Every("content")
    else:
        q = Every("content")

    filters = []
    if doc_type:
        filters.append(Term("doc_type", doc_type))
    if category:
        filters.append(Term("category", category))
    if tag:
        filters.append(Term("tags", tag.lower()))
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to)
    if dt_from or dt_to:
        filters.append(DateRange("modified", dt_from, dt_to))
    if path_prefix:
        from whoosh.query import Prefix
        filters.append(Prefix("path", path_prefix))

    final_q = q
    if filters:
        from whoosh.query import And as QAnd
        final_q = QAnd([q] + filters)

    try:
        paged = searcher.search_page(final_q, page, pagelen=page_size)
        hits = list(paged)
        total = paged.total
    except Exception as e:  # noqa: BLE001
        log.exception("search failed")
        return {"results": [], "total": 0, "page": page, "page_size": page_size,
                "error": str(e), "query": query}

    results = []
    for hit in hits:
        snippet_src = hit.get("content", "")
        snippet = snippet_src[:250]
        try:
            if nq:
                snippet = _hl.highlight_hit(hit, "content") or snippet
        except Exception:  # noqa: BLE001
            snippet = snippet_src[:250]

        tags_val = hit.get("tags", "") or ""
        results.append({
            "id": hit.get("id"),
            "path": hit.get("path", ""),
            "title": hit.get("title", "") or Path(hit.get("path", "")).name,
            "doc_type": hit.get("doc_type", "other"),
            "category": hit.get("category", "uncategorized"),
            "tags": [t.strip() for t in tags_val.split(",") if t.strip()],
            "mtime": hit.get("mtime", 0),
            "size": hit.get("size", 0),
            "snippet": snippet,
        })

    searcher.close()
    return {
        "results": results,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "query": query,
        "error": None,
    }


def stats() -> dict:
    """Index statistics for the dashboard."""
    try:
        ix = get_index()
        with ix.searcher() as s:
            return {"doc_count": s.doc_count()}
    except Exception:  # noqa: BLE001
        return {"doc_count": 0}


def _parse_date(value: str | None):
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
