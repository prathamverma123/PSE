"""Personal Search Engine — Flask application factory.

Everything runs on 127.0.0.1 on your machine. No cloud services, no
telemetry, no outbound requests (except URL crawls you start yourself).
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, render_template
from flask_login import current_user

import auth as auth_module
import crawler
import crypto
import database
import search_index
from config import load_config

# routes blueprints
from routes.api import bp as api_bp
from routes.backup import bp as backup_bp
from routes.manage import bp as manage_bp
from routes.search import bp as search_bp


def create_app(config_overrides: dict | None = None) -> Flask:
    cfg = load_config(**(config_overrides or {}))

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_load_secret_key(cfg),
        DEBUG=bool(cfg["DEBUG"]),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=cfg["SESSION_LIFETIME"]),
        SESSION_IDLE_TIMEOUT=cfg["SESSION_IDLE_TIMEOUT"],
        SESSION_LIFETIME=cfg["SESSION_LIFETIME"],
        ALLOW_REMOTE=bool(cfg["ALLOW_REMOTE"]),
        MAX_CONTENT_LENGTH=cfg["MAX_CONTENT_LENGTH"],
        # expose the rest of the knobs to routes via app.config
        DATA_DIR=cfg["DATA_DIR"],
        DB_PATH=cfg["DB_PATH"],
        INDEX_DIR=cfg["INDEX_DIR"],
        BACKUP_DIR=cfg["BACKUP_DIR"],
        PAGE_SIZE=cfg["PAGE_SIZE"],
        ENABLE_FILE_WATCHER=cfg["ENABLE_FILE_WATCHER"],
    )

    # storage dirs
    for key in ("DATA_DIR", "INDEX_DIR", "BACKUP_DIR"):
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)

    # ensure DB exists, and propagate the effective config to the modules
    database.CONFIG.update(cfg)
    database.DB_PATH = cfg["DB_PATH"]
    database.init_db()
    search_index.CONFIG.update(cfg)
    search_index.INDEX_DIR = Path(cfg["INDEX_DIR"])
    crypto.CONFIG = cfg
    crypto.ENCRYPT_CONTENT = bool(cfg.get("ENCRYPT_CONTENT"))
    crypto.DATA_DIR = Path(cfg["DATA_DIR"])
    if cfg.get("ENCRYPT_CONTENT"):
        crypto.unpack_index_on_startup(cfg["INDEX_DIR"])

    # optional file watcher
    if cfg.get("ENABLE_FILE_WATCHER"):
        try:
            import watcher
            watcher.start()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("file watcher failed to start: %s", e)

    # security: init DB + index dirs, register blueprints and auth guards
    auth_module.register_auth(app)
    app.register_blueprint(search_bp)
    app.register_blueprint(manage_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(api_bp)

    # dashboard route (endpoint referenced by auth.py login)
    @app.route("/dashboard")
    @auth_module.login_required
    def dashboard():
        stats = database.dump_stats()
        try:
            index_stats = search_index.stats()
        except Exception:  # noqa: BLE001
            index_stats = {"doc_count": 0}
        stats["index"] = index_stats
        jobs = database.latest_jobs(5)
        return render_template("dashboard.html", stats=stats, jobs=jobs)

    # ---- Jinja helpers used by the templates ----------------------------
    from datetime import datetime as _dt

    @app.template_filter("datetimeformat")
    def _datetimeformat(ts, fmt="%Y-%m-%d %H:%M"):
        try:
            if not ts:
                return ""
            return _dt.fromtimestamp(float(ts)).strftime(fmt)
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    @app.context_processor
    def _template_globals():
        return {
            "d_tags": database.tags_for_document,
            "all_tags": database.all_tags_with_counts,
        }

    @app.errorhandler(404)
    def not_found(e):
        if request_wants_json():
            from flask import jsonify
            return jsonify({"error": "not found"}), 404
        return render_template("error.html", code=404, message="Not found"), 404
    @app.errorhandler(500)
    def server_error(e):
        if request_wants_json():
            from flask import jsonify
            return jsonify({"error": "internal error"}), 500
        error = "Internal server error"
        return render_template("error.html", code=500, message=error), 500

    def request_wants_json():
        from flask import request
        return (request.path.startswith("/api/") or
                "application/json" in (request.headers.get("Accept") or ""))

    # shutdown hook: encrypt index when ENCRYPT_CONTENT is on
    @app.teardown_appcontext
    def _teardown(exc):
        pass

    return app


def _load_secret_key(cfg: dict) -> bytes:
    """Load (or create) the per-install secret key file."""
    path = Path(cfg["SECRET_KEY_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes().strip()
        if key:
            return key
    key = secrets.token_hex(32).encode()
    path.write_bytes(key)
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


# ------------------------------------------------------------------ Flask CLI

def create_app_for_cli():
    return create_app()


@click.group()
def cli():  # pragma: no cover
    """Management commands."""


@cli.command("create-user")
@click.option("--username", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
def create_user_command(username, password):  # pragma: no cover
    app = create_app()
    with app.app_context():
        if database.count_users() == 0:
            database.create_user(username, password)
            click.echo(f"User '{username}' created.")
        else:
            if database.set_password(username, password):
                click.echo(f"Password updated for '{username}'.")
            else:
                database.create_user(username, password)
                click.echo(f"User '{username}' created.")


@cli.command("reindex")
def reindex_command():  # pragma: no cover
    """Rebuild the full-text index from the SQLite documents + cache."""
    app = create_app()
    with app.app_context():
        rows = database.list_documents(limit=10**9)
        search_index.rebuild_index()
        n = 0
        for row in rows:
            content = database.read_cache(row["path"]) or ""
            search_index.add_document({
                "id": row["id"], "path": row["path"], "title": row["title"],
                "content": content, "doc_type": row["doc_type"],
                "category": row["category"], "tags": database.tags_for_document(row["id"]),
                "mtime": row["mtime"], "size": row["size"], "added_at": row["added_at"]})
            n += 1
        click.echo(f"Re-indexed {n} documents.")


def register_cli(app: Flask) -> None:
    app.cli.add_command(create_user_command)
    app.cli.add_command(reindex_command)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app()
    register_cli(app)
    cfg = app.config
    app.run(host=cfg.get("HOST", "127.0.0.1"), port=int(cfg.get("PORT", 5000)),
            debug=False)
