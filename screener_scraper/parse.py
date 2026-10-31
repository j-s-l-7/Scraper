"""Pure parsing functions: screener.in HTML / JSON -> dataclasses. No I/O, no DB."""
from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Tag

MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}
PERIOD_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{4})$")
SECTIONS = ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios")


class ParseError(Exception):
    """Structural parse failure (page layout changed, captcha/login page, malformed JSON)."""


def _loads(text: str):
    try:
        return json.loads(text)
    except ValueError as e:
        raise ParseError(f"invalid JSON: {e}") from e


# ───────────────────────────── dataclasses ─────────────────────────────
@dataclass
class CompanyInfo:
    company_id: int
    warehouse_id: Optional[int] = None
    name: Optional[str] = None
    nse_symbol: Optional[str] = None
    bse_code: Optional[str] = None
    website: Optional[str] = None
    sector: Optional[str] = None
    industry_group: Optional[str] = None
    industry: Optional[str] = None
    sub_industry: Optional[str] = None
    industry_code: Optional[str] = None
    about: Optional[str] = None
    key_points: Optional[str] = None
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)


@dataclass
class Row:
    """One statement line item (or schedule child) with its parsed values."""
    section: str
    label: str
    unit: str
    order: int
    schedule_parent: Optional[str] = None      # set when the row has a drill-down button (its own name)
    values: List[Tuple[str, date, float]] = field(default_factory=list)   # (period_type, period_end, value)


@dataclass
class TopRatios:
    market_cap_cr: Optional[float] = None
    price: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    pe: Optional[float] = None
    book_value: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    roce_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    face_value: Optional[float] = None
    extra: Dict[str, object] = field(default_factory=dict)


@dataclass
class GrowthItem:
    measure: str
    horizon: str
    value_pct: Optional[float]


@dataclass
class ShareRow:
    frequency: str      # Q / Y
    category: str
    period_end: date
    value: float


@dataclass
class DocLink:
    doc_type: str
    url: str
    title: Optional[str] = None
    period_label: Optional[str] = None
    source: Optional[str] = None


@dataclass
class PageData:
    info: CompanyInfo
    basis_label: Optional[str]                    # 'C', 'S' or None (page carries no basis label)
    statements: Dict[str, List[Row]]
    ttm_end: Optional[date]
    bs_fy_month: Optional[int]
    schedule_buttons: List[Tuple[str, str]]       # (parent, section) in page order, de-duplicated
    top_ratios: TopRatios
    growth: List[GrowthItem]
    shareholding: List[ShareRow]
    holder_buttons: Dict[str, List[str]]          # 'quarterly'|'yearly' -> categories having a button
    documents: List[DocLink]

    @property
    def has_financials(self) -> bool:
        """True if any statement row carries a value. The /consolidated/ page of a company that has no
        consolidated financials is an empty shell (headers only, blank top-ratios)."""
        return any(r.values for rows in self.statements.values() for r in rows)

    @property
    def is_consolidated(self) -> bool:
        return self.basis_label == "C" and self.has_financials


# ───────────────────────────── helpers ─────────────────────────────
def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def month_end(label: str) -> Optional[date]:
    m = PERIOD_RE.match(label.strip())
    if not m or m.group(1) not in MONTHS:
        return None
    y, mo = int(m.group(2)), MONTHS[m.group(1)]
    return date(y, mo, calendar.monthrange(y, mo)[1])


def parse_number(txt: Optional[str]) -> Optional[float]:
    """'1,234' '12%' '₹ 5' '-3.2' -> float; ''/'-'/garbage -> None."""
    if txt is None:
        return None
    t = re.sub(r"[,%₹\s]", "", str(txt))
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def clean_label(td: Tag) -> str:
    """Row label: schedule button text if present, else first direct text node; trailing '+' removed."""
    btn = td.find("button")
    if btn is not None:
        txt = "".join(s for s in btn.find_all(string=True, recursive=False))
    else:
        txt = next((s for s in td.find_all(string=True, recursive=False) if s.strip()), "")
    txt = " ".join(txt.split())
    return re.sub(r"\s*\+$", "", txt).strip()


def unit_for(label: str, raw_cells: List[str]) -> str:
    l = label.strip()
    if l.endswith("%"):
        return "pct"
    if l == "EPS in Rs":
        return "rs"
    if "Days" in l or "Cash Conversion Cycle" in l:
        return "days"
    nonempty = [c for c in raw_cells if c.strip()]
    if nonempty and all(c.strip().endswith("%") for c in nonempty):
        return "pct"          # e.g. 'CFO/EBITDA' shows '105%' although the label has no '%'
    return "cr"


