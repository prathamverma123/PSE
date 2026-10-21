"""Per-format text/metadata extraction.

Each handler takes a filesystem path and returns (title, text, metadata dict).
Handlers never raise outwards — on failure they return filename-only data so
the file is still searchable by name/path/metadata.

Supported: PDF, DOCX, TXT/MD/JSON/code, JPG/PNG/GIF/BMP/WEBP (EXIF),
MP4/MKV/AVI/MOV/MP3/FLAC/OGG (mutagen or raw header), HTML, and unknown
formats get filename/path metadata only.
"""

from __future__ import annotations

import json
import logging
import re
import struct
from pathlib import Path

from config import load_config

log = logging.getLogger("pse.extract")

CONFIG = load_config()
ENABLED = CONFIG["ENABLED_PARSERS"]
MAX_TEXT = int(CONFIG["MAX_TEXT_CHARS"])
MAX_PDF_PAGES = int(CONFIG["MAX_PDF_PAGES"])

# ------------------------------------------------------------------ types

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".yaml", ".yml",
    ".csv", ".tsv", ".ini", ".cfg", ".conf", ".toml", ".xml", ".html",
    ".htm", ".js", ".ts", ".jsx", ".tsx", ".py", ".rb", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bat", ".ps1",
    ".sql", ".css", ".scss", ".svg", ".srt", ".vtt", ".sub", ".nfo",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".ico"}
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg", ".3gp"}
DOC_EXTS = {".docx"}
PDF_EXTS = {".pdf"}
HTML_EXTS = {".html", ".htm"}

DOC_TYPE_BY_EXT = {}
for exts, dtype in (
    (PDF_EXTS, "pdf"), (DOC_EXTS, "docx"), (IMAGE_EXTS, "image"),
    (AUDIO_EXTS, "audio"), (VIDEO_EXTS, "video"), (HTML_EXTS, "web"),
    (TEXT_EXTS, "text"),
):
    for e in exts:
        DOC_TYPE_BY_EXT[e] = dtype

KNOWN_EXTS = set(DOC_TYPE_BY_EXT)


