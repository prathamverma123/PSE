"""Directory and URL crawling with incremental change detection.

- ``crawl_directory(root)`` recursive os.walk with skip-list + content-hash
  change detection, so re-crawls only touch changed files.
- ``crawl_url(url, depth)`` fetches a page, extracts text, and follows
  same-host links up to the configured depth/page limits.

Crawls run in a background thread; progress is written to the crawl_jobs
table so the UI can poll /crawl/status. No data ever leaves the machine
except the pages YOU explicitly ask to fetch.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import database
import extractors
import search_index

from config import load_config

log = logging.getLogger("pse.crawler")

CONFIG = load_config()

# in-process registry of live threads (jobs also live in the DB)
_threads: dict[int, threading.Thread] = {}
_registry_lock = threading.Lock()


# ------------------------------------------------------------------ helpers

def path_allowed(path: str) -> bool:
    """True if the path is allowed to be indexed per ALLOWED_ROOTS."""
    roots = CONFIG.get("ALLOWED_ROOTS") or []
    if not roots:
        return True
    p = Path(path).resolve()
    for root in roots:
        r = Path(root).expanduser().resolve()
        try:
            p.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def is_skipped_dir(name: str) -> bool:
    return name in set(CONFIG["SKIP_DIRS"])


def is_skipped_file(name: str) -> bool:
    for pat in CONFIG["SKIP_FILE_PATTERNS"]:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def file_hash(path: Path) -> str:
    """Content hash: full for small files, windowed (head+tail) for huge ones."""
    h = hashlib.sha256()
    try:
        size = path.stat().st_size
        if size <= CONFIG["LARGE_FILE_HASH_LIMIT"]:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
        else:
            window = CONFIG["HASH_WINDOW"]
            with path.open("rb") as fh:
                h.update(fh.read(window))
                fh.seek(max(0, size - window))
                h.update(fh.read(window))
            h.update(str(size).encode())
    except OSError:
        return ""
    return h.hexdigest()


# ---------------------------------------------------------- index single file

def index_file(path: str, source: str = "manual", job_id: int | None = None) -> str:
    """Index a single file. Returns 'indexed', 'unchanged', 'skipped', or 'error:<msg>'."""
    p = Path(path)
    try:
        if not p.exists() or not p.is_file():
            return "error:notfound"
        if not path_allowed(str(p)):
            return "error:forbidden-path"
        if is_skipped_file(p.name):
            return "skipped"
        stat = p.stat()
        dtype, title, text, meta = extractors.extract(p)
        content_hash = file_hash(p)
        row = database.get_document_by_path(str(p))
        if row is not None and row["content_hash"] and content_hash \
                and row["content_hash"] == content_hash:
            return "unchanged"
        doc_id = database.upsert_document({
            "path": str(p), "source": source, "doc_type": dtype,
            "title": title, "size": stat.st_size,
            "mtime": stat.st_mtime, "ctime": stat.st_ctime,
            "content_hash": content_hash,
        })
        database.cache_content(str(p), text, False)
        database.add_tag(doc_id, dtype)
        search_index.add_document({
            "id": doc_id, "path": str(p), "title": title, "content": text,
            "doc_type": dtype,
            "category": row["category"] if row else "uncategorized",
            "tags": database.tags_for_document(doc_id),
            "mtime": stat.st_mtime, "size": stat.st_size,
            "added_at": row["added_at"] if row else "",
        })
        return "indexed"
    except Exception as e:  # noqa: BLE001
        log.exception("index_file failed: %s", path)
        if job_id is not None:
            database.update_job(job_id, errors=1, message=f"{path}: {e}")
        return f"error:{e}"


def collect_files(root: Path) -> list[Path]:
    """Recursive walk with skip rules applied."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d)]
        for name in filenames:
            if is_skipped_file(name):
                continue
            out.append(Path(dirpath) / name)
    return out


def index_directory_sync(root: str, job_id: int | None = None) -> dict:
    """Walk a directory and index every candidate file. Returns counters."""
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(root)
    if not path_allowed(str(root_path)):
        raise PermissionError(f"Path outside ALLOWED_ROOTS: {root}")
    files = collect_files(root_path)
    stats = {"total": len(files), "indexed": 0, "skipped": 0,
             "unchanged": 0, "errors": 0}
    if job_id is not None:
        database.update_job(job_id, total=len(files))
    for i, f in enumerate(files):
        result = index_file(str(f), source="crawl", job_id=job_id)
        if result == "indexed":
            stats["indexed"] += 1
        elif result in ("unchanged", "skipped"):
            stats["skipped"] += 1
        else:
            stats["errors"] += 1
        if job_id is not None and (i + 1) % 5 == 0:
            database.update_job(
                job_id, processed=i + 1, indexed=stats["indexed"],
                skipped=stats["skipped"], errors=stats["errors"])
    if job_id is not None:
        database.update_job(
            job_id, processed=len(files), status="done",
            indexed=stats["indexed"], skipped=stats["skipped"],
            errors=stats["errors"],
            message=f"done: {stats['indexed']} indexed, "
                    f"{stats['skipped']} unchanged/skipped, {stats['errors']} errors")
    return stats


# ------------------------------------------------------------------ URL crawl