def _first_int(el: Optional[Tag], attr: str) -> Optional[int]:
    if el is None:
        return None
    v = el.get(attr)
    return int(v) if v and str(v).isdigit() else None


# ───────────────────────────── company identity ─────────────────────────────
def parse_company_info(s: BeautifulSoup) -> CompanyInfo:
    el = s.select_one("[data-company-id]")
    if el is None:
        raise ParseError("no data-company-id on page (blocked / login / layout change?)")
    info = CompanyInfo(company_id=int(el["data-company-id"]))
    info.warehouse_id = _first_int(s.select_one("[data-warehouse-id]"), "data-warehouse-id")
    h1 = s.find("h1")
    info.name = " ".join(h1.get_text().split()) if h1 else None

    # NOTE: the spec's `.company-links` does not exist in current HTML; links live in `.company-profile`
    # and the page header. Scan all anchors, first match wins.
    for a in s.find_all("a", href=True):
        h = a["href"]
        if info.nse_symbol is None:
            m = re.search(r"nseindia\.com/get-quotes/equity\?symbol=([^&#]+)", h)
            if m:
                info.nse_symbol = unquote(m.group(1)).upper()
        if info.bse_code is None and "bseindia.com/stock-share-price" in h:
            m = re.search(r"/(\d{5,7})/?(?:[?#].*)?$", h) or re.search(r"/(\d{5,7})/", h)
            if m:
                info.bse_code = m.group(1)
    for a in s.select(".company-profile a[href]"):
        if a.find("i", class_="icon-link"):          # the 'Website' link
            info.website = a["href"]
            break
    if info.website is None:
        for a in s.select(".company-profile a[href^=http]"):
            host = urlparse(a["href"]).netloc
            if not re.search(r"bseindia|nseindia|screener\.in", host):
                info.website = a["href"]
                break

    # classification: breadcrumb of /market/ links in #peers (each level extends the previous href)
    chain: List[Tuple[str, str]] = []
    for a in s.select('#peers a[href^="/market/"]'):
        h = a["href"]
        if not chain or h.startswith(chain[-1][0]) and h != chain[-1][0]:
            chain.append((h, " ".join(a.get_text().split())))
    names = [n for _, n in chain[:4]]
    names += [None] * (4 - len(names))
    info.sector, info.industry_group, info.industry, info.sub_industry = names
    if chain:
        info.industry_code = chain[:4][-1][0].rstrip("/").split("/")[-1]

    about = s.select_one(".company-profile .about") or s.select_one(".about")
    info.about = " ".join(about.get_text(" ").split()) if about else None
    kp = s.select_one(".company-profile .commentary")
    if kp:
        for junk in kp.select(".show-more-button, sup"):
            junk.decompose()
        paras = [" ".join(p.get_text(" ").split()) for p in kp.find_all("p")] or [" ".join(kp.get_text(" ").split())]
        info.key_points = "\n".join(p for p in paras if p) or None
    info.pros = [" ".join(li.get_text().split()) for li in s.select(".pros li")]
    info.cons = [" ".join(li.get_text().split()) for li in s.select(".cons li")]
    return info


def detect_basis(s: BeautifulSoup) -> Optional[str]:
    """'C' if the P&L/quarters header says Consolidated, 'S' if Standalone, None if unlabelled
    (company without consolidated financials: screener shows plain 'Figures in Rs. Crores', no toggle)."""
    for sec in ("profit-loss", "quarters", "balance-sheet"):
        p = s.select_one(f"#{sec} p.sub")
        if p is not None:
            t = " ".join(p.get_text().split())
            if t.startswith("Consolidated"):
                return "C"
            if t.startswith("Standalone"):
                return "S"
            return None
    return None


# ───────────────────────────── statements ─────────────────────────────
def _header_periods(table: Tag) -> List[str]:
    return [th.get_text(strip=True) for th in table.select("thead th")][1:]


def classify_periods(section: str, labels: List[str], ttm_end: Optional[date],
                     fy_month: Optional[int] = None) -> List[Optional[Tuple[str, date]]]:
    """Map column labels -> (period_type, period_end). TTM needs ttm_end; balance-sheet columns whose
    month differs from the fiscal-year-end month are 'H'."""
    out: List[Optional[Tuple[str, date]]] = []
    for lab in labels:
        if lab.strip() == "TTM":
            out.append(("TTM", ttm_end) if ttm_end else None)
            continue
        d = month_end(lab)
        if d is None:
            out.append(None)
        elif section == "quarters":
            out.append(("Q", d))
        elif section == "balance-sheet" and fy_month and d.month != fy_month:
            out.append(("H", d))
        else:
            out.append(("A", d))
    return out


