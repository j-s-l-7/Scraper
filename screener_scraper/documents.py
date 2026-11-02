"""Documents pipeline: document_link -> download PDF -> per-page text -> data/documents/<SYMBOL>/<doc_type>/*.jsonl.gz.

PDFs are never kept. equity.document catalogs every file (status ok / no_text / missing / failed).
Entry point for the worker: run_company(db, company_id, run_id, kind).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import re
import threading
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import psycopg2
import requests
from psycopg2.extras import execute_values

from . import config
from .http import MAX_GET_S, FetchError, safe_name
from .rules import BLOCK_RE, DocNotFound, NoTextError

log = logging.getLogger(__name__)

DOCS_DIR = config.ROOT / "data" / "documents"
DOC_TYPE_OF = {"docs_ar": "annual_report", "docs_concall": "concall_transcript", "docs_ppt": "concall_ppt"}
MAX_BYTES = 250 * 1024 * 1024
DOC_INTERVAL = 0.5           # min seconds between requests per worker process (BSE politeness)
DOC_MAX_INTERVAL = 10.0
TERMINAL = ("ok", "no_text", "missing")      # skipped on re-run unless force
# A failed / unreadable document's url is not downloaded again for this long (force overrides). A dead or broken file
# was otherwise re-fetched on every 10-min fleet pass: JASH AR 2023 (a 91 MB zip) 30 times in 11 h. The stamp is the
# latest attempts_log entry from either download of the url itself; the fallback chain keeps its own schedule.
PRIMARY_COOLDOWN_DAYS = 7
COOLDOWN_SOURCES = ["primary", "original_retry"]
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ───────── label -> fiscal year / date ─────────
def derive_fiscal_year(doc_type: str, label: Optional[str]) -> Optional[int]:
    if not label:
        return None
    s = label.strip()
    if doc_type == "annual_report":
        m = re.search(r"(?<!\d)((?:19|20)\d{2})\s*[-–/]\s*(\d{2,4})(?!\d)", s)
        if m:
            end = m.group(2)
            return int(m.group(1)[:2] + end) if len(end) == 2 else int(end)
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", s)
        return int(m.group(1)) if m else None
    d = derive_doc_date(s)
    if d:
        return d.year + 1 if d.month >= 4 else d.year
    m = re.search(r"FY\s?-?(\d{2,4})", s, re.I)
    if m:
        y = int(m.group(1))
        return 2000 + y if y < 100 else y
    return None


def derive_doc_date(label: Optional[str]) -> Optional[date]:
    if not label:
        return None
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?,?\s+((?:19|20)\d{2})\b", label)
    if m and m.group(1).lower() in MONTHS:
        return date(int(m.group(2)), MONTHS[m.group(1).lower()], 1)
    return None


def slugify(label: Optional[str], default: str = "doc") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:60]
    return s or default


def source_of(url: str, given: Optional[str] = None) -> str:
    h = urlparse(url).netloc.lower()
    if "bseindia" in h:
        return "bse"
    if "nseindia" in h:
        return "nse"
    return given or "other"


# ───────── polite HTTP ─────────
class Spacer:
    def __init__(self, interval: float = DOC_INTERVAL):
        self.base = self.interval = interval
        self.next_at = 0.0
        self.ok_streak = 0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        if start > now:
            time.sleep(start - now)

    def on_429(self) -> None:
        with self.lock:
            self.ok_streak = 0
            self.interval = min(self.interval * 2, DOC_MAX_INTERVAL)

    def on_ok(self) -> None:
        with self.lock:
            self.ok_streak += 1
            if self.ok_streak >= 20 and self.interval > self.base:
                self.ok_streak = 0
                self.interval = max(self.base, self.interval * 0.8)


_spacer = Spacer()
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/pdf,application/zip,*/*;q=0.8",
                          "Accept-Language": "en-US,en;q=0.9"})
    return s


def doc_headers(url: str) -> dict:
    return {}      # plain GETs only: no Referer / cookie tricks


# Per-host breaker, one per process: a host whose last BREAK_AFTER downloads all failed on the network (timeouts,
# resets, MAX_GET_S) is skipped for BREAK_S. A dead IR host otherwise costs ~75 s per document on every pass.
BREAK_AFTER, BREAK_S = 3, 3600
_breaker_lock = threading.Lock()
_breaker: Dict[str, Tuple[int, float]] = {}        # host -> (downloads failed in a row, skipped until; monotonic)


def _host_skipped(host: str) -> Optional[str]:
    with _breaker_lock:
        n, until = _breaker.get(host, (0, 0.0))
    left = until - time.monotonic()
    return f"{host} skipped for {left / 60:.0f} more min: its last {n} downloads failed on the network" \
        if left > 0 else None


def _host_result(host: str, network_failed: bool) -> None:
    """Any HTTP answer resets the host (status codes never count); a download that failed on the network counts one."""
    with _breaker_lock:
        if not network_failed:
            _breaker.pop(host, None)
            return
        n = _breaker.get(host, (0, 0.0))[0] + 1
        _breaker[host] = (n, time.monotonic() + BREAK_S if n >= BREAK_AFTER else 0.0)


def download(url: str, spacer: Spacer = None, tries: int = 3) -> bytes:
    """GET with polite spacing. 404/410 -> DocNotFound; 403/429/5xx/network -> FetchError (status_code=None: network).
    429 is retried in-process with a doubling interval; 403 is raised at once (rule R2 decides). A body still arriving
    after MAX_GET_S raises FetchError without a retry; a host the breaker skips raises FetchError with no request."""
    spacer = spacer or _spacer
    host = urlparse(url).netloc.lower()
    skipped = _host_skipped(host)
    if skipped:
        e = FetchError(skipped, None, 0)
        e.host_skipped = True                         # nothing was requested: no cooldown stamp (run_company)
        raise e
    last: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        spacer.wait()
        try:
            t0 = time.monotonic()
            r = _session().get(url, headers=doc_headers(url), timeout=(15, 120), stream=True)
            try:
                _host_result(host, False)
                st = r.status_code
                if st in (404, 410):
                    raise DocNotFound(f"HTTP {st} for {url}")
                if st == 429:
                    spacer.on_429()
                    last = FetchError(f"HTTP 429 for {url}", 429, attempt)
                    time.sleep(min(5 * 2 ** (attempt - 1), 60))
                    continue
                if st == 403:
                    raise FetchError(f"HTTP 403 for {url}", 403, attempt)
                if st >= 500 or st in (408,):
                    last = FetchError(f"HTTP {st} for {url}", st, attempt)
                    time.sleep(5 * attempt)
                    continue
                if st >= 400:
                    raise DocNotFound(f"HTTP {st} for {url}")
                buf = io.BytesIO()
                for chunk in r.iter_content(1 << 16):        # small reads, so the MAX_GET_S check runs often
                    buf.write(chunk)
                    if buf.tell() > MAX_BYTES:
                        raise DocNotFound(f"larger than {MAX_BYTES >> 20} MB: {url}")
                    if time.monotonic() - t0 > MAX_GET_S:
                        _host_result(host, True)
                        raise FetchError(f"still downloading after {MAX_GET_S // 60} min "
                                         f"({buf.tell() >> 20} MB so far): {url}", None, attempt)
                spacer.on_ok()
                return buf.getvalue()
            finally:
                r.close()
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            last = FetchError(f"{type(e).__name__}: {e}", None, attempt)
            time.sleep(5 * attempt)
    assert last is not None
    if getattr(last, "status", 0) is None:            # every attempt failed on the network
        _host_result(host, True)
    raise last


def unwrap(data: bytes, url: str) -> bytes:
    """Return PDF bytes from a download: plain PDF, or the first PDF inside a .zip (NSE archives)."""
    head = data[:8]
    if head.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(n for n in z.namelist() if n.lower().endswith(".pdf"))
            if not names:
                raise DocNotFound(f"zip has no PDF: {url}")
            return z.read(names[0])
    if b"%PDF" in data[:1024]:
        return data
    text = data[:4000].decode("utf-8", "ignore")
    if BLOCK_RE.search(text):
        raise FetchError(f"blocked page instead of a PDF (captcha/access denied): {url}", 403)
    raise DocNotFound(f"not a PDF ({len(data)} bytes): {url}")


# ───────── extraction ─────────
class Unreadable(Exception):
    pass


def _clean(t: str) -> str:
    return (t or "").replace("\x00", "")


def extract_pages(pdf: bytes) -> Tuple[List[str], str]:
    """Per-page text. PyMuPDF first, pypdf fallback (when pymupdf fails or finds no text at all).
    Raises NoTextError (encrypted / no text, with .pages set) or Unreadable (corrupt)."""
    pages: List[str] = []
    extractor = ""
    err: Optional[Exception] = None
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            if doc.needs_pass and not doc.authenticate(""):
                e = NoTextError("encrypted PDF")
                e.pages = doc.page_count
                raise e
            pages = [_clean(p.get_text()) for p in doc]
            extractor = f"pymupdf-{fitz.VersionBind}"
        finally:
            doc.close()
    except NoTextError:
        raise
    except Exception as e:                      # corrupt or pymupdf missing
        err = e
    if not any(p.strip() for p in pages):
        try:
            import pypdf
            rd = pypdf.PdfReader(io.BytesIO(pdf))
            if rd.is_encrypted and not rd.decrypt(""):
                e = NoTextError("encrypted PDF")
                e.pages = len(rd.pages)
                raise e
            p2 = [_clean(p.extract_text() or "") for p in rd.pages]
            if any(p.strip() for p in p2) or not pages:
                pages, extractor = p2, f"pypdf-{pypdf.__version__}"
        except NoTextError:
            raise
        except Exception as e:
            err = err or e
            if not pages:
                raise Unreadable(f"{type(err).__name__}: {err}")
    if not any(p.strip() for p in pages):
        ocr = ocr_pages(pdf)
        if ocr is not None and any(p.strip() for p in ocr):
            return ocr, "tesseract"
        e = NoTextError("no extractable text (scanned?)" + ("" if ocr is not None else "; OCR unavailable"))
        e.pages = len(pages)
        raise e
    return pages, extractor


OCR_MAX_PAGES = 600
OCR_DPI = 200


def ocr_available() -> bool:
    import os
    import shutil
    if os.environ.get("DOCS_NO_OCR"):
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def ocr_pages(pdf: bytes) -> Optional[List[str]]:
    """OCR every page (PyMuPDF render at 200 dpi + tesseract eng), capped at 600 pages. None if tesseract is missing."""
    if not ocr_available():
        return None
    import pytesseract
    from PIL import Image
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        n = doc.page_count
        if n > OCR_MAX_PAGES:
            log.warning("OCR: %d pages, only the first %d are read", n, OCR_MAX_PAGES)
        out = []
        for i in range(min(n, OCR_MAX_PAGES)):
            pix = doc[i].get_pixmap(dpi=OCR_DPI)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            out.append(_clean(pytesseract.image_to_string(img, lang="eng")))
        out += [""] * (n - len(out))
        return out
    finally:
        doc.close()


def write_text(symbol: str, doc_type: str, label: Optional[str], sha: str, pages: List[str]) -> Path:
    d = DOCS_DIR / safe_name(symbol) / doc_type
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slugify(label)}_{sha[:12]}.jsonl.gz"
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        for i, t in enumerate(pages, 1):
            if t.strip():
                f.write(json.dumps({"page": i, "text": t}, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return p


# ───────── catalog (equity.document) ─────────
def _upsert_pending(cur, company_id, doc_type, url, label, src) -> None:
    cur.execute("""insert into equity.document (company_id, doc_type, url, period_label, fiscal_year, doc_date, source)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (company_id, doc_type, url) do update set period_label=excluded.period_label,
                       fiscal_year=excluded.fiscal_year, doc_date=excluded.doc_date, source=excluded.source""",
                (company_id, doc_type, url, label, derive_fiscal_year(doc_type, label),
                 derive_doc_date(label) if doc_type != "annual_report" else None, src))


def _set_status(cur, company_id, doc_type, url, status, **kw) -> None:
    cols = {"status": status, "error": kw.get("error"), "page_count": kw.get("page_count"),
            "char_count": kw.get("char_count"), "pdf_bytes": kw.get("pdf_bytes"), "pdf_sha256": kw.get("sha"),
            "text_path": kw.get("text_path"), "extractor": kw.get("extractor"),
            "extracted_at": datetime.now(timezone.utc) if status in ("ok", "no_text") else None}
    sets = ", ".join(f"{k}=%s" for k in cols)
    cur.execute(f"update equity.document set {sets}, text_location='local' "
                f"where company_id=%s and doc_type=%s and url=%s",
                (*cols.values(), company_id, doc_type, url))


def _note_primary(cur, company_id, doc_type, url, result: str) -> None:
    """Stamp a failed download of the document's own url (attempts_log source 'primary'): run_company then skips the
    url for PRIMARY_COOLDOWN_DAYS. Only the latest 'primary' entry is kept, so a url that fails every week stays small."""
    entry = json.dumps([{"source": "primary", "url": url, "result": result[:200],
                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}])
    cur.execute("""update equity.document d set attempts_log = coalesce(
                       (select jsonb_agg(e.v order by e.i) from jsonb_array_elements(d.attempts_log) with ordinality e(v, i)
                        where e.v->>'source' is distinct from 'primary'), '[]'::jsonb) || %s::jsonb
                   where company_id=%s and doc_type=%s and url=%s""", (entry, company_id, doc_type, url))


def process_document(url: str, symbol: str, doc_type: str, label: Optional[str]) -> dict:
    """Download + extract + write one document. Returns {'status', ...fields}. Raises FetchError (transient/blocked)
    and DocNotFound (404); NoTextError / Unreadable are converted to statuses here."""
    t0 = time.monotonic()
    return process_bytes(download(url), url, symbol, doc_type, label, t0)


def process_bytes(raw: bytes, url: str, symbol: str, doc_type: str, label: Optional[str],
                  t0: Optional[float] = None) -> dict:
    """Sniff the format (PDF / HTML / DOCX / PPTX / TXT / ZIP of those), extract per-page text, write the file."""
    t0 = t0 or time.monotonic()
    try:
        data, fmt = sniff_and_unwrap(raw, url)
    except UnsupportedFormat as e:
        return {"sha": hashlib.sha256(raw).hexdigest(), "pdf_bytes": len(raw), "status": "no_text",
                "error": f"unsupported_format: {e}", "char_count": 0, "download_s": round(time.monotonic() - t0, 2)}
    sha = hashlib.sha256(data).hexdigest()
    base = {"sha": sha, "pdf_bytes": len(data), "download_s": round(time.monotonic() - t0, 2)}
    t1 = time.monotonic()
    try:
        pages, extractor = extract_pages(data) if fmt == "pdf" else extract_other(data, fmt)
    except NoTextError as e:
        return {**base, "status": "no_text", "error": str(e), "page_count": getattr(e, "pages", None),
                "char_count": 0, "extract_s": round(time.monotonic() - t1, 2)}
    except Unreadable as e:
        return {**base, "status": "failed", "error": f"unreadable document: {e}"[:500],
                "extract_s": round(time.monotonic() - t1, 2)}
    path = write_text(symbol, doc_type, label, sha, pages)
    return {**base, "status": "ok", "page_count": len(pages), "char_count": sum(len(p) for p in pages),
            "text_path": str(path.relative_to(config.ROOT)), "extractor": extractor,
            "extract_s": round(time.monotonic() - t1, 2)}


class UnsupportedFormat(Exception):
    pass


def sniff_and_unwrap(raw: bytes, url: str, depth: int = 0) -> Tuple[bytes, str]:
    """-> (bytes, fmt) with fmt in pdf|html|docx|pptx|txt. Looks at magic bytes, not the url or content-type."""
    head = raw[:2048]
    zipped = raw[:4] == b"PK\x03\x04"
    # the zip test comes first: a ZIP_STORED archive of a PDF (common for NSE annual reports) has '%PDF' in its first KB
    if not zipped and b"%PDF" in head[:1024]:
        return raw, "pdf"
    if zipped or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return raw, "docx"
                if any(n.startswith("ppt/") for n in names):
                    return raw, "pptx"
            if depth >= 1:
                raise DocNotFound(f"nested zip: {url}")
            order = {".pdf": 0, ".docx": 1, ".pptx": 2, ".htm": 3, ".html": 3, ".txt": 4}
            members = sorted((n for n in names if n.lower().endswith(tuple(order))),
                             key=lambda n: (order[next(e for e in order if n.lower().endswith(e))], n))
            if not members:
                other = [n for n in names if n.lower().endswith((".doc", ".ppt"))]
                if other:
                    raise UnsupportedFormat(f"legacy {other[0].rsplit('.', 1)[-1]} inside zip")
                raise DocNotFound(f"zip has no supported document: {url}")
            return sniff_and_unwrap(z.read(members[0]), url, depth + 1)
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":              # legacy OLE2: .doc / .ppt / .xls
        converted = convert_legacy(raw)
        if converted is None:
            raise UnsupportedFormat("legacy .doc/.ppt and no libreoffice available")
        return converted, "pdf"
    text = head.decode("utf-8", "ignore")
    low = text.lower()
    if "<html" in low or "<!doctype html" in low or "<body" in low or "<head" in low:
        if BLOCK_RE.search(raw[:6000].decode("utf-8", "ignore")) and len(raw) < 30000:
            raise FetchError(f"blocked page (captcha/access denied): {url}", 403)
        return raw, "html"
    printable = sum(1 for ch in raw[:4000] if ch in (9, 10, 13) or 32 <= ch < 127 or ch >= 160)
    if raw and printable / max(len(raw[:4000]), 1) > 0.95 and len(raw) > 500:
        return raw, "txt"
    raise DocNotFound(f"not a recognised document ({len(raw)} bytes): {url}")


def convert_legacy(raw: bytes) -> Optional[bytes]:
    import os
    import shutil
    import subprocess
    import tempfile
    exe = shutil.which("libreoffice") or shutil.which("soffice")
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.bin")
        open(src, "wb").write(raw)
        try:
            subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", d, src], capture_output=True, timeout=180)
            out = os.path.join(d, "in.pdf")
            return open(out, "rb").read() if os.path.exists(out) else None
        except Exception:
            return None


def paginate(paras: List[str], size: int = 3000) -> List[str]:
    """Pack paragraphs into ~size-char pseudo-pages (paragraph breaks kept; overlong paragraphs are split)."""
    pages, cur = [], ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                pages.append(cur)
                cur = ""
            pages.append(para[:cut].strip())
            para = para[cut:].strip()
        if cur and len(cur) + len(para) + 2 > size:
            pages.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        pages.append(cur)
    return pages


def html_paragraphs(data: bytes) -> List[str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile("content|main", re.I)) or soup.body or soup
    paras = []
    for el in main.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote", "td", "div"]):
        if el.name == "div" and el.find(["p", "div", "li", "td"]):
            continue                              # only leaf blocks
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if txt:
            paras.append(txt)
    if not paras:
        paras = [re.sub(r"\s+", " ", main.get_text(" ", strip=True))]
    return paras


def extract_other(data: bytes, fmt: str) -> Tuple[List[str], str]:
    try:
        if fmt == "html":
            pages = paginate(html_paragraphs(data))
            if sum(len(p) for p in pages) < 1500:            # soft-404 / stub pages are not transcripts
                raise DocNotFound("html page with too little text")
            return pages, "bs4-html"
        if fmt == "docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            paras = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    paras.append(" | ".join(c.text.strip() for c in row.cells))
            pages = paginate(paras)
            ex = "python-docx"
        elif fmt == "pptx":
            from pptx import Presentation
            prs = Presentation(io.BytesIO(data))
            pages = []
            for sl in prs.slides:
                parts = []
                for sh in sl.shapes:
                    if sh.has_text_frame:
                        parts.append(sh.text_frame.text)
                    if getattr(sh, "has_table", False) and sh.has_table:
                        for row in sh.table.rows:
                            parts.append(" | ".join(c.text for c in row.cells))
                if sl.has_notes_slide:
                    parts.append("Notes: " + sl.notes_slide.notes_text_frame.text)
                pages.append(_clean("\n".join(x for x in parts if x.strip())))
            ex = "python-pptx"
        else:
            txt = data.decode("utf-8", "ignore")
            pages = paginate(re.split(r"\n\s*\n", txt))
            ex = "plain-text"
    except DocNotFound:
        raise
    except Exception as e:
        raise Unreadable(f"{type(e).__name__}: {e}")
    if not any(p.strip() for p in pages):
        e = NoTextError("no text in document")
        e.pages = len(pages)
        raise e
    return [_clean(p) for p in pages], ex


_SRC_ORDER = {"bse": 0, "nse": 1}


def run_company(db, company_id: int, run_id: int, kind: str, force: bool = False, limit: Optional[int] = None,
                hosts: Optional[set] = None) -> dict:
    """Process every document_link of the kind's doc_type for one company. Idempotent: documents already ok
    (and, unless force, no_text / missing, and failed ones within PRIMARY_COOLDOWN_DAYS of their last download) are
    skipped. Raises the first transient FetchError AFTER processing everything else so the rule engine can retry the
    job (finished documents are skipped next time)."""
    doc_type = DOC_TYPE_OF[kind]
    t0 = time.monotonic()
    with db.trail.cursor() as cur:
        cur.execute("select coalesce(nse_symbol, screener_slug, bse_code) from equity.company where company_id=%s",
                    (company_id,))
        r = cur.fetchone()
        if r is None:
            raise LookupError(f"company_id {company_id} not in equity.company")
        symbol = r[0]
        cur.execute("select url, period_label, source from equity.document_link where company_id=%s and doc_type=%s "
                    "order by period_label desc nulls last, url", (company_id, doc_type))
        links = cur.fetchall()
        # cooling: failed, and its url was downloaded (primary or the fallback's original_retry) in the cooldown window
        cur.execute("""select url, status, status = 'failed' and exists (
                           select 1 from jsonb_array_elements(attempts_log) e
                           where e->>'source' = any(%s) and (e->>'at')::timestamptz > now() - make_interval(days => %s))
                       from equity.document where company_id=%s and doc_type=%s""",
                    (COOLDOWN_SOURCES, PRIMARY_COOLDOWN_DAYS, company_id, doc_type))
        rows = cur.fetchall()
    have = {url: status for url, status, _ in rows}
    cooling = set() if force else {url for url, _, c in rows if c}

    groups: Dict[str, list] = {}
    for url, label, src in links:
        groups.setdefault(label or url, []).append((url, label, source_of(url, src)))
    stats = {"symbol": symbol, "doc_type": doc_type, "links": len(links), "groups": len(groups), "ok": 0,
             "no_text": 0, "missing": 0, "failed": 0, "skipped": 0, "cooling": 0, "groups_done": 0, "pages": 0,
             "chars": 0, "docs": []}
    transient: Optional[Exception] = None

    for key, members in groups.items():
        members.sort(key=lambda m: _SRC_ORDER.get(m[2], 2))
        done = [m for m in members if have.get(m[0]) == "ok" or (not force and have.get(m[0]) in TERMINAL)]
        if done:
            stats["skipped"] += 1
            continue
        if hosts is not None:
            # host routing: one polite process per host never overlaps another. A process only attempts members on
            # its own hosts, and the non-BSE processes wait until the BSE member has been tried (BSE goes first).
            if "bse" not in hosts and any(m[2] == "bse" and have.get(m[0]) is None for m in members):
                stats["skipped"] += 1
                continue
            members = [m for m in members if m[2] in hosts]
            if not members:
                stats["skipped"] += 1
                continue
        if cooling:                                  # an alternate url of the same document is still tried
            members = [m for m in members if m[0] not in cooling]
            if not members:
                stats["skipped"] += 1
                stats["cooling"] += 1
                continue
        if limit is not None and stats["groups_done"] >= limit:
            break
        stats["groups_done"] += 1
        with db.trail.cursor() as cur:
            for url, label, src in members:
                _upsert_pending(cur, company_id, doc_type, url, label, src)
        gt: Optional[Exception] = None
        resolved = False
        for url, label, src in members:
            t1 = time.monotonic()
            try:
                res = process_document(url, symbol, doc_type, label)
            except DocNotFound as e:
                with db.trail.cursor() as cur:
                    _set_status(cur, company_id, doc_type, url, "missing", error=str(e)[:500])
                stats["missing"] += 1
                log.info("%s %s: missing (%s)", symbol, label, e)
                continue                                    # try the alternate url (R5)
            except FetchError as e:
                with db.trail.cursor() as cur:
                    _set_status(cur, company_id, doc_type, url, "failed", error=str(e)[:500])
                    if not getattr(e, "host_skipped", False):
                        _note_primary(cur, company_id, doc_type, url, f"FetchError: {e}")
                gt = gt or e
                stats["failed"] += 1
                if e.status_code in (403, 429):             # blocked / throttled: stop hammering, let R1/R2 decide
                    _finish(db, run_id, company_id, kind, stats, t0)
                    raise
                continue
            except Exception as e:                           # unexpected: record and move on
                with db.trail.cursor() as cur:
                    _set_status(cur, company_id, doc_type, url, "failed", error=f"{type(e).__name__}: {e}"[:500])
                    _note_primary(cur, company_id, doc_type, url, f"{type(e).__name__}: {e}")
                log.exception("%s %s: unexpected error", symbol, label)
                continue
            st = res["status"]
            with db.trail.cursor() as cur:
                _set_status(cur, company_id, doc_type, url, st, **{k: v for k, v in res.items() if k != "status"})
                if st == "failed":                                  # unreadable
                    _note_primary(cur, company_id, doc_type, url, res.get("error") or "failed")
            stats[st] += 1
            stats["pages"] += res.get("page_count") or 0
            stats["chars"] += res.get("char_count") or 0
            stats["docs"].append({"label": label, "status": st, "pages": res.get("page_count"),
                                  "chars": res.get("char_count"), "bytes": res.get("pdf_bytes"),
                                  "seconds": round(time.monotonic() - t1, 2),
                                  "download_s": res.get("download_s"), "extract_s": res.get("extract_s")})
            log.info("%s %s %s: %s pages=%s chars=%s %.1fs", symbol, doc_type, label, st, res.get("page_count"),
                     res.get("char_count"), time.monotonic() - t1)
            if st in ("ok", "no_text"):
                resolved = True
                break
            # 'failed' (unreadable): try the alternate
        if gt is not None and not resolved:
            transient = transient or gt
    _finish(db, run_id, company_id, kind, stats, t0)
    if transient is not None:
        raise transient
    return stats


def _finish(db, run_id, company_id, kind, stats, t0) -> None:
    stats["seconds"] = round(time.monotonic() - t0, 1)
    failed = stats["failed"]
    try:
        db.record_task(run_id, company_id, f"{kind}", "failed" if (failed and not stats["ok"]) else "ok",
                       rows_written=stats["ok"] + stats["no_text"],
                       error=None if not failed else f"{failed} docs failed")
    except Exception:
        log.exception("could not record task")


# ───────── bulk load into the DB (after an upgrade) ─────────
def load_documents(db, limit: Optional[int] = None, batch_docs: int = 20) -> dict:
    """Bulk-load local jsonl.gz into equity.document_page and flip text_location to 'db'.
    NOT for the free tier: the text is far larger than the 500 MB database."""
    with db.trail.cursor() as cur:
        cur.execute("select company_id, doc_type, url, text_path from equity.document "
                    "where status='ok' and text_location='local' and text_path is not null order by company_id, url")
        todo = cur.fetchall()
    if limit:
        todo = todo[:limit]
    n_docs = n_pages = 0
    for cid, dt, url, path in todo:
        p = config.ROOT / path
        if not p.exists():
            log.warning("missing file %s", p)
            continue
        rows = []
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                rows.append((cid, dt, url, o["page"], o["text"]))
        with db.trail.cursor() as cur:
            execute_values(cur, "insert into equity.document_page (company_id, doc_type, url, page_no, text) values %s "
                                "on conflict do nothing", rows, page_size=200)
            cur.execute("update equity.document set text_location='db' where company_id=%s and doc_type=%s and url=%s",
                        (cid, dt, url))
        n_docs += 1
        n_pages += len(rows)
        if n_docs % batch_docs == 0:
            log.info("loaded %d documents, %d pages", n_docs, n_pages)
    return {"documents": n_docs, "pages": n_pages}


KIND_ALIASES = {"annual_report": "docs_ar", "concall_transcript": "docs_concall", "concall_ppt": "docs_ppt",
                "docs_ar": "docs_ar", "docs_concall": "docs_concall", "docs_ppt": "docs_ppt"}


def _has_links(db, company_id: int, doc_type: str) -> bool:
    with db.trail.cursor() as cur:
        cur.execute("select 1 from equity.document_link where company_id=%s and doc_type=%s limit 1",
                    (company_id, doc_type))
        return cur.fetchone() is not None


def run_docs_cli(db, symbols: Optional[List[str]], kinds: List[str], force: bool = False, limit: Optional[int] = None,
                 universe: str = config.UNIVERSE_CODE, shard=None, slots: int = 1, retry_missing: bool = False, discover: bool = False, hosts: Optional[set] = None) -> int:
    """`docs`: run the documents pipeline directly (no queue) for --symbols or a universe shard, with `slots`
    threads. Companies without document_link rows yet are skipped (a later run picks them up). Idempotent."""
    from concurrent.futures import ThreadPoolExecutor
    from .db import DB
    from .pipeline import shard_items, shard_ranked
    kinds = [KIND_ALIASES[k] for k in kinds]
    if symbols:
        companies = []
        for sym in shard_items(symbols, shard):
            with db.trail.cursor() as cur:
                cur.execute("select company_id, coalesce(nse_symbol, screener_slug) from equity.company "
                            "where nse_symbol=%s or screener_slug=%s", (sym, sym))
                r = cur.fetchone()
            if r is None:
                log.warning("%s not in equity.company", sym)
                continue
            companies.append(r)
    else:
        companies = [(r[1], r[2]) for r in shard_ranked(db.universe_companies(universe), shard)]
    run_id = db.start_run("docs", {"symbols": symbols, "universe": universe, "kinds": kinds, "force": force,
                                   "limit": limit, "slots": slots, "shard": f"{shard[0]}/{shard[1]}" if shard else None})
    lock = threading.Lock()
    all_docs: Dict[str, list] = {}
    tot = {"failures": 0, "no_links": 0, "companies": len(companies)}
    local = threading.local()
    opened: list = []

    def tdb():
        if slots == 1:
            return db
        d = getattr(local, "db", None)
        if d is None:
            d = local.db = DB(db.url)
            with lock:
                opened.append(d)
        return d

    po = None
    recovered: Dict[str, int] = {}
    if retry_missing or discover:
        from .fallback import Polite
        po = Polite()

    def work(item):
        cid, sym = item
        d = tdb()
        for kind in kinds:
            if discover:
                if kind != "docs_concall":
                    continue
                from .fallback import discover_company
                try:
                    st = discover_company(d, cid, run_id, po)
                except Exception as e:
                    with lock:
                        tot["failures"] += 1
                    log.error("%s discover: %s: %s", sym, type(e).__name__, e)
                    continue
                with lock:
                    tot["discovered"] = tot.get("discovered", 0) + st["discovered"]
                    print(f"{sym} discover: expected={st['expected']} found={st['found']} missing={st['missing']} "
                          f"discovered={st['discovered']}", flush=True)
                continue
            if retry_missing:
                from .fallback import retry_company
                try:
                    st = retry_company(d, cid, run_id, kind, po)
                except Exception as e:
                    with lock:
                        tot["doc_errors"] = tot.get("doc_errors", 0) + (not isinstance(e, psycopg2.Error))
                        tot["failures"] += isinstance(e, psycopg2.Error)
                    log.error("%s %s retry-missing: %s: %s", sym, kind, type(e).__name__, e)
                    continue
                with lock:
                    for k, v in st["by_source"].items():
                        recovered[k] = recovered.get(k, 0) + v
                    tot["tried"] = tot.get("tried", 0) + st["tried"]
                    tot["recovered"] = tot.get("recovered", 0) + st["recovered"]
                    print(f"{sym} {kind}: retried={st['tried']} recovered={st['recovered']} {st['by_source']} "
                          f"still_missing={st['still_missing']} {st['seconds']}s", flush=True)
                continue
            if not _has_links(d, cid, DOC_TYPE_OF[kind]):
                with lock:
                    tot["no_links"] += 1
                log.info("%s %s: no document_link rows yet, skipped", sym, kind)
                continue
            try:
                st = run_company(d, cid, run_id, kind, force=force, limit=limit, hosts=hosts)
            except Exception as e:
                with lock:
                    tot["doc_errors"] = tot.get("doc_errors", 0) + 1
                    if isinstance(e, psycopg2.Error):
                        tot["failures"] += 1
                log.error("%s %s: %s: %s", sym, kind, type(e).__name__, e)
                continue
            with lock:
                all_docs.setdefault(kind, []).extend(st["docs"])
                print(f"{sym} {kind}: ok={st['ok']} no_text={st['no_text']} missing={st['missing']} "
                      f"failed={st['failed']} skipped={st['skipped']} pages={st['pages']} chars={st['chars']} "
                      f"{st['seconds']}s", flush=True)

    try:
        if slots == 1:
            for c in companies:
                work(c)
        else:
            with ThreadPoolExecutor(max_workers=slots) as ex:
                list(ex.map(work, companies))
    finally:
        for d in opened:
            d.close()
    for kind, docs in all_docs.items():
        n = len(docs)
        if n:
            print(f"== {kind}: {n} docs, avg {sum(x['seconds'] for x in docs) / n:.1f}s, "
                  f"avg pages {sum(x['pages'] or 0 for x in docs) / n:.0f}, "
                  f"avg chars {sum(x['chars'] or 0 for x in docs) / n:.0f}")
    if retry_missing:
        tot["by_source"] = recovered
        print(f"== retry-missing: tried {tot.get('tried', 0)}, recovered {tot.get('recovered', 0)} by source {recovered}")
    db.finish_run(run_id, "partial" if tot["failures"] else "success", tot)
    print(f"== {len(companies)} companies, {tot['no_links']} kind-runs skipped for no links, "
          f"{tot['failures']} failures")
    return 1 if tot["failures"] else 0


def docs_coverage(db, universe: str = config.UNIVERSE_CODE) -> None:
    """Print equity.v_document_completeness for the universe."""
    with db.trail.cursor() as cur:
        cur.execute("""select v.nse_symbol, v.ar_found, v.ar_expected, v.ar_missing, v.cc_found, v.cc_expected,
                              coalesce(array_length(v.cc_missing,1),0)
                       from equity.v_document_completeness v
                       where v.company_id in (select company_id from equity.universe where universe_code=%s and as_of=
                             (select max(as_of) from equity.universe where universe_code=%s))
                       order by v.nse_symbol""", (universe, universe))
        rows = cur.fetchall()
    print("symbol\tAR found/expected\tAR missing FYs\tcalls found/expected\tcalls missing")
    tf = te = cf = ce = 0
    for sym, af, ae, am, qf, qe, qm in rows:
        print(f"{sym}\t{af}/{ae}\t{','.join(map(str, am)) or '-'}\t{qf}/{qe}\t{qm}")
        tf, te, cf, ce = tf + af, te + ae, cf + qf, ce + qe
    if rows:
        print(f"-- {len(rows)} companies: annual reports {tf}/{te} ({100 * tf / max(te, 1):.1f}%), "
              f"concall quarters {cf}/{ce} ({100 * cf / max(ce, 1):.1f}%)")


def collect_documents(db, run_id: str, repo: Optional[str] = None, dest: Optional[Path] = None) -> dict:
    """Download a workflow run's `documents-shard-*` artifacts with `gh run download`, merge them into data/documents
    without overwriting existing files, then verify every equity.document row with status ok has its file locally."""
    import os
    import shutil
    import subprocess
    import tempfile
    repo = repo or os.environ.get("GH_REPO", "j-s-l-7/Scraper")
    dest = dest or DOCS_DIR
    merged = skipped = 0
    with tempfile.TemporaryDirectory(prefix="docs-collect-") as tmp:
        r = subprocess.run(["gh", "run", "download", str(run_id), "-R", repo, "-D", tmp, "-p", "documents-shard-*"],
                           capture_output=True, text=True)
        err = (r.stderr or r.stdout).strip()
        if r.returncode != 0 and "no valid artifacts" not in err:   # shards that fetched nothing upload nothing
            raise RuntimeError(f"gh run download failed: {err[:300]}")
        merged, skipped = merge_tree(Path(tmp), dest)
    out = verify_documents(db)
    out.update({"merged": merged, "skipped_existing": skipped})
    return out


def merge_tree(src: Path, dest: Path) -> Tuple[int, int]:
    """Copy every *.jsonl.gz under src (any depth; artifacts unpack to documents-shard-k/<SYMBOL>/<doc_type>/...)
    into dest/<SYMBOL>/<doc_type>/..., never overwriting. Returns (copied, skipped)."""
    import shutil
    copied = skipped = 0
    for f in sorted(src.rglob("*.jsonl.gz")):
        parts = f.parts
        # locate <SYMBOL>/<doc_type>/<file> as the last three components
        rel = Path(*parts[-3:])
        target = dest / rel
        if target.exists():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        copied += 1
    return copied, skipped


def verify_documents(db) -> dict:
    with db.trail.cursor() as cur:
        cur.execute("select text_path from equity.document where status='ok' and text_location='local'")
        paths = [r[0] for r in cur.fetchall()]
    missing = [p for p in paths if not p or not (config.ROOT / p).exists()]
    print(f"documents ok in catalog: {len(paths)}; text file missing locally: {len(missing)}")
    for p in missing[:20]:
        print("  missing:", p)
    return {"ok_rows": len(paths), "missing": len(missing)}
