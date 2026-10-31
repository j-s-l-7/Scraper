"""Equity Console API. Read-only: only SELECTs against equity.* and reads of data/documents + data/library.sqlite.

Every SQL statement is parameterised; user-supplied column/sort/section/range values go through whitelists.
"""
from __future__ import annotations

import bisect
import gzip
import json
import re
import sqlite3
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from library import search as lib_search
from library.index import DEFAULT_DB as LIB_DB, doc_key
from library.search import query_terms

from . import db

ROOT = Path(__file__).resolve().parents[2]
WEB_OUT = ROOT / "console" / "web" / "out"
DOCS_ROOT = (ROOT / "data" / "documents").resolve()
UNIVERSE = "TOP100_MCAP"

SCREENER_SECTIONS = ["quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"]
YF_SECTIONS = ["yf-income", "yf-balance", "yf-cashflow"]
SECTIONS = {"screener": SCREENER_SECTIONS, "yfinance": YF_SECTIONS}
PERIODS = {"A", "Q", "H"}
RANGES = {"1m": 31, "6m": 183, "1y": 366, "3y": 1096, "5y": 1827, "10y": 3653, "max": None}
DOC_TYPES = {"annual_report", "concall_transcript", "concall_ppt"}
SORTS = {"rank", "symbol", "name", "sector", "market_cap_cr", "price", "pe", "roe_pct", "roce_pct", "ret_1y"}
MAX_POINTS = 1500
YF_PRIORITY = {
    "yf-income": ["Total Revenue", "Operating Revenue", "Gross Profit", "EBITDA", "EBIT", "Operating Income", "Pretax Income",
                  "Tax Provision", "Net Income", "Net Income Common Stockholders", "Diluted EPS", "Basic EPS"],
    "yf-balance": ["Total Assets", "Current Assets", "Total Liabilities Net Minority Interest", "Current Liabilities",
                   "Total Debt", "Stockholders Equity", "Cash And Cash Equivalents", "Working Capital"],
    "yf-cashflow": ["Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow", "Free Cash Flow", "Capital Expenditure",
                    "Cash Dividends Paid", "Repurchase Of Capital Stock"],
}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&\-]{1,20}$")

@asynccontextmanager
async def _lifespan(_app):
    import threading
    threading.Thread(target=_warm_loop, daemon=True, name="warm").start()
    yield


app = FastAPI(title="Equity Console", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=_lifespan)


# --------------------------------------------------------------------------- helpers
def downsample(rows: list, n: int = MAX_POINTS) -> list:
    """Evenly spaced points, always keeping the first and the last."""
    if len(rows) <= n:
        return rows
    step = (len(rows) - 1) / (n - 1)
    idx = sorted({round(i * step) for i in range(n)} | {len(rows) - 1})
    return [rows[i] for i in idx]


def bad(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


def pick(value: str | None, allowed, name: str, default=None):
    if value is None or value == "":
        return default
    if value not in allowed:
        bad(f"{name} must be one of {sorted(allowed)}")
    return value


def companies() -> dict[str, dict]:
    def load():
        rows = db.query(
            """select c.company_id, c.nse_symbol as symbol, c.name, c.sector, c.industry, coalesce(c.has_consolidated, false) as has_consolidated
               from equity.company c where c.nse_symbol is not null""")
        return {r["symbol"].upper(): r for r in rows}
    return db.cached("companies", load, ttl=300)


def company(symbol: str) -> dict:
    if not SYMBOL_RE.match(symbol):
        bad("bad symbol")
    c = companies().get(symbol.upper())
    if not c:
        bad(f"unknown symbol {symbol}", 404)
    return c


def default_basis(c: dict, basis: str | None) -> str:
    return pick(basis, {"C", "S"}, "basis", "C" if c["has_consolidated"] else "S")


def label_period(d: str, ptype: str) -> str:
    y, m, _ = d.split("-")
    mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(m) - 1]
    return f"{mon} {y}"


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health():
    row = db.one("select now() as now, current_database() as db")
    return {"ok": True, "db": row["db"], "now": row["now"], "library": library_stats()}


