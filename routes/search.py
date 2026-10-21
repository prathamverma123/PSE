"""Search routes: /search with filters, saved searches, result previews."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

import database
import search_index

bp = Blueprint("search", __name__)


def _filters_from_request() -> dict:
    """Extract all supported filters from the request (GET or POST)."""
    def get(name: str) -> str:
        return (request.values.get(name) or "").strip()
    return {
        "q": get("q"),
        "type": get("type"),
        "category": get("category"),
        "tag": get("tag"),
        "date_from": get("date_from"),
        "date_to": get("date_to"),
        "sort": get("sort") or "relevance",
    }


@bp.route("/")
@login_required
def index():
    return redirect(url_for("search.search_page"))


@bp.route("/search", methods=["GET", "POST"])
@login_required
def search_page():
    filters = _filters_from_request()
    page = max(1, _to_int(request.values.get("page"), 1))
    page_size = _to_int(request.values.get("page_size"), None)

    results = None
    if request.method == "POST" or filters["q"] or any(
            filters[k] for k in ("type", "category", "tag", "date_from", "date_to")):
        try:
            results = search_index.search(
                filters["q"],
                doc_type=filters["type"] or None,
                category=filters["category"] or None,
                tag=filters["tag"] or None,
                date_from=filters["date_from"] or None,
                date_to=filters["date_to"] or None,
                page=page,
                page_size=page_size,
            )
        except Exception as e:  # noqa: BLE001
            flash(f"Search error: {e}", "error")
            results = None

    tags = [r["name"] for r in database.all_tags_with_counts()]
    return render_template(
        "search.html",
        f=filters,
        results=results,
        tags=tags,
        page=page,
        saved=request.args.get("saved") == "1",
    )


@bp.route("/search/save", methods=["POST"])
@login_required
def save_search_route():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Please give the saved search a name.", "error")
        return redirect(url_for("search.search_page"))
    params = _filters_from_request()
    # fall back to current query string when POST body is sparse
    if not params["q"] and request.args.get("q"):
        params["q"] = request.args.get("q")
    params = {k: v for k, v in params.items() if v}
    database.save_search(name, params)
    flash(f"Saved search “{name}”.", "success")
    return redirect(url_for("search.saved_list"))


@bp.route("/search/saved")
@login_required
def saved_list():
    saved = database.list_saved_searches()
    parsed = []
    for row in saved:
        try:
            params = json.loads(row["query_json"])
        except (json.JSONDecodeError, TypeError):
            params = {}
        parsed.append({"row": row, "params": params,
                       "querystring": urlencode({k: v for k, v in params.items() if v})})
    return render_template("saved_searches.html", saved=parsed)


@bp.route("/search/saved/<int:search_id>/delete", methods=["POST"])
@login_required
def saved_delete(search_id: int):
    if database.delete_saved_search(search_id):
        flash("Saved search deleted.", "success")
    else:
        flash("Saved search not found.", "error")
    return redirect(url_for("search.saved_list"))


@bp.route("/search/saved/<int:search_id>/run", methods=["GET"])
@login_required
def saved_run(search_id: int):
    row = database.get_saved_search(search_id)
    if row is None:
        flash("Saved search not found.", "error")
        return redirect(url_for("search.saved_list"))
    try:
        params = json.loads(row["query_json"])
    except (json.JSONDecodeError, TypeError):
        params = {}
    params.pop("page", None)
    return redirect(url_for("search.search_page") + "?" +
                    urlencode({k: v for k, v in params.items() if v}))


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
