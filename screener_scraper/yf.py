"""yfinance layer (ARCHITECTURE.md section 7): statements, daily prices, actions, shares, snapshot, estimates,
earnings, insiders, (the NSE corporate-actions fallback was removed). Parsers are pure (DataFrame -> tuples) and unit-tested;
network calls are thin wrappers with >=1s spacing and backoff on rate limits."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .db import DB
from .pipeline import TaskResult, pseudo_id, shard_ranked, shard_items

log = logging.getLogger(__name__)

YF_MIN_INTERVAL = 1.0
SECTIONS = {"income": "yf-income", "balance": "yf-balance", "cashflow": "yf-cashflow"}
STATEMENTS = [("income_stmt", "yf-income", "A"), ("quarterly_income_stmt", "yf-income", "Q"),
              ("balance_sheet", "yf-balance", "A"), ("quarterly_balance_sheet", "yf-balance", "Q"),
              ("cashflow", "yf-cashflow", "A"), ("quarterly_cashflow", "yf-cashflow", "Q")]


# ───────────────────────────── helpers ─────────────────────────────
def clean(v):
    """NaN / inf / pandas NA -> None; numpy scalars -> python."""
    if v is None:
        return None
    try:
        if hasattr(v, "item"):
            v = v.item()
    except Exception:
        pass
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if v.__class__.__name__ in ("NAType", "NaTType"):
        return None
    return v


def to_date(x) -> Optional[date]:
    x = clean(x)
    if x is None:
        return None
    if hasattr(x, "date"):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def ticker_for(nse_symbol: Optional[str], bse_code: Optional[str]) -> Optional[str]:
    if nse_symbol:
        return f"{nse_symbol}.NS"
    if bse_code:
        return f"{bse_code}.BO"
    return None


# ───────────────────────────── pure parsers ─────────────────────────────
def statement_unit(name: str) -> str:
    n = name.strip()
    if "EPS" in n:
        return "rs"
    if "Tax Rate" in n:
        return "x"
    if "Shares" in n or n in ("Share Issued", "Ordinary Shares Number"):
        return "count"
    return "cr"


def statement_rows(df) -> List[Tuple[str, str, date, float]]:
    """income_stmt-like frame (index = line item, columns = period end) -> [(name, unit, period_end, value)].
    Amounts are converted from rupees to crore (/1e7); EPS, tax rate and share counts stay raw. NaN skipped."""
    out: List[Tuple[str, str, date, float]] = []
    if df is None or getattr(df, "empty", True):
        return out
    for name in df.index:
        unit = statement_unit(str(name))
        for col in df.columns:
            v = clean(df.at[name, col])
            d = to_date(col)
            if v is None or d is None:
                continue
            v = float(v)
            if unit == "cr":
                v = round(v / 1e7, 4)
            out.append((str(name).strip(), unit, d, v))
    return out


def price_rows(df, symbol: str) -> List[tuple]:
    """history(period='max', auto_adjust=False) -> historical_prices rows; volume=0 / NaN-close rows skipped."""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    for idx, r in df.iterrows():
        d = to_date(idx)
        close, vol = clean(r.get("Close")), clean(r.get("Volume"))
        if d is None or close is None or not vol:
            continue
        adj = clean(r.get("Adj Close"))
        out.append((symbol, d, clean(r.get("Open")), clean(r.get("High")), clean(r.get("Low")), close,
                    adj if adj is not None else close, int(vol)))
    return out


def action_rows(df, company_id: int) -> List[tuple]:
    """actions frame (Dividends, Stock Splits) -> corporate_action rows (source yfinance)."""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    for idx, r in df.iterrows():
        d = to_date(idx)
        div, spl = clean(r.get("Dividends")), clean(r.get("Stock Splits"))
        if d is None:
            continue
        if div:
            out.append((company_id, "dividend", d, float(div), None, "yfinance"))
        if spl:
            out.append((company_id, "split", d, float(spl), None, "yfinance"))
    return out


def shares_rows(series, company_id: int) -> List[tuple]:
    out = {}
    if series is None or len(series) == 0:
        return []
    for idx, v in series.items():
        d, v = to_date(idx), clean(v)
        if d is not None and v:
            out[d] = (company_id, d, int(v), "yfinance")
    return list(out.values())


_SNAP_MAP = [("market_cap", "marketCap"), ("enterprise_value", "enterpriseValue"),
             ("shares_outstanding", "sharesOutstanding"), ("float_shares", "floatShares"),
             ("held_pct_insiders", "heldPercentInsiders"), ("held_pct_institutions", "heldPercentInstitutions"),
             ("beta", "beta"), ("trailing_pe", "trailingPE"), ("forward_pe", "forwardPE"),
             ("forward_eps", "forwardEps"), ("peg_ratio", "trailingPegRatio"), ("price_to_book", "priceToBook"),
             ("ev_to_ebitda", "enterpriseToEbitda"), ("ev_to_revenue", "enterpriseToRevenue"),
             ("target_mean_price", "targetMeanPrice"), ("target_median_price", "targetMedianPrice"),
             ("target_high_price", "targetHighPrice"), ("target_low_price", "targetLowPrice"),
             ("recommendation_key", "recommendationKey"), ("recommendation_mean", "recommendationMean"),
             ("num_analyst_opinions", "numberOfAnalystOpinions"), ("all_time_high", "allTimeHigh"),
             ("all_time_low", "allTimeLow"), ("full_time_employees", "fullTimeEmployees")]
_INT_COLS = {"market_cap", "enterprise_value", "shares_outstanding", "float_shares", "num_analyst_opinions",
             "full_time_employees"}
_NUM_LIMIT = {"held_pct_insiders": 1e3, "held_pct_institutions": 1e3, "beta": 1e3, "recommendation_mean": 1e3}


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    o = clean(o)
    return o if o is None or isinstance(o, (str, int, float, bool)) else str(o)


def snapshot_values(info: dict) -> Optional[dict]:
    """info dict -> typed columns + 'info' jsonb (companyOfficers / executiveTeam dropped)."""
    if not info:
        return None
    vals: Dict[str, object] = {}
    for col, key in _SNAP_MAP:
        v = clean(info.get(key))
        if key == "trailingPegRatio" and v is None:
            v = clean(info.get("pegRatio"))
        if v is not None and col not in ("recommendation_key",):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            if v is not None and abs(v) >= _NUM_LIMIT.get(col, 1e21 if col in _INT_COLS else 1e7):
                v = None
            if v is not None and col in _INT_COLS:
                v = int(v)
        vals[col] = v
    ms = clean(info.get("firstTradeDateMilliseconds"))
    vals["first_trade_date"] = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date() if ms else None
    vals["info"] = _json_safe({k: v for k, v in info.items() if k not in ("companyOfficers", "executiveTeam")})
    return vals


def estimate_rows(company_id: int, as_of: date, frames: Dict[str, object], price_target: Optional[dict] = None
                  ) -> List[tuple]:
    """frames: estimate_type -> DataFrame(index=period, columns=field). -> analyst_estimate rows."""
    out = {}
    for etype, df in frames.items():
        if df is None or getattr(df, "empty", True):
            continue
        for period in df.index:
            for field in df.columns:
                v = clean(df.at[period, field])
                if v is None:
                    continue
                try:
                    out[(etype, str(period), str(field))] = (company_id, as_of, etype, str(period), str(field), float(v))
                except (TypeError, ValueError):
                    pass
    for field, v in (price_target or {}).items():
        v = clean(v)
        if v is not None:
            out[("price_target", "current", str(field))] = (company_id, as_of, "price_target", "current", str(field), float(v))
    return list(out.values())


def earnings_rows(df, company_id: int) -> List[tuple]:
    out = {}
    if df is None or getattr(df, "empty", True):
        return []
    for idx, r in df.iterrows():
        try:
            d = idx.tz_convert("Asia/Kolkata").date() if getattr(idx, "tzinfo", None) else to_date(idx)
        except Exception:
            d = to_date(idx)
        if d is None:
            continue
        sp = clean(r.get("Surprise(%)"))
        out[d] = (company_id, d, clean(r.get("EPS Estimate")), clean(r.get("Reported EPS")),
                  None if sp is None else max(min(float(sp), 1e5), -1e5), "yfinance")
    return list(out.values())


def insider_rows(df, company_id: int) -> List[tuple]:
    out = {}
    if df is None or getattr(df, "empty", True):
        return []
    for _, r in df.iterrows():
        rec = {str(k): _json_safe(v) for k, v in r.to_dict().items()}
        h = hashlib.md5(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
        sh, val = clean(r.get("Shares")), clean(r.get("Value"))
        out[h] = (company_id, h, to_date(r.get("Start Date")), clean(r.get("Insider")), clean(r.get("Position")),
                  clean(r.get("Transaction")), clean(r.get("Text")), None if sh is None else int(sh),
                  None if val is None else float(val), clean(r.get("URL")))
    return list(out.values())


# ───────────────────────────── network wrappers ─────────────────────────────
_yf_lock = threading.Lock()
_yf_last = [0.0]


def _space() -> None:
    with _yf_lock:
        wait = YF_MIN_INTERVAL - (time.monotonic() - _yf_last[0])
        if wait > 0:
            time.sleep(wait)
        _yf_last[0] = time.monotonic()


def with_retry(fn: Callable, what: str, attempts: int = 4):
    """Call fn() spaced >=1s; retry with exponential backoff on rate-limit style errors."""
    for i in range(1, attempts + 1):
        _space()
        try:
            return fn()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            limited = "ratelimit" in type(e).__name__.lower() or "too many requests" in msg.lower() or "429" in msg
            if limited and i == attempts:
                from .http import HTTPFailure
                raise HTTPFailure(f"{what}: {msg}", 429) from e
            if not limited or i == attempts:
                raise
            delay = 5 * 2 ** (i - 1)
            log.warning("%s rate limited (attempt %d/%d), sleeping %ds", what, i, attempts, delay)
            time.sleep(delay)


def _dataset(tasks: List[TaskResult], name: str, fn: Callable[[TaskResult], int]) -> None:
    t = TaskResult(name, fatal=True)
    tasks.append(t)
    try:
        t.rows = fn(t)
        t.http_status = 200 if t.status == "ok" else t.http_status
    except Exception as e:
        log.warning("%s failed: %s: %s", name, type(e).__name__, e)
        t.status = "failed"
        t.exc = e
        t.errors.append(f"{type(e).__name__}: {e}")
    t.finish()


def process_company_yf(db: DB, run_id: int, company_id: int, nse_symbol: Optional[str], bse_code: Optional[str],
                       as_of: Optional[date] = None, nse_fallback: bool = False) -> List[TaskResult]:
    """Run every yfinance dataset for one company (each in its own transaction + scrape_task). Never raises;
    returns the TaskResults (`.exc`, `.fatal`)."""
    import yfinance
    as_of = as_of or date.today()
    tk = ticker_for(nse_symbol, bse_code)
    price_symbol = nse_symbol or bse_code
    if not tk:
        db.record_task(run_id, company_id, "yf:ticker", "failed", error="no nse_symbol or bse_code")
        return [TaskResult("yf:ticker", "failed", error="no nse_symbol or bse_code",
                           exc=LookupError("no nse_symbol or bse_code"))]
    tasks: List[TaskResult] = []
    t = yfinance.Ticker(tk)

    def tx(write):
        return db.run_tx(write)

    def prices(task):
        df = with_retry(lambda: t.history(period="max", auto_adjust=False), f"{tk} history")
        rows = price_rows(df, price_symbol)
        if rows:
            tx(lambda cur: db.upsert_historical_prices(cur, rows))
        else:
            task.errors.append("no price rows")
        return len(rows)

    def statements(task):
        n = 0
        metrics = 0
        for attr, section, ptype in STATEMENTS:
            df = with_retry(lambda: getattr(t, attr), f"{tk} {attr}")
            rows = statement_rows(df)
            if not rows:
                continue

            def write(cur, rows=rows, section=section, ptype=ptype):
                facts = []
                for name, unit, pend, v in rows:
                    mid = db.metric_id(cur, section, name, None, unit, None, source="yfinance")
                    facts.append((company_id, "C", mid, ptype, pend, v, run_id))
                return db.upsert_facts(cur, facts)
            n += tx(write)
        return n

    def actions(task):
        rows = action_rows(with_retry(lambda: t.actions, f"{tk} actions"), company_id)
        return tx(lambda cur: db.upsert_corporate_actions(cur, rows)) if rows else 0

    def shares(task):
        rows = shares_rows(with_retry(lambda: t.get_shares_full(start="1990-01-01"), f"{tk} shares"), company_id)
        return tx(lambda cur: db.upsert_shares(cur, rows)) if rows else 0

    def snapshot(task):
        vals = snapshot_values(with_retry(lambda: t.info, f"{tk} info"))
        if not vals:
            task.errors.append("empty info")
            return 0
        return tx(lambda cur: db.upsert_yf_snapshot(cur, company_id, as_of, tk, vals, run_id))

    def estimates(task):
        frames = {}
        for etype, attr in (("eps", "earnings_estimate"), ("revenue", "revenue_estimate"), ("eps_trend", "eps_trend"),
                            ("eps_revisions", "eps_revisions"), ("growth", "growth_estimates"),
                            ("recommendation", "recommendations")):
            try:
                df = with_retry(lambda: getattr(t, attr), f"{tk} {attr}")
                if etype == "recommendation" and df is not None and "period" in getattr(df, "columns", []):
                    df = df.set_index("period")
                frames[etype] = df
            except Exception as e:
                task.errors.append(f"{attr}: {type(e).__name__}: {e}")
        try:
            pt = with_retry(lambda: t.analyst_price_targets, f"{tk} price targets")
        except Exception as e:
            pt = None
            task.errors.append(f"price_targets: {type(e).__name__}: {e}")
        rows = estimate_rows(company_id, as_of, frames, pt)
        if task.errors and not rows:
            task.status = "failed"
        return tx(lambda cur: db.upsert_estimates(cur, rows)) if rows else 0

    def earnings(task):
        rows = earnings_rows(with_retry(lambda: t.get_earnings_dates(limit=100), f"{tk} earnings"), company_id)
        return tx(lambda cur: db.upsert_earnings(cur, rows)) if rows else 0

    def insiders(task):
        rows = insider_rows(with_retry(lambda: t.insider_transactions, f"{tk} insiders"), company_id)
        return tx(lambda cur: db.upsert_insiders(cur, rows)) if rows else 0

    for name, fn in (("yf:prices", prices), ("yf:statements", statements), ("yf:actions", actions),
                     ("yf:shares", shares), ("yf:snapshot", snapshot), ("yf:estimates", estimates),
                     ("yf:earnings", earnings), ("yf:insiders", insiders)):
        _dataset(tasks, name, fn)

    # The NSE corporate-actions fallback (undocumented nseindia.com/api with cookie warming) was removed:
    # yfinance is the only source of corporate actions here.
    log.info("nse_fallback_removed")

    for task in tasks:
        db.record_task(run_id, company_id, task.task, task.status, http_status=task.http_status,
                       rows_written=task.rows, error=task.error, started_at=task.started_at)
    return tasks


def run_company(company_id: int, run_id: int, *, db: Optional[DB] = None) -> dict:
    """Per-company entry point for the job worker. Runs all yfinance datasets (+ NSE actions fallback), recording a
    scrape_task per dataset under `run_id`. RAISES after recording if any fatal dataset failed: requests.HTTPError
    subclass with `.status_code` (429 for yfinance rate limits, NSE status codes), or the original exception.
    The best-effort NSE fallback never raises."""
    own = db is None
    db = db or DB()
    try:
        with db.trail.cursor() as cur:
            cur.execute("select nse_symbol, bse_code from equity.company where company_id=%s", (company_id,))
            r = cur.fetchone()
        if r is None:
            raise LookupError(f"company_id {company_id} not in equity.company")
        t0 = time.monotonic()
        tasks = process_company_yf(db, run_id, company_id, r[0], r[1])
        stats = {"company_id": company_id, "symbol": r[0] or r[1], "seconds": round(time.monotonic() - t0, 1),
                 "rows": {t.task: t.rows for t in tasks}, "tasks": {t.task: t.status for t in tasks}}
        for t in tasks:
            if t.status == "failed" and t.fatal:
                raise t.exc or RuntimeError(f"{t.task} failed: {t.error}")
        return stats
    finally:
        if own:
            db.close()


def run_yf(db: DB, *, symbols: Optional[List[str]] = None, universe: str = config.UNIVERSE_CODE,
           shard: Optional[Tuple[int, int]] = None) -> str:
    if symbols:
        wanted = shard_items(symbols, shard)
        companies = []
        for s in wanted:
            with db.trail.cursor() as cur:
                cur.execute("select company_id, nse_symbol, bse_code from equity.company "
                            "where nse_symbol=%s or screener_slug=%s", (s, s))
                r = cur.fetchone()
            if r is None:
                log.warning("%s is not in equity.company yet (run the screener scrape first): skipped", s)
                continue
            companies.append(r)
    else:
        rows = shard_ranked(db.universe_companies(universe), shard)
        companies = [(r[1], r[3], r[4]) for r in rows]
    run_id = db.start_run("yf", {"symbols": symbols, "universe": universe,
                                 "shard": f"{shard[0]}/{shard[1]}" if shard else None})
    stats = {"companies_ok": 0, "companies_failed": 0, "failed": [], "seconds_per_company": []}
    t0 = time.monotonic()
    for i, (cid, nse, bse) in enumerate(companies, 1):
        t1 = time.monotonic()
        try:
            ok = all(x.status != "failed" for x in process_company_yf(db, run_id, cid, nse, bse))
        except Exception:
            log.exception("yf %s crashed", nse or bse)
            ok = False
        dt = time.monotonic() - t1
        stats["companies_ok" if ok else "companies_failed"] += 1
        if not ok:
            stats["failed"].append(nse or bse)
        stats["seconds_per_company"].append(round(dt, 1))
        log.info("[%d/%d] yf %s %s in %.0fs", i, len(companies), nse or bse, "ok" if ok else "PARTIAL", dt)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    status = "partial" if stats["companies_failed"] else "success"
    db.finish_run(run_id, status, stats)
    return status