# --------------------------------------------------------------------------- securities
_SECURITIES_SQL = """
with u as (
    select * from equity.universe
    where universe_code = %s and as_of = (select max(as_of) from equity.universe where universe_code = %s)
), docs as (
    select company_id,
           count(*) filter (where doc_type = 'annual_report' and status = 'ok')       as ar_n,
           count(*) filter (where doc_type = 'concall_transcript' and status = 'ok')  as cc_n,
           count(*) filter (where status in ('failed','missing','no_text'))           as doc_gaps
    from equity.document group by company_id
)
select u.rank, c.company_id, c.nse_symbol as symbol, c.name, c.sector, c.industry, c.has_consolidated,
       s.as_of as snapshot_as_of, coalesce(s.market_cap_cr, u.market_cap_cr) as market_cap_cr, s.price, s.pe,
       s.roe_pct, s.roce_pct, s.high_52w, s.low_52w, s.dividend_yield_pct, g.value_pct as ret_1y,
       exists (select 1 from equity.financial_fact f join equity.metric m on m.metric_id = f.metric_id
               where f.company_id = c.company_id and f.basis = case when c.has_consolidated then 'C' else 'S' end and m.section = 'profit-loss') as has_screener,
       exists (select 1 from equity.yf_snapshot y where y.company_id = c.company_id) as has_yf,
       coalesce(d.ar_n, 0) as ar_n, coalesce(d.cc_n, 0) as cc_n, coalesce(d.doc_gaps, 0) as doc_gaps
from u
join equity.company c on c.company_id = u.company_id
left join equity.v_latest_snapshot s on s.company_id = c.company_id
left join lateral (
    select value_pct from equity.growth_summary gs
    where gs.company_id = c.company_id and gs.measure = 'price_cagr' and gs.horizon = '1Y'
    order by (gs.basis = case when c.has_consolidated then 'C' else 'S' end) desc, gs.as_of desc limit 1) g on true
left join docs d on d.company_id = c.company_id
order by u.rank
"""


def _load_securities():
    return db.query(_SECURITIES_SQL, (UNIVERSE, UNIVERSE))


@app.get("/api/securities")
def securities(sort: str = "rank", desc: bool = False):
    sort = pick(sort, SORTS, "sort", "rank")

    rows = db.cached("securities", _load_securities)
    if sort != "rank" or desc:
        rows = sorted(rows, key=lambda r: (r.get(sort) is None, r.get(sort) if not isinstance(r.get(sort), str) else r[sort].lower()), reverse=desc)
        if desc:  # keep missing values last when descending
            rows = [r for r in rows if r.get(sort) is not None] + [r for r in rows if r.get(sort) is None]
    return {"count": len(rows), "as_of": max((r["snapshot_as_of"] or "" for r in rows), default=None), "items": rows}


@app.get("/api/securities/{sym}")
def security(sym: str):
    c = company(sym)

    def load():
        cid = c["company_id"]
        prof = db.one(
            """select c.company_id, c.nse_symbol as symbol, c.bse_code, c.isin, c.name, c.sector, c.industry_group, c.industry,
                      c.sub_industry, c.website, c.about, c.key_points, c.pros, c.cons, c.has_consolidated, c.screener_slug
               from equity.company c where c.company_id = %s""", (cid,))
        uni = db.one(
            """select rank, market_cap_cr, as_of from equity.universe where universe_code = %s and company_id = %s
               order by as_of desc limit 1""", (UNIVERSE, cid))
        snap = db.one("select * from equity.v_latest_snapshot where company_id = %s", (cid,))
        growth = db.query(
            """select distinct on (basis, measure, horizon) basis, measure, horizon, value_pct, as_of
               from equity.growth_summary where company_id = %s order by basis, measure, horizon, as_of desc""", (cid,))
        yf = db.one(
            """select as_of, yf_ticker, market_cap, enterprise_value, beta, trailing_pe, forward_pe, forward_eps, peg_ratio, price_to_book,
                      ev_to_ebitda, ev_to_revenue, target_mean_price, target_median_price, target_high_price, target_low_price,
                      recommendation_key, recommendation_mean, num_analyst_opinions, all_time_high, all_time_low, first_trade_date,
                      full_time_employees, held_pct_insiders, held_pct_institutions,
                      info->>'longBusinessSummary' as yf_summary, info->>'currentPrice' as current_price,
                      info->>'fiftyTwoWeekHigh' as w52_high, info->>'fiftyTwoWeekLow' as w52_low
               from equity.yf_snapshot where company_id = %s order by as_of desc limit 1""", (cid,))
        return {"profile": prof, "universe": uni, "snapshot": snap, "growth": growth, "yf": yf}
    return db.cached(("security", c["company_id"]), load)


