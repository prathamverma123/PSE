"""Configuration for the personal search engine.

Everything runs locally: no cloud services, no telemetry, no outbound
connections (except URL crawls you explicitly start). Defaults can be
overridden with a ``config.yaml`` file next to this module and/or ``PSE_*``
environment variables. See README.md for all knobs.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
YAML_PATH = BASE_DIR / "config.yaml"
ENV_PREFIX = "PSE_"

DEFAULTS = {
    # server
    "HOST": "127.0.0.1",
    "PORT": 5000,
    "DEBUG": False,
    # storage (all local; paths derived from DATA_DIR when empty)
    "DATA_DIR": str(BASE_DIR / "data"),
    "SECRET_KEY_FILE": "",
    "DB_PATH": "",
    "INDEX_DIR": "",
    "BACKUP_DIR": "",
    # access control
    "ALLOW_REMOTE": False,
    "SESSION_IDLE_TIMEOUT": 30 * 60,      # seconds of inactivity before logout
    "SESSION_LIFETIME": 12 * 60 * 60,     # absolute session lifetime (seconds)
    # crawling / indexing
    "ALLOWED_ROOTS": [],                  # empty list = any local directory allowed
    "SKIP_DIRS": [
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".cache", ".thumbnails", ".idea",
        ".vscode", "$RECYCLE.BIN", "System Volume Information", "AppData",
    ],
    "SKIP_FILE_PATTERNS": [
        "Thumbs.db", "desktop.ini", ".DS_Store", "~$*", "*.tmp", "*.bak",
        "*.crdownload", "*.part", "*.lnk",
    ],
    "MAX_TEXT_CHARS": 200_000,
    "MAX_PDF_PAGES": 500,
    "LARGE_FILE_HASH_LIMIT": 64 * 1024 * 1024,   # full hash under, partial above
    "HASH_WINDOW": 512 * 1024,
    "ENABLED_PARSERS": {
        "pdf": True, "docx": True, "text": True,
        "image": True, "media": True, "web": True,
    },
    # URL crawling
    "URL_MAX_DEPTH": 2,
    "URL_MAX_PAGES": 100,
    "URL_SAME_HOST_ONLY": True,
    "CRAWL_DELAY": 0.5,
    "URL_TIMEOUT": 15,
    "USER_AGENT": "personal-search-engine/1.0 (local)",
    # misc
    "PAGE_SIZE": 20,
    "ENCRYPT_CONTENT": False,
    "ENABLE_FILE_WATCHER": False,
    "MAX_CONTENT_LENGTH": 1024 * 1024 * 1024,  # upload cap (backup restore)
}

_LIST_KEYS = {"ALLOWED_ROOTS", "SKIP_DIRS", "SKIP_FILE_PATTERNS"}
_BOOL_KEYS = {
    "ALLOW_REMOTE", "ENCRYPT_CONTENT", "ENABLE_FILE_WATCHER", "DEBUG",
    "URL_SAME_HOST_ONLY",
}
_FLOAT_KEYS = {"CRAWL_DELAY", "URL_TIMEOUT"}
_INT_KEYS = {
    "PORT", "SESSION_IDLE_TIMEOUT", "SESSION_LIFETIME", "MAX_TEXT_CHARS",
    "MAX_PDF_PAGES", "URL_MAX_DEPTH", "URL_MAX_PAGES", "PAGE_SIZE",
    "MAX_CONTENT_LENGTH", "LARGE_FILE_HASH_LIMIT", "HASH_WINDOW",
}


def _coerce(key: str, value):
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key in _INT_KEYS:
        return int(float(value))
    if key in _FLOAT_KEYS:
        return float(value)
    if key in _LIST_KEYS:
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [p.strip() for p in str(value).replace(";", ",").split(",") if p.strip()]
    return value


def _merge_yaml(cfg: dict) -> None:
    if not YAML_PATH.exists():
        return
    with open(YAML_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise RuntimeError("config.yaml must contain a mapping at the top level")
    for key, value in data.items():
        key = str(key).upper()
        if key == "ENABLED_PARSERS" and isinstance(value, dict):
            cfg["ENABLED_PARSERS"] = dict(cfg["ENABLED_PARSERS"])
            cfg["ENABLED_PARSERS"].update(value)
        elif key in cfg:
            cfg[key] = _coerce(key, value)


def _merge_env(cfg: dict) -> None:
    for key in list(cfg.keys()):
        env = os.environ.get(ENV_PREFIX + key)
        if env is not None and env != "":
            cfg[key] = _coerce(key, env)


def _derive_paths(cfg: dict) -> None:
    data_dir = Path(cfg["DATA_DIR"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = (BASE_DIR / data_dir).resolve()
    cfg["DATA_DIR"] = str(data_dir)
    if not cfg.get("SECRET_KEY_FILE"):
        cfg["SECRET_KEY_FILE"] = str(data_dir / "secret.key")
    if not cfg.get("DB_PATH"):
        cfg["DB_PATH"] = str(data_dir / "search.db")
    if not cfg.get("INDEX_DIR"):
        cfg["INDEX_DIR"] = str(data_dir / "index")
    if not cfg.get("BACKUP_DIR"):
        cfg["BACKUP_DIR"] = str(data_dir / "backups")


def load_config(**overrides) -> dict:
    """Build the effective config: defaults < config.yaml < env < overrides."""
    cfg = dict(DEFAULTS)
    cfg["ENABLED_PARSERS"] = dict(DEFAULTS["ENABLED_PARSERS"])
    _merge_yaml(cfg)
    _merge_env(cfg)
    for key, value in overrides.items():
        cfg[key] = value
    _derive_paths(cfg)
    return cfg
