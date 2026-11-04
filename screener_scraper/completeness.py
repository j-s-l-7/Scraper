"""Completeness ledger: what each company should have, what it has, and why anything is absent.

For every dataset there is
  1. an expectation rule, aware of listing date, fiscal-year end, filing deadlines, SME half-yearly reporting and
     suspensions: items outside it (pre-listing years, results not due yet) are not expected and never counted;
  2. a source chain tried in order (CHAINS). Zero-network steps run here as set-based SQL (`fill`); network steps
     belong to the fleet role that owns that host (one polite client per host), which reads `next_source`;
  3. a reason for every expected item still absent: 'explained' (the data does not exist, or every source was
     tried) or 'open' (it should exist; `next_source` says what is tried next). REASONS is the catalog.

`gaps` rebuilds equity.data_gap, equity.completeness and today's equity.completeness_daily from evidence already in
the database. It never fetches anything. Units: items are fiscal years, quarters or trading days (prices).
"""
from __future__ import annotations

import json
import logging
import re
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from psycopg2.extras import Json, execute_values

log = logging.getLogger(__name__)

UNIVERSE = "TOP1000_MCAP"
EQ_SERIES = ("EQ", "BE", "BZ", "SM", "ST")
SME_SERIES = ("SM", "ST")
NSE_WINDOW = date(2010, 1, 4)        # first NSE archive file: a listing seen then means "listed before 2010"
BSE_WINDOW = date(2016, 12, 8)       # first BSE bhavcopy in the lake
PRICE_FROM = NSE_WINDOW              # daily prices are expected from here (earlier yfinance history is a bonus)
AR_FIRST_FY = 2013                   # annual-report links (screener / BSE archive) start with FY2012-13
FIN_Q_WINDOW, FIN_A_WINDOW, SHP_WINDOW = 13, 12, 12   # what screener shows without a login
RESULT_LAG, YEAR_RESULT_LAG = 45, 60  # LODR 33: quarterly results in 45 days, the year's audited results in 60
SHP_LAG = 21                          # LODR 31: shareholding pattern within 21 days of the quarter end
CALL_LAG = 15                         # an earnings call and its transcript follow the results by about two weeks
AR_LAG_MONTHS = 7                     # AGM within 6 months of the year end; the report is filed before it
SUSPENDED_AFTER = 10                  # no trade on either exchange for this many days before the last session
EXHAUSTED_RUNS = 4                    # fallback-chain runs after which a document counts as tried everywhere
REGULAR_CALLS_FROM = date(2022, 6, 30)  # from Q1 FY23 a regular caller's quarter without a transcript is a search target

# dataset -> ordered sources. 'local' steps run in `fill`; the others are run by the fleet role for that host.
CHAINS: Dict[str, List[str]] = {
    "annual_report": ["screener_link", "original", "original_retry", "wayback", "commoncrawl", "tijori",
                      "company_site", "ocr"],
    "concall_transcript": ["screener_link", "original", "original_retry", "wayback", "commoncrawl", "tijori",
                           "company_site", "audio"],
    "concall_ppt": ["screener_link", "original", "original_retry", "wayback", "commoncrawl", "company_site"],
    "fin_quarterly": ["screener", "yfinance"],
    "fin_annual": ["screener", "yfinance"],
    "shareholding": ["screener"],
    "prices": ["yfinance", "nse_bhavcopy:local"],
    "dividends": ["yfinance", "nse_archive:local", "yfinance_bse"],   # BSE ticker: years before the NSE listing
    "shares_outstanding": ["yfinance", "nse_issue_size:local", "screener_equity_capital:local"],
    "bse_code": ["screener", "bse_bhavcopy:local"],
}
DATASETS = list(CHAINS)
# dataset -> (label, unit of `expected`), for reports and the console
DATASET_META: Dict[str, Tuple[str, str]] = {
    "annual_report": ("Annual reports", "fiscal years"), "concall_transcript": ("Concall transcripts", "quarters"),
    "concall_ppt": ("Investor decks", "linked decks"), "fin_quarterly": ("Quarterly results", "quarters"),
    "fin_annual": ("Annual results", "fiscal years"), "shareholding": ("Shareholding pattern", "quarters"),
    "prices": ("Daily prices (NSE)", "sessions"), "dividends": ("Dividends", "years with a payout"),
    "shares_outstanding": ("Shares outstanding", "companies"), "bse_code": ("BSE listing", "companies"),
}

# code -> (state, label). The console shows the label; `detail` carries the evidence.
REASONS: Dict[str, Tuple[str, str]] = {
    "listed_recently": ("explained", "Listed recently: nothing was due yet"),
    "suspended": ("explained", "No trade on NSE or BSE in the period (suspended or delisted)"),
    "sme_half_yearly": ("explained", "SME platform: results are half-yearly"),
    "no_trade": ("explained", "No trades on these sessions (exchange record)"),
    "basis_conflict": ("explained", "Traded, but the exchange row's price basis conflicts with the yfinance series "
                                    "(a demerger or consolidation the factors don't know): not merged"),
    "no_concalls": ("explained", "No earnings call on record in any source"),
    "no_call_that_quarter": ("explained", "No call on record for this quarter (no transcript, deck or recording)"),
    "deck_only": ("explained", "Investor deck only: no call recording or transcript on record"),
    "before_source_history": ("explained", "Older than the history any source holds for this company"),
    "no_dividends": ("explained", "No dividend declared (screener payout 0% in every year)"),
    "not_on_bse": ("explained", "Not listed on BSE (absent from the BSE bhavcopy)"),
    "exhausted": ("explained", "Every source in the chain was tried"),
    "queued": ("open", "Link found; download queued"),
    "dead_link": ("open", "Original link is dead; fallback chain in progress"),
    "download_failed": ("open", "Download failed; fallback chain in progress"),
    "scanned_no_text": ("open", "Scanned PDF without a text layer; OCR next"),
    "no_link": ("open", "No link in any source yet"),
    "call_held_no_transcript": ("open", "Call held (recording or notes on record) but no transcript found yet"),
    "call_likely_missed": ("open", "Regular caller (transcripts the quarter before and after); this one not found yet"),
    "missing_in_source": ("open", "Not in the source yet; the next scheduled refresh retries it"),
    "no_record": ("open", "Declared in the financials but no record in any feed yet"),
    "fillable": ("open", "In a local source; filled on the next `fill`"),
}
DOC_REASON = {"pending": "queued", "missing": "dead_link", "failed": "download_failed", "no_text": "scanned_no_text"}


# ───────────────────────── calendar helpers (pure) ─────────────────────────
def month_end(y: int, m: int) -> date:
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def add_months(d: date, n: int) -> date:
    """Same day n months later, clamped to the month's end; a month end maps to a month end."""
    y, m = divmod(d.month - 1 + n, 12)
    last = month_end(d.year + y, m + 1)
    return last if d == month_end(d.year, d.month) else last.replace(day=min(d.day, last.day))


