"""JSON API endpoints mirroring the UI routes, for external local tools.

All endpoints require either a logged-in session or an ``X-Api-Token``
header. Tokens are per-user and can be generated on the settings page.
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user

import crawler
import database
import search_index
from auth import api_auth_required

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/search")
@api_auth_required
def api_search():
    q = (request.args.get("q") or "").strip()
    page = _to_int(request.args.get("page"), 1)
    page_size = _to_int(request.args.get("page_size"), 20)
    res = search_index.search(
        q,
        doc_type=request.args.get("type") or None,
        category=request.args.get("category") or None,
        tag=request.args.get("tag") or None,
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        page=page, page_size=page_size,
    )
    return jsonify(res)


@bp.route("/documents")
@api_auth_required
def api_documents():
    rows = database.list_documents(
        category=request.args.get("category") or None,
        doc_type=request.args.get("type") or None,
        tag=request.args.get("tag") or None,
        q=request.args.get("q") or None,
        limit=_to_int(request.args.get("limit"), 100),
        offset=_to_int(request.args.get("offset"), 0))
    docs = []
    for r in rows:
        d = dict(r)
        d["tags"] = database.tags_for_document(r["id"])
        docs.append(d)
    return jsonify({"documents": docs, "count": len(docs)})


@bp.route("/documents/<int:doc_id>")
@api_auth_required
def api_document(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    d["tags"] = database.tags_for_document(doc_id)
    return jsonify(d)


@bp.route("/documents/<int:doc_id>/content")
@api_auth_required
def api_document_content(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    content = database.read_cache(row["path"]) or ""
    return jsonify({"id": doc_id, "path": row["path"], "content": content})


@bp.route("/crawl", methods=["POST"])
@api_auth_required
def api_crawl():
    data = request.get_json(silent=True) or request.form or {}
    kind = (data.get("kind") or "directory").lower()
    if kind == "url":
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": "url must start with http(s)://"}), 400
        depth = _to_int(data.get("depth"), None)
        job_id = crawler.start_url_crawl(url, depth=depth)
    else:
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "path required"}), 400
        if not Path(path).exists():
            return jsonify({"error": f"path not found: {path}"}), 404
        if not crawler.path_allowed(path):
            return jsonify({"error": "path outside ALLOWED_ROOTS"}), 403
        job_id = crawler.start_directory_crawl(path)
    return jsonify({"job_id": job_id, "status_url": f"/api/crawl/{job_id}"})


@bp.route("/crawl/<int:job_id>")
@api_auth_required
def api_crawl_status(job_id: int):
    row = database.get_job(job_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@bp.route("/tags")
@api_auth_required
def api_tags():
    return jsonify({"tags": [dict(r) for r in database.all_tags_with_counts()]})


@bp.route("/stats")
@api_auth_required
def api_stats():
    stats = database.dump_stats()
    stats["index"] = search_index.stats()
    return jsonify(stats)


@bp.route("/saved-searches")
@api_auth_required
def api_saved():
    out = []
    for row in database.list_saved_searches():
        try:
            params = json.loads(row["query_json"])
        except (json.JSONDecodeError, TypeError):
            params = {}
        out.append({"id": row["id"], "name": row["name"], "params": params})
    return jsonify({"saved_searches": out})


@bp.route("/token", methods=["POST"])
@api_auth_required
def api_token():
    """Generate (or clear) the API token for the current user."""
    action = (request.form.get("action") or (request.get_json(silent=True) or {}).get("action") or "generate")
    user_id = getattr(current_user, "id", None) or getattr(g_user(), "id", None)
    if user_id is None:
        return jsonify({"error": "no user"}), 401
    if action == "clear":
        database.set_api_token(user_id, None)
        return jsonify({"token": None})
    import secrets
    token = secrets.token_urlsafe(32)
    database.set_api_token(user_id, token)
    return jsonify({"token": token})


def g_user():
    from flask import g
    return getattr(g, "user", None)


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
