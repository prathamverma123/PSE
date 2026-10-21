# Personal Search Engine (PSE)

A private, fully local, password-protected search engine for your own
documents, images, audio, video, and saved web pages. Nothing leaves your
machine — no cloud services, no telemetry, no outbound requests (the only
network activity is URL crawls *you* explicitly start).

## Features

- **Local-first & private** — Flask + Whoosh + SQLite, all on your disk.
- **Crawling & indexing** — recursive directory crawls with content-hash
  change detection (re-crawls are incremental), plus URL crawling with
  bounded, same-host link following. Optional live file watcher.
- **Many formats** — PDF, DOCX, plain text/markdown/JSON/code, images
  (EXIF), audio/video (tags, duration, resolution), HTML, and
  filename-only fallback for anything unknown.
- **Search** — full-text with phrase and prefix support, highlighted
  snippets, filters (type, category, tag, date range), pagination, saved
  searches.
- **Privacy & security** — password auth (scrypt-hashed via werkzeug),
  session idle + absolute timeouts, session regeneration on login, CSRF
  tokens on every state-changing request, loopback-only by default, and
  optional Fernet encryption at rest for the index and cached text.
- **Content management** — manual add of files/folders, tagging system,
  banned/unbanned/uncategorized classification, document viewer.
- **Backup & restore** — one-click local zip of DB + index; restore via
  upload or path.
- **API** — JSON endpoints under `/api/...` for your own local tools,
  authenticated by session or per-user API token.

## Quick start (Windows)

```bat
cd "D:\code\search emgine"
python -m pip install -r requirements.txt
python run.py
```

Then open <http://127.0.0.1:5000>. On first run the login page shows a
"create account" form — pick a username and password. That account is
stored only in your local database.

Command-line user management:

```bat
python -m flask --app app create-user
```

## Daily use

1. **Add content** — the *Crawl* page, or "Add manually" on the Documents
   page. A directory starts a background job with live progress; a single
   file indexes immediately.
2. **Search** — type words (`alpha beta`), a phrase (`"exact phrase"`), or
   a prefix (`zeph*`). Narrow with the sidebar filters; save a search for
   later from the results page.
3. **Organize** — tag documents and set banned/unbanned categories from
   the Documents page; both are searchable filters.
4. **Back up** — Backup page → "Create backup zip". Restore from a zip
   file or a local path.

## Configuration

All knobs live in `config.py` defaults and can be overridden with a
`config.yaml` next to it, or `PSE_*` environment variables.

| Key | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address (loopback only) |
| `DATA_DIR` | `./data` | DB, index, keys, backups |
| `ALLOW_REMOTE` | `false` | `true` to allow LAN access (not recommended) |
| `SESSION_IDLE_TIMEOUT` | `1800` | Inactivity logout (seconds) |
| `SESSION_LIFETIME` | `43200` | Absolute session lifetime |
| `ALLOWED_ROOTS` | *(empty = all)* | Directories that may be indexed |
| `SKIP_DIRS` / `SKIP_FILE_PATTERNS` | sensible defaults | Crawl exclusions |
| `ENABLED_PARSERS` | all on | Toggle pdf/docx/text/image/media/web |
| `URL_MAX_DEPTH` / `URL_MAX_PAGES` / `CRAWL_DELAY` | `2` / `100` / `0.5` | URL crawl bounds |
| `ENCRYPT_CONTENT` | `false` | Fernet-encrypt cached text + index at rest |
| `ENABLE_FILE_WATCHER` | `false` | Auto-index changed files under ALLOWED_ROOTS |
| `PAGE_SIZE` | `20` | Results per page |

Example `config.yaml`:

```yaml
ALLOWED_ROOTS:
  - D:\docs
  - D:\media
SESSION_IDLE_TIMEOUT: 900
ENCRYPT_CONTENT: true
```

## Encryption at rest

With `ENCRYPT_CONTENT: true`, extracted text is Fernet-encrypted before it
hits the cache, and the Whoosh index directory is packed and encrypted at
shutdown (and unpacked at startup). The key is `data/encryption.key` — you
own it; if you delete it, that data is unrecoverable. `python crypto.py
pack|unpack` does the same manually.

## API

All `/api/*` endpoints need a session or an `X-Api-Token` header.

```bat
:: generate a token (once logged in)
curl -X POST http://127.0.0.1:5000/api/token

:: search
curl -H "X-Api-Token: <token>" "http://127.0.0.1:5000/api/search?q=zephyr&type=pdf"

:: start a directory crawl and poll it
curl -X POST -H "X-Api-Token: <token>" -H "Content-Type: application/json" ^
     -d "{\"kind\":\"directory\",\"path\":\"D:\\docs\"}" http://127.0.0.1:5000/api/crawl
curl -H "X-Api-Token: <token>" http://127.0.0.1:5000/api/crawl/1
```

## Tests

```bat
python -m pytest tests/ -q
```

17 smoke tests cover the auth gate, indexing PDF/DOCX/JPG/text fixtures,
highlighted search, type/tag/category/date filters, saved-search round
trip, tag/category management, backup/restore, the JSON API, CSRF
enforcement, loopback-only access, idle timeout, and deletion.

## File map

```
app.py           Flask factory, dashboard, CLI (create-user, reindex)
run.py           `python run.py` launcher
config.py        all configuration knobs
database.py     SQLite layer (schema.sql)
search_index.py  Whoosh full-text index + highlighted search
extractors.py    per-format text/metadata extraction
crawler.py       directory & URL crawling, background jobs
watcher.py       optional live file watcher
crypto.py        optional Fernet encryption at rest
auth.py          login, sessions, CSRF, loopback-only guard, API tokens
routes/          search / manage / backup / api blueprints
templates/       Jinja2 UI (no CDN — all local assets)
static/          style.css, app.js
tests/           pytest smoke suite
```