def quarter_end(d: date) -> date:
    return month_end(d.year, 3 * ((d.month - 1) // 3) + 3)


def quarters_back(last: date, n: int) -> List[date]:
    """n quarter ends ending at `last` (a quarter end), oldest first."""
    out, q = [], last
    for _ in range(n):
        out.append(q)
        q = month_end(q.year - (q.month == 3), (q.month - 4) % 12 + 1)
    return out[::-1]


def quarters_between(first: date, last: date) -> List[date]:
    """Quarter ends from quarter_end(first) to `last` inclusive (empty when first is later)."""
    a = quarter_end(first)
    n = (last.year - a.year) * 4 + (last.month - a.month) // 3 + 1
    return quarters_back(last, n) if n > 0 else []


def reported_quarter(call: date) -> date:
    """Quarter whose results a call held on `call` discusses: the quarter before the call's quarter."""
    return quarter_end(date(call.year, 3 * ((call.month - 1) // 3) + 1, 1) - timedelta(days=1))


def fy_of(period_end: date, fy_month: int = 3) -> int:
    """Fiscal-year label of a period end (FY ends in `fy_month`; label = the calendar year it ends in)."""
    return period_end.year + (period_end.month > fy_month)


def result_lag(qend: date, fy_month: int) -> int:
    return YEAR_RESULT_LAG if qend.month == fy_month else RESULT_LAG


def last_due_quarter(today: date, fy_month: int, extra: int = 0) -> date:
    q = quarter_end(today)
    while q + timedelta(days=result_lag(q, fy_month) + extra) > today:
        q = quarters_back(q, 2)[0]
    return q


def first_ar_fy(listed: Optional[date], fy_month: int = 3) -> int:
    """First FY whose annual report a listed company files: the report for FY Y goes to the exchange before the AGM,
    which is due within 6 months of the FY end. Listed before that deadline -> FY Y, else the next FY."""
    if listed is None:
        return AR_FIRST_FY
    fy = fy_of(listed, fy_month)
    return fy - 1 if listed <= add_months(month_end(fy - 1, fy_month), 6) else fy


# ───────────────────────── company context ─────────────────────────
@dataclass
class Co:
    company_id: int
    symbol: str
    isin: Optional[str]
    bse_code: Optional[str]
    rank: Optional[int]
    listed: Optional[date] = None        # first listing on any exchange; None: before the exchange archives begin
    last_trade: Optional[date] = None    # last session on either exchange
    sme: bool = False
    fy_month: int = 3
    nse_listed: Optional[date] = None    # NSE only (the daily-price series is the NSE one); None: before 2010
    nse_last: Optional[date] = None

    def suspended_before(self, d: date, cal_last: date) -> bool:
        """True when trading had stopped before `d` (or the company is no longer trading at all and d is after)."""
        return (self.last_trade is not None and self.last_trade < cal_last - timedelta(days=SUSPENDED_AFTER)
                and d > self.last_trade)

    def listed_by(self, d: date) -> bool:
        return self.listed is None or self.listed <= d


@dataclass
class Gap:
    dataset: str
    period: str
    reason: str
    detail: dict = field(default_factory=dict)
    next_source: Optional[str] = None

    @property
    def state(self) -> str:
        return REASONS[self.reason][0]


@dataclass
class Result:
    expected: int = 0
    present: int = 0
    gaps: List[Gap] = field(default_factory=list)
    note: Optional[Gap] = None           # company-level reason when nothing is expected (period '')
    weight: Dict[str, int] = field(default_factory=dict)   # period -> items it stands for (prices: days)

    def counts(self) -> Tuple[int, int, int, int]:
        ex = sum(self.weight.get(g.period, 1) for g in self.gaps if g.state == "explained")
        op = sum(self.weight.get(g.period, 1) for g in self.gaps if g.state == "open")
        return self.expected, self.present, ex, op


def next_in_chain(dataset: str, tried: Iterable[str]) -> Optional[str]:
    t = set(tried)
    return next((s for s in CHAINS[dataset] if s.split(":")[0] not in t), None)


# ───────────────────────── expectation rules (pure) ─────────────────────────
def doc_gap(dataset: str, period: str, status: str, runs: int, tried: Sequence[str], **detail) -> Gap:
    """A document row that is not ok: queued, or somewhere along the fallback chain, or tried everywhere."""
    tried = [t for t in tried if t and t != "primary"]
    if status != "pending" and runs >= EXHAUSTED_RUNS:
        return Gap(dataset, period, "exhausted", {"status": status, "runs": runs, "tried": sorted(set(tried)), **detail})
    if status == "no_text":
        return Gap(dataset, period, "scanned_no_text", {"runs": runs, **detail}, "ocr")
    nxt = "original" if status == "pending" else next_in_chain(dataset, ["screener_link", "original", *tried])
    return Gap(dataset, period, DOC_REASON.get(status, "download_failed"),
               {"status": status, "runs": runs, "tried": sorted(set(tried)), **detail}, nxt)


def expect_annual_reports(co: Co, docs: Dict[int, dict], today: date, cal_last: date) -> Result:
    """docs: fiscal_year -> {status, runs, tried} (best status of the company's reports for that FY)."""
    r = Result()
    fy_last = fy_of(today, co.fy_month)
    while add_months(month_end(fy_last, co.fy_month), AR_LAG_MONTHS) > today:
        fy_last -= 1
    first = max(AR_FIRST_FY, first_ar_fy(co.listed, co.fy_month))
    for fy in range(first, fy_last + 1):
        r.expected += 1
        d = docs.get(fy)
        p = f"FY{fy}"
        if d and d["status"] == "ok":
            r.present += 1
        elif co.suspended_before(month_end(fy, co.fy_month), cal_last):
            r.gaps.append(Gap("annual_report", p, "suspended", {"last_trade": str(co.last_trade)}))
        elif d:
            r.gaps.append(doc_gap("annual_report", p, d["status"], d["runs"], d["tried"]))
        else:
            r.gaps.append(Gap("annual_report", p, "no_link", {}, "company_site"))
    if r.expected == 0:
        due = add_months(month_end(max(first, fy_last + 1), co.fy_month), AR_LAG_MONTHS)
        r.note = Gap("annual_report", "", "listed_recently", {"listed": str(co.listed), "first_report_due": str(due)})
    return r


def expect_concalls(co: Co, transcripts: Dict[date, dict], evidence: Dict[date, Set[str]], today: date,
                    cal_last: date) -> Result:
    """transcripts: reported quarter -> {status, runs, tried} (document rows); evidence: reported quarter -> link
    kinds on record (concall_transcript / _ppt / _recording / _notes). Calls are voluntary: quarters from the first
    one on record are expected. A recording or notes prove a call; a deck alone does not (decks are often published
    without a call)."""
    r = Result()
    last = last_due_quarter(today, co.fy_month, CALL_LAG)
    have = set(transcripts) | set(evidence)
    ok = {q for q, t in transcripts.items() if t["status"] == "ok"}
    recent = co.listed is not None and co.listed > quarters_back(last, 2)[0]
    if not have:
        r.note = Gap("concall_transcript", "", "listed_recently" if recent else "no_concalls",
                     {"listed": str(co.listed)} if recent else {})
        return r
    for q in quarters_between(min(have), last):
        if not co.listed_by(q + timedelta(days=result_lag(q, co.fy_month))):
            continue
        r.expected += 1
        p = q.isoformat()
        t, kinds = transcripts.get(q), evidence.get(q, set())
        if t and t["status"] == "ok":
            r.present += 1
        elif t:
            r.gaps.append(doc_gap("concall_transcript", p, t["status"], t["runs"], t["tried"]))
        elif "concall_transcript" in kinds:      # linked, no document row yet: the next docs pass picks it up
            r.gaps.append(Gap("concall_transcript", p, "queued", {}, "original"))
        elif kinds & {"concall_recording", "concall_notes"}:
            r.gaps.append(Gap("concall_transcript", p, "call_held_no_transcript", {"evidence": sorted(kinds)},
                              "company_site"))
        elif q >= REGULAR_CALLS_FROM and quarters_back(q, 2)[0] in ok and quarter_end(q + timedelta(days=1)) in ok:
            r.gaps.append(Gap("concall_transcript", p, "call_likely_missed", {"evidence": sorted(kinds)},
                              "company_site"))
        elif kinds:
            r.gaps.append(Gap("concall_transcript", p, "deck_only"))
        elif co.suspended_before(q, cal_last):
            r.gaps.append(Gap("concall_transcript", p, "suspended", {"last_trade": str(co.last_trade)}))
        else:
            r.gaps.append(Gap("concall_transcript", p, "no_call_that_quarter"))
    if r.expected == 0:                          # only calls newer than the last due quarter so far
        r.note = Gap("concall_transcript", "", "listed_recently", {"listed": str(co.listed)})
    return r


def expect_linked_docs(dataset: str, docs: Dict[str, dict]) -> Result:
    """Datasets with no schedule of their own (decks): every linked document is expected."""
    r = Result()
    for p, d in sorted(docs.items()):
        r.expected += 1
        if d["status"] == "ok":
            r.present += 1
        else:
            r.gaps.append(doc_gap(dataset, p, d["status"], d["runs"], d["tried"]))
    return r


def expect_periods(dataset: str, co: Co, periods: Sequence[date], have: Dict[date, str], lag_of, today: date,
                   cal_last: date, by_fy: bool = False) -> Result:
    """Statement / shareholding periods. have: period end -> source that has it ('screener' or 'yfinance').
    by_fy: an annual period counts for its fiscal-year label (a company that moved its year end, e.g. HCLTech from
    June to March, has 'Jun 2015' for FY2016). A period older than everything the sources hold for the company is
    outside their history (screener shows a fixed number of columns); only holes after that are actionable."""
    r = Result()
    key = (lambda d: fy_of(d, co.fy_month)) if by_fy else (lambda d: d)
    held = {key(d) for d in have}
    oldest = min(held, default=None)
    for q in periods:
        if not co.listed_by(q + timedelta(days=lag_of(q))):
            continue
        r.expected += 1
        p = q.isoformat()
        if key(q) in held:
            r.present += 1
        elif co.suspended_before(q + timedelta(days=lag_of(q)), cal_last):
            r.gaps.append(Gap(dataset, p, "suspended", {"last_trade": str(co.last_trade)}))
        elif oldest is not None and key(q) < oldest:
            r.gaps.append(Gap(dataset, p, "before_source_history", {"source_first": str(min(have))}))
        else:                                    # neither screener nor yfinance has it: the next screener refresh
            r.gaps.append(Gap(dataset, p, "missing_in_source", {}, "screener"))
    if r.expected == 0:
        r.note = Gap(dataset, "", "listed_recently", {"listed": str(co.listed)})
    return r


# Statement / shareholding windows are what screener shows: the last N periods ending at the later of the last
# due period and the newest one the company already published (an early filer's window has moved on).
def _quarter_anchor(newest: Optional[date]) -> date:
    """Only a true quarter end anchors a window (shareholding also has event filings dated e.g. 31 Oct)."""
    return newest if newest and newest == quarter_end(newest) else date.min


def fin_quarter_periods(co: Co, today: date, newest: Optional[date] = None) -> List[date]:
    last = max(last_due_quarter(today, co.fy_month), _quarter_anchor(newest))
    qs = quarters_back(last, FIN_Q_WINDOW)
    if co.sme:                                   # SME boards report half-years: the FY end and six months before
        half = {co.fy_month, (co.fy_month + 6 - 1) % 12 + 1}
        qs = [q for q in qs if q.month in half]
    return qs


def fin_year_periods(co: Co, today: date, newest: Optional[date] = None) -> List[date]:
    fy = fy_of(today, co.fy_month)
    while month_end(fy, co.fy_month) + timedelta(days=YEAR_RESULT_LAG) > today:
        fy -= 1
    if newest and newest.month == co.fy_month:
        fy = max(fy, newest.year)
    return [month_end(y, co.fy_month) for y in range(fy - FIN_A_WINDOW + 1, fy + 1)]


def shp_periods(today: date, newest: Optional[date] = None) -> List[date]:
    q = quarter_end(today)
    while q + timedelta(days=SHP_LAG) > today:
        q = quarters_back(q, 2)[0]
    return quarters_back(max(q, _quarter_anchor(newest)), SHP_WINDOW)


def sessions_by_year(cal: Sequence[date], start: date, end: date) -> Dict[int, int]:
    """Calendar sessions per year within [start, end]; cal is sorted."""
    i, j = bisect_left(cal, start), bisect_right(cal, end)
    out: Dict[int, int] = {}
    while i < j:
        y = cal[i].year
        k = min(bisect_left(cal, date(y + 1, 1, 1), i, j), j)
        out[y] = k - i
        i = k
    return out


def expect_prices(co: Co, cal: Sequence[date], present_by_year: Dict[int, int], cal_last: date,
                  rejected_by_year: Optional[Dict[int, int]] = None) -> Result:
    """Trading sessions from listing (or 2010) to the last trade. Sessions still absent after `fill` copied the
    bhavcopy in are sessions with no trade in the exchange record (illiquid or suspended)."""
    r = Result()
    start = max(PRICE_FROM, co.nse_listed or PRICE_FROM)
    last = co.nse_last or co.last_trade
    stopped = last is not None and last < cal_last - timedelta(days=SUSPENDED_AFTER)
    for y, n in sorted(sessions_by_year(cal, start, last if stopped else cal_last).items()):
        have = min(present_by_year.get(y, 0), n)
        r.expected += n
        r.present += have
        rej = min((rejected_by_year or {}).get(y, 0), n - have)
        if rej:
            r.gaps.append(Gap("prices", f"{y}:basis", "basis_conflict", {"sessions": rej}))
            r.weight[f"{y}:basis"] = rej
        if have + rej < n:
            r.gaps.append(Gap("prices", str(y), "no_trade", {"sessions": n, "missing": n - have - rej}))
            r.weight[str(y)] = n - have - rej
    if stopped:
        r.note = Gap("prices", "", "suspended", {"last_nse_session": str(last)})
    return r


def expect_dividends(co: Co, payout: Dict[date, float], div_dates: Sequence[date],
                     archive: Optional[Tuple[date, date]]) -> Result:
    """payout: annual period end -> screener Dividend Payout % (max over bases). A year with payout > 0 expects a
    dividend ex-date between the year's start and 9 months after its end (interim + final). yfinance has been read
    for every company; the NSE archive (loaded ex-date range `archive`) counts as tried once it covers the window."""
    r = Result()
    years = sorted(p for p, v in payout.items() if v and v > 0 and co.listed_by(p + timedelta(days=YEAR_RESULT_LAG)))
    if not years:
        if payout:
            r.note = Gap("dividends", "", "no_dividends", {"years_checked": len(payout)})
        return r
    for p in years:
        r.expected += 1
        lo, hi = add_months(p, -12) + timedelta(days=1), add_months(p, 9)
        if any(lo <= d <= hi for d in div_dates):
            r.present += 1
            continue
        detail = {"payout_pct": payout[p]}
        if co.nse_listed and hi < co.nse_listed:  # paid while on BSE only: no NSE source can have it
            nxt, detail["before_nse_listing"] = "yfinance_bse", str(co.nse_listed)
        else:
            covered = archive is not None and archive[0] <= lo and hi <= archive[1]
            nxt = next_in_chain("dividends", ["yfinance", "yfinance_bse"] + (["nse_archive"] if covered else []))
        r.gaps.append(Gap("dividends", p.isoformat(), "no_record" if nxt else "exhausted", detail, nxt))
    return r


def expect_one(dataset: str, present: bool, absent: Optional[Gap]) -> Result:
    r = Result(expected=1, present=int(present))
    if not present and absent:
        r.gaps.append(absent)
    return r


# ───────────────────────── evidence (set-based SQL) ─────────────────────────
def _q(conn, sql: str, params=None) -> List[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)          # None, not (): a literal % (e.g. 'Dividend Payout %') stays literal
        return cur.fetchall()


def _values(cur, sql: str, rows: Sequence[tuple], page: int = 5000) -> int:
    """execute_values page by page, summing rowcount (execute_values alone reports only its last page)."""
    n = 0
    for i in range(0, len(rows), page):
        execute_values(cur, sql, rows[i:i + page], page_size=page)
        n += max(cur.rowcount, 0)
    return n


def listing_date(nse_first: Optional[date], bse_first: Optional[date]) -> Optional[date]:
    """First listing from the exchange archives' first sightings; None = listed before the archives (long listed).
    A sighting in an archive's first file only bounds the date: NSE's (Jan 2010) means long listed; BSE's (Dec 2016)
    means 'by Dec 2016', so an earlier NSE date is the listing and a later one is a move from BSE (long listed)."""
    nse_floor, bse_floor = NSE_WINDOW + timedelta(days=7), BSE_WINDOW + timedelta(days=7)
    if nse_first and nse_first <= nse_floor:
        return None
    if bse_first and bse_first <= bse_floor:
        return nse_first if nse_first and nse_first <= BSE_WINDOW else None
    return min([d for d in (nse_first, bse_first) if d], default=None)


def load_companies(conn) -> Tuple[Dict[int, Co], List[date]]:
    """Universe companies with listing date, last trade, SME flag and FY-end month; plus the trading calendar."""
    cal = [d for (d,) in _q(conn, "select date from equity.nse_bhav_full where series='EQ' group by date "
                                  "having count(*) > 300 order by date")]
    rows = _q(conn, """
        with u as (select company_id, rank from equity.universe where universe_code=%s
                   and as_of=(select max(as_of) from equity.universe where universe_code=%s)),
        lh as (select * from equity.listing_history where series = any(%s)),
        nse as (select c.company_id, min(l.first_seen) f, max(l.last_seen) l,
                       bool_or(l.is_listed_now and l.series = any(%s)) sme_now,
                       bool_or(l.is_listed_now and not l.series = any(%s)) main_now
                from equity.company c join u using (company_id)
                join lh l on (l.isin = c.isin and c.isin <> '')
                          or (l.isin = '' and l.symbol = c.nse_symbol
                              and not exists (select 1 from lh x where x.symbol = l.symbol and x.isin not in ('', c.isin)))
                group by 1),
        bse as (select c.company_id, min(b.date) f, max(b.date) l from equity.company c join u using (company_id)
                join equity.bse_bhav b on b.isin = c.isin group by 1),
        fym as (select f.company_id, mode() within group (order by extract(month from f.period_end)::int) m
                from equity.financial_fact f join equity.metric m using (metric_id) join u using (company_id)
                where m.source='screener' and m.section='profit-loss' and m.name in ('Sales','Revenue','Net Profit')
                  and m.parent_metric_id is null and f.period_type='A' group by 1)
        select c.company_id, c.nse_symbol, c.isin, c.bse_code, u.rank, nse.f, nse.l, bse.f, bse.l,
               coalesce(nse.sme_now and not nse.main_now, false), coalesce(fym.m, 3)
        from equity.company c join u using (company_id) left join nse using (company_id)
        left join bse using (company_id) left join fym using (company_id)""",
              (UNIVERSE, UNIVERSE, list(EQ_SERIES), list(SME_SERIES), list(SME_SERIES)))
    cos = {}
    for cid, sym, isin, bse_code, rank, nf, nl, bf, bl, sme, fym in rows:
        last = max([d for d in (nl, bl) if d], default=None)
        listed = listing_date(nf, bf)
        nse_listed = nf if nf and nf > NSE_WINDOW + timedelta(days=7) else None
        cos[cid] = Co(cid, sym, isin, bse_code, rank, listed, last, bool(sme), int(fym), nse_listed, nl)
    return cos, cal


def load_doc_evidence(conn) -> Tuple[dict, dict, dict, dict]:
    """(annual reports by (cid, fy), transcripts by (cid, quarter), call evidence quarters by cid, decks by (cid, q))."""
    from .documents import derive_doc_date
    rank = {"ok": 4, "pending": 3, "no_text": 2, "failed": 1, "missing": 0}
    ar: Dict[tuple, dict] = {}
    tr: Dict[tuple, dict] = {}
    ppt: Dict[tuple, dict] = {}
    ev: Dict[int, Dict[date, Set[str]]] = defaultdict(lambda: defaultdict(set))   # cid -> quarter -> link kinds
    rows = _q(conn, """
        select d.company_id, d.doc_type, d.fiscal_year, d.doc_date, d.status,
               coalesce(a.runs, 0), coalesce(a.tried, '{}')
        from equity.document d
        left join lateral (select count(distinct e->>'at') filter (where e->>'source' is distinct from 'primary') runs,
                                  array_agg(distinct e->>'source') tried
                           from jsonb_array_elements(d.attempts_log) e where d.status <> 'ok') a on true
        where d.doc_type in ('annual_report','concall_transcript','concall_ppt')""")
    for cid, dt, fy, ddate, status, runs, tried in rows:
        rec = {"status": status, "runs": runs, "tried": [t for t in tried if t]}
        if dt == "annual_report":
            if fy is None:
                continue
            key, target = (cid, fy), ar
        else:
            if ddate is None:
                continue
            q = reported_quarter(ddate)
            ev[cid][q].add(dt)
            key, target = (cid, q), (tr if dt == "concall_transcript" else ppt)
        cur = target.get(key)
        if cur is None or rank[status] > rank[cur["status"]]:
            target[key] = rec
    for cid, kind, label in _q(conn, "select company_id, doc_type, period_label from equity.document_link where doc_type "
                                     "in ('concall_recording','concall_notes','concall_transcript','concall_ppt')"):
        d = derive_doc_date(label)
        if d:
            ev[cid][reported_quarter(d)].add(kind)
    return ar, tr, ev, ppt


def load_fin_evidence(conn) -> Tuple[dict, dict]:
    """(quarterly, annual) period ends with the source that has them: {(cid, period_end): 'screener'|'yfinance'}."""
    q: Dict[tuple, str] = {}
    a: Dict[tuple, str] = {}
    rows = _q(conn, """
        select f.company_id, f.period_type, f.period_end, bool_or(m.source='screener')
        from equity.financial_fact f join equity.metric m using (metric_id)
        where m.parent_metric_id is null and f.period_type in ('Q','A')
          and ((m.source='screener' and m.section in ('quarters','profit-loss') and m.name in ('Sales','Revenue','Net Profit'))
            or (m.source='yfinance' and m.section='yf-income' and m.name in ('Total Revenue','Net Income')))
        group by 1, 2, 3""")
    for cid, pt, pe, scr in rows:
        (q if pt == "Q" else a)[(cid, pe)] = "screener" if scr else "yfinance"
    return q, a


def load_shp_evidence(conn) -> Set[tuple]:
    return set(_q(conn, "select distinct company_id, period_end from equity.shareholding where frequency='Q'"))


def load_price_evidence(conn, cal: Sequence[date]) -> Dict[str, Dict[int, int]]:
    """symbol -> year -> sessions present (calendar sessions only)."""
    out: Dict[str, Dict[int, int]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute("create temp table if not exists cg_cal (date date primary key) on commit drop")
        execute_values(cur, "insert into cg_cal values %s on conflict do nothing", [(d,) for d in cal], page_size=5000)
        cur.execute("""select h.symbol, extract(year from h.date)::int, count(*) from equity.historical_prices h
                       join cg_cal using (date) where h.date >= %s group by 1, 2""", (PRICE_FROM,))
        for sym, y, n in cur.fetchall():
            out[sym][y] = n
    return out


def load_dividend_evidence(conn) -> Tuple[dict, dict]:
    pay: Dict[int, Dict[date, float]] = defaultdict(dict)
    for cid, pe, v in _q(conn, """
            select f.company_id, f.period_end, max(f.value) from equity.financial_fact f join equity.metric m using (metric_id)
            where m.source='screener' and m.section='profit-loss' and m.name='Dividend Payout %'
              and m.parent_metric_id is null and f.period_type='A' group by 1, 2"""):
        pay[cid][pe] = float(v)
    divs: Dict[int, List[date]] = defaultdict(list)
    for cid, ex in _q(conn, "select company_id, ex_date from equity.corporate_action where action_type='dividend'"):
        divs[cid].append(ex)
    return pay, divs


# ───────────────────────── zero-network chain steps (`fill`) ─────────────────────────
PRICE_FILL_KEY = "gaps:prices_filled"   # equity.control: date of the last full bhavcopy -> daily-price pass
PRICE_FULL_EVERY = 7                     # days; between full passes only the last PRICE_INCREMENTAL days are read
PRICE_INCREMENTAL = 14


STEP_LIMIT = 1.4                          # a filled row stepping more than this against a yfinance neighbour is refused
PRICE_CHUNKS = 8                          # symbol chunks per pass, each its own transaction
OVERLAP_DAYS = 45                         # how far the basis is looked for on each side (PK range scan)


def fill_prices_from_bhavcopy(conn, full: Optional[bool] = None) -> int:
    """NSE bhavcopy sessions the daily-price table lacks, mapped to the current symbol through the ISIN (renames).
    Only its own rows are ever updated (source 'nse_bhavcopy:fill'): a session yfinance or the daily bhavcopy job wrote
    is never touched (rule 7). The row is put on the basis of its neighbours, measured, not assumed: yfinance's
    close / adj_close over the raw exchange close on the nearest overlapping session before and after, carried across
    any split/bonus/dividend factor between that session and this one (equity.adj_factor); the two sides agree -> that
    basis, else the nearer side; no overlap at all -> the factors alone. A filled row that still steps more than
    STEP_LIMIT against a non-filled neighbour (a demerger or consolidation the factors don't know) is deleted and
    recorded in equity.price_fill_reject. A full pass (~3 min) runs weekly and re-checks the rejects; else 14 days."""
    last = _q(conn, "select value #>> '{}' from equity.control where key=%s", (PRICE_FILL_KEY,))
    if full is None:
        full = not last or date.fromisoformat(last[0][0]) <= date.today() - timedelta(days=PRICE_FULL_EVERY)
    since = date(1990, 1, 1) if full else date.today() - timedelta(days=PRICE_INCREMENTAL)
    n = 0
    with conn.cursor() as cur:
        if full:
            cur.execute("delete from equity.price_fill_reject")
            conn.commit()
    for chunk in range(PRICE_CHUNKS):        # one commit per symbol chunk: row locks are held briefly
        with conn, conn.cursor() as cur:
            cur.execute("set local equity.writer = 'fill'")       # sql/113: other writers take filled rows over
            cur.execute("""
                with lh as (select * from equity.listing_history where series = any(%(s)s)),
                m as (select distinct c.nse_symbol tgt, l.symbol src, l.first_seen, l.last_seen
                      from equity.company c join lh l on (l.isin = c.isin and c.isin <> '')
                            or (l.isin = '' and l.symbol = c.nse_symbol
                                and not exists (select 1 from lh x where x.symbol = l.symbol and x.isin not in ('', c.isin)))
                      where c.nse_symbol is not null and abs(hashtext(c.nse_symbol)) %% %(chunks)s = %(chunk)s),
                b as (select distinct on (m.tgt, b.date) m.tgt, m.src, b.series, b.date, b.open, b.high, b.low, b.close,
                             b.ttl_trd_qnty
                      from equity.nse_bhav_full b join m on b.symbol = m.src and b.date between m.first_seen and m.last_seen
                      where b.date >= %(since)s and b.series = any(%(s)s) and b.ttl_trd_qnty > 0 and b.close > 0
                        and b.high >= b.low and b.high >= greatest(b.open, b.close) and b.low <= least(b.open, b.close)
                        and not exists (select 1 from equity.historical_prices h where h.symbol = m.tgt and h.date = b.date
                                        and h.source is distinct from 'nse_bhavcopy:fill')
                        and not exists (select 1 from equity.price_fill_reject r where r.symbol = m.tgt and r.date = b.date)
                      order by m.tgt, b.date, array_position(%(s)s, b.series)),
                e as (select b.*, pv.d pd, pv.rc prc, pv.ra pra, nx.d nd, nx.rc nrc, nx.ra nra from b
                      left join lateral (select h.date d, h.close / x.close rc, h.adj_close / x.close ra
                                         from equity.historical_prices h join equity.nse_bhav_full x
                                           on x.symbol = b.src and x.series = b.series and x.date = h.date
                                         where h.symbol = b.tgt and h.date < b.date and h.date >= b.date - %(days)s
                                           and h.source is null and x.close > 0 and h.close > 0
                                         order by h.date desc limit 1) pv on true
                      left join lateral (select h.date d, h.close / x.close rc, h.adj_close / x.close ra
                                         from equity.historical_prices h join equity.nse_bhav_full x
                                           on x.symbol = b.src and x.series = b.series and x.date = h.date
                                         where h.symbol = b.tgt and h.date > b.date and h.date <= b.date + %(days)s
                                           and h.source is null and x.close > 0 and h.close > 0
                                         order by h.date limit 1) nx on true),
                f as (select e.*, k.*,
                             case when prc is not null and nrc is not null and abs((prc / ps) / (nrc * ns) - 1) < 0.02
                                       then prc / ps
                                  when prc is not null and (nrc is null or date - pd <= nd - date) then prc / ps
                                  when nrc is not null then nrc * ns
                                  else fs end bc,
                             case when pra is not null and nra is not null and abs((pra / pa) / (nra * na) - 1) < 0.02
                                       then pra / pa
                                  when pra is not null and (nra is null or date - pd <= nd - date) then pra / pa
                                  when nra is not null then nra * na
                                  else fa end ba
                      from e cross join lateral (
                          -- basis(session) = basis(prev) / factors in (prev, session] = basis(next) x factors in
                          -- (session, next]; fs/fa: every factor after the session (no overlap at all)
                          select coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.date
                                                                        and a.kind in ('split','bonus'))), 1) fs,
                                 coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.date)), 1) fa,
                                 coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.pd and a.ex_date <= e.date
                                                                        and a.kind in ('split','bonus'))), 1) ps,
                                 coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.pd
                                                                        and a.ex_date <= e.date)), 1) pa,
                                 coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.date and a.ex_date <= e.nd
                                                                        and a.kind in ('split','bonus'))), 1) ns,
                                 coalesce(exp(sum(ln(a.factor)) filter (where a.ex_date > e.date
                                                                        and a.ex_date <= e.nd)), 1) na
                          from equity.adj_factor a where a.symbol = e.tgt) k)
                insert into equity.historical_prices (symbol, date, open, high, low, close, adj_close, volume, source)
                select tgt, date, round(open * bc, 4), round(high * bc, 4), round(low * bc, 4), round(close * bc, 4),
                       round(close * ba, 4), round(ttl_trd_qnty / nullif(bc, 0))::bigint, 'nse_bhavcopy:fill'
                from f where bc > 0 and ba > 0
                on conflict (symbol, date) do update set open = excluded.open, high = excluded.high,
                    low = excluded.low, close = excluded.close, adj_close = excluded.adj_close, volume = excluded.volume
                where equity.historical_prices.source = 'nse_bhavcopy:fill'
                  and (equity.historical_prices.close, equity.historical_prices.adj_close, equity.historical_prices.volume)
                      is distinct from (excluded.close, excluded.adj_close, excluded.volume)""",
                        {"s": list(EQ_SERIES), "since": since, "chunk": chunk, "chunks": PRICE_CHUNKS,
                         "days": OVERLAP_DAYS})
            n += cur.rowcount
    with conn, conn.cursor() as cur:
        n -= reject_price_steps(cur, since)
        cur.execute("""insert into equity.control (key, value, reason) values (%s, to_jsonb(%s::text), 'gaps fill')
                       on conflict (key) do update set value = excluded.value, updated_at = now()""",
                    (PRICE_FILL_KEY, (date.today() if full else date.fromisoformat(last[0][0])).isoformat()))
    return n


def reject_price_steps(cur, since: date) -> int:
    """Delete filled rows that step more than STEP_LIMIT against an adjacent non-filled row, record them in
    equity.price_fill_reject; returns how many."""
    cur.execute("""
        with s as (select distinct symbol from equity.historical_prices where source = 'nse_bhavcopy:fill' and date >= %(since)s),
        w as (select h.symbol, h.date, h.close, h.source,
                     lag(h.close) over k pc, lag(h.source) over k ps, lead(h.close) over k nc, lead(h.source) over k ns
              from equity.historical_prices h join s using (symbol) window k as (partition by h.symbol order by h.date)),
        bad as (select symbol, date, greatest(case when ps is null and pc > 0 then abs(ln(close / pc)) end,
                                              case when ns is null and nc > 0 then abs(ln(close / nc)) end) lr
                from w where source = 'nse_bhavcopy:fill' and date >= %(since)s and close > 0),
        del as (delete from equity.historical_prices h using bad
                where bad.lr > ln(%(lim)s) and h.symbol = bad.symbol and h.date = bad.date
                  and h.source = 'nse_bhavcopy:fill' returning h.symbol, h.date, round(exp(bad.lr), 3) r)
        insert into equity.price_fill_reject (symbol, date, ratio) select symbol, date, r from del
        on conflict (symbol, date) do update set ratio = excluded.ratio, at = now()""",
                {"since": since, "lim": STEP_LIMIT})
    return cur.rowcount


def fill_bse_codes(conn) -> int:
    """BSE scrip code from the BSE bhavcopy's ISIN where screener had no BSE link (codes are unique)."""
    with conn.cursor() as cur:
        cur.execute("""
            with need as (select company_id, isin from equity.company where bse_code is null and isin <> ''),
            b as (select n.company_id, x.sc_code from need n
                  cross join lateral (select sc_code from equity.bse_bhav where isin = n.isin limit 1) x)
                  -- one scrip code per ISIN; no ORDER BY, which walked the date index through every row per ISIN
            update equity.company c set bse_code = b.sc_code, updated_at = now() from b
            where c.company_id = b.company_id
              and not exists (select 1 from equity.company x where x.bse_code = b.sc_code)""")
        return cur.rowcount


_NUM = r"(\d+(?:\.\d+)?)"
# DIV glued to anything ("DIVRE 0.50", "INT DIVD", "DIVINDEND") but never SUB-DIVISION / DIVISION
_DIV_RE = re.compile(r"(?<!SUB)(?<!SUB-)(?<!SUB )DIV(?!IS)", re.I)
_INT_RS_RE = re.compile(r"^(?:AGM\s*/\s*)?INT(?:ERIM)?\b[^A-Z]*\b(?:RS|RE)\b", re.I)    # "INT RS 5/-": no DIV word
# a word start, or glued to a dividend-type prefix ("SPLRS20"); never the RE of "SHARE 2023"
_RS_RE = re.compile(r"(?:\b|(?<=SPL)|(?<=FIN)|(?<=INT)|(?<=DIV)|(?<=DIVD))(?:RS|RE|INR)\.?\s*-?\s*" + _NUM, re.I)
_PCT_RE = re.compile(_NUM + r"\s*%")
_BARE_RE = re.compile(r"(?<![\w.:/-])" + _NUM + r"(?![\d:%]|(?:ST|ND|RD|TH)\b)")   # not "2ND INTERIM"
_FV_RE = re.compile(r"\(?\s*(?:FACE\s*VALUE|F\.?\s?V\.?)\s*(?:OF\s*)?(?:RS|RE)\.?\s*\d+(?:\.\d+)?\s*(?:/-)?\s*(?:EACH)?\s*\)?",
                    re.I)                                           # "(FACE VALUE RS 1)" is not an amount
# BON / BONUS then the ratio, glued or not ("BON1:1"); never BOND, and a bonus debenture / preference / DVR issue
# ("BON DEB 1:1", "BONUS NCRPS1:116") has a word before the ratio, so it is not an equity bonus
_BONUS_RE = re.compile(r"\bBON(?:US)?(?![A-Z])\s*[-:@]?\s*" + _NUM + r"\s*:\s*" + _NUM, re.I)
# FV x -> y: "FACE VALUE SPLIT ... FROM RS 10/- PER SHARE TO RS 2/-", "FVSPLTFRM10TO1", "FV SPL-RS10TORS5";
# a bare "SPL" is a special dividend, so it counts only after FV
_SPLIT_RE = re.compile(r"(?:SPLIT|SPLT|SUB[\s-]*DIV(?:ISION)?\b|FV\s*SPL)\D*?" + _NUM      # \D*? skips "TFRM" (no \w*)
                       + r"\s*(?:/-)?\s*(?:PER\s*(?:EQ\.?\s*)?SH(?:ARE)?\.?)?\s*-?\s*TO\s*(?:RS|RE|INR)?\.?\s*" + _NUM,
                       re.I)
_CONSOL_RE = re.compile(r"CONSOLIDATION\D*?" + _NUM + r"\s*(?:/-)?\s*(?:PER\s*SH(?:ARE)?)?\s*-?\s*TO\s*(?:RS|RE)?\.?\s*"
                        + _NUM, re.I)                               # reverse split: FV x -> y, x < y
_RIGHTS_RE = re.compile(r"RIGHTS?\b\s*[-:@]?\s*(\d+)\s*[A-Z]*\s*:\s*(\d+)", re.I)


def parse_purpose(purpose: str, face_value: Optional[float] = None) -> List[Tuple[str, Optional[float]]]:
    """NSE book-closure purpose -> [(action_type, value)]. Dividend value is Rs per share (a percentage is of face
    value), bonus a:b -> (a+b)/b, split FV x -> y -> x/y new shares per old (a consolidation gives a value < 1),
    rights a:b -> a/b."""
    p = " ".join(purpose.upper().split())
    out: List[Tuple[str, Optional[float]]] = []
    if (m := _BONUS_RE.search(p)):
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            out.append(("bonus", round((a + b) / b, 6)))
    if (m := _SPLIT_RE.search(p)):
        x, y = float(m.group(1)), float(m.group(2))
        if x > y > 0:
            out.append(("split", round(x / y, 6)))
    elif (m := _CONSOL_RE.search(p)):
        x, y = float(m.group(1)), float(m.group(2))
        if y > x > 0:
            out.append(("split", round(x / y, 6)))
    if (m := _RIGHTS_RE.search(p)):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            out.append(("rights", round(a / b, 6)))
    m = _DIV_RE.search(p)
    if m or _INT_RS_RE.search(p):
        # amounts after the first DIV: rupee amounts win; else a percentage of face value; else bare numbers
        # ("DIV-SPL 10+FIN 1 P S"), never a year
        seg = p[m.start():] if m else p
        for rx in (_RIGHTS_RE, _BONUS_RE, _SPLIT_RE, _CONSOL_RE, _FV_RE):
            seg = rx.sub(" ", seg)
        rs = [float(x) for x in _RS_RE.findall(seg)]
        pct = [float(x) for x in _PCT_RE.findall(seg)]
        if rs:
            total = sum(rs)
        elif pct:
            total = sum(pct) * face_value / 100 if face_value else 0.0
        else:
            total = sum(v for v in map(float, _BARE_RE.findall(seg)) if v < 1900)
        out.append(("dividend", round(total, 4) if total > 0 else None))
    return out


def fill_corporate_actions(conn) -> int:
    """equity.nse_corp_action (NSE PR archive) -> equity.corporate_action, source 'nse_archive', for events no other
    source recorded: a dividend within 3 days of an existing dividend, or a split/bonus within 3 days of an existing
    split/bonus (yfinance files bonuses as splits), is the same event and is skipped."""
    rows = _q(conn, """
        with lh as (select * from equity.listing_history where series = any(%s)),
        m as (select distinct c.company_id, l.symbol, l.first_seen, l.last_seen from equity.company c
              join lh l on l.isin = c.isin and c.isin <> ''
              union select c.company_id, c.nse_symbol, date '1990-01-01', date '2100-01-01' from equity.company c
              where c.nse_symbol is not null and not exists (select 1 from lh x where x.symbol = c.nse_symbol
                                                             and x.isin not in ('', coalesce(c.isin, ''))))
        select distinct on (m.company_id, a.ex_date, a.purpose) m.company_id, a.ex_date, a.purpose, a.symbol,
               (select i.face_value from equity.nse_issue_size i where i.symbol = a.symbol and i.date <= a.ex_date
                order by i.date desc limit 1),
               (select s.face_value from equity.market_snapshot s where s.company_id = m.company_id
                order by s.as_of desc limit 1)
        from equity.nse_corp_action a join m on a.symbol = m.symbol
             and a.ex_date between m.first_seen - 60 and m.last_seen + 60
        order by m.company_id, a.ex_date, a.purpose""", (list(EQ_SERIES),))
    have = defaultdict(list)
    for cid, kind, ex in _q(conn, "select company_id, action_type, ex_date from equity.corporate_action "
                                  "where source <> 'nse_archive'"):
        have[(cid, "dividend" if kind == "dividend" else "capital")].append(ex)
    acts: Dict[tuple, list] = {}
    for cid, ex, purpose, sym, fv_nse, fv_scr in rows:
        fv = float(fv_nse or fv_scr) if (fv_nse or fv_scr) else None
        for kind, value in parse_purpose(purpose, fv):
            fam = "dividend" if kind == "dividend" else ("capital" if kind in ("split", "bonus") else kind)
            if any(abs((ex - d).days) <= 3 for d in have.get((cid, fam), ())):
                continue
            key = (cid, kind, ex)
            if key in acts:                    # two purposes on one ex-date (final + special): add the amounts
                if kind == "dividend" and value and acts[key][3]:
                    acts[key][3] = round(acts[key][3] + value, 4)
                    acts[key][4] += f"; {purpose}"
                continue
            acts[key] = [cid, kind, ex, value, purpose, "nse_archive"]
    with conn.cursor() as cur:
        # an archive row that another source now has within 3 days is the same event: drop it (adj_factor would
        # otherwise apply it twice)
        cur.execute("""delete from equity.corporate_action a where a.source = 'nse_archive' and exists (
                         select 1 from equity.corporate_action b where b.company_id = a.company_id
                         and b.source <> 'nse_archive' and abs(b.ex_date - a.ex_date) <= 3
                         and (b.action_type = a.action_type
                              or (a.action_type in ('split','bonus') and b.action_type in ('split','bonus'))))""")
        return _values(cur, """insert into equity.corporate_action (company_id, action_type, ex_date, value, detail, source)
                               values %s on conflict (company_id, action_type, ex_date) do update
                               set value = excluded.value, detail = excluded.detail
                               where equity.corporate_action.source = 'nse_archive'""",
                       [tuple(a) for a in acts.values()])


def fill_shares(conn) -> int:
    """Shares outstanding: NSE PR-archive issue size (change log) for every company, then screener Equity Capital /
    Face Value for companies still without any history. Never overwrites another source's row."""
    with conn.cursor() as cur:
        cur.execute("""
            insert into equity.shares_outstanding (company_id, as_of, shares, source)
            select distinct on (c.company_id, i.date) c.company_id, i.date, i.issue_size, 'nse_issue_size'
            from equity.nse_issue_size i join equity.company c on c.nse_symbol = i.symbol
            where i.issue_size > 0 order by c.company_id, i.date
            on conflict (company_id, as_of) do update set shares = excluded.shares
            where equity.shares_outstanding.source = 'nse_issue_size'""")
        n = cur.rowcount
        cur.execute("""
            with cap as (select distinct on (f.company_id) f.company_id, f.period_end, f.value
                         from equity.financial_fact f join equity.metric m using (metric_id)
                         where m.source='screener' and m.section='balance-sheet' and m.name='Equity Capital'
                           and m.parent_metric_id is null and f.period_type in ('A','H') and f.value > 0
                         order by f.company_id, f.period_end desc, f.basis),
            fv as (select distinct on (company_id) company_id, face_value from equity.market_snapshot
                   where face_value > 0 order by company_id, as_of desc)
            insert into equity.shares_outstanding (company_id, as_of, shares, source)
            select cap.company_id, cap.period_end, round(cap.value * 1e7 / fv.face_value)::bigint, 'screener_derived'
            from cap join fv using (company_id)
            where not exists (select 1 from equity.shares_outstanding s where s.company_id = cap.company_id
                              and s.source <> 'screener_derived')
            on conflict (company_id, as_of) do update set shares = excluded.shares
            where equity.shares_outstanding.source = 'screener_derived'""")
        return n + cur.rowcount


def fill_adj_factors(conn) -> int:
    """Recompute equity.adj_factor after new corporate actions (prices filled next use it for adj_close)."""
    from .db import DB
    from .lake import lake_url
    from .sources.adj_factor import compute_factors
    db = DB(lake_url())
    try:
        return compute_factors(db)
    finally:
        db.close()


def fill(conn, full_prices: Optional[bool] = None) -> Dict[str, int]:
    """Every zero-network chain step in dependency order, each in its own transaction."""
    out = {}
    for name, step in (("bse_code", fill_bse_codes), ("corporate_actions", fill_corporate_actions),
                       ("adj_factor", fill_adj_factors), ("shares_outstanding", fill_shares),
                       ("prices", lambda c: fill_prices_from_bhavcopy(c, full_prices))):
        t = time.monotonic()
        with conn:
            out[name] = step(conn)
        log.info("fill %s: %d rows in %.1fs", name, out[name], time.monotonic() - t)
    return out


# ───────────────────────── build + persist the ledger ─────────────────────────
def build(conn, today: Optional[date] = None) -> Dict[int, Dict[str, Result]]:
    today = today or date.today()
    t0 = time.monotonic()
    cos, cal = load_companies(conn)
    cal_last = cal[-1] if cal else today
    ar, tr, ev, ppt = load_doc_evidence(conn)
    fq, fa = load_fin_evidence(conn)
    shp = load_shp_evidence(conn)
    px = load_price_evidence(conn, cal)
    rejects: Dict[str, Dict[int, int]] = defaultdict(dict)
    for sym, y, n in _q(conn, "select symbol, extract(year from date)::int, count(*) from equity.price_fill_reject "
                              "group by 1, 2"):
        rejects[sym][y] = n
    pay, divs = load_dividend_evidence(conn)
    shares = {cid for (cid,) in _q(conn, "select distinct company_id from equity.shares_outstanding")}
    bse_isins = {i for (i,) in _q(conn, "select distinct isin from equity.bse_bhav where isin is not null")}
    a0, a1 = _q(conn, "select min(ex_date), max(ex_date) from equity.nse_corp_action")[0]
    archive = (a0, a1) if a0 else None
    log.info("completeness evidence loaded in %.1fs (%d companies, %d sessions)", time.monotonic() - t0, len(cos), len(cal))

    by_co_ar = defaultdict(dict)
    for (cid, fy), d in ar.items():
        by_co_ar[cid][fy] = d
    by_co_tr = defaultdict(dict)
    for (cid, q), d in tr.items():
        by_co_tr[cid][q] = d
    by_co_ppt = defaultdict(dict)
    for (cid, q), d in ppt.items():
        by_co_ppt[cid][q.isoformat()] = d
    fq_by, fa_by = defaultdict(dict), defaultdict(dict)
    for (cid, pe), s in fq.items():
        fq_by[cid][pe] = s
    for (cid, pe), s in fa.items():
        fa_by[cid][pe] = s
    shp_by = defaultdict(dict)
    for cid, pe in shp:
        shp_by[cid][pe] = "screener"

    out: Dict[int, Dict[str, Result]] = {}
    for cid, co in cos.items():
        res = {
            "annual_report": expect_annual_reports(co, by_co_ar[cid], today, cal_last),
            "concall_transcript": expect_concalls(co, by_co_tr[cid], ev.get(cid, {}), today, cal_last),
            "concall_ppt": expect_linked_docs("concall_ppt", by_co_ppt[cid]),
            "fin_quarterly": expect_periods("fin_quarterly", co, fin_quarter_periods(co, today, max(
                (q for q in fq_by[cid] if q == quarter_end(q)), default=None)), fq_by[cid],
                                            lambda q, m=co.fy_month: result_lag(q, m), today, cal_last),
            "fin_annual": expect_periods("fin_annual", co, fin_year_periods(co, today, max(fa_by[cid], default=None)), fa_by[cid],
                                         lambda q: YEAR_RESULT_LAG, today, cal_last, by_fy=True),
            "shareholding": expect_periods("shareholding", co, shp_periods(today, max(
                (q for q in shp_by[cid] if q == quarter_end(q)), default=None)), shp_by[cid],
                                           lambda q: SHP_LAG, today, cal_last),
            "prices": expect_prices(co, cal, px.get(co.symbol, {}), cal_last, rejects.get(co.symbol)),
            "dividends": expect_dividends(co, pay.get(cid, {}), divs.get(cid, []), archive),
            "shares_outstanding": expect_one("shares_outstanding", cid in shares,
                                             Gap("shares_outstanding", "", "exhausted",
                                                 {"tried": ["yfinance", "nse_issue_size", "screener_equity_capital"]})),
            "bse_code": expect_one("bse_code", bool(co.bse_code),
                                   Gap("bse_code", "", "fillable", {}, "bse_bhavcopy") if co.isin in bse_isins
                                   else Gap("bse_code", "", "not_on_bse", {"isin": co.isin})),
        }
        if co.sme and res["fin_quarterly"].note is None:    # why only two periods a year are expected
            res["fin_quarterly"].note = Gap("fin_quarterly", "", "sme_half_yearly")
        out[cid] = res
    # network steps record their attempts on the ledger row (fallback.discover_ledger); a gap searched there
    # LEDGER_MAX_RUNS times without a find has been tried in every source
    from .fallback import LEDGER_MAX_RUNS
    from .tijori import TIJORI_RETRY_DAYS
    # Tijori (verified company documents, one page per company) goes first for every open report / transcript
    # gap it has not searched in TIJORI_RETRY_DAYS; the company-site search waits for it
    tried = {(c, d, p) for c, d, p in _q(conn, """select company_id, dataset, period from equity.data_gap g
                 where exists (select 1 from jsonb_array_elements(g.attempts) e where e->>'source' = 'tijori'
                               and (e->>'at')::timestamptz > now() - make_interval(days => %s))""", (TIJORI_RETRY_DAYS,))}
    for cid, res in out.items():
        for ds in ("annual_report", "concall_transcript"):
            for g in res[ds].gaps:
                if g.state == "open" and (cid, ds, g.period) not in tried:
                    g.next_source = "tijori"
    max_runs = {"company_site": LEDGER_MAX_RUNS, "yfinance_bse": 1}   # network steps that log on the ledger row
    for cid, ds, period, attempts in _q(conn, "select company_id, dataset, period, attempts from equity.data_gap "
                                              "where jsonb_array_length(attempts) > 0"):
        r = out.get(cid, {}).get(ds)
        if r is None:
            continue
        runs = Counter(a.get("source") for a in attempts)
        r.gaps = [Gap(ds, g.period, "exhausted", {**g.detail, "was": g.reason, "tried": dict(runs),
                                                  "last_result": attempts[-1].get("result")})
                  if g.period == period and g.next_source in max_runs and runs[g.next_source] >= max_runs[g.next_source]
                  else g for g in r.gaps]
    log.info("completeness built in %.1fs", time.monotonic() - t0)
    return out


def persist(conn, built: Dict[int, Dict[str, Result]], today: Optional[date] = None) -> Dict[str, dict]:
    """Replace the ledger in one transaction. A gap that continues keeps first_seen and its attempts."""
    today = today or date.today()
    gap_rows: Dict[tuple, tuple] = {}            # keyed by the primary key: one row per (company, dataset, period)
    comp_rows = []
    daily: Dict[str, dict] = defaultdict(lambda: {"companies": 0, "expected": 0, "present": 0, "explained": 0,
                                                  "open": 0, "reasons": Counter()})
    for cid, res in built.items():
        for ds, r in res.items():
            e, p, x, o = r.counts()
            comp_rows.append((cid, ds, e, p, x, o))
            agg = daily[ds]
            agg["companies"] += 1
            for k, v in zip(("expected", "present", "explained", "open"), (e, p, x, o)):
                agg[k] += v
            for g in r.gaps + ([r.note] if r.note else []):
                gap_rows[(cid, g.dataset, g.period)] = (cid, g.dataset, g.period, g.state, g.reason, Json(g.detail),
                                                        g.next_source)
                agg["reasons"][g.reason] += r.weight.get(g.period, 1) if g.period else 1
    with conn, conn.cursor() as cur:
        cur.execute("create temp table cg_gap (company_id int, dataset text, period text, state text, reason text, "
                    "detail jsonb, next_source text) on commit drop")
        execute_values(cur, "insert into cg_gap values %s", list(gap_rows.values()), page_size=10000)
        cur.execute("""insert into equity.data_gap (company_id, dataset, period, state, reason, detail, next_source)
                       select * from cg_gap on conflict (company_id, dataset, period) do update set
                         state = excluded.state, reason = excluded.reason, detail = excluded.detail,
                         next_source = excluded.next_source, updated_at = now()""")
        cur.execute("""delete from equity.data_gap g where not exists (select 1 from cg_gap t where t.company_id = g.company_id
                       and t.dataset = g.dataset and t.period = g.period)""")
        cur.execute("delete from equity.completeness")
        execute_values(cur, "insert into equity.completeness (company_id, dataset, expected, present, explained, open) "
                            "values %s", comp_rows, page_size=10000)
        execute_values(cur, """insert into equity.completeness_daily (as_of, dataset, companies, expected, present,
                                   explained, open, reasons) values %s on conflict (as_of, dataset) do update set
                                   companies = excluded.companies, expected = excluded.expected, present = excluded.present,
                                   explained = excluded.explained, open = excluded.open, reasons = excluded.reasons""",
                       [(today, ds, a["companies"], a["expected"], a["present"], a["explained"], a["open"],
                         json.dumps(dict(a["reasons"]))) for ds, a in daily.items()])
    return {ds: {k: (dict(v) if isinstance(v, Counter) else v) for k, v in a.items()} for ds, a in daily.items()}


DAILY_AFTER_HOUR = 20


def gaps_cli(do_fill: bool = True, daily: bool = False, full_prices: bool = False) -> int:
    from datetime import datetime
    from .lake import lake_connect
    conn = lake_connect()
    try:
        if daily:                       # due once the evening files are in, unless a build already ran after that
            done = _q(conn, "select 1 from equity.completeness where computed_at >= current_date + make_interval("
                            "hours => %s) limit 1", (DAILY_AFTER_HOUR,))
            if done or datetime.now().hour < DAILY_AFTER_HOUR:
                print("gaps: not due (ran today, or before %d:00)" % DAILY_AFTER_HOUR)
                return 0
        if do_fill:
            print("fill:", fill(conn, full_prices or None))
        summary = persist(conn, build(conn))
    finally:
        conn.close()
    w = max(map(len, summary))
    print(f"{'dataset':<{w}}  companies   expected    present  explained       open  covered  accounted")
    for ds in DATASETS:
        a = summary.get(ds)
        if not a:
            continue
        e = a["expected"] or 1
        print(f"{ds:<{w}}  {a['companies']:>9} {a['expected']:>10} {a['present']:>10} {a['explained']:>10} "
              f"{a['open']:>10}  {100 * a['present'] / e:6.1f}%  {100 * (a['present'] + a['explained']) / e:8.1f}%")
    return 0
