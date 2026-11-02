import gzip
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from screener_scraper import documents as docs
from screener_scraper.rules import DocNotFound, NoTextError


def make_pdf(pages):
    import pymupdf as fitz
    d = fitz.open()
    for t in pages:
        p = d.new_page()
        if t:
            p.insert_text((72, 72), t)
    b = d.tobytes()
    d.close()
    return b


def test_fiscal_year_annual():
    f = docs.derive_fiscal_year
    assert f("annual_report", "Annual Report 2026") == 2026
    assert f("annual_report", "Financial Year 2024-25") == 2025
    assert f("annual_report", "2019") == 2019
    assert f("annual_report", None) is None
    assert f("annual_report", "no year") is None


def test_fiscal_year_concall():
    f = docs.derive_fiscal_year
    assert f("concall_transcript", "Oct 2026") == 2027
    assert f("concall_transcript", "Dec 2025") == 2026
    assert f("concall_transcript", "Jan 2026") == 2026
    assert f("concall_transcript", "Mar 2026") == 2026
    assert f("concall_transcript", "Apr 2026") == 2027
    assert docs.derive_doc_date("Oct 2026") == date(2026, 10, 1)


def test_slug_and_source():
    assert docs.slugify("Annual Report 2026!") == "annual-report-2026"
    assert docs.slugify(None) == "doc"
    assert docs.source_of("https://www.bseindia.com/x.pdf") == "bse"
    assert docs.source_of("https://archives.nseindia.com/x.zip") == "nse"
    assert docs.source_of("https://example.com/x.pdf", "other") == "other"


def test_headers():
    assert docs.doc_headers("https://www.bseindia.com/a.pdf")["Referer"] == "https://www.bseindia.com/"


def test_extract_pages_pymupdf():
    pages, ext = docs.extract_pages(make_pdf(["hello alpha", "", "page three"]))
    assert len(pages) == 3 and "alpha" in pages[0] and pages[1].strip() == "" and ext.startswith("pymupdf")


def test_no_text_pdf():
    with pytest.raises(NoTextError) as ei:
        docs.extract_pages(make_pdf(["", ""]))
    assert ei.value.pages == 2


def test_corrupt_pdf_unreadable():
    with pytest.raises(docs.Unreadable):
        docs.extract_pages(b"%PDF-1.4 garbage not really a pdf")


def test_unwrap_zip_first_pdf_and_plain():
    pdf = make_pdf(["x"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("b.txt", "n")
        z.writestr("z.pdf", b"other")
        z.writestr("a.pdf", pdf)
    assert docs.unwrap(buf.getvalue(), "u") == pdf
    assert docs.unwrap(pdf, "u") == pdf


def test_unwrap_errors():
    with pytest.raises(DocNotFound):
        docs.unwrap(b"<html>not found</html>", "u")
    from screener_scraper.http import FetchError
    with pytest.raises(FetchError) as ei:
        docs.unwrap(b"<html>Access Denied</html>", "u")
    assert ei.value.status_code == 403
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "n")
    with pytest.raises(DocNotFound):
        docs.unwrap(buf.getvalue(), "u")


def test_write_text_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path)
    p = docs.write_text("TCS", "annual_report", "Annual Report 2026", "a" * 64, ["one", "", "three"])
    assert p.name == "annual-report-2026_aaaaaaaaaaaa.jsonl.gz" and p.parent == tmp_path / "TCS" / "annual_report"
    lines = [json.loads(l) for l in gzip.open(p, "rt")]
    assert lines == [{"page": 1, "text": "one"}, {"page": 3, "text": "three"}]


def test_spacer_backoff():
    s = docs.Spacer(0.5)
    s.on_429(); s.on_429()
    assert s.interval == 2.0
    for _ in range(40):
        s.on_429()
    assert s.interval == docs.DOC_MAX_INTERVAL


def test_merge_tree_never_overwrites(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    for shard, sym in (("documents-shard-1", "TCS"), ("documents-shard-2", "INFY")):
        f = src / shard / sym / "annual_report" / "annual-report-2026_abc.jsonl.gz"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"new")
    existing = dest / "TCS" / "annual_report" / "annual-report-2026_abc.jsonl.gz"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    assert docs.merge_tree(src, dest) == (1, 1)
    assert existing.read_bytes() == b"old"
    assert (dest / "INFY" / "annual_report" / "annual-report-2026_abc.jsonl.gz").read_bytes() == b"new"


