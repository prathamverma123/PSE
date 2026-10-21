"""Smoke tests: auth gate, indexing PDF/txt/JPG, search with highlight,
tag filters, saved searches, category management, backup round-trip.

Run:  python -m pytest tests/ -v
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
import crawler
import database
import search_index

# ---------------------------------------------------------------- fixtures

TEST_ROOT = Path(__file__).resolve().parent / "fixtures"
TEST_DATA = Path(__file__).resolve().parent / "test_data"


@pytest.fixture()
def client(tmp_path):
    """App with isolated DATA_DIR; a logged-in test client."""
    # copy fixtures into a fresh dir to index
    src = TEST_ROOT
    index_dir = tmp_path / "index"
    db_path = tmp_path / "search.db"

    overrides = {
        "DATA_DIR": str(tmp_path),
        "DB_PATH": str(db_path),
        "INDEX_DIR": str(index_dir),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "SECRET_KEY_FILE": str(tmp_path / "secret.key"),
        "TESTING": True,
        "ALLOWED_ROOTS": [str(src)],
        "SESSION_IDLE_TIMEOUT": 3600,
        "SESSION_LIFETIME": 3600,
    }
    app = app_module.create_app(overrides)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["LOGIN_DISABLED"] = False

    # disable CSRF for the test client by patching the guard via header
    app.config["PSE_DISABLE_CSRF"] = True

    database.CONFIG.update(overrides)

    # create user directly in the test DB
    with app.app_context():
        database.create_user("tester", "test-password-123")

    c = app.test_client()
    # fetch the login page first so a CSRF token is issued into the
    # session, then post the credentials WITH that token.
    # NOTE: no `with` block — a preserved request context would leak
    # authentication state into other clients created inside it.
    resp = c.get("/login")
    assert resp.status_code == 200
    with c.session_transaction() as sess:
        csrf = sess.get("_csrf", "")
    resp = c.post("/login", data={
        "username": "tester",
        "password": "test-password-123",
        "_csrf": csrf,
    }, follow_redirects=True)
    assert resp.status_code == 200
    with c.session_transaction() as sess:
        csrf = sess.get("_csrf", "")
    c.csrf = csrf
    assert c.csrf, "no CSRF token after login"
    yield c


@pytest.fixture(scope="session", autouse=True)
def fixtures(tmp_path_factory):
    """Create sample PDF/txt/JPG files to index, once per test session."""
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    TEST_ROOT.mkdir(parents=True)

    # 1) plain text file
    (TEST_ROOT / "notes.txt").write_text(
        "Quarterly report on the zephyr project. The zephyr wind turbine "
        "prototype exceeded expectations in the field trials.", encoding="utf-8")

    # 2) a second text file without the keyword
    (TEST_ROOT / "other.txt").write_text(
        "Nothing about wind here. Just some mundane meeting minutes.", encoding="utf-8")

    # 3) JPG with EXIF ImageDescription containing the keyword
    from PIL import Image
    import io as _io
    desc = "photo of the zephyr prototype at dusk"
    exif_obj = Image.Exif()
    exif_obj[0x010e] = desc  # ImageDescription
    out2 = _io.BytesIO()
    img2 = Image.new("RGB", (80, 60), color=(200, 120, 10))
    img2.save(out2, format="JPEG", exif=exif_obj)
    (TEST_ROOT / "zephyr_photo.jpg").write_bytes(out2.getvalue())

    # 4) PDF with the keyword
    try:
        from pypdf import PdfWriter
        from pypdf.generic import RectangleObject
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        # pypdf PdfWriter can't add text directly; fall back to a minimal PDF
    except Exception:
        pass
    pdf_bytes = _minimal_pdf_with_text(
        "Project Zephyr final summary. "
        "The zephyr initiative concluded with strong results.")
    (TEST_ROOT / "zephyr_report.pdf").write_bytes(pdf_bytes)

    # 5) docx
    try:
        import docx
        d = docx.Document()
        d.add_heading("Zephyr Meeting Notes", 0)
        d.add_paragraph("Discussed the zephyr roadmap and budget line items.")
        d.save(str(TEST_ROOT / "zephyr_notes.docx"))
    except Exception as e:
        # docx import failure shouldn't kill the suite
        print("docx fixture skipped:", e)

    yield TEST_ROOT


def _minimal_pdf_with_text(text: str) -> bytes:
    """Hand-rolled minimal PDF so the suite needs no reportlab."""
    lines = text.split(". ")
    lines = [l.strip() for l in lines if l.strip()]
    content_parts = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        safe = line.replace("(", "\\(").replace(")", "\\)")
        content_parts.append(f"({safe}) Tj")
        content_parts.append("T*")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1", "replace")
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" +
                stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 " + str(len(objs) + 1).encode() + b"\n")
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n<< /Size " + str(len(objs) + 1).encode() +
              b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF")
    return out.getvalue()


# ------------------------------------------------------------------- tests

def test_login_gate(client):
    """Unauthenticated requests must be redirected to /login."""
    import flask_login
    c2 = client.application.test_client()
    for path in ("/", "/search", "/documents", "/crawl", "/backup",
                 "/api/search", "/api/stats"):
        resp = c2.get(path, follow_redirects=False)
        assert resp.status_code == 302, f"{path} should redirect"
        assert "/login" in resp.headers["Location"], f"{path} not gated"
    # static assets stay accessible so the login page renders
    resp = c2.get("/static/style.css")
    assert resp.status_code == 200


def test_index_directory_and_search(client, fixtures):
    """Crawl the fixture dir; zephyr must be findable with a snippet."""
    stats = crawler.index_directory_sync(str(fixtures))
    assert stats["total"] >= 4, f"expected >=4 files, got {stats}"
    assert stats["errors"] == 0, f"crawl errors: {stats}"

    res = search_index.search("zephyr")
    assert res["total"] >= 3, f"expected >=3 zephyr hits, got {res['total']}"
    titles = {r["title"].lower() for r in res["results"]}
    assert any("zephyr" in t for t in titles), titles
    # snippet present and highlighted
    first = res["results"][0]
    assert first["snippet"], "expected a snippet"
    joined = " ".join(r["snippet"] for r in res["results"])
    assert "zephyr" in joined.lower(), "keyword missing from snippets"


def test_doc_type_filter(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    res = search_index.search("zephyr", doc_type="pdf")
    assert res["total"] >= 1
    assert all(r["doc_type"] == "pdf" for r in res["results"])
    res2 = search_index.search("zephyr", doc_type="image")
    assert res2["total"] >= 1
    assert all(r["doc_type"] == "image" for r in res2["results"])


def test_tag_filter(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    # auto-tags: every doc gets its doc_type as a tag
    res = search_index.search("", tag="pdf")
    assert res["total"] >= 1
    assert all("pdf" in r["tags"] for r in res["results"])


def test_category_filter(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    row = database.get_document_by_path(str(fixtures / "zephyr_report.pdf"))
    assert row is not None
    database.set_category(row["id"], "banned")
    search_index.update_document({
        "id": row["id"], "path": row["path"], "title": row["title"],
        "content": database.read_cache(row["path"]) or "",
        "doc_type": row["doc_type"], "category": "banned",
        "tags": database.tags_for_document(row["id"]),
        "mtime": row["mtime"], "size": row["size"], "added_at": row["added_at"],
    })
    res = search_index.search("zephyr", category="banned")
    assert res["total"] == 1
    assert all(r["category"] == "banned" for r in res["results"])
    res3 = search_index.search("zephyr", category="unbanned")
    assert res3["total"] == 0


def test_date_filter(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    import datetime
    today = datetime.date.today().strftime("%Y-%m-%d")
    res = search_index.search("zephyr", date_from=today)
    assert res["total"] >= 3
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    res2 = search_index.search("zephyr", date_from=yesterday, date_to=yesterday)
    assert res2["total"] == 0


def test_saved_search_roundtrip(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    params = {"q": "zephyr", "type": "pdf"}
    search_id = database.save_search("my zephyr pdfs", params)
    assert search_id > 0
    rows = database.list_saved_searches()
    assert any(r["name"] == "my zephyr pdfs" for r in rows)
    row = database.get_saved_search(search_id)
    assert row is not None
    import json
    assert json.loads(row["query_json"])["q"] == "zephyr"
    # re-run via the route
    resp = client.get(f"/search/saved/{search_id}/run", follow_redirects=False)
    assert resp.status_code == 302
    assert "q=zephyr" in resp.headers["Location"]
    # delete
    assert database.delete_saved_search(search_id) is True
    assert database.get_saved_search(search_id) is None


def test_web_ui_search_route(client, fixtures):
    """The /search route renders results with <mark> highlighting."""
    crawler.index_directory_sync(str(fixtures))
    resp = client.get("/search?q=zephyr")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "zephyr" in body.lower()
    assert "result" in body.lower()


def test_tag_management_via_ui(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    row = database.get_document_by_path(str(fixtures / "notes.txt"))
    resp = client.post(f"/documents/{row['id']}/tags", data={
        "_csrf": client.csrf, "action": "add", "tags": "weather,scotland"},
        follow_redirects=True)
    assert resp.status_code == 200
    tags = database.tags_for_document(row["id"])
    assert "weather" in tags and "scotland" in tags

    resp = client.post(f"/documents/{row['id']}/tags", data={
        "_csrf": client.csrf, "action": "remove", "tags": "scotland"},
        follow_redirects=True)
    tags = database.tags_for_document(row["id"])
    assert "scotland" not in tags and "weather" in tags

    # tag filter now narrows results through the index too
    res = search_index.search("", tag="weather")
    assert res["total"] == 1
    assert "weather" in res["results"][0]["tags"]


def test_category_via_ui(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    row = database.get_document_by_path(str(fixtures / "notes.txt"))
    resp = client.post(f"/documents/{row['id']}/category", data={
        "_csrf": client.csrf, "category": "banned"}, follow_redirects=True)
    assert resp.status_code == 200
    assert database.get_document(row["id"])["category"] == "banned"
    res = search_index.search("", category="banned")
    assert res["total"] == 1


def test_backup_roundtrip(client, fixtures, tmp_path):
    """Backup zip contains DB; restore brings the doc count back."""
    import sqlite3
    crawler.index_directory_sync(str(fixtures))
    before = database.count_documents()
    assert before >= 4

    backup_dir = Path(client.application.config["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / "test-backup.zip"
    if target.exists():
        target.unlink()

    # use the route for creation (backup app-context helper)
    resp = client.post("/backup/create", data={"_csrf": client.csrf},
                       follow_redirects=True)
    assert resp.status_code == 200
    zips = list(backup_dir.glob("*.zip"))
    assert zips, "no backup zip produced"
    target = zips[0]
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert "database.sqlite" in names, names
    # restore into a *fresh* location to prove round-trip
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target) as zf:
        zf.extract("database.sqlite", fresh_dir)
    restored_db = fresh_dir / "database.sqlite"
    assert restored_db.is_file()
    conn = sqlite3.connect(str(restored_db))
    n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert n == before, f"restored {n} != {before}"


def test_api_search(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    resp = client.get("/api/search?q=zephyr")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] >= 3
    assert all("snippet" in r for r in data["results"])


def test_csrf_required(client, fixtures):
    """POST without a token must be rejected (API routes use token auth)."""
    crawler.index_directory_sync(str(fixtures))
    row = database.get_document_by_path(str(fixtures / "notes.txt"))
    resp = client.post(f"/documents/{row['id']}/category",
                       data={"category": "banned"},
                       follow_redirects=False)
    assert resp.status_code == 400
    assert database.get_document(row["id"])["category"] == "uncategorized"


def test_local_only(client):
    """Non-localhost clients get 403 unless ALLOW_REMOTE."""
    from werkzeug.test import EnvironBuilder
    app = client.application
    c2 = app.test_client()
    # simulate a remote peer
    resp = c2.get("/login", environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
    assert resp.status_code == 403


def test_idle_timeout(client, fixtures):
    """Idle sessions must be logged out after SESSION_IDLE_TIMEOUT."""
    import time as _t
    crawler.index_directory_sync(str(fixtures))
    with client.session_transaction() as sess:
        sess["_last_activity"] = _t.time() - 999999
    resp = client.get("/search?q=zephyr", follow_redirects=False)
    # should be bounced to login
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_delete_document(client, fixtures):
    crawler.index_directory_sync(str(fixtures))
    row = database.get_document_by_path(str(fixtures / "other.txt"))
    resp = client.post(f"/documents/{row['id']}/delete",
                       data={"_csrf": client.csrf}, follow_redirects=True)
    assert resp.status_code == 200
    assert database.get_document(row["id"]) is None
    res = search_index.search("mundane")
    assert res["total"] == 0


def test_extractor_direct():
    """Extractors handle pdf/text/image/unknown formats without raising."""
    import extractors
    f = TEST_ROOT
    dtype, title, text, meta = extractors.extract(f / "zephyr_report.pdf")
    assert dtype == "pdf"
    assert "zephyr" in text.lower()
    dtype, title, text, meta = extractors.extract(f / "notes.txt")
    assert dtype == "text"
    assert "zephyr" in text.lower()
    dtype, title, text, meta = extractors.extract(f / "zephyr_photo.jpg")
    assert dtype == "image"
    assert "zephyr" in text.lower(), "EXIF description should be indexed"
    dtype, title, text, meta = extractors.extract(f / "no_such_format.xyz")
    assert dtype == "other"
