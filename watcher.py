"""Optional live file watcher (watchdog).

When ENABLE_FILE_WATCHER=true in config, newly added / changed files under
the ALLOWED_ROOTS are picked up automatically within a few seconds, and
deleted files are dropped from the index. Nothing is ever transmitted
anywhere — this only touches your local files and your local index.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from config import load_config

log = logging.getLogger("pse.watcher")

CONFIG = load_config()
_started = False
_lock = threading.Lock()


def _handle(path: str) -> None:
    try:
        import crawler
        import database
        import search_index
        p = Path(path)
        if not p.is_file():
            return
        if not crawler.path_allowed(path):
            return
        res = crawler.index_file(path, source="crawl")
        if res == "indexed":
            log.info("watcher indexed %s", path)
        elif res.startswith("error"):
            log.warning("watcher failed on %s: %s", path, res)
    except Exception as e:  # noqa: BLE001
        log.exception("watcher handler failed: %s", e)


def _handle_delete(path: str) -> None:
    try:
        import database
        import search_index
        row = database.get_document_by_path(path)
        if row is not None:
            database.delete_document(row["id"])
            search_index.delete_document(path)
            log.info("watcher removed %s", path)
    except Exception as e:  # noqa: BLE001
        log.exception("watcher delete failed: %s", e)


def start(roots: list[str] | None = None) -> bool:
    """Start the watchdog observers. Returns True when running."""
    global _started
    with _lock:
        if _started:
            return True
        roots = roots or (CONFIG.get("ALLOWED_ROOTS") or [])
        if not roots:
            log.warning("ENABLE_FILE_WATCHER is on but ALLOWED_ROOTS is empty "
                        "— nothing to watch.")
            return False
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.error("watchdog is not installed; cannot start file watcher.")
            return False

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    _handle(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    _handle(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    _handle_delete(event.src_path)
                    _handle(event.dest_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    _handle_delete(event.src_path)

        observer = Observer()
        for root in roots:
            r = Path(root).expanduser()
            if r.exists():
                observer.schedule(Handler(), str(r), recursive=True)
                log.info("watching %s", r)
            else:
                log.warning("watch root does not exist: %s", r)
        observer.daemon = True
        observer.start()
        _started = True
        log.info("file watcher started")
        return True


def stop() -> None:  # pragma: no cover
    global _started
    with _lock:
        _started = False
