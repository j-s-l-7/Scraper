"""AMC monthly portfolio disclosures (ISIN level) -> equity.mf_holding (lake only; never the Supabase `mf` schema).
Layouts differ per fund house, so the parser matches header cells by meaning (ISIN, instrument name, quantity, market
value, % to NAV) instead of by position. Files are found by scanning each fund house's public disclosure page for
.xls/.xlsx/.csv/.zip links; pages that are JS-rendered or return 401/403/429 yield nothing (a block is final)."""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from . import _common as c

log = logging.getLogger(__name__)
NAME = "amc_portfolio"
COLS = ["month", "amc", "scheme", "isin", "company_id", "instrument", "qty", "market_value", "pct_nav", "source"]
PK = ["month", "amc", "scheme", "isin"]

# ~15 largest fund houses: public disclosure pages (best effort; a missing page is skipped)
AMCS: Dict[str, str] = {
    "SBI": "https://www.sbimf.com/portfolios",
    "ICICI Prudential": "https://www.icicipruamc.com/blob/knowledgecentre/all-disclosures/portfolio/monthly-portfolio",
    "HDFC": "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio",
    "Aditya Birla Sun Life": "https://mutualfund.adityabirlacapital.com/forms-and-downloads/portfolio",
    "Nippon India": "https://mf.nipponindiaim.com/investor-service/downloads/factsheets-portfolio-and-other-disclosures",
    "Kotak": "https://www.kotakmf.com/Information/forms-and-downloads",
    "Axis": "https://www.axismf.com/statutory-disclosures",
    "UTI": "https://www.utimf.com/forms-and-downloads",
    "DSP": "https://www.dspim.com/mandatory-disclosures/portfolio-disclosures",
    "Mirae Asset": "https://www.miraeassetmf.co.in/downloads/portfolios",
    "Tata": "https://www.tatamutualfund.com/downloads",
    "Franklin Templeton": "https://www.franklintempletonindia.com/downloads",
    "HSBC": "https://www.assetmanagement.hsbc.co.in/en/mutual-funds/investor-resources/information-library",
    "Invesco": "https://invescomutualfund.com/literature-and-form",
    "Canara Robeco": "https://www.canararobeco.com/downloads/statutory-disclosures/",
}
LINK_RE = re.compile(r'href=["\']([^"\']+\.(?:xlsx?|csv|zip)(?:\?[^"\']*)?)["\']', re.I)
HEADER_KEYS = {
    "isin": re.compile(r"\bisin\b", re.I),
    "instrument": re.compile(r"(name of (the )?instrument|instrument|security|issuer|company)", re.I),
    "qty": re.compile(r"(quantity|qty|no\.? of (shares|units)|number of)", re.I),
    "market_value": re.compile(r"(market|fair)\s*(/fair)?\s*value|market value|value\s*\(", re.I),
    "pct_nav": re.compile(r"(%|per ?cent|percentage).*(nav|net assets|asset)|(nav|net assets).*%", re.I),
}
MONTH_RE = re.compile(r"(january|february|march|april|may|june|july|august|september|october|november|december)"
                      r"[\s_,-]*(\d{4})", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")


def match_header(cells: List[str]) -> Optional[Dict[str, int]]:
    """Column map {isin, instrument, qty, market_value, pct_nav -> index} if this row is a holdings header."""
    idx: Dict[str, int] = {}
    for i, raw in enumerate(cells):
        t = " ".join(str(raw or "").split())
        if not t:
            continue
        for key in ("isin", "pct_nav", "market_value", "qty", "instrument"):     # most specific first
            if key not in idx and HEADER_KEYS[key].search(t):
                idx[key] = i
                break
    return idx if "isin" in idx and ("pct_nav" in idx or "market_value" in idx or "qty" in idx) else None


def _unit_scale(cells: List[str], i: Optional[int]) -> float:
    """market value column unit -> multiplier to Rs crore."""
    if i is None:
        return 1.0
    t = str(cells[i]).lower()
    if "lakh" in t or "lac" in t:
        return 0.01
    if "crore" in t or "cr" in t.split():
        return 1.0
    if "million" in t or "mn" in t:
        return 0.1
    if "rs" in t or "inr" in t:
        return 1e-7
    return 0.01          # AMC sheets overwhelmingly report Rs lakh


def parse_rows(rows: Iterable[List], amc: str, scheme_default: str, month: date,
               isin_company: Optional[Dict[str, int]] = None) -> List[tuple]:
    """Generic sheet -> mf_holding tuples. Scheme name: a 'Scheme Name' / title row above the header, else the default."""
    out: List[tuple] = []
    hdr, scale, scheme = None, 1.0, scheme_default
    for row in rows:
        cells = ["" if v is None else str(v).strip() for v in row]
        if not any(cells):
            continue
        if hdr is None:
            m = match_header(cells)
            if m:
                hdr, scale = m, _unit_scale(cells, m.get("market_value"))
                continue
            first = next((x for x in cells if x), "")
            if re.search(r"fund|scheme|plan|fof|etf|fmp", first, re.I) and len(first) < 160 and "isin" not in first.lower():
                scheme = re.sub(r"^\s*(scheme name\s*[:\-]?)\s*", "", first, flags=re.I).strip() or scheme
            continue
        isin = cells[hdr["isin"]].upper() if hdr["isin"] < len(cells) else ""
        if not ISIN_RE.match(isin):
            continue
        g = lambda k: c.num(cells[hdr[k]]) if k in hdr and hdr[k] < len(cells) else None
        mv = g("market_value")
        name = cells[hdr["instrument"]] if "instrument" in hdr and hdr["instrument"] < len(cells) else None
        out.append((month, amc, scheme[:200], isin, (isin_company or {}).get(isin), name, g("qty"),
                    None if mv is None else mv * scale, g("pct_nav"), "amc_portfolio"))
    return out


def month_from_text(text: str) -> Optional[date]:
    m = MONTH_RE.search(text or "")
    if not m:
        return None
    y, mo = int(m.group(2)), MONTHS[m.group(1).lower()]
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def sheets_from_bytes(raw: bytes, name: str):
    """Yield (sheet_name, rows) from xlsx/xls/csv bytes (zip archives are unpacked recursively)."""
    low = name.lower().split("?")[0]
    if raw[:2] == b"PK" and not low.endswith((".xlsx", ".xlsm")):
        z = zipfile.ZipFile(io.BytesIO(raw))
        for n in z.namelist():
            if n.lower().endswith((".xlsx", ".xls", ".csv")):
                yield from sheets_from_bytes(z.read(n), n)
        return
    if low.endswith(".csv"):
        yield name, list(csv.reader(io.StringIO(raw.decode("utf-8-sig", "ignore"))))
    elif raw[:4] == b"PK\x03\x04":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        for ws in wb.worksheets:
            yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        import xlrd
        wb = xlrd.open_workbook(file_contents=raw)
        for ws in wb.sheets():
            yield ws.name, [ws.row_values(i) for i in range(ws.nrows)]


def parse_file(raw: bytes, name: str, amc: str, month: date, isin_company: Optional[Dict[str, int]] = None) -> List[tuple]:
    out = []
    for sheet, rows in sheets_from_bytes(raw, name):
        out += parse_rows(rows, amc, sheet, month, isin_company)
    return out


def discover(f: c.Fetcher, page: str) -> List[str]:
    r = f.get(page, timeout=(15, 60))
    return [urljoin(page, h) for h in LINK_RE.findall(r.text)
            if re.search(r"portfolio|holding|disclos|monthly", h, re.I)]


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    with db.conn.cursor() as cur:
        cur.execute("select isin, company_id from equity.company where isin is not null")
        isin_company = dict(cur.fetchall())
    db.conn.commit()
    f = c.Fetcher()
    rows, files, notes = 0, 0, {}
    status = "ok"
    try:
        for amc, page in AMCS.items():
            try:
                links = discover(f, page)
            except (c.NotFound, c.Blocked, RuntimeError) as e:
                notes[amc] = f"{type(e).__name__}"
                continue
            if not links:
                notes[amc] = "no static file links (JS-rendered page)"
                continue
            for url in links[:12]:
                month = month_from_text(url)
                if not month or (since and month < since):
                    continue
                try:
                    raw = f.get(url, timeout=(15, 180)).content
                    parsed = parse_file(raw, url, amc, month, isin_company)
                except (c.NotFound, RuntimeError) as e:
                    continue
                except Exception as e:
                    log.warning("amc %s %s: %s", amc, url, str(e)[:100])
                    continue
                rows += c.upsert(db, "equity.mf_holding", COLS, PK, parsed, ts_col="fetched_at")
                files += 1
    except c.Blocked as e:
        status = "blocked"
        c.set_marker(db, NAME, blocked_on=today)
    c.set_marker(db, NAME, status=status, run_at=datetime.now(timezone.utc).isoformat(), last_date=today,
                 files=files, notes=notes)
    return {"status": status, "rows": rows, "files": files, "skipped": notes}
