"""Document management routes: list, add, tags, category, delete, crawl."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

import crawler
import database
import search_index

bp = Blueprint("manage", __name__)

CATEGORIES = ("banned", "unbanned", "uncategorized")


@bp.route("/documents")
@login_required
def documents():
    category = (request.args.get("category") or "").strip()
    doc_type = (request.args.get("type") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    q = (request.args.get("q") or "").strip()
    page = _to_int(request.args.get("page"), 1)
    page_size = 50
    rows = database.list_documents(
        category=category or None, doc_type=doc_type or None,
        tag=tag or None, q=q or None,
        limit=page_size, offset=(page - 1) * page_size)
    total = database.count_documents()
    tags = database.all_tags_with_counts()
    doc = None
    if request.args.get("doc"):
        doc = database.get_document(_to_int(request.args.get("doc"), 0))
    return render_template(
        "documents.html", docs=rows, total=total, tags=tags,
        filters={"category": category, "type": doc_type, "tag": tag, "q": q},
        page=page, page_size=page_size, selected_doc=doc,
    )


@bp.route("/documents/add", methods=["POST"])
@login_required
def add_documents():
    path = (request.form.get("path") or "").strip()
    if not path:
        flash("Enter a file or directory path.", "error")
        return redirect(url_for("manage.documents"))
    p = Path(path).expanduser()
    if not p.exists():
        flash(f"Path not found: {path}", "error")
        return redirect(url_for("manage.documents"))
    if not crawler.path_allowed(str(p)):
        flash(f"Path is outside ALLOWED_ROOTS: {path}", "error")
        return redirect(url_for("manage.documents"))
    if p.is_dir():
        job_id = crawler.start_directory_crawl(str(p))
        flash(f"Directory crawl started (job #{job_id}).", "success")
        return redirect(url_for("manage.crawl_page", job_id=job_id))
    result = crawler.index_file(str(p), source="manual")
    if result == "indexed":
        flash(f"Indexed {p.name}.", "success")
    elif result == "unchanged":
        flash(f"{p.name} is already indexed and unchanged.", "success")
    elif result == "skipped":
        flash(f"{p.name} was skipped by the skip-list.", "error")
    else:
        flash(f"Could not index {p.name}: {result}", "error")
    return redirect(url_for("manage.documents"))


@bp.route("/documents/<int:doc_id>/tags", methods=["POST"])
@login_required
def doc_tags(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        flash("Document not found.", "error")
        return redirect(url_for("manage.documents"))
    action = request.form.get("action") or "add"
    tags_raw = request.form.get("tags") or ""
    names = [t.strip().lower() for t in tags_raw.replace(";", ",").split(",")
             if t.strip()]
    if action == "set":
        database.set_tags(doc_id, names)
        search_index.update_document(_index_doc(doc_id))
        flash("Tags updated.", "success")
    elif action == "add":
        if not names:
            flash("Enter at least one tag.", "error")
        else:
            for n in names:
                database.add_tag(doc_id, n)
            search_index.update_document(_index_doc(doc_id))
            flash("Tag(s) added.", "success")
    elif action == "remove":
        for n in names:
            database.remove_tag(doc_id, n)
        search_index.update_document(_index_doc(doc_id))
        flash("Tag(s) removed.", "success")
    else:
        flash("Unknown tag action.", "error")
    return redirect(url_for("manage.documents", doc=doc_id))


@bp.route("/documents/<int:doc_id>/category", methods=["POST"])
@login_required
def doc_category(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        flash("Document not found.", "error")
        return redirect(url_for("manage.documents"))
    category = (request.form.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        flash(f"Category must be one of: {', '.join(CATEGORIES)}", "error")
        return redirect(url_for("manage.documents", doc=doc_id))
    database.set_category(doc_id, category)
    search_index.update_document(_index_doc(doc_id))
    flash(f"Category set to {category}.", "success")
    return redirect(url_for("manage.documents", doc=doc_id))


@bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def doc_delete(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        flash("Document not found.", "error")
        return redirect(url_for("manage.documents"))
    database.delete_document(doc_id)
    search_index.delete_document(row["path"])
    flash("Document removed from the index.", "success")
    return redirect(url_for("manage.documents"))


@bp.route("/crawl", methods=["GET"])
@login_required
def crawl_page():
    jobs = database.latest_jobs(15)
    return render_template("crawl.html", jobs=jobs)


@bp.route("/crawl", methods=["POST"])
@login_required
def crawl_start():
    kind = request.form.get("kind") or "directory"
    if kind == "url":
        url = (request.form.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            flash("Enter a full URL including http:// or https://.", "error")
            return redirect(url_for("manage.crawl_page"))
        depth = _to_int(request.form.get("depth"), None)
        job_id = crawler.start_url_crawl(url, depth=depth)
    else:
        path = (request.form.get("path") or "").strip()
        if not path:
            start = (request.form.get("directory") or "").strip()
            path = start
        if not path:
            flash("Enter a directory to crawl.", "error")
            return redirect(url_for("manage.crawl_page"))
        from pathlib import Path as P
        if not P(path).exists():
            flash(f"Directory not found: {path}", "error")
            return redirect(url_for("manage.crawl_page"))
        if not crawler.path_allowed(path):
            flash(f"Path is outside ALLOWED_ROOTS: {path}", "error")
            return redirect(url_for("manage.crawl_page"))
        job_id = crawler.start_directory_crawl(path)
    flash(f"Crawl started (job #{job_id}).", "success")
    return redirect(url_for("manage.crawl_page", job_id=job_id))


@bp.route("/crawl/status")
@login_required
def crawl_status():
    jobs = database.latest_jobs(15)
    return jsonify({"jobs": [dict(j) for j in jobs]})


@bp.route("/documents/<int:doc_id>/view")
@login_required
def doc_view(doc_id: int):
    row = database.get_document(doc_id)
    if row is None:
        flash("Document not found.", "error")
        return redirect(url_for("manage.documents"))
    content = database.read_cache(row["path"]) or ""
    tags = database.tags_for_document(doc_id)
    return render_template("document_view.html", doc=row, content=content, tags=tags)


def _index_doc(doc_id: int) -> dict:
    """Rebuild the index payload for a document from the DB + cache."""
    row = database.get_document(doc_id)
    if row is None:
        return {}
    content = database.read_cache(row["path"]) or ""
    return {
        "id": doc_id, "path": row["path"], "title": row["title"],
        "content": content, "doc_type": row["doc_type"],
        "category": row["category"],
        "tags": database.tags_for_document(doc_id),
        "mtime": row["mtime"], "size": row["size"], "added_at": row["added_at"],
    }


def _to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