def doc_type_for(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    return DOC_TYPE_BY_EXT.get(ext, "other")


def _read_text(path: Path, max_chars: int) -> str:
    """Read a text file robustly (UTF-8 first, fallback encodings)."""
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        return ""
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc != "utf-8" and text and text.count("\ufffd") > len(text) * 0.1:
                continue
            return text[:max_chars]
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


# ------------------------------------------------------------------ handlers

def extract_pdf(path: Path) -> tuple[str, str, dict]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    n_pages = len(reader.pages)
    if n_pages > MAX_PDF_PAGES:
        reader = PdfReader(str(path))
        pages = reader.pages[:MAX_PDF_PAGES]
    else:
        pages = reader.pages
    chunks = []
    for p in pages:
        try:
            chunks.append(p.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            log.debug("pdf page failed %s: %s", path, e)
    text = "\n".join(chunks)[:MAX_TEXT]
    meta = reader.metadata or {}
    title = str(meta.get("/Title") or "") or path.stem
    info = {"pages": n_pages}
    for k in ("/Author", "/Subject", "/Keywords", "/Creator", "/Producer"):
        v = meta.get(k)
        if v:
            info[k.lstrip("/").lower()] = str(v)
    return title, text, info


def extract_docx(path: Path) -> tuple[str, str, dict]:
    import docx  # python-docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    core = d.core_properties
    info = {}
    for attr in ("author", "subject", "keywords", "comments", "category"):
        v = getattr(core, attr, None)
        if v:
            info[attr] = str(v)
    title = (core.title or "").strip() or path.stem
    return title, "\n".join(parts)[:MAX_TEXT], info


def extract_text_file(path: Path) -> tuple[str, str, dict]:
    text = _read_text(path, MAX_TEXT)
    ext = path.suffix.lower()
    extra = {}
    if ext in {".html", ".htm"}:
        try:
            plain = _html_to_text(text)
            title = _html_title(text) or path.stem
            return title, plain[:MAX_TEXT], extra
        except Exception:  # noqa: BLE001
            return path.stem, text, extra
    if ext in {".json",}:
        try:
            data = json.loads(text)
            keys = list(data.keys())[:20] if isinstance(data, dict) else []
            extra["json_keys"] = ", ".join(str(k) for k in keys)
        except Exception:  # noqa: BLE001
            pass
    if ext in {".srt", ".vtt", ".sub"}:
        extra["note"] = "subtitle file"
    return path.stem, text, extra


def extract_image(path: Path) -> tuple[str, str, dict]:
    from PIL import Image
    from PIL.ExifTags import TAGS as EXIF_TAGS
    info: dict[str, str] = {}
    text_parts: list[str] = []
    with Image.open(path) as im:
        info["width"], info["height"] = str(im.size[0]), str(im.size[1])
        info["format"] = im.format or path.suffix.lstrip(".").upper()
        try:
            exif = im.getexif()
            for tag_id, value in exif.items():
                name = EXIF_TAGS.get(tag_id, str(tag_id))
                if name in ("ImageDescription", "DocumentName", "XPComment",
                            "XPSubject", "XPKeywords", "UserComment"):
                    if value:
                        text_parts.append(str(value))
                info[name] = str(value)[:120]
            ifd = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else None
            if ifd:
                cam = {}
                for k, v in ifd.items():
                    if isinstance(v, (int, float, str)):
                        cam[str(k)] = str(v)[:80]
                if cam:
                    info["camera"] = json.dumps(cam)
        except Exception as e:  # noqa: BLE001
            log.debug("exif failed %s: %s", path, e)
    title = path.stem
    return title, "\n".join(text_parts), info


def extract_media(path: Path) -> tuple[str, str, dict]:
    """Audio/video via mutagen; falls back to raw header sniffing."""
    info: dict[str, str] = {}
    text_parts: list[str] = []
    ext = path.suffix.lower()
    title = path.stem
    try:
        import mutagen
        m = mutagen.File(str(path), easy=True)
        if m is not None:
            for key in ("title", "artist", "album", "albumartist", "genre",
                        "composer", "tracknumber", "date"):
                v = m.get(key)
                if v:
                    val = str(v[0] if isinstance(v, list) else v)
                    info[key] = val[:120]
                    if key in ("title", "artist", "album", "albumartist", "genre"):
                        text_parts.append(val)
            try:
                info["duration_s"] = str(round(m.info.length, 1))
                if hasattr(m.info, "sample_rate"):
                    info["sample_rate"] = str(m.info.sample_rate)
                if hasattr(m.info, "bitrate"):
                    info["bitrate_kbps"] = str(m.info.bitrate // 1000)
                if hasattr(m.info, "channels"):
                    info["channels"] = str(m.info.channels)
                if hasattr(m.info, "width"):
                    info["width"] = str(m.info.width)
                if hasattr(m.info, "height"):
                    info["height"] = str(m.info.height)
            except Exception:  # noqa: BLE001
                pass
            if info.get("title"):
                title = info["title"]
    except Exception as e:  # noqa: BLE001
        log.debug("mutagen failed %s: %s", path, e)
    # raw header sniff for MP4 resolution / faststart atoms
    if ext in VIDEO_EXTS and not info.get("width"):
        sniffed = _sniff_video(path)
        info.update(sniffed)
    return title, "\n".join(text_parts), info


def _sniff_video(path: Path) -> dict[str, str]:
    """Very small MP4 box parser to pull width/height/fourcc when mutagen can't."""
    out: dict[str, str] = {}
    try:
        with path.open("rb") as fh:
            data = fh.read(512 * 1024)
        i = 0
        while i + 8 <= len(data):
            size = struct.unpack(">I", data[i:i + 4])[0]
            box = data[i + 4:i + 8]
            if size < 8:
                break
            if box in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                i += 8
                continue
            if box == b"tkhd" and i + 8 + 84 <= len(data):
                w = struct.unpack(">I", data[i + 8 + 76:i + 8 + 80])[0] >> 16
                h = struct.unpack(">I", data[i + 8 + 80:i + 8 + 84])[0] >> 16
                if w and h:
                    out["width"], out["height"] = str(w), str(h)
                break
            i += size
    except (OSError, struct.error):
        pass
    return out


def extract_unknown(path: Path) -> tuple[str, str, dict]:
    """Filename/path-only indexing: split camelCase/underscores into words."""
    words = re.split(r"[_\-\.\s]+", path.stem)
    words = [w for w in words if w]
    # also split camelCase
    expanded: list[str] = []
    for w in words:
        expanded.extend(re.findall(r"[A-Z]?[a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|\b)|\d+", w))
    text = " ".join(expanded)
    return path.stem, text, {}


# ------------------------------------------------------------------ dispatch

def extract(path: str | Path) -> tuple[str, str, str, dict]:
    """Returns (doc_type, title, text, metadata)."""
    p = Path(path)
    ext = p.suffix.lower()
    dtype = doc_type_for(p)
    try:
        if ext in PDF_EXTS and ENABLED.get("pdf", True):
            title, text, meta = extract_pdf(p)
        elif ext in DOC_EXTS and ENABLED.get("docx", True):
            title, text, meta = extract_docx(p)
        elif ext in IMAGE_EXTS and ENABLED.get("image", True):
            title, text, meta = extract_image(p)
        elif ext in AUDIO_EXTS | VIDEO_EXTS and ENABLED.get("media", True):
            title, text, meta = extract_media(p)
        elif ext in TEXT_EXTS and ENABLED.get("text", True):
            title, text, meta = extract_text_file(p)
        else:
            dtype = "other" if ext not in KNOWN_EXTS else dtype
            title, text, meta = extract_unknown(p)
            if ext in KNOWN_EXTS:
                dtype = DOC_TYPE_BY_EXT[ext]
    except Exception as e:  # noqa: BLE001
        log.warning("extract failed for %s (%s); indexing name only", p, e)
        title, text, meta = p.stem, "", {"error": str(e)[:200]}
    meta_text = " ".join(f"{k}:{v}" for k, v in meta.items())
    full_text = (text + "\n" + meta_text).strip()[:MAX_TEXT]
    return dtype, title or p.stem, full_text, meta


# ------------------------------------------------------------------ helpers

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except ImportError:
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        return _TAG_RE.sub(" ", html)


def _html_title(html: str) -> str:
    m = re.search(r"(?is)<title>(.*?)</title>", html)
    return m.group(1).strip() if m else ""


def extract_html_string(html: str, url: str = "") -> tuple[str, str]:
    """For URL crawling: returns (title, text)."""
    title = _html_title(html) or url
    return title, _html_to_text(html)