# --------------------------------------------------------------------------- financials
def _metric_sort_key(section: str, r: dict):
    if r["display_order"] is not None:
        return (0, r["display_order"], r["name"])
    pri = YF_PRIORITY.get(section, [])
    if r["name"] in pri:
        return (1, pri.index(r["name"]), r["name"])
    return (2, -r.get("_n", 0), r["name"])


@app.get("/api/securities/{sym}/financials")
def financials(sym: str, basis: str | None = None, source: str = "screener", section: str | None = None, period: str | None = None):
    c = company(sym)
    source = pick(source, SECTIONS, "source", "screener")
    section = pick(section, SECTIONS[source], "section", "profit-loss" if source == "screener" else "yf-income")
    basis = default_basis(c, basis)
    period = pick(period, PERIODS, "period", "Q" if section == "quarters" else "A")

    def load():
        rows = db.query(
            """select m.metric_id, m.name, m.parent_metric_id, m.unit, m.display_order, f.period_type, f.period_end::text as period_end, f.value
               from equity.financial_fact f join equity.metric m on m.metric_id = f.metric_id
               where f.company_id = %s and f.basis = %s and m.section = %s and m.source = %s""",
            (c["company_id"], basis, section, source))
        other = db.one("select count(distinct basis) n, string_agg(distinct basis, ',') b from equity.financial_fact f join equity.metric m using (metric_id) "
                       "where f.company_id = %s and m.section = %s and m.source = %s", (c["company_id"], section, source))
        available = sorted({r["period_type"] for r in rows} - {"TTM"})
        use = period if period in available else (available[0] if available else period)
        cols = sorted({r["period_end"] for r in rows if r["period_type"] == use})
        col_idx = {d: i for i, d in enumerate(cols)}
        ttm_dates = sorted({r["period_end"] for r in rows if r["period_type"] == "TTM"})
        ttm_end = ttm_dates[-1] if ttm_dates and use == "A" else None
        metrics: dict[int, dict] = {}
        for r in rows:
            m = metrics.setdefault(r["metric_id"], dict(metric_id=r["metric_id"], name=r["name"], unit=r["unit"], parent=r["parent_metric_id"],
                                                        display_order=r["display_order"], values=[None] * len(cols), ttm=None, _n=0))
            if r["period_type"] == use:
                m["values"][col_idx[r["period_end"]]] = r["value"]
                m["_n"] += 1
            elif r["period_type"] == "TTM" and ttm_end and r["period_end"] == ttm_end:
                m["ttm"] = r["value"]
        missing_parents = {m["parent"] for m in metrics.values() if m["parent"] and m["parent"] not in metrics}
        if missing_parents:
            for p in db.query("select metric_id, name, parent_metric_id, unit, display_order from equity.metric where metric_id = any(%s)",
                              (list(missing_parents),)):
                metrics[p["metric_id"]] = dict(metric_id=p["metric_id"], name=p["name"], unit=p["unit"], parent=p["parent_metric_id"],
                                               display_order=p["display_order"], values=[None] * len(cols), ttm=None, _n=0)
        kids: dict[int, list] = {}
        for m in metrics.values():
            if m["parent"]:
                kids.setdefault(m["parent"], []).append(m)
        top = [m for m in metrics.values() if not m["parent"] or m["parent"] not in metrics]
        top.sort(key=lambda m: _metric_sort_key(section, m))

        def render(m):
            ch = sorted(kids.get(m["metric_id"], []), key=lambda k: _metric_sort_key(section, k))
            return {"metric_id": m["metric_id"], "name": m["name"], "unit": m["unit"], "values": m["values"], "ttm": m["ttm"],
                    "children": [render(k) for k in ch]}
        return {
            "symbol": c["symbol"], "basis": basis, "source": source, "section": section, "period": use,
            "available_periods": available, "bases_available": (other or {}).get("b"),
            "sections": SECTIONS[source],
            "columns": [{"period_end": d, "label": label_period(d, use)} for d in cols],
            "ttm": {"period_end": ttm_end, "label": "TTM"} if ttm_end else None,
            "rows": [render(m) for m in top],
            "units_note": "Amounts in ₹ crore unless the unit says otherwise; percentages are percent numbers.",
        }
    return db.cached(("fin", c["company_id"], basis, source, section, period), load)