def fiscal_month(labels: List[str]) -> Optional[int]:
    months = [month_end(l).month for l in labels if month_end(l)]
    return Counter(months).most_common(1)[0][0] if months else None


def parse_statement(s: BeautifulSoup, section: str, ttm_end: Optional[date] = None) -> List[Row]:
    sec = s.select_one(f"#{section}")
    table = sec.select_one("table.data-table") if sec else None
    if table is None:
        return []
    labels = _header_periods(table)
    fy = fiscal_month(labels) if section == "balance-sheet" else None
    periods = classify_periods(section, labels, ttm_end, fy)
    rows: List[Row] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        label = clean_label(tds[0])
        if not label or label == "Raw PDF":
            continue
        cells = [td.get_text(strip=True) for td in tds[1:]]
        if not any(c.strip() for c in cells):
            continue
        btn = tds[0].find("button", onclick=re.compile("showSchedule"))
        row = Row(section=section, label=label, unit=unit_for(label, cells), order=len(rows),
                  schedule_parent=label if btn is not None else None)
        for per, c in zip(periods, cells):
            v = parse_number(c)
            if per is None or v is None:
                continue
            row.values.append((per[0], per[1], v))
        rows.append(row)
    return rows


def parse_schedule_buttons(s: BeautifulSoup) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for b in s.find_all("button", onclick=re.compile("showSchedule")):
        m = re.search(r"showSchedule\(\s*['\"](.+?)['\"]\s*,\s*['\"](.+?)['\"]", b["onclick"])
        if m and m.group(2) in SECTIONS and (m.group(1), m.group(2)) not in out:
            out.append((m.group(1), m.group(2)))
    return out


def parse_schedule_json(text: str, section: str, parent: str, ttm_end: Optional[date],
                        fy_month: Optional[int] = None) -> List[Row]:
    """{child: {period: '12%'|'1,234', ...non-period keys...}} -> child rows (schedule_parent = parent)."""
    data = _loads(text)
    if not isinstance(data, dict):
        raise ParseError("schedule JSON is not an object")
    rows: List[Row] = []
    for child, per_vals in data.items():
        if not isinstance(per_vals, dict):
            continue
        keys = [k for k in per_vals if k == "TTM" or PERIOD_RE.match(k)]
        periods = classify_periods(section, keys, ttm_end, fy_month)
        raw = [str(per_vals[k]) for k in keys]
        pct = child.strip().endswith("%") or any(r.strip().endswith("%") for r in raw)
        row = Row(section=section, label=child.strip(), unit="pct" if pct else "cr",
                  order=len(rows), schedule_parent=parent)
        for per, r in zip(periods, raw):
            v = parse_number(r)
            if per is not None and v is not None:
                row.values.append((per[0], per[1], v))
        if row.values:
            rows.append(row)
    return rows


# ───────────────────────────── top ratios / growth ─────────────────────────────
_TOP_MAP = {"Market Cap": "market_cap_cr", "Current Price": "price", "Stock P/E": "pe",
            "Book Value": "book_value", "Dividend Yield": "dividend_yield_pct", "ROCE": "roce_pct",
            "ROE": "roe_pct", "Face Value": "face_value"}


def parse_top_ratios(s: BeautifulSoup) -> TopRatios:
    tr = TopRatios()
    for li in s.select("#top-ratios li"):
        name_el = li.select_one(".name")
        val_el = li.select_one(".value")
        if name_el is None or val_el is None:
            continue
        name = " ".join(name_el.get_text().split())
        nums = [parse_number(n.get_text()) for n in val_el.select(".number")]
        if not nums:
            nums = [parse_number(val_el.get_text())]
        if name == "High / Low":
            tr.high_52w = nums[0] if nums else None
            tr.low_52w = nums[1] if len(nums) > 1 else None
        elif name in _TOP_MAP:
            setattr(tr, _TOP_MAP[name], nums[0] if nums else None)
        else:
            txt = " ".join(val_el.get_text().split())
            tr.extra[name] = nums[0] if len(nums) == 1 and nums[0] is not None else txt
    return tr


_GROWTH_MEASURE = {"Compounded Sales Growth": "sales_growth", "Compounded Profit Growth": "profit_growth",
                   "Stock Price CAGR": "price_cagr", "Return on Equity": "roe"}