def crawl_url_sync(url: str, depth: int | None = None,
                   job_id: int | None = None) -> dict:
    """Fetch a URL and follow same-host links (BFS, bounded)."""
    import requests
    from bs4 import BeautifulSoup

    max_depth = int(depth if depth is not None else CONFIG["URL_MAX_DEPTH"])
    max_pages = int(CONFIG["URL_MAX_PAGES"])
    delay = float(CONFIG["CRAWL_DELAY"])
    timeout = float(CONFIG["URL_TIMEOUT"])
    same_host_only = bool(CONFIG["URL_SAME_HOST_ONLY"])

    stats = {"visited": 0, "indexed": 0, "errors": 0}
    seen: set[str] = set()
    frontier: list[tuple[str, int]] = [(url, 0)]
    origin_host = urllib.parse.urlparse(url).netloc
    session = requests.Session()
    session.headers.update({"User-Agent": CONFIG["USER_AGENT"]})

    if job_id is not None:
        database.update_job(job_id, total=max_pages)

    while frontier and stats["visited"] < max_pages:
        current, d = frontier.pop(0)
        norm = _normalize_url(current)
        if norm in seen or d > max_depth:
            continue
        seen.add(norm)
        stats["visited"] += 1
        if job_id is not None:
            database.update_job(job_id, processed=stats["visited"],
                                indexed=stats["indexed"],
                                errors=stats["errors"])
        try:
            resp = session.get(current, timeout=timeout, stream=False)
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" in ctype or "application/xhtml" in ctype:
                html = resp.text
                title, text = extractors.extract_html_string(html, current)
                upsert_url_doc(current, title, text, len(html))
                stats["indexed"] += 1
                if d < max_depth:
                    for link, _ in _extract_links(html, current)[:50]:
                        if _normalize_url(link) in seen:
                            continue
                        if same_host_only and \
                                urllib.parse.urlparse(link).netloc != origin_host:
                            continue
                        frontier.append((link, d + 1))
            elif ctype.split("/")[0] in ("image", "audio", "video", "application") \
                    and "json" not in ctype:
                ext = os.path.splitext(urllib.parse.urlparse(current).path)[1]
                if not ext or len(ext) > 6:
                    ext = ".bin"
                tmp = Path(tempfile.gettempdir()) / (
                    f"pse_url_{stats['visited']}_{int(time.time())}{ext}")
                tmp.write_bytes(resp.content)
                res = index_file(str(tmp), source="url", job_id=job_id)
                tmp.unlink(missing_ok=True)
                if res == "indexed":
                    stats["indexed"] += 1
            else:
                pass  # unsupported content type — skip
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            if job_id is not None:
                database.update_job(job_id, errors=stats["errors"],
                                    message=f"{current[:120]}: {e}")
        if delay > 0:
            time.sleep(delay)

    if job_id is not None:
        database.update_job(job_id, status="done",
                            processed=stats["visited"],
                            indexed=stats["indexed"],
                            errors=stats["errors"],
                            message=f"done: {stats['indexed']} pages indexed, "
                                    f"{stats['errors']} errors")
    return stats


def upsert_url_doc(url: str, title: str, text: str, size: int) -> int:
    norm = _normalize_url(url)
    content_hash = hashlib.sha256((title + text).encode("utf-8", "replace")).hexdigest()
    row = database.get_document_by_path(norm)
    now = time.time()
    doc_id = database.upsert_document({
        "path": norm, "source": "url", "doc_type": "web",
        "title": title, "size": size, "mtime": now, "ctime": now,
        "content_hash": content_hash,
    })
    database.cache_content(norm, text, False)
    database.add_tag(doc_id, "web")
    search_index.add_document({
        "id": doc_id, "path": norm, "title": title, "content": text,
        "doc_type": "web",
        "category": row["category"] if row else "uncategorized",
        "tags": database.tags_for_document(doc_id),
        "mtime": now, "size": size,
        "added_at": row["added_at"] if row else "",
    })
    return doc_id


def _normalize_url(url: str) -> str:
    p = urllib.parse.urlparse(url.strip())
    host = p.netloc.lower().rstrip(".")
    path = p.path or "/"
    return urllib.parse.urlunparse((p.scheme.lower(), host, path, "", "", ""))


def _extract_links(html: str, base: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(base, href)
        if absolute.startswith(("http://", "https://")):
            out.append((absolute, a.get_text(" ", strip=True)))
    return out


# ------------------------------------------------------------- thread runner

def start_directory_crawl(root: str) -> int:
    job_id = database.create_job("directory", root)
    t = threading.Thread(target=_run_directory_job, args=(root, job_id),
                         daemon=True, name=f"crawl-dir-{job_id}")
    with _registry_lock:
        _threads[job_id] = t
    t.start()
    return job_id


def start_url_crawl(url: str, depth: int | None = None) -> int:
    job_id = database.create_job("url", url)
    t = threading.Thread(target=_run_url_job, args=(url, depth, job_id),
                         daemon=True, name=f"crawl-url-{job_id}")
    with _registry_lock:
        _threads[job_id] = t
    t.start()
    return job_id


def _run_directory_job(root: str, job_id: int) -> None:
    try:
        index_directory_sync(root, job_id=job_id)
    except Exception as e:  # noqa: BLE001
        log.exception("directory crawl failed")
        database.update_job(job_id, status="error", message=str(e)[:400])


def _run_url_job(url: str, depth: int | None, job_id: int) -> None:
    try:
        crawl_url_sync(url, depth=depth, job_id=job_id)
    except Exception as e:  # noqa: BLE001
        log.exception("url crawl failed")
        database.update_job(job_id, status="error", message=str(e)[:400])


def job_is_running(job_id: int) -> bool:
    t = _threads.get(job_id)
    return bool(t and t.is_alive())


def wait_for_jobs(timeout: float = 60.0) -> None:
    """Block until background crawl threads finish (used by tests/shutdown)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _registry_lock:
            alive = [t for t in _threads.values() if t.is_alive()]
        if not alive:
            return
        alive[0].join(timeout=max(0.1, deadline - time.time()))