# --------------------------------------------------------------------------- prices / valuation
@app.get("/api/securities/{sym}/prices")
def prices(sym: str, range: str = "1y"):
    c = company(sym)
    rng = pick(range, RANGES, "range", "1y")

    def load():
        last = db.one("select max(date)::text as d, min(date)::text as first from equity.historical_prices where symbol = %s", (c["symbol"],))
        if not last or not last["d"]:
            return {"symbol": c["symbol"], "range": rng, "points": [], "count_raw": 0, "stats": None}
        since = date.fromisoformat(last["d"]) - timedelta(days=RANGES[rng]) if RANGES[rng] else date(1990, 1, 1)
        rows = db.query("select date::text as d, close, volume from equity.historical_prices where symbol = %s and date >= %s order by date",
                        (c["symbol"], since))
        pts = [[r["d"], r["close"], r["volume"]] for r in rows if r["close"] is not None]
        raw = len(pts)
        stats = None
        if pts:
            closes = [p[1] for p in pts]
            stats = {"first": pts[0][1], "last": pts[-1][1], "from": pts[0][0], "to": pts[-1][0], "high": max(closes), "low": min(closes),
                     "change_pct": round((pts[-1][1] / pts[0][1] - 1) * 100, 2) if pts[0][1] else None, "history_from": last["first"]}
        return {"symbol": c["symbol"], "range": rng, "points": downsample(pts), "count_raw": raw, "stats": stats}
    return db.cached(("prices", c["symbol"], rng), load)


VAL_SERIES = ["pe", "median_pe", "pbv", "median_pbv", "ev_multiple", "median_ev_multiple", "mcap_sales", "median_mcap_sales", "opm", "npm", "gpm"]