_GROWTH_HORIZON = {"10 Years": "10Y", "5 Years": "5Y", "3 Years": "3Y", "1 Year": "1Y",
                   "TTM": "TTM", "Last Year": "LAST"}


def parse_growth(s: BeautifulSoup) -> List[GrowthItem]:
    out: List[GrowthItem] = []
    for t in s.select("#profit-loss table.ranges-table"):
        th = t.find("th")
        measure = _GROWTH_MEASURE.get(" ".join(th.get_text().split())) if th else None
        if not measure:
            continue
        for tr in t.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            hz = _GROWTH_HORIZON.get(tds[0].get_text(strip=True).rstrip(":").strip())
            if hz:
                out.append(GrowthItem(measure, hz, parse_number(tds[1].get_text())))
    return out


# ───────────────────────────── shareholding ─────────────────────────────
_SH_MAP = {"Promoters": "promoters", "FIIs": "fii", "DIIs": "dii", "Government": "government",
           "Public": "public", "Others": "others", "No. of Shareholders": "num_shareholders"}


def parse_shareholding(s: BeautifulSoup) -> Tuple[List[ShareRow], Dict[str, List[str]]]:
    rows: List[ShareRow] = []
    buttons: Dict[str, List[str]] = {"quarterly": [], "yearly": []}
    sec = s.select_one("#shareholding")
    if sec is None:
        return rows, buttons
    tables = []
    for freq, tid in (("Q", "quarterly-shp"), ("Y", "yearly-shp")):
        box = sec.select_one(f"#{tid}")
        t = box.select_one("table") if box else None
        tables.append((freq, t))
    if all(t is None for _, t in tables):       # fallback: positional (quarterly first, yearly second)
        found = sec.select("table.data-table")
        tables = [("Q", found[0] if found else None), ("Y", found[1] if len(found) > 1 else None)]
    for freq, t in tables:
        if t is None:
            continue
        periods = [month_end(l) for l in _header_periods(t)]
        for tr in t.select("tbody tr"):
            tds = tr.find_all("td")
            cat = _SH_MAP.get(clean_label(tds[0])) if tds else None
            if not cat:
                continue
            for p, td in zip(periods, tds[1:]):
                v = parse_number(td.get_text())
                if p is not None and v is not None:
                    rows.append(ShareRow(freq, cat, p, v))
            b = tds[0].find("button", onclick=re.compile("showShareholders"))
            if b:
                m = re.search(r"showShareholders\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]", b["onclick"])
                if m and m.group(2) in buttons and m.group(1) not in buttons[m.group(2)]:
                    buttons[m.group(2)].append(m.group(1))
    return rows, buttons


def parse_investors_json(text: str) -> List[Tuple[str, Optional[str], date, float]]:
    """-> [(holder_name, person_url|None, period_end, pct)]. Non-period keys ignored."""
    data = _loads(text)
    if not isinstance(data, dict):
        raise ParseError("investors JSON is not an object")
    out = []
    for name, per in data.items():
        if not isinstance(per, dict):
            continue
        url = (per.get("setAttributes") or {}).get("data-person-url")
        for k, v in per.items():
            d = month_end(k) if PERIOD_RE.match(k) else None
            val = parse_number(v)
            if d and val is not None:
                out.append((name.strip(), url, d, val))
    return out


def holder_key(name: str, person_url: Optional[str]) -> str:
    return person_url or "name:" + name.strip().lower()


# ───────────────────────────── documents ─────────────────────────────
def _source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "bseindia" in host:
        return "bse"
    if "nseindia" in host:
        return "nse"
    return "other"


_CONCALL = {"Transcript": "concall_transcript", "PPT": "concall_ppt", "REC": "concall_recording",
            "Notes": "concall_notes"}