def test_kind_aliases():
    assert docs.KIND_ALIASES["annual_report"] == "docs_ar" and docs.KIND_ALIASES["concall_transcript"] == "docs_concall"


def test_sniff_html_docx_pptx_txt_zip():
    import docx
    from pptx import Presentation
    html = b"<html><head><title>t</title></head><body><nav>menu</nav><main><p>" + b"Hello transcript. " * 120 + \
           b"</p><p>Second para</p></main><footer>foot</footer><script>x=1</script></body></html>"
    data, fmt = docs.sniff_and_unwrap(html, "u")
    assert fmt == "html"
    pages, ex = docs.extract_other(data, "html")
    joined = "\n".join(pages)
    assert ex == "bs4-html" and "Hello transcript" in joined and "menu" not in joined and "foot" not in joined \
        and "x=1" not in joined and all(len(p) <= 3200 for p in pages)
    with pytest.raises(DocNotFound):
        docs.extract_other(b"<html><body><p>Page not found</p></body></html>", "html")

    buf = io.BytesIO()
    d = docx.Document()
    d.add_paragraph("Opening remarks from the CFO")
    d.save(buf)
    data, fmt = docs.sniff_and_unwrap(buf.getvalue(), "u")
    assert fmt == "docx" and "CFO" in docs.extract_other(data, fmt)[0][0]

    buf = io.BytesIO()
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Q2 highlights"
    s1.notes_slide.notes_text_frame.text = "speaker note"
    prs.save(buf)
    data, fmt = docs.sniff_and_unwrap(buf.getvalue(), "u")
    pages, ex = docs.extract_other(data, fmt)
    assert fmt == "pptx" and len(pages) == 1 and "Q2 highlights" in pages[0] and "speaker note" in pages[0]

    txt = (b"Plain transcript line. " * 60)
    assert docs.sniff_and_unwrap(txt, "u")[1] == "txt"
    assert docs.extract_other(txt, "txt")[1] == "plain-text"

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("notes.txt", txt)
        z.writestr("a.docx", buf2 := b"")  # placeholder, ignored below
    # zip with a real docx inside
    dbuf = io.BytesIO()
    d2 = docx.Document(); d2.add_paragraph("inside zip"); d2.save(dbuf)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("readme.txt", txt)
        z.writestr("call.docx", dbuf.getvalue())
    data, fmt = docs.sniff_and_unwrap(zbuf.getvalue(), "u")
    assert fmt == "docx"


def test_legacy_and_garbage():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
    import shutil
    if not (shutil.which("libreoffice") or shutil.which("soffice")):
        with pytest.raises(docs.UnsupportedFormat):
            docs.sniff_and_unwrap(ole, "u")
        r = docs.process_bytes(ole, "u", "X", "concall_ppt", "Oct 2023")
        assert r["status"] == "no_text" and "unsupported_format" in r["error"]
    with pytest.raises(DocNotFound):
        docs.sniff_and_unwrap(bytes(range(256)) * 4, "u")


def test_paginate():
    pages = docs.paginate(["a" * 2000, "b" * 2000, "c" * 7000])
    assert all(len(p) <= 3000 for p in pages) and "".join(pages).count("c") == 7000


def test_ppt_kind():
    assert docs.KIND_ALIASES["concall_ppt"] == "docs_ppt" and docs.DOC_TYPE_OF["docs_ppt"] == "concall_ppt"


def test_collect_a_run_that_uploaded_nothing(monkeypatch, tmp_path):
    """An equity-docs run whose shards found nothing new uploads no artifact: that is collected, not an error."""
    import subprocess
    from types import SimpleNamespace
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="no valid artifacts found to download")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(docs, "verify_documents", lambda db: {"ok_files": 0})
    out = docs.collect_documents(None, "41501861142", "o/r", dest=tmp_path)
    assert out["merged"] == 0 and calls
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="HTTP 401"))
    with pytest.raises(RuntimeError):
        docs.collect_documents(None, "1", "o/r", dest=tmp_path)


