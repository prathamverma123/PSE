"""Optional at-rest encryption for extracted text using Fernet (cryptography).

When ENCRYPT_CONTENT is enabled in config, every extracted text blob is
encrypted before it touches disk (SQLite content_cache) and the Whoosh
index directory is transparently encrypted at shutdown / decrypted at
startup. The key lives in a file next to the database, owned by you —
if you delete it, the encrypted data is gone for good.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import load_config

log = logging.getLogger("pse.crypto")

CONFIG = load_config()
ENCRYPT_CONTENT = bool(CONFIG.get("ENCRYPT_CONTENT", False))
DATA_DIR = Path(CONFIG["DATA_DIR"])
KEY_FILE = DATA_DIR / "encryption.key"

_fernet: Fernet | None = None


def _load_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes().strip()
        try:
            _fernet = Fernet(key)
            return _fernet
        except (ValueError, TypeError):
            # Key file present but invalid — DO NOT silently destroy data.
            log.error("encryption.key is invalid; refusing to continue with it.")
            raise RuntimeError("Invalid encryption key file: %s" % KEY_FILE)
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    _fernet = Fernet(key)
    log.info("Generated new encryption key at %s", KEY_FILE)
    return _fernet


def encrypt_text(text: str) -> bytes:
    """Encrypt a UTF-8 string into a Fernet token (bytes)."""
    if not ENCRYPT_CONTENT:
        return text.encode("utf-8")
    return _load_fernet().encrypt(text.encode("utf-8"))


def decrypt_bytes(blob: bytes) -> bytes:
    """Decrypt a Fernet token; returns original bytes when not encrypted."""
    if not ENCRYPT_CONTENT:
        return blob
    return _load_fernet().decrypt(blob)


def decrypt_text(blob: bytes) -> str:
    return decrypt_bytes(blob).decode("utf-8", "replace")


def is_encryption_enabled() -> bool:
    return ENCRYPT_CONTENT


# ------------------------------------------------- index directory encryption
# A simple mount-style wrapper: on shutdown, zip+encrypt the index into a single
# .enc file; on startup, decrypt+unpack it. Only files under INDEX_DIR are touched.

ENC_FILE = DATA_DIR / "index.enc"


def pack_index_on_shutdown(index_dir: str | Path) -> None:
    """Encrypt the whole index directory into ENC_FILE, then remove plaintext."""
    if not ENCRYPT_CONTENT:
        return
    src = Path(index_dir)
    if not src.exists():
        return
    import io
    import zipfile

    fernet = _load_fernet()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    token = fernet.encrypt(buf.getvalue())
    tmp = ENC_FILE.with_suffix(".enc.tmp")
    tmp.write_bytes(token)
    tmp.replace(ENC_FILE)
    shutil.rmtree(src, ignore_errors=True)
    log.info("Index packed and encrypted into %s", ENC_FILE)


def unpack_index_on_startup(index_dir: str | Path) -> bool:
    """Restore an encrypted index back to disk before the app opens it."""
    if not ENCRYPT_CONTENT:
        return False
    dst = Path(index_dir)
    if not ENC_FILE.exists():
        return False
    import io
    import zipfile

    fernet = _load_fernet()
    try:
        raw = fernet.decrypt(ENC_FILE.read_bytes())
    except InvalidToken:
        log.error("Cannot decrypt %s — wrong key?", ENC_FILE)
        return False
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(dst)
    ENC_FILE.unlink(missing_ok=True)
    log.info("Index decrypted back to %s", dst)
    return True


def main(argv: list[str] | None = None) -> int:
    """Small CLI: python crypto.py pack|unpack — manual maintenance."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python crypto.py pack|unpack")
        return 2
    if not ENCRYPT_CONTENT:
        print("ENCRYPT_CONTENT is disabled in config — nothing to do.")
        return 0
    if argv[0] == "pack":
        pack_index_on_shutdown(CONFIG["INDEX_DIR"])
        print("packed ->", ENC_FILE)
        return 0
    if argv[0] == "unpack":
        ok = unpack_index_on_startup(CONFIG["INDEX_DIR"])
        print("unpacked" if ok else "nothing to unpack")
        return 0 if ok else 1
    print("usage: python crypto.py pack|unpack")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