@app.get("/api/securities/{sym}/valuation")
def valuation(sym: str, basis: str | None = None, range: str = "max"):
    c = company(sym)
    basis = default_basis(c, basis)
    rng = pick(range, RANGES, "range", "max")

    def load():
        b = basis
        rows = db.query(f"select obs_date::text as d, {', '.join(VAL_SERIES)} from equity.valuation_history "
                        "where company_id = %s and basis = %s order by obs_date", (c["company_id"], b))
        if not rows:
            b = "S" if basis == "C" else "C"
            rows = db.query(f"select obs_date::text as d, {', '.join(VAL_SERIES)} from equity.valuation_history "
                            "where company_id = %s and basis = %s order by obs_date", (c["company_id"], b))
        if RANGES[rng] and rows:
            since = (date.fromisoformat(rows[-1]["d"]) - timedelta(days=RANGES[rng])).isoformat()
            rows = [r for r in rows if r["d"] >= since]
        summary = {}
        for s in ("pe", "pbv", "ev_multiple", "mcap_sales"):
            vals = sorted(r[s] for r in rows if r.get(s) is not None)
            if vals:
                cur = next((r[s] for r in reversed(rows) if r.get(s) is not None), None)
                summary[s] = {"current": cur, "median": vals[len(vals) // 2], "min": vals[0], "max": vals[-1],
                              "percentile": round(100 * bisect.bisect_right(vals, cur) / len(vals), 1) if cur is not None else None}
        rows = downsample(rows)
        return {"symbol": c["symbol"], "basis": b, "range": rng, "dates": [r["d"] for r in rows],
                "series": {s: [r.get(s) for r in rows] for s in VAL_SERIES if any(r.get(s) is not None for r in rows)},
                "summary": summary}
    return db.cached(("val", c["company_id"], basis, rng), load)


# --------------------------------------------------------------------------- shareholding / actions / estimates
CATS = ["promoters", "fii", "dii", "government", "public", "others"]


@app.get("/api/securities/{sym}/shareholding")
def shareholding(sym: str, frequency: str = "Q"):
    c = company(sym)
    freq = pick(frequency, {"Q", "Y"}, "frequency", "Q")

    def load():
        rows = db.query("select category, period_end::text as d, value from equity.shareholding where company_id = %s and frequency = %s order by period_end",
                        (c["company_id"], freq))
        periods = sorted({r["d"] for r in rows})
        idx = {d: i for i, d in enumerate(periods)}
        series: dict[str, list] = {}
        for r in rows:
            series.setdefault(r["category"], [None] * len(periods))[idx[r["d"]]] = r["value"]
        holders = db.query(
            """select h.name, sh.category, sh.period_end::text as d, sh.pct
               from equity.shareholding_holder sh join equity.holder h using (holder_id)
               where sh.company_id = %s and sh.frequency = %s
                 and sh.period_end in (select distinct period_end from equity.shareholding_holder where company_id = %s and frequency = %s
                                       order by period_end desc limit 2)
               order by sh.period_end desc, sh.pct desc""", (c["company_id"], freq, c["company_id"], freq))
        latest = max((h["d"] for h in holders), default=None)
        prev = {(h["name"], h["category"]): h["pct"] for h in holders if h["d"] != latest}
        top = []
        seen = set()
        for h in holders:
            k = (h["name"], h["category"])
            if h["d"] == latest and k not in seen:
                seen.add(k)
                top.append({**h, "prev_pct": prev.get(k)})
        return {"symbol": c["symbol"], "frequency": freq, "periods": periods, "categories": [k for k in CATS if k in series],
                "series": {k: v for k, v in series.items() if k != "num_shareholders"}, "num_shareholders": series.get("num_shareholders"),
                "holders": top[:40], "holders_as_of": latest}
    return db.cached(("sh", c["company_id"], freq), load)


@app.get("/api/securities/{sym}/actions")
def actions(sym: str):
    c = company(sym)

    def load():
        acts = db.query("select action_type, ex_date::text as ex_date, value, detail, source from equity.corporate_action "
                        "where company_id = %s order by ex_date desc", (c["company_id"],))
        shares = db.query("select as_of::text as d, shares from equity.shares_outstanding where company_id = %s order by as_of", (c["company_id"],))
        sh = downsample([[r["d"], r["shares"]] for r in shares], 300)
        divs = [a for a in acts if a["action_type"] == "dividend"]
        by_year: dict[str, float] = {}
        for a in divs:
            by_year[a["ex_date"][:4]] = round(by_year.get(a["ex_date"][:4], 0) + (a["value"] or 0), 4)
        return {"symbol": c["symbol"], "actions": acts, "dividends_by_year": [{"year": y, "per_share": v} for y, v in sorted(by_year.items())],
                "shares": sh}
    return db.cached(("actions", c["company_id"]), load)


REV_FIELDS = {"avg", "low", "high", "yearAgoRevenue"}


@app.get("/api/securities/{sym}/estimates")
def estimates(sym: str):
    c = company(sym)

    def load():
        rows = db.query(
            """select estimate_type, period, field, value, as_of::text as as_of from equity.analyst_estimate
               where company_id = %s and as_of = (select max(as_of) from equity.analyst_estimate where company_id = %s)""",
            (c["company_id"], c["company_id"]))
        tree: dict[str, dict[str, dict[str, float | None]]] = {}
        for r in rows:
            v = r["value"]
            if r["estimate_type"] == "revenue" and r["field"] in REV_FIELDS and v is not None:
                v = v / 1e7  # rupees -> crore
            tree.setdefault(r["estimate_type"], {}).setdefault(r["period"], {})[r["field"]] = v
        events = db.query("select event_date::text as event_date, eps_estimate, eps_reported, surprise_pct, source from equity.earnings_event "
                          "where company_id = %s order by event_date desc limit 20", (c["company_id"],))
        yf = db.one("select as_of::text as as_of, target_mean_price, target_median_price, target_high_price, target_low_price, "
                    "recommendation_key, recommendation_mean, num_analyst_opinions, forward_eps, forward_pe "
                    "from equity.yf_snapshot where company_id = %s order by as_of desc limit 1", (c["company_id"],))
        return {"symbol": c["symbol"], "as_of": rows[0]["as_of"] if rows else None, "estimates": tree, "earnings": events, "targets": yf,
                "units_note": "Revenue estimates in ₹ crore; EPS in ₹."}
    return db.cached(("est", c["company_id"]), load)


# --------------------------------------------------------------------------- documents
def doc_catalog() -> dict[str, dict]:
    def load():
        rows = db.query(
            """select d.company_id, c.nse_symbol as symbol, c.name, d.doc_type, d.url, d.period_label, d.fiscal_year, d.doc_date::text as doc_date,
                      d.source, d.status, d.page_count, d.char_count, d.pdf_bytes, d.text_path, d.fetch_source, d.fetched_url, d.error,
                      d.extractor, d.extracted_at::text as extracted_at
               from equity.document d join equity.company c using (company_id)""")
        out = {}
        for r in rows:
            r["key"] = doc_key(r["company_id"], r["doc_type"], r["url"])
            r["has_file"] = bool(r["text_path"]) and _safe_path(r["text_path"]) is not None and _safe_path(r["text_path"]).is_file()
            out[r["key"]] = r
        return out
    return db.cached("doc_catalog", load)


def _safe_path(text_path: str | None) -> Path | None:
    if not text_path:
        return None
    p = (ROOT / text_path).resolve()
    return p if p.is_relative_to(DOCS_ROOT) else None


def _indexed_keys() -> set[str]:
    def load():
        if not Path(LIB_DB).exists():
            return set()
        con = lib_search.connect_ro(LIB_DB)
        try:
            return {r[0] for r in con.execute("select key from doc")}
        except sqlite3.Error:
            return set()
        finally:
            con.close()
    return db.cached("indexed_keys", load)


@app.get("/api/securities/{sym}/documents")
def documents(sym: str):
    c = company(sym)
    indexed = _indexed_keys()
    items = [dict(d, in_library=d["key"] in indexed) for d in doc_catalog().values() if d["symbol"] == c["symbol"]]
    for d in items:
        d.pop("text_path", None)
    items.sort(key=lambda d: (d["doc_type"], -(d["fiscal_year"] or 0), d["doc_date"] or "", d["period_label"] or ""), reverse=False)
    counts: dict[str, int] = {}
    for d in items:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {"symbol": c["symbol"], "count": len(items), "status_counts": counts, "items": items}


@app.get("/api/securities/{sym}/quality")
def quality(sym: str):
    c = company(sym)

    def load():
        cid = c["company_id"]
        cov = db.one("select * from equity.v_coverage where company_id = %s", (cid,))
        comp = db.one("select ar_expected, ar_found, ar_missing, cc_expected, cc_found, cc_missing, ppt_expected, ppt_found, ppt_missing "
                      "from equity.v_document_completeness where company_id = %s", (cid,))
        tasks = db.query(
            """select t.run_id, r.command, t.task, t.status, t.http_status, t.attempts, t.rows_written, t.error,
                      t.started_at, t.finished_at
               from equity.scrape_task t join equity.scrape_run r using (run_id)
               where t.company_id = %s order by t.started_at desc limit 80""", (cid,))
        missing = db.query(
            """select doc_type, period_label, fiscal_year, doc_date::text as doc_date, url, status, error, fetch_source, attempts_log
               from equity.document where company_id = %s and status <> 'ok'
               order by doc_type, fiscal_year desc nulls last""", (cid,))
        task_summary = db.query("select status, count(*) n from equity.scrape_task where company_id = %s group by status", (cid,))
        return {"symbol": c["symbol"], "coverage": cov, "completeness": comp, "tasks": tasks, "task_summary": task_summary, "missing_docs": missing}
    return db.cached(("quality", c["company_id"]), load)


def _load_pages(path: Path) -> list[dict]:
    return _pages_cached(str(path), path.stat().st_mtime_ns)


_PAGES_CACHE: "OrderedDict[tuple, list]" = OrderedDict()


def _pages_cached(path: str, mtime: int) -> list[dict]:
    k = (path, mtime)
    if k in _PAGES_CACHE:
        _PAGES_CACHE.move_to_end(k)
        return _PAGES_CACHE[k]
    pages = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                pages.append({"page": int(d.get("page", len(pages) + 1)), "text": d.get("text") or ""})
    pages.sort(key=lambda p: p["page"])
    _PAGES_CACHE[k] = pages
    while len(_PAGES_CACHE) > 6:
        _PAGES_CACHE.popitem(last=False)
    return pages


def _doc_or_404(key: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{14}", key):
        bad("bad document key", 404)
    d = doc_catalog().get(key)
    if not d:
        bad("unknown document", 404)
    return d


def _meta(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "text_path"}


@app.get("/api/documents/{key}")
def document(key: str, page: int = Query(1, ge=1), limit: int = Query(3, ge=1, le=20)):
    d = _doc_or_404(key)
    meta = _meta(d)
    p = _safe_path(d["text_path"])
    if not d["has_file"] or p is None:
        return {"meta": meta, "total_pages": d["page_count"], "page_from": page, "pages": [], "available": False}
    pages = _load_pages(p)
    sel = [x for x in pages if page <= x["page"] < page + limit]
    return {"meta": meta, "total_pages": len(pages), "page_from": page, "pages": sel, "available": True}


@app.get("/api/documents/{key}/find")
def document_find(key: str, q: str = Query(..., min_length=1, max_length=200)):
    d = _doc_or_404(key)
    p = _safe_path(d["text_path"])
    if not d["has_file"] or p is None:
        return {"q": q, "total": 0, "pages": []}
    terms = [t for t in re.findall(r'"([^"]+)"|(\S+)', q) for t in t if t][:6]
    rx = re.compile("|".join(re.escape(t) for t in terms), re.I)
    hits, total = [], 0
    for pg in _load_pages(p):
        ms = list(rx.finditer(pg["text"]))
        if ms:
            total += len(ms)
            m = ms[0]
            s = max(0, m.start() - 70)
            hits.append({"page": pg["page"], "count": len(ms), "snippet": " ".join(pg["text"][s : m.end() + 90].split())})
    return {"q": q, "total": total, "pages": hits[:300]}


# --------------------------------------------------------------------------- library
def library_stats() -> dict:
    def load():
        p = Path(LIB_DB)
        if not p.exists():
            return {"built": False, "docs": 0, "chunks": 0, "size_mb": 0, "by_type": []}
        con = lib_search.connect_ro(p)
        try:
            docs = con.execute("select count(*) from doc").fetchone()[0]
            chunks = con.execute("select count(*) from chunk").fetchone()[0]
            by_type = [dict(doc_type=r[0], docs=r[1]) for r in con.execute("select doc_type, count(*) from doc group by 1")]
            fy = con.execute("select min(fiscal_year), max(fiscal_year) from doc").fetchone()
        finally:
            con.close()
        size = sum(f.stat().st_size for f in p.parent.glob(p.name + "*")) / 1e6
        return {"built": True, "docs": docs, "chunks": chunks, "size_mb": round(size, 1), "by_type": by_type, "fy_min": fy[0], "fy_max": fy[1],
                "modified": date.fromtimestamp(p.stat().st_mtime).isoformat()}
    return db.cached("library_stats", load)


@app.get("/api/library/stats")
def library_stats_route():
    return library_stats()


def _csv(v: str | None, pattern: re.Pattern | None = None, upper=False) -> list[str] | None:
    if not v:
        return None
    out = []
    for x in v.split(","):
        x = x.strip()
        if not x:
            continue
        if pattern and not pattern.match(x):
            bad(f"bad value {x!r}")
        out.append(x.upper() if upper else x)
    return out[:50] or None


@app.get("/api/library/search")
def library_search(q: str = Query("", max_length=300), symbols: str | None = None, types: str | None = None,
                   fy_from: int | None = Query(None, ge=1990, le=2100), fy_to: int | None = Query(None, ge=1990, le=2100),
                   limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0, le=5000), facets: bool = True):
    syms = _csv(symbols, SYMBOL_RE, upper=True)
    dts = _csv(types)
    if dts:
        for t in dts:
            pick(t, DOC_TYPES, "types")
    if not q.strip():
        return {"q": q, "hits": [], "total_strict": 0, "terms": [], "limit": limit, "offset": offset, "facets": None, "stats": library_stats()}

    def load():
        res = lib_search.search(q, symbols=syms, doc_types=dts, fy_from=fy_from, fy_to=fy_to, limit=limit, offset=offset, db_path=LIB_DB)
        res["q"] = q
        res["facets"] = lib_search.facets(q, db_path=LIB_DB) if facets and offset == 0 else None
        res["stats"] = library_stats()
        return res
    return db.cached(("lib", q, tuple(syms or ()), tuple(dts or ()), fy_from, fy_to, limit, offset, facets), load)


# --------------------------------------------------------------------------- ops
@app.get("/api/ops")
def ops():
    def load():
        runs = db.query("select run_id, command, status, params, stats, host, started_at, finished_at from equity.scrape_run order by run_id desc limit 20")
        jobs = db.query("select kind, status, count(*) n from equity.job group by 1, 2 order by 1, 2")
        job_fail = db.query("select kind, coalesce(error_class, 'unknown') as error_class, count(*) n from equity.job "
                            "where status in ('retry','dead') group by 1, 2 order by n desc")
        task_fail = db.query("select split_part(task, ':', 1) as task, left(coalesce(error, 'no error text'), 90) as error, count(*) n "
                             "from equity.scrape_task where status = 'failed' group by 1, 2 order by n desc limit 20")
        size = db.one("select pg_database_size(current_database()) as bytes")
        limit = db.one("select value from equity.control where key = 'storage_limit_mb'")
        control = db.query("select key, value, reason, updated_at from equity.control order by key")
        tables = db.query(
            """select c.relname as name, pg_total_relation_size(c.oid) as bytes, c.reltuples::bigint as est_rows
               from pg_class c join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'equity' and c.relkind = 'r' order by 2 desc limit 10""")
        docs = db.query("select doc_type, status, count(*) n from equity.document group by 1, 2 order by 1, 2")
        comp = db.one("select sum(ar_expected) ar_expected, sum(ar_found) ar_found, sum(cc_expected) cc_expected, sum(cc_found) cc_found, "
                      "sum(ppt_expected) ppt_expected, sum(ppt_found) ppt_found, count(*) companies from equity.v_document_completeness")
        gaps = db.query("select nse_symbol as symbol, ar_expected - ar_found as ar_gap, cc_expected - cc_found as cc_gap, ar_found, ar_expected, cc_found, cc_expected "
                        "from equity.v_document_completeness order by (ar_expected - ar_found) + (cc_expected - cc_found) desc limit 12")
        return {"runs": runs, "jobs": jobs, "job_failures": job_fail, "task_failures": task_fail,
                "db": {"bytes": size["bytes"], "limit_mb": limit["value"] if limit else 500, "cap_mb": 500, "tables": tables},
                "control": control, "documents": docs, "completeness": comp, "completeness_gaps": gaps}
    out = db.cached("ops", load)
    return {**out, "library": library_stats()}


def _warm_loop() -> None:
    """Keep the hot, slow-to-compute payloads fresh so /api/securities is always served from memory."""
    import time
    while True:
        try:
            companies()
            db.put("securities", _load_securities())
            doc_catalog()
        except Exception as e:  # DB asleep, network: try again next round
            db.log.warning("warm loop: %s", e)
        time.sleep(45)


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {str(exc)[:300]}"})


# --------------------------------------------------------------------------- redirects + static SPA
@app.get("/s/{sym}")
def redirect_company(sym: str):
    return RedirectResponse(f"/company/?s={sym}")


@app.get("/doc/{key}")
def redirect_doc(key: str):
    return RedirectResponse(f"/doc/?key={key}")


def mount_web() -> None:
    if not (WEB_OUT / "index.html").exists():
        @app.get("/")
        def _no_build():
            return JSONResponse({"detail": "web app not built; run `.venv/bin/python -m console --build`"}, status_code=503)
        return
    if (WEB_OUT / "_next").is_dir():
        app.mount("/_next", StaticFiles(directory=WEB_OUT / "_next"), name="next")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        target = (WEB_OUT / path).resolve()
        if target.is_relative_to(WEB_OUT.resolve()):
            if target.is_file():
                return FileResponse(target)
            if (target / "index.html").is_file():
                return FileResponse(target / "index.html")
            html = target.with_suffix(".html")
            if html.is_file():
                return FileResponse(html)
        return FileResponse(WEB_OUT / "index.html")


mount_web()