def parse_documents(s: BeautifulSoup) -> List[DocLink]:
    docs: List[DocLink] = []
    seen = set()

    def add(dt, url, title, label):
        url = url.strip()
        if url and (dt, url) not in seen:
            seen.add((dt, url))
            docs.append(DocLink(dt, url, title, label, _source(url)))

    sec = s.select_one("#documents")
    if sec is None:
        return docs
    for a in sec.select(".annual-reports li a[href]"):
        t = " ".join(a.get_text().split())
        add("annual_report", a["href"], t, t)
    for li in sec.select(".concalls li"):
        lab = li.find("div")
        label = " ".join(lab.get_text().split()) if lab else None
        for a in li.find_all("a", href=True):
            dt = _CONCALL.get(" ".join(a.get_text().split()))
            if dt:
                add(dt, a["href"], f"{a.get_text(strip=True)} {label or ''}".strip(), label)
    for a in sec.select(".credit-ratings li a[href]"):
        sub = a.find("div")
        label = " ".join(sub.get_text().split()) if sub else None
        title = " ".join(next((x for x in a.find_all(string=True, recursive=False) if x.strip()), "").split())
        add("credit_rating", a["href"], title or None, label)
    for a in sec.select("#company-announcements-tab ul.list-links li a[href]"):
        t = a.find("time")
        title = " ".join(next((x for x in a.find_all(string=True, recursive=False) if x.strip()), "").split())
        label = (t.get("datetime") or "")[:10] if t else None
        add("announcement", a["href"], title or None, label or None)
    return docs


# ───────────────────────────── whole page ─────────────────────────────
def parse_page(html: str) -> PageData:
    s = soup(html)
    info = parse_company_info(s)
    quarters = parse_statement(s, "quarters")
    ttm_end = max((d for r in quarters for pt, d, _ in r.values if pt == "Q"), default=None)
    statements = {"quarters": quarters}
    for sec in SECTIONS[1:]:
        statements[sec] = parse_statement(s, sec, ttm_end)
    if ttm_end is None:     # no quarterly table: fall back to latest annual period for TTM
        ttm_end = max((d for r in statements["profit-loss"] for pt, d, _ in r.values if pt == "A"), default=None)
        statements["profit-loss"] = parse_statement(s, "profit-loss", ttm_end)
    bs = s.select_one("#balance-sheet table.data-table")
    fy = fiscal_month(_header_periods(bs)) if bs else None
    sh_rows, sh_buttons = parse_shareholding(s)
    return PageData(info=info, basis_label=detect_basis(s), statements=statements, ttm_end=ttm_end,
                    bs_fy_month=fy, schedule_buttons=parse_schedule_buttons(s),
                    top_ratios=parse_top_ratios(s), growth=parse_growth(s), shareholding=sh_rows,
                    holder_buttons=sh_buttons, documents=parse_documents(s))


# ───────────────────────────── charts ─────────────────────────────
_SERIES_ALIAS = {
    "price to earning": "pe", "median pe": "median_pe", "eps": "eps",
    "price to book value": "pbv", "median pbv": "median_pbv", "book value": "book_value",
    "ev multiple": "ev_multiple", "median ev multiple": "median_ev_multiple", "ebitda": "ebitda",
    "market cap to sales": "mcap_sales", "median market cap to sales": "median_mcap_sales",
}


def series_slug(metric: str) -> str:
    m = metric.strip().lower()
    return _SERIES_ALIAS.get(m) or re.sub(r"[^a-z0-9]+", "_", m).strip("_")


def _chart_date(x: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def parse_chart_series(text: str) -> Dict[str, List[Tuple[date, float]]]:
    """Valuation chart JSON -> {series_slug: [(date, value)]}, nulls dropped."""
    out: Dict[str, List[Tuple[date, float]]] = {}
    for ds in _loads(text).get("datasets", []):
        pts = []
        for v in ds.get("values", []):
            d, val = _chart_date(v[0]), parse_number(v[1]) if len(v) > 1 and v[1] is not None else None
            if d and val is not None:
                pts.append((d, val))
        if pts:
            out[series_slug(ds.get("metric", ""))] = pts
    return out


def parse_price_chart(text: str) -> List[dict]:
    """Price-DMA50-DMA200-Volume JSON -> one dict per date: trade_date, close, dma50, dma200, volume, delivery_pct."""
    by: Dict[date, dict] = {}
    col = {"Price": "close", "DMA50": "dma50", "DMA200": "dma200", "Volume": "volume"}
    for ds in _loads(text).get("datasets", []):
        c = col.get(ds.get("metric"))
        if not c:
            continue
        for v in ds.get("values", []):
            d = _chart_date(v[0])
            if d is None:
                continue
            row = by.setdefault(d, {"trade_date": d, "close": None, "dma50": None, "dma200": None,
                                    "volume": None, "delivery_pct": None})
            val = parse_number(v[1]) if len(v) > 1 and v[1] is not None else None
            if c == "volume" and val is not None:
                val = int(val)
            row[c] = val
            if c == "volume" and len(v) > 2 and isinstance(v[2], dict) and v[2].get("delivery") is not None:
                row["delivery_pct"] = parse_number(v[2]["delivery"])
    return [by[d] for d in sorted(by)]
