"""
Earnings Conference-Call (concall) Transcript Ingestion
=======================================================

Fetches FULL VERBATIM quarterly earnings-call transcripts for an NSE
symbol and upserts them into the `concalls` table. Full text only —
never summaries or metadata stubs. The transcript column must contain
every word of the PDF.

Pipeline
--------
  Screener.in company page          (index only — where the PDFs live)
    → per-quarter transcript PDF    (BSE filing or company IR site)
    → Wayback Machine fallback      (company IR sites often block bots)
    → pdfplumber full-text extract
    → ON CONFLICT upsert keyed (symbol, quarter, fiscal_year)

Source hierarchy per PDF
------------------------
  1. bseindia.com  — exchange's own filed copy (SEBI-mandated since 2021)
  2. company IR site direct
  3. Wayback Machine snapshot of the IR URL (original bytes via `id_`)

Idempotent — re-running refreshes rows without duplicating. On a key
collision the longer transcript wins (protects against partial scans).

The concalls primary key is the natural composite **(symbol, fiscal_year,
quarter)** — no surrogate id (same decision as historical_prices): insert
order never tracks chronology across re-runs, so the PK doubles as the
correct default sort (per symbol, FY ascending, Q1–Q4 within the year).

Usage
-----
    # Pilot: TCS, full available history
    python ingestion/ingest_concalls.py --symbol TCS

    # Custom screener slug (usually == NSE symbol)
    python ingestion/ingest_concalls.py --symbol M&M --slug M-M

Raw PDFs are archived to ~/Desktop/claude/backups/concalls_pdfs/<SYMBOL>/
so every stored transcript can be re-verified against its source bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field

import pdfplumber
import psycopg2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(                    # no override: a caller's DATABASE_URL wins
    os.path.join(os.path.dirname(__file__), "..", ".env"),
    override=False,
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("ingest_concalls")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
REQUEST_GAP_S = 2.0  # politeness delay between downloads

PDF_ARCHIVE_ROOT = os.path.expanduser("~/Desktop/claude/backups/concalls_pdfs")

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})


@dataclass
class Concall:
    symbol: str
    label: str                    # screener listing label, e.g. "Apr 2026"
    url: str
    source: str = ""              # bse | ir_site | wayback
    pdf_bytes: bytes = b""
    text: str = ""
    page_count: int = 0
    quarter: str = ""             # Q1..Q4
    fiscal_year: str = ""         # FY2026 (Indian FY, ends March)
    call_date: str | None = None  # ISO date
    page_texts: list[str] = field(default_factory=list)  # first pages, for parsing
    problems: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdf_bytes).hexdigest()

    @property
    def word_count(self) -> int:
        return len(self.text.split())


# --------------------------------------------------------------------------
# Index: screener.in concall listing (links only, never content)
# --------------------------------------------------------------------------

def screener_transcript_links(slug: str, symbol: str) -> list[tuple[str, str]]:
    url = f"https://www.screener.in/company/{slug}/consolidated/"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    # Guard 1 — the page must belong to the claimed NSE symbol, or a slug
    # typo/mismatch would silently store another company's transcripts.
    if not re.search(r"NSE\s*:\s*" + re.escape(symbol), r.text, re.IGNORECASE):
        raise ValueError(
            f"screener page '{slug}' does not show 'NSE: {symbol}' — "
            "wrong slug for this symbol, or page markup changed")

    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.find_all("li"):
        a = li.find("a", string=lambda s: s and s.strip().lower() == "transcript")
        if not a:
            continue
        label_div = li.find("div")
        label = label_div.get_text(strip=True) if label_div else "?"
        out.append((label, a["href"]))

    # Guard 2 — distinguish "company has no concalls" from "markup changed":
    # a schema drift would otherwise silently no-op the whole backfill.
    if not out:
        if "concall" not in r.text.lower():
            raise RuntimeError(
                f"screener page '{slug}': no concalls section found — "
                "markup changed? refusing to treat as 'no transcripts'")
        log.warning("screener lists a concalls section but zero transcript "
                    "links for %s — genuinely none published yet", slug)

    log.info("screener index: %d transcript links for %s", len(out), slug)
    return out


# --------------------------------------------------------------------------
# Download with Wayback fallback
# --------------------------------------------------------------------------

def wayback_snapshot(url: str) -> str | None:
    # Layer 1: availability API (fast but misses snapshots sometimes)
    try:
        j = requests.get(
            "http://archive.org/wayback/available",
            params={"url": url}, timeout=30,
        ).json()
        snap = (j.get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available"):
            # id_ suffix returns the original archived bytes (no banner)
            return re.sub(r"/web/(\d+)/", r"/web/\1id_/", snap["url"])
    except Exception as e:  # noqa: BLE001 — wayback is best-effort
        log.warning("wayback availability lookup failed for %s: %s", url, e)
    # Layer 2: CDX index — authoritative, catches what availability misses
    try:
        r = requests.get(
            "http://web.archive.org/cdx/search/cdx",
            params={"url": url, "output": "json", "limit": "-3",
                    "filter": "statuscode:200"},
            timeout=60,
        )
        rows = r.json() if r.status_code == 200 and r.text.strip() else []
        if len(rows) > 1:                      # rows[0] is the header
            ts, original = rows[-1][1], rows[-1][2]
            return f"http://web.archive.org/web/{ts}id_/{original}"
    except Exception as e:  # noqa: BLE001
        log.warning("wayback CDX lookup failed for %s: %s", url, e)
    return None


def download_pdf(cc: Concall) -> bool:
    """Fill cc.pdf_bytes + cc.source. Direct first, Wayback fallback."""
    headers = dict(HEADERS)
    if "bseindia.com" in cc.url:
        candidates = [(cc.url, "bse")]
    else:
        candidates = [(cc.url, "ir_site")]

    for url, source in candidates:
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                cc.pdf_bytes, cc.source = r.content, source
                return True
            log.info("direct fetch %s -> HTTP %s (%s); trying wayback",
                     url[:80], r.status_code, r.headers.get("content-type"))
        except Exception as e:  # noqa: BLE001
            log.warning("direct fetch error %s: %s", url[:80], e)

    wb = wayback_snapshot(cc.url)
    if wb:
        try:
            r = requests.get(wb, headers=HEADERS, timeout=90)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                cc.pdf_bytes, cc.source = r.content, "wayback"
                return True
            log.warning("wayback fetch %s -> HTTP %s", wb[:100], r.status_code)
        except Exception as e:  # noqa: BLE001
            log.warning("wayback fetch error: %s", e)
    cc.problems.append("download_failed")
    return False


# --------------------------------------------------------------------------
# Extraction + parsing
# --------------------------------------------------------------------------

def extract_text(cc: Concall) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(cc.pdf_bytes)) as doc:
            cc.page_count = len(doc.pages)
            pages = [(p.extract_text() or "") for p in doc.pages]
            cc.page_texts = pages[:3]
            cc.text = "\n".join(pages).strip()
    except Exception as e:  # noqa: BLE001
        cc.problems.append(f"extract_failed:{e}")
        return False
    if cc.word_count < 500:  # a real earnings call is thousands of words
        cc.problems.append(f"suspiciously_short:{cc.word_count}w")
        return False
    return True


# Month alternation accepting full or 3-letter abbreviated names — TCS IR
# filenames really do mix them ("July 13, 2017" vs "Jan 12, 2022").
_MONTH_RX = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
             r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
             r"Nov(?:ember)?|Dec(?:ember)?")

# Each pattern owns its group semantics via a decoder returning
# (quarter_digit, fy_raw) — so reordering patterns can't swap meanings.
# Years are (20\d{2}|\d{2})(?!\d): a slice-truncated "FY 202" fails to
# match instead of parsing as a corrupt 3-digit year.
_QFY_PATTERNS = [
    # "Q4 2017-18", "Q1 2018-19" (TCS IR filename + body style) — FY = end year
    (re.compile(r"Q([1-4])[\s,]*(?:FY)?\s*(20\d{2})\s*[-–]\s*(\d{2})", re.I),
     lambda g: (g[0], g[2])),
    # "Q4 FY26", "Q1FY2027", "Q4 & Full Year FY 2026" (filler tolerated)
    (re.compile(r"Q([1-4])[^\n]{0,24}?FY\s*'?\s*(20\d{2}|\d{2})(?!\d)", re.I),
     lambda g: (g[0], g[1])),
    # "FY26 Q4"
    (re.compile(r"FY\s*'?(20\d{2}|\d{2})(?!\d)\s*Q([1-4])", re.I),
     lambda g: (g[1], g[0])),
]

# BSE filing cover letters: "... for the quarter (and year / nine-month
# period / six-month period / half year) ended March 31, 2026". Wording
# drifts between filings, so the middle clause is a bounded wildcard.
_QTR_ENDED = re.compile(
    r"quarter\s+(?:and\s+[a-z\s-]{1,25}?\s*)?ended\s+"
    r"(" + _MONTH_RX + r")\s+\d{1,2}\s*,?\s*(20\d{2})", re.IGNORECASE)

# Indian FY: Apr–Mar. Quarter-end month → (quarter, FY-offset from calendar year)
_QTR_END_MONTH = {6: ("Q1", 1), 9: ("Q2", 1), 12: ("Q3", 1), 3: ("Q4", 0)}
_QTR_END_DAY = {"Q1": (6, 30), "Q2": (9, 30), "Q3": (12, 31), "Q4": (3, 31)}

_DATE_PATTERNS = [
    re.compile(r"(" + _MONTH_RX + r")\s+(\d{1,2})\s*,?\s*(20\d{2})", re.I),
    re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_RX + r")\s*,?\s*"
               r"(20\d{2})", re.I),
]

# Positive document classification: only quarterly *earnings* calls belong
# in the concalls table. Analyst days, fireside chats, press meets etc. are
# filed with the same "Transcript" label on screener but must be skipped —
# and never by mere parse fallthrough (their body text can still contain a
# parseable quarter reference).
_EARNINGS_RX = re.compile(r"earnings\s+(?:conference\s+)?call", re.IGNORECASE)


def _norm_fy(raw: str) -> str:
    y = int(raw)
    if y < 100:
        y += 2000
    return f"FY{y}"


def _find_date(probe: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(probe)
        if not m:
            continue
        g = m.groups()
        if g[0].lower()[:3] in MONTHS:
            mo, d, y = MONTHS[g[0].lower()[:3]], int(g[1]), int(g[2])
        else:
            d, mo, y = int(g[0]), MONTHS[g[1].lower()[:3]], int(g[2])
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _match_qfy(probe: str) -> tuple[str, str] | None:
    for pat, decode in _QFY_PATTERNS:
        m = pat.search(probe)
        if m:
            q, fy = decode(m.groups())
            return f"Q{q}", _norm_fy(fy)
    return None


def is_earnings_call(cc: Concall) -> bool:
    """Positive classification — URL filename, BSE cover-letter Sub: line,
    or an early-page title block must literally name an earnings call."""
    from urllib.parse import unquote
    if _EARNINGS_RX.search(unquote(cc.url)):
        return True
    if cc.page_texts and _EARNINGS_RX.search(cc.page_texts[0][:2500]):
        return True
    return any(_EARNINGS_RX.search(p[:400]) for p in cc.page_texts)


def parse_quarter_fy(cc: Concall) -> None:
    """Derive (quarter, fiscal_year, call_date) and cross-validate them.

    Quarter/FY probe order: URL (IR filenames carry "Q1 2018-19") →
    BSE cover-letter quarter-end date ("quarter and year ended March 31,
    2026" — authoritative, filed by the company) → page *title blocks*
    only ("Q4 & Full Year FY 2026"). Never deep body text: managements
    reference other quarters mid-call, which mislabels the row.

    Call-date probe order: URL ("held ... on July 13, 2017") → any early
    page whose title block names a conference call → remaining pages.
    When the cover letter matched, page 1 IS the filing letter — its date
    lags the call by days, so it is excluded from every date probe.

    Finally: an Indian-FY earnings call must land within ~75 days after
    the quarter-end it covers (SEBI results deadlines are 45d, 60d for
    Q4). Rows failing that cross-check are flagged, never stored — this
    is the guard that catches every plausible-but-wrong regex match.
    """
    from urllib.parse import unquote
    url_probe = unquote(cc.url)

    # Priority 2 pre-compute — the company's own filed statement of which
    # quarter the call covers. Also identifies page 1 as a filing letter.
    cover_qfy = None
    m = _QTR_ENDED.search(cc.page_texts[0][:2500] if cc.page_texts else "")
    if m:
        month = MONTHS[m.group(1).lower()[:3]]
        year = int(m.group(2))
        if month in _QTR_END_MONTH:
            q, fy_off = _QTR_END_MONTH[month]
            cover_qfy = (q, f"FY{year + fy_off}")

    # Pages eligible for probing — the filing cover letter is excluded.
    content_pages = cc.page_texts[1:] if cover_qfy else cc.page_texts

    qfy = _match_qfy(url_probe) or cover_qfy
    if not qfy:
        for p in content_pages:
            qfy = _match_qfy(p[:400])
            if qfy:
                break
    if qfy:
        cc.quarter, cc.fiscal_year = qfy

    date_probes = [url_probe]
    date_probes += [p[:400] for p in content_pages
                    if re.search(r"conference\s*call", p[:400], re.IGNORECASE)]
    date_probes += [p[:2000] for p in content_pages]
    for probe in date_probes:
        cc.call_date = _find_date(probe)
        if cc.call_date:
            break

    if not cc.quarter:
        cc.problems.append("quarter_unparsed")
        return
    if not cc.call_date:
        cc.problems.append("no_call_date")
        return

    import datetime as dt
    fyn = int(cc.fiscal_year[2:])
    qm, qd = _QTR_END_DAY[cc.quarter]
    qend = dt.date(fyn if cc.quarter == "Q4" else fyn - 1, qm, qd)
    delta = (dt.date.fromisoformat(cc.call_date) - qend).days
    if not 0 < delta <= 75:
        cc.problems.append(
            f"date_quarter_mismatch:call {cc.call_date} vs "
            f"{cc.quarter}-end {qend} ({delta:+d}d)")


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO equity.concalls (symbol, quarter, fiscal_year, call_date, transcript,
                      url, source, word_count, page_count, pdf_sha256, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
ON CONFLICT (symbol, fiscal_year, quarter) DO UPDATE SET
    call_date  = EXCLUDED.call_date,
    transcript = EXCLUDED.transcript,
    url        = EXCLUDED.url,
    source     = EXCLUDED.source,
    word_count = EXCLUDED.word_count,
    page_count = EXCLUDED.page_count,
    pdf_sha256 = EXCLUDED.pdf_sha256,
    updated_at = now()
-- longer transcript wins; never overwrite full text with a shorter scan
WHERE concalls.word_count IS NULL
   OR EXCLUDED.word_count >= concalls.word_count;
"""


def upsert(conn, cc: Concall) -> None:
    with conn.cursor() as cur:
        # The table also enforces UNIQUE(url), which the ON CONFLICT arbiter
        # does not cover. A parser fix can relabel a stored PDF's quarter
        # (happened in the TCS pilot: Jan-2026 call, Q2→Q3) — clear the
        # stale row first so the re-keyed insert can't hit concalls_url_key.
        cur.execute(
            "DELETE FROM equity.concalls WHERE url = %s AND NOT "
            "(symbol = %s AND quarter = %s AND fiscal_year = %s)",
            (cc.url, cc.symbol, cc.quarter, cc.fiscal_year))
        if cur.rowcount:
            log.warning("   relabel: removed %d stale row(s) holding this url",
                        cur.rowcount)
        cur.execute(UPSERT_SQL, (
            cc.symbol, cc.quarter, cc.fiscal_year, cc.call_date, cc.text,
            cc.url, cc.source, cc.word_count, cc.page_count, cc.sha256,
        ))
    conn.commit()


def archive_pdf(cc: Concall) -> None:
    d = os.path.join(PDF_ARCHIVE_ROOT, cc.symbol)
    os.makedirs(d, exist_ok=True)
    name = f"{cc.fiscal_year or 'FYxx'}_{cc.quarter or 'Qx'}_{cc.sha256[:12]}.pdf"
    path = os.path.join(d, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(cc.pdf_bytes)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

# Supplemental transcript URLs not present in screener's index (e.g. old
# quarters recovered by hand from the Wayback Machine). Keyed by symbol —
# keeps delete+re-run / DB-restore fully reproducible.
EXTRA_URLS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "data",
    "concall_extra_urls.json")


def load_extra_urls(symbol: str) -> list[str]:
    try:
        with open(EXTRA_URLS_PATH) as f:
            return json.load(f).get(symbol, [])
    except FileNotFoundError:
        return []


def run(symbol: str, slug: str, extra_urls: list[str] | None = None) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL is not set in environment")
    conn = psycopg2.connect(dsn, connect_timeout=20)

    links = screener_transcript_links(slug, symbol)
    links += [("manifest", u) for u in load_extra_urls(symbol)]
    links += [("cli", u) for u in (extra_urls or [])]

    ok, failed = [], []
    try:
        for label, url in links:
            cc = Concall(symbol=symbol, label=label, url=url)
            log.info("── %s | %s", label, url[:90])
            try:
                if not download_pdf(cc) or not extract_text(cc):
                    failed.append(cc)
                    continue
                if not is_earnings_call(cc):
                    cc.problems.append("not_earnings_call")
                    log.info("   skipping non-earnings document: %s", label)
                    failed.append(cc)
                    continue
                parse_quarter_fy(cc)
                if cc.problems:
                    log.warning("   skipping %s: %s", label, cc.problems)
                    failed.append(cc)
                    continue
                archive_pdf(cc)
                upsert(conn, cc)
                ok.append(cc)
                log.info("   ✔ %s %s | %s | %dw %dp | %s",
                         cc.fiscal_year, cc.quarter, cc.call_date,
                         cc.word_count, cc.page_count, cc.source)
            except Exception as e:  # noqa: BLE001 — one bad row must not
                conn.rollback()     # abort the remaining backfill
                cc.problems.append(f"error:{e}")
                failed.append(cc)
                log.error("   ✗ %s errored: %s", label, e)
            finally:
                # politeness gap on every path — failure streaks must not
                # hammer screener/BSE/Wayback back-to-back
                time.sleep(REQUEST_GAP_S)
    finally:
        conn.close()

    log.info("stored %d | failed/skipped %d", len(ok), len(failed))
    for cc in failed:
        log.info("   ✗ %s %s -> %s", cc.label, cc.url[:70], cc.problems)
    hard_fail = any("download_failed" in c.problems or
                    any(str(p).startswith("error:") for p in c.problems)
                    for c in failed)
    return 0 if ok and not hard_fail else 1


def main() -> int:
    from screener_scraper.jobs import overflow_for_url
    why = overflow_for_url(os.getenv("DATABASE_URL"))
    if why:
        log.warning("Supabase overflow (%s): nothing written, the lake is primary for the top 100", why)
        return 0
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", required=True, help="NSE symbol, e.g. TCS")
    ap.add_argument("--slug", help="screener.in slug (default: same as symbol)")
    ap.add_argument("--extra-urls", help="comma-separated supplemental "
                    "transcript PDF URLs (also see data/concall_extra_urls.json)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(levelname).1s %(message)s",
    )
    extra = [u.strip() for u in a.extra_urls.split(",")] if a.extra_urls else None
    return run(a.symbol.upper(), a.slug or a.symbol.upper(), extra)


if __name__ == "__main__":
    sys.exit(main())
