"""Backup & restore: zip the SQLite DB + Whoosh index to a local file."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, current_app, flash, redirect, render_template,
                  request, send_file, url_for)
from flask_login import login_required

import database
import search_index

log = logging.getLogger("pse.backup")

bp = Blueprint("backup", __name__)

CONFIG = current_app.config if current_app else None


@bp.route("/backup")
@login_required
def backup_page():
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    backups = []
    if backup_dir.exists():
        for p in sorted(backup_dir.glob("*.zip"), reverse=True):
            backups.append({"name": p.name, "size": p.stat().st_size,
                            "mtime": p.stat().st_mtime})
    db_path = Path(current_app.config["DB_PATH"])
    return render_template(
        "backup.html", backups=backups,
        db_exists=db_path.exists(), db_size=db_path.stat().st_size if db_path.exists() else 0)


@bp.route("/backup/create", methods=["POST"])
@login_required
def backup_create():
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"pse-backup-{stamp}.zip"
    try:
        make_backup_zip(target)
        flash(f"Backup created: {target.name} ({target.stat().st_size:,} bytes).",
              "success")
    except Exception as e:  # noqa: BLE001
        log.exception("backup failed")
        flash(f"Backup failed: {e}", "error")
    return redirect(url_for("backup.backup_page"))


@bp.route("/backup/download/<path:name>")
@login_required
def backup_download(name: str):
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    p = (backup_dir / name).resolve()
    try:
        p.relative_to(backup_dir.resolve())
    except ValueError:
        flash("Invalid backup name.", "error")
        return redirect(url_for("backup.backup_page"))
    if not p.is_file():
        flash("Backup not found.", "error")
        return redirect(url_for("backup.backup_page"))
    return send_file(p, as_attachment=True, download_name=p.name)


@bp.route("/backup/restore", methods=["POST"])
@login_required
def backup_restore():
    upload = request.files.get("backup")
    src = None
    if upload and upload.filename:
        suffix = Path(upload.filename).suffix.lower()
        tmpdir = Path(tempfile.mkdtemp(prefix="pse_restore_"))
        dest = tmpdir / "upload.zip"
        upload.save(dest)
        src = dest
    else:
        path = (request.form.get("path") or "").strip()
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                flash(f"Backup file not found: {path}", "error")
                return redirect(url_for("backup.backup_page"))
            src = p
    if src is None:
        flash("Upload a backup zip or enter a path to one.", "error")
        return redirect(url_for("backup.backup_page"))
    try:
        n = restore_backup_zip(src)
        flash(f"Restore complete: {n} document rows restored. "
              f"Re-crawl to rebuild full text index if results look thin.",
              "success")
    except zipfile.BadZipFile as e:
        flash(f"Invalid backup zip: {e}", "error")
    except sqlite3.DatabaseError as e:
        flash(f"Database restore failed: {e}", "error")
    except Exception as e:  # noqa: BLE001
        log.exception("restore failed")
        flash(f"Restore failed: {e}", "error")
    return redirect(url_for("backup.backup_page"))


def make_backup_zip(target: Path) -> int:
    """Write DB (via sqlite3 backup API for consistency) + index into a zip."""
    db_path = Path(current_app.config["DB_PATH"])
    index_dir = Path(current_app.config["INDEX_DIR"])
    wrote = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        if db_path.exists():
            tmp = Path(tempfile.gettempdir()) / f"pse_db_{os.getpid()}.sqlite"
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(str(tmp))
            with src:
                src.backup(dst)
            src.close()
            dst.close()
            zf.write(tmp, "database.sqlite")
            wrote += 1
            tmp.unlink(missing_ok=True)
        if index_dir.exists():
            for p in index_dir.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(index_dir.parent).as_posix())
                    wrote += 1
    return wrote


def restore_backup_zip(src: Path) -> int:
    """Restore DB from the zip; rebuild index from the zip when present."""
    db_path = Path(current_app.config["DB_PATH"])
    index_dir = Path(current_app.config["INDEX_DIR"])
    with zipfile.ZipFile(src) as zf:
        names = zf.namelist()
        if "database.sqlite" not in names:
            raise ValueError("backup zip does not contain database.sqlite")
        # 1) DB restore via temp + atomic replace
        tmpdir = Path(tempfile.mkdtemp(prefix="pse_restore_db_"))
        tmp_db = tmpdir / "database.sqlite"
        zf.extract("database.sqlite", tmpdir)
        # sanity: can we open it?
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.replace(tmp_db.with_suffix(".bak"))
        shutil.copy2(tmp_db, db_path)
        n_docs = None
        try:
            conn = sqlite3.connect(str(db_path))
            n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            conn.close()
        except sqlite3.DatabaseError:
            pass
        # 2) index restore
        prefix = index_dir.name + "/"
        idx_files = [nm for nm in names if nm.startswith(prefix)]
        if idx_files:
            if index_dir.exists():
                shutil.rmtree(index_dir, ignore_errors=True)
            index_dir.mkdir(parents=True, exist_ok=True)
            for nm in idx_files:
                dest = index_dir.parent / nm
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(nm) as fh, open(dest, "wb") as out:
                    shutil.copyfileobj(fh, out)
        else:
            # rebuild the empty index so searches work
            search_index.rebuild_index()
        search_index.get_index()  # touch to create if needed
        return n_docs or 0