# ───────── stored zips; the primary-download cooldown for failed documents ─────────
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_stored_zip_holding_a_pdf_is_unwrapped():
    """NSE annual-report zips are often ZIP_STORED, so the inner PDF's '%PDF' sits in the zip's first KB. That was
    sniffed as a PDF and the zip bytes went to the extractors (JASH AR 2023: 'unreadable', 30 downloads in 11 h)."""
    raw = (FIXTURES / "stored_zip_pdf.zip").read_bytes()
    assert raw[:4] == b"PK\x03\x04" and b"%PDF" in raw[:1024]          # the shape that fooled the old check
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        inner = z.read("AR_JASH_2022_2023.pdf")
    data, fmt = docs.sniff_and_unwrap(raw, "u")
    assert fmt == "pdf" and data == inner
    pages, _ = docs.extract_pages(data)
    assert "stored zip fixture" in pages[0] and "Directors report" in pages[1]


class _Cur:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.db.sql.append((sql, params))
        self.rows = next((list(v) for k, v in self.db.answers.items() if k in sql), [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _DB:
    """db.trail.cursor() answering by SQL substring; record_task is recorded."""
    def __init__(self, links, have):
        self.answers = {"coalesce(nse_symbol": [("AAA",)], "equity.document_link": links, "attempts_log": have}
        self.sql, self.tasks = [], []
        self.trail = self

    def cursor(self):
        return _Cur(self)

    def record_task(self, *a, **k):
        self.tasks.append((a, k))

    def notes(self):
        """urls that got a 'primary' attempts_log entry"""
        return [p[-1] for s, p in self.sql if s.lstrip().startswith("update") and "'primary'" in s]


BSE, NSE = "https://www.bseindia.com/a.pdf", "https://archives.nseindia.com/a.zip"


def _run(monkeypatch, have, outcome, force=False, hosts=None, links=None):
    tried = []

    def fake(url, symbol, doc_type, label):
        tried.append(url)
        r = outcome(url)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(docs, "process_document", fake)
    db = _DB(links or [(BSE, "Annual Report 2023", "bse"), (NSE, "Annual Report 2023", "nse")], have)
    try:
        st = docs.run_company(db, 1, 9, "docs_ar", force=force, hosts=hosts)
    except Exception as e:          # transient errors are raised after the whole company was processed
        st = e
    return tried, db, st


def test_a_recently_failed_url_is_not_downloaded_again(monkeypatch):
    # (url, status, cooling): both members failed within PRIMARY_COOLDOWN_DAYS
    have = [(BSE, "failed", True), (NSE, "failed", True)]
    tried, db, st = _run(monkeypatch, have, lambda u: {"status": "ok"})
    assert tried == [] and st["skipped"] == 1 and st["cooling"] == 1
    tried, _, _ = _run(monkeypatch, have, lambda u: {"status": "ok"}, force=True)     # --force overrides
    assert tried == [BSE]


def test_cooling_member_is_skipped_but_its_alternate_is_tried(monkeypatch):
    tried, _, st = _run(monkeypatch, [(BSE, "failed", True)], lambda u: {"status": "ok", "page_count": 3})
    assert tried == [NSE] and st["ok"] == 1


def test_a_failed_download_is_stamped_for_the_cooldown(monkeypatch):
    from screener_scraper.http import FetchError
    unreadable = {"status": "failed", "error": "unreadable document: PdfReadError"}
    tried, db, st = _run(monkeypatch, [], lambda u: unreadable if u == BSE else FetchError("HTTP 503", 503, 3))
    assert tried == [BSE, NSE] and db.notes() == [BSE, NSE]
    assert isinstance(st, FetchError)                       # still raised for the rule engine / retry

    def skipped(u):                                         # the host breaker made no request: no stamp
        e = FetchError("host skipped", None, 0)
        e.host_skipped = True
        return e
    tried, db, _ = _run(monkeypatch, [], skipped)
    assert tried == [BSE, NSE] and db.notes() == []


def test_an_old_failure_is_retried(monkeypatch):
    tried, db, st = _run(monkeypatch, [(BSE, "failed", False)], lambda u: {"status": "ok"})
    assert tried == [BSE] and st["ok"] == 1 and db.notes() == []
