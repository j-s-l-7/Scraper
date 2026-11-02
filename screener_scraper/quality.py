"""Autonomous data-quality engine: validation checks -> equity.dq_issue, bounded auto-remediation, scorecard.

`python -m screener_scraper dq [--db supabase|lake|both] [--force]` runs once per day per database (marker in
equity.control 'dq_last_run'). No human and no LLM: every action comes from the tables below. The pure check
functions take plain rows, so they are unit-tested with synthetic data (tests/test_quality.py).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from psycopg2.extras import Json, RealDictCursor

from . import config

log = logging.getLogger(__name__)

TOL, HARD = 0.02, 0.05                    # cross-source tolerance / error threshold
MAX_ISSUES_PER_CHECK = 300                # storage guard: the rest is summarised in one global issue
RET_LIMIT = 0.40                          # daily |return| flagged above this without a split/bonus
PRICE_PRIORITY_DAYS = 400


@dataclass
class Issue:
    check: str
    severity: str                         # info | warn | error
    company_id: int
    key: str = ""
    detail: dict = field(default_factory=dict)


# ═══════════════════════════ pure checks (unit-tested) ═══════════════════════════
def rel_diff(a: float, b: float) -> Optional[float]:
    """|a-b| / max(|a|,|b|); None when both are ~0."""
    m = max(abs(a), abs(b))
    return None if m < 1e-9 else abs(a - b) / m


def check_reconcile(code: str, pairs: Dict[Tuple[int, date], Tuple[float, float]], names=("screener", "yfinance"),
                    min_abs: float = 0.0) -> List[Issue]:
    """pairs[(company_id, period_end)] = (a, b). One issue per company listing its worst periods."""
    per: Dict[int, list] = defaultdict(list)
    for (cid, pe), (a, b) in pairs.items():
        if max(abs(a), abs(b)) < min_abs:
            continue
        d = rel_diff(a, b)
        if d is not None and d > TOL:
            per[cid].append({"period_end": pe.isoformat(), names[0]: a, names[1]: b, "diff_pct": round(100 * d, 2)})
    out = []
    for cid, items in per.items():
        items.sort(key=lambda x: -x["diff_pct"])
        worst = items[0]["diff_pct"]
        out.append(Issue(code, "error" if worst > 100 * HARD else "warn", cid,
                         detail={"worst_pct": worst, "periods": items[:5], "n_periods": len(items)}))
    return out


def check_balance_identity(rows: Dict[Tuple[int, str, date], Tuple[float, float]]) -> List[Issue]:
    """(company, basis, period_end) -> (total_assets, total_liabilities); equal within 0.5% or 1 crore."""
    per: Dict[int, list] = defaultdict(list)
    for (cid, basis, pe), (ta, tl) in rows.items():
        if abs(ta - tl) > max(1.0, 0.005 * max(abs(ta), abs(tl))):
            per[cid].append({"basis": basis, "period_end": pe.isoformat(), "assets": ta, "liabilities": tl})
    return [Issue("identity_balance", "error", cid, detail={"periods": sorted(v, key=lambda x: x["period_end"])[-5:],
                                                           "n_periods": len(v)}) for cid, v in per.items()]


def check_cashflow_identity(rows: Dict[Tuple[int, str, date], Tuple[float, float, float, float]]) -> List[Issue]:
    """(company, basis, period_end) -> (net, cfo, cfi, cff); net = cfo+cfi+cff within rounding (screener rounds
    each line, so allow 2 crore or 0.5% of the largest component)."""
    per: Dict[int, list] = defaultdict(list)
    for (cid, basis, pe), (net, o, i, f) in rows.items():
        tol = max(2.0, 0.005 * max(abs(o), abs(i), abs(f), abs(net)))
        if abs(net - (o + i + f)) > tol:
            per[cid].append({"basis": basis, "period_end": pe.isoformat(), "net": net, "sum": round(o + i + f, 2)})
    return [Issue("identity_cashflow", "warn", cid, detail={"periods": v[-5:], "n_periods": len(v)})
            for cid, v in per.items()]


def check_opm(rows: Dict[Tuple[int, str, str, date], Tuple[float, float, float]]) -> List[Issue]:
    """(company, basis, period_type, period_end) -> (opm_pct, operating_profit, sales); within 1.0 percentage point."""
    per: Dict[int, list] = defaultdict(list)
    for (cid, basis, pt, pe), (opm, op, sales) in rows.items():
        if abs(sales) < 1e-6:
            continue
        calc = 100.0 * op / sales
        if abs(calc - opm) > 1.0:
            per[cid].append({"basis": basis, "period_type": pt, "period_end": pe.isoformat(),
                             "opm_pct": opm, "calc_pct": round(calc, 2)})
    return [Issue("identity_opm", "warn", cid, detail={"periods": v[-5:], "n_periods": len(v)}) for cid, v in per.items()]


def check_ohlc(rows: Iterable[Tuple[str, date, float, float, float, float]]) -> List[Issue]:
    """rows (symbol, date, open, high, low, close); low <= open/close <= high and low <= high. One issue per symbol."""
    per: Dict[str, list] = defaultdict(list)
    for sym, d, o, h, l, c in rows:
        vals = [v for v in (o, h, l, c) if v is not None]
        if len(vals) < 4:
            continue
        o, h, l, c = map(float, vals)          # numeric columns arrive as Decimal; float * Decimal raises
        eps = 1e-6 + 1e-6 * h
        if l > h + eps or o > h + eps or c > h + eps or o < l - eps or c < l - eps:
            per[sym].append(d.isoformat())
    return [Issue("price_ohlc", "error", 0, key=sym, detail={"symbol": sym, "n_bad": len(v), "dates": v[-5:]})
            for sym, v in per.items()]


def check_returns(cands: Iterable[Tuple[str, date, float]], action_dates: Dict[str, List[date]],
                  window_days: int = 5) -> List[Issue]:
    """cands (symbol, date, return) already filtered |ret| > limit; drop those within +-window_days of a split/bonus."""
    per: Dict[str, list] = defaultdict(list)
    for sym, d, r in cands:
        if abs(r) <= RET_LIMIT:
            continue
        if any(abs((d - a).days) <= window_days for a in action_dates.get(sym, [])):
            continue
        per[sym].append({"date": d.isoformat(), "ret_pct": round(100 * r, 1)})
    return [Issue("price_return", "warn", 0, key=sym, detail={"symbol": sym, "n": len(v), "moves": v[-5:]})
            for sym, v in per.items()]


def trading_calendar(counts_by_date: Dict[date, int], n_symbols: int, share: float = 0.6) -> List[date]:
    """Dates that most symbols have a row for."""
    return sorted(d for d, c in counts_by_date.items() if c >= share * max(n_symbols, 1))


def check_missing_days(sym_dates: Dict[str, Sequence[date]], calendar: Sequence[date]) -> List[Issue]:
    """Per symbol: calendar days after its first date (in the window) that it lacks. Needs >=50% coverage of the
    window, otherwise the symbol is a partial history (recent listing / delisting), not a gap."""
    out = []
    cal = list(calendar)
    for sym, ds in sym_dates.items():
        have = set(ds)
        if not have or not cal:
            continue
        first, last = min(have), max(have)
        window = [d for d in cal if first <= d <= last]
        if len(window) < 5:
            continue
        miss = [d for d in window if d not in have]
        if miss:
            out.append(Issue("price_gap", "warn", 0, key=sym,
                             detail={"symbol": sym, "n_missing": len(miss), "dates": [d.isoformat() for d in miss[-10:]]}))
    return out


def check_stale(last_dates: Dict[str, date], cal_last: Optional[date], grace_days: int = 3) -> List[Issue]:
    if cal_last is None:
        return []
    return [Issue("price_stale", "warn", 0, key=sym, detail={"symbol": sym, "last_date": d.isoformat(),
                                                             "market_last": cal_last.isoformat(),
                                                             "days_behind": (cal_last - d).days})
            for sym, d in last_dates.items() if (cal_last - d).days > grace_days]


def check_close_mismatch(db_close: Dict[Tuple[str, date], float], other: Dict[Tuple[str, date], float],
                         tol: float = 0.005) -> List[Issue]:
    per: Dict[str, list] = defaultdict(list)
    for k, v in db_close.items():
        o = other.get(k)
        if o is None or v == 0:
            continue
        d = abs(v - o) / abs(o)
        if d > tol:
            per[k[0]].append({"date": k[1].isoformat(), "db": v, "yfinance": o, "diff_pct": round(100 * d, 2)})
    return [Issue("price_mismatch", "warn", 0, key=s, detail={"symbol": s, "n": len(v), "days": v[-5:]})
            for s, v in per.items()]


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, mo = d.year + m // 12, m % 12 + 1
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return nxt - timedelta(days=1)                       # month end


def find_gaps(dates: Sequence[date], step_months: int) -> List[date]:
    """Missing expected period ends between the first and last observed (month-end dates, fixed step)."""
    if len(dates) < 2:
        return []
    have = {(d.year, d.month) for d in dates}
    cur, last = min(dates), max(dates)
    out = []
    cur = add_months(cur, step_months)
    while cur < last:
        if (cur.year, cur.month) not in have:
            out.append(cur)
        cur = add_months(cur, step_months)
    return out


def universe_latest(per_company_latest: Dict[int, date], share: float = 0.5) -> Optional[date]:
    """Latest period that at least `share` of companies have reported (so one early filer does not define it)."""
    if not per_company_latest:
        return None
    vals = sorted(per_company_latest.values(), reverse=True)
    return vals[max(0, int(len(vals) * share) - 1)]


def check_completeness(q_dates: Dict[int, List[date]], a_dates: Dict[int, List[date]], today: date) -> List[Issue]:
    out: List[Issue] = []
    lq = universe_latest({c: max(v) for c, v in q_dates.items() if v})
    for cid, ds in q_dates.items():
        gaps = find_gaps(sorted(set(ds)), 3)
        if gaps:
            out.append(Issue("quarter_gap", "warn", cid, detail={"missing": [g.isoformat() for g in gaps[-8:]]}))
        if lq and max(ds) < lq:
            out.append(Issue("latest_quarter_missing", "warn", cid,
                             detail={"company_latest": max(ds).isoformat(), "universe_latest": lq.isoformat()}))
    for cid, ds in a_dates.items():
        gaps = find_gaps(sorted(set(ds)), 12)
        if gaps:
            out.append(Issue("year_gap", "warn", cid, detail={"missing": [g.isoformat() for g in gaps[-8:]]}))
    return out


def check_no_financials(company_ids: Iterable[int], with_facts: set) -> List[Issue]:
    return [Issue("no_financials", "error", c, detail={}) for c in company_ids if c not in with_facts]


def check_shareholding(sh_dates: Dict[int, List[date]]) -> List[Issue]:
    out = []
    lq = universe_latest({c: max(v) for c, v in sh_dates.items() if v})
    for cid, ds in sh_dates.items():
        gaps = find_gaps(sorted(set(ds)), 3)
        late = bool(lq and max(ds) < lq)
        if gaps or late:
            out.append(Issue("shareholding_gap", "info", cid, detail={"missing": [g.isoformat() for g in gaps[-6:]],
                                                                     "behind_universe": late}))
    return out


def check_docs(rows: Iterable[dict]) -> List[Issue]:
    """rows from v_document_completeness: flag companies with missing annual reports or call quarters."""
    out = []
    for r in rows:
        am, cm = len(r.get("ar_missing") or []), len(r.get("cc_missing") or [])
        if am or cm:
            tot = (r.get("ar_expected") or 0) + (r.get("cc_expected") or 0)
            miss = am + cm
            sev = "warn" if tot and miss / tot > 0.25 else "info"
            out.append(Issue("docs_gap", sev, r["company_id"],
                             detail={"ar_missing": [int(x) for x in (r.get("ar_missing") or [])][-6:],
                                     "cc_missing": [str(x) for x in (r.get("cc_missing") or [])][-6:],
                                     "ar_found": r.get("ar_found"), "ar_expected": r.get("ar_expected"),
                                     "cc_found": r.get("cc_found"), "cc_expected": r.get("cc_expected")}))
    return out


def check_outliers(series: Dict[Tuple[int, str], List[Tuple[date, float]]], factor: float = 10.0,
                   min_base: float = 5.0) -> List[Issue]:
    """series[(company, line_item)] = [(quarter_end, value)] -> quarter-on-quarter jumps above `factor`x."""
    per: Dict[int, list] = defaultdict(list)
    for (cid, item), pts in series.items():
        pts = sorted(pts)
        for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
            if abs(v0) >= min_base and v0 * v1 > 0 and (abs(v1) / abs(v0) > factor or abs(v0) / abs(v1) > factor):
                per[cid].append({"item": item, "from": d0.isoformat(), "to": d1.isoformat(), "prev": v0, "value": v1})
    return [Issue("outlier_qoq", "warn", cid, detail={"jumps": v[-5:], "n": len(v)}) for cid, v in per.items()]


# ═══════════════════════════ scoring (pure) ═══════════════════════════
def score_company(*, q_count: int, a_count: int, price_days_behind: Optional[int], q_behind: bool,
                  open_errors: int, open_warns: int, docs_ratio: Optional[float]) -> Dict[str, float]:
    """0-100 sub-scores and the weighted overall (coverage 30, freshness 25, consistency 25, documents 20)."""
    coverage = 50.0 * min(q_count, 13) / 13 + 50.0 * min(a_count, 12) / 12
    f_price = 50.0 if price_days_behind is not None and price_days_behind <= 3 else \
        (25.0 if price_days_behind is not None and price_days_behind <= 10 else 0.0)
    freshness = f_price + (0.0 if q_behind else 50.0)
    consistency = max(0.0, 100.0 - 25.0 * open_errors - 8.0 * open_warns)
    documents = 100.0 * (docs_ratio or 0.0)
    overall = 0.30 * coverage + 0.25 * freshness + 0.25 * consistency + 0.20 * documents
    return {"coverage": round(coverage, 1), "freshness": round(freshness, 1), "consistency": round(consistency, 1),
            "documents": round(documents, 1), "overall": round(overall, 1)}


# ═══════════════════════════ remediation planning (pure) ═══════════════════════════
BACKOFF_DAYS = {"docs_retry": [0, 3, 7, 14], "rescrape": [0, 2, 5], "recon_rescrape": [0], "bhavcopy": [0, 2, 5], "outlier_rescrape": [0]}
ACTION_OF = {                       # check -> (action, kinds)
    "price_stale": ("rescrape", ("yf",)), "latest_quarter_missing": ("rescrape", ("screener",)),
    "quarter_gap": ("rescrape", ("screener",)), "year_gap": ("rescrape", ("screener",)),
    "shareholding_gap": ("rescrape", ("screener",)),
    "recon_sales": ("recon_rescrape", ("screener", "yf")), "recon_profit": ("recon_rescrape", ("screener", "yf")),
    "recon_eps": ("recon_rescrape", ("screener", "yf")), "price_gap": ("bhavcopy", ()), "price_mismatch": ("bhavcopy", ()),
    "docs_gap": ("docs_retry", ()), "outlier_qoq": ("outlier_rescrape", ("screener",)),
    "identity_balance": ("rescrape", ("screener",)), "identity_cashflow": ("rescrape", ("screener",)),
}


def plan_action(check: str, history: Sequence[dict], now: datetime) -> Optional[Tuple[str, tuple]]:
    """Next remediation for an open issue, or None (not remediable, backing off, or attempts exhausted)."""
    if check not in ACTION_OF:
        return None
    action, kinds = ACTION_OF[check]
    tries = [h for h in history if h.get("action") == action]
    sched = BACKOFF_DAYS[action]
    if len(tries) >= len(sched):
        return None
    if tries:
        last = datetime.fromisoformat(tries[-1]["at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last < timedelta(days=sched[len(tries)]):
            return None
    return action, kinds


# ═══════════════════════════ database layer ═══════════════════════════
def rows(conn, sql: str, params=()) -> List[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_ctl(conn, key, default=None):
    r = rows(conn, "select value from equity.control where key=%s", (key,))
    return r[0][0] if r else default


def set_ctl(conn, key, value, reason=""):
    with conn.cursor() as cur:
        cur.execute("""insert into equity.control(key,value,reason) values (%s,%s,%s)
                       on conflict (key) do update set value=excluded.value, reason=excluded.reason, updated_at=now()""",
                    (key, Json(value), reason))


def sync_active(conn) -> int:
    """Companies that dropped out of every universe are flagged inactive, never deleted (no survivorship bias)."""
    with conn.cursor() as cur:
        cur.execute("""with latest as (select universe_code, max(as_of) a from equity.universe group by 1),
                            members as (select distinct u.company_id from equity.universe u
                                        join latest l on l.universe_code = u.universe_code and l.a = u.as_of)
                       update equity.company c set is_active = exists (select 1 from members m where m.company_id = c.company_id)
                       where c.is_active is distinct from exists (select 1 from members m where m.company_id = c.company_id)
                         and (select count(*) from members) > 0""")
        return cur.rowcount


def gather_issues(conn, today: date, with_net: bool = True) -> List[Issue]:
    issues: List[Issue] = []
    active = [r[0] for r in rows(conn, "select company_id from equity.company where is_active")]
    sym_of = dict(rows(conn, "select coalesce(nse_symbol, screener_slug), company_id from equity.company"))
    cid_of_sym = sym_of

    # ---- cross-source reconciliation (annual, basis C preferred) ----
    def annual(section, names):
        ids = {r[0]: r[1] for r in rows(conn, "select metric_id, name from equity.metric where section=%s and name = any(%s) "
                                              "and parent_metric_id is null", (section, list(names)))}
        out: Dict[Tuple[int, date], float] = {}
        for cid, basis, pe, v in rows(conn, "select company_id, basis, period_end, value from equity.financial_fact "
                                            "where metric_id = any(%s) and period_type='A' order by basis desc",
                                      (list(ids),)):
            out[(cid, pe)] = float(v)          # 'S' sorts after 'C' descending: C overwrites S
        return out
    for code, (sec, names), (ysec, yname), min_abs in (
            ("recon_sales", ("profit-loss", ("Sales", "Revenue")), ("yf-income", "Total Revenue"), 1.0),
            ("recon_profit", ("profit-loss", ("Net Profit",)), ("yf-income", "Net Income"), 1.0),
            ("recon_eps", ("profit-loss", ("EPS in Rs",)), ("yf-income", "Diluted EPS"), 0.5)):
        a, b = annual(sec, names), annual(ysec, (yname,))
        pairs = {}
        for (cid, pe), av in a.items():
            for k in (pe, pe + timedelta(days=1), pe - timedelta(days=1)):
                if (cid, k) in b:
                    pairs[(cid, pe)] = (av, b[(cid, k)])
                    break
        issues += check_reconcile(code, pairs, min_abs=min_abs)

    # ---- statement identities ----
    inv = {r[0]: r[1] for r in rows(conn, "select metric_id, name from equity.metric where section='balance-sheet' "
                                          "and name in ('Total Assets','Total Liabilities') and parent_metric_id is null")}
    bs = list(inv)
    tmp: Dict[Tuple[int, str, date], dict] = defaultdict(dict)
    for cid, basis, mid, pe, v in rows(conn, "select company_id, basis, metric_id, period_end, value from equity.financial_fact "
                                             "where metric_id = any(%s) and period_type in ('A','H')", (bs,)):
        tmp[(cid, basis, pe)][inv[mid]] = float(v)
    issues += check_balance_identity({k: (d["Total Assets"], d["Total Liabilities"]) for k, d in tmp.items()
                                      if len(d) == 2})
    cf = {r[0]: r[1] for r in rows(conn, "select metric_id, name from equity.metric where section='cash-flow' and parent_metric_id is null "
                                         "and name in ('Net Cash Flow','Cash from Operating Activity','Cash from Investing Activity','Cash from Financing Activity')")}
    tmp = defaultdict(dict)
    for cid, basis, mid, pe, v in rows(conn, "select company_id, basis, metric_id, period_end, value from equity.financial_fact "
                                             "where metric_id = any(%s) and period_type='A'", (list(cf),)):
        tmp[(cid, basis, pe)][cf[mid]] = float(v)
    issues += check_cashflow_identity({k: (d["Net Cash Flow"], d["Cash from Operating Activity"],
                                           d["Cash from Investing Activity"], d["Cash from Financing Activity"])
                                       for k, d in tmp.items() if len(d) == 4})
    pl = {r[0]: (r[1], r[2]) for r in rows(conn, "select metric_id, section, name from equity.metric where parent_metric_id is null "
                                                 "and section in ('profit-loss','quarters') and name in ('OPM %%','Operating Profit','Sales')")}
    tmp = defaultdict(dict)
    for cid, basis, mid, pt, pe, v in rows(conn, "select company_id, basis, metric_id, period_type, period_end, value from equity.financial_fact "
                                                 "where metric_id = any(%s) and period_type in ('A','Q')", (list(pl),)):
        tmp[(cid, basis, pt, pe)][pl[mid][1]] = float(v)
    issues += check_opm({k: (d["OPM %"], d["Operating Profit"], d["Sales"]) for k, d in tmp.items() if len(d) == 3})

    # ---- completeness / new quarters / outliers ----
    kp = [r[0] for r in rows(conn, "select metric_id from equity.metric where parent_metric_id is null and name = 'Net Profit' "
                                   "and section in ('quarters','profit-loss')")]
    qd: Dict[int, set] = defaultdict(set)
    ad: Dict[int, set] = defaultdict(set)
    for cid, pt, pe in rows(conn, "select distinct company_id, period_type, period_end from equity.financial_fact "
                                  "where metric_id = any(%s) and period_type in ('Q','A')", (kp,)):
        (qd if pt == "Q" else ad)[cid].add(pe)
    issues += check_completeness({c: sorted(v) for c, v in qd.items()}, {c: sorted(v) for c, v in ad.items()}, today)
    issues += check_no_financials(active, set(qd) | set(ad))
    sh: Dict[int, set] = defaultdict(set)
    for cid, pe in rows(conn, "select distinct company_id, period_end from equity.shareholding where frequency='Q' and category='promoters'"):
        sh[cid].add(pe)
    issues += check_shareholding({c: sorted(v) for c, v in sh.items()})
    q_ids = [r[0] for r in rows(conn, "select metric_id from equity.metric where section='quarters' and parent_metric_id is null "
                                      "and name in ('Sales','Revenue','Net Profit')")]
    series: Dict[Tuple[int, str], list] = defaultdict(list)
    for cid, basis, mid, pe, v in rows(conn, "select company_id, basis, metric_id, period_end, value from equity.financial_fact "
                                             "where metric_id = any(%s) and period_type='Q'", (q_ids,)):
        series[(cid, f"{basis}:{mid}")].append((pe, float(v)))
    issues += check_outliers(series)

    # ---- prices ----
    since = today - timedelta(days=PRICE_PRIORITY_DAYS)
    cnt = {d: c for d, c in rows(conn, "select date, count(*) from equity.historical_prices where date >= %s group by 1", (since,))}
    nsym = rows(conn, "select count(distinct symbol) from equity.historical_prices where date >= %s", (since,))[0][0]
    cal = trading_calendar(cnt, nsym)
    last_dates = {s: d for s, d in rows(conn, "select symbol, max(date) from equity.historical_prices group by 1")}
    act_syms = {s for s, in rows(conn, "select coalesce(nse_symbol, screener_slug) from equity.company where is_active")}
    last_dates = {s: d for s, d in last_dates.items() if s in act_syms}
    issues += check_stale(last_dates, cal[-1] if cal else None)
    sd: Dict[str, list] = defaultdict(list)
    for s, d in rows(conn, "select symbol, date from equity.historical_prices where date >= %s", (since,)):
        if s in act_syms:
            sd[s].append(d)
    issues += check_missing_days(sd, cal)
    issues += check_ohlc(rows(conn, """select symbol, date, open, high, low, close from equity.historical_prices
                                       where date >= %s and open is not null and high is not null and low is not null
                                         and close is not null and (low > high or open > high or close > high
                                              or open < low or close < low)""", (today - timedelta(days=3650),)))
    cands = rows(conn, """select symbol, date, ret from (select symbol, date, close / nullif(lag(close) over
                            (partition by symbol order by date), 0) - 1 as ret from equity.historical_prices) t
                          where abs(ret) > %s""", (RET_LIMIT,))
    acts: Dict[str, list] = defaultdict(list)
    cid_sym = {c: s for s, c in cid_of_sym.items()}
    for cid, ex in rows(conn, "select company_id, ex_date from equity.corporate_action where action_type in ('split','bonus')"):
        if cid in cid_sym:
            acts[cid_sym[cid]].append(ex)
    issues += check_returns([(s, d, float(r)) for s, d, r in cands if s in act_syms], acts)
    # ---- documents ----
    try:
        cols = ["company_id", "ar_expected", "ar_found", "ar_missing", "cc_expected", "cc_found", "cc_missing"]
        issues += check_docs([dict(zip(cols, r)) for r in rows(
            conn, "select " + ",".join(cols) + " from equity.v_document_completeness where company_id in "
                  "(select company_id from equity.company where is_active)")])
    except Exception as e:
        conn.rollback() if not conn.autocommit else None
        log.warning("docs completeness unavailable: %s", e)
    # price issues carry a symbol key: attach the company id
    for i in issues:
        if i.company_id == 0 and i.key in cid_of_sym:
            i.company_id = cid_of_sym[i.key]
    return issues


def close_vs_yf(conn, today: date, sample: int = 8) -> List[Issue]:
    """Live spot check: last 5 trading days of DB closes vs yfinance (unadjusted). A small random-but-stable sample."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance
    except Exception:
        return []
    syms = [r[0] for r in rows(conn, "select symbol from (select distinct symbol from equity.historical_prices) s "
                                     "order by md5(symbol || %s) limit %s", (today.isoformat(), sample))]
    db_close, yf_close = {}, {}
    for s in syms:
        try:
            h = yfinance.Ticker(s + ".NS").history(period="10d", auto_adjust=False)
            for ts, row in h.tail(5).iterrows():
                yf_close[(s, ts.date())] = float(row["Close"])
        except Exception as e:
            log.debug("yf spot check %s failed: %s", s, e)
    for s, d, c in rows(conn, "select symbol, date, close from equity.historical_prices where symbol = any(%s) and date >= %s",
                        (syms, today - timedelta(days=14))):
        db_close[(s, d)] = float(c) if c is not None else 0.0
    return check_close_mismatch(db_close, yf_close)


# ───────────────────────── ledger upsert / resolve ─────────────────────────
def persist(conn, issues: List[Issue], checks_run: Sequence[str]) -> Dict[str, int]:
    """Upsert issues (cap per check), resolve those that no longer reproduce. Returns counts."""
    seen = set()
    by_check: Dict[str, List[Issue]] = defaultdict(list)
    for i in issues:
        by_check[i.check].append(i)
    n_new = 0
    with conn.cursor() as cur:
        for code, lst in by_check.items():
            lst.sort(key=lambda i: {"error": 0, "warn": 1, "info": 2}[i.severity])
            keep, extra = lst[:MAX_ISSUES_PER_CHECK], lst[MAX_ISSUES_PER_CHECK:]
            if extra:
                keep.append(Issue(code, "info", 0, key="__overflow__", detail={"not_stored": len(extra)}))
            for i in keep:
                seen.add((i.check, i.company_id, i.key))
                cur.execute("""insert into equity.dq_issue (check_code, company_id, key, severity, detail)
                               values (%s,%s,%s,%s,%s)
                               on conflict (check_code, company_id, key) do update set severity = excluded.severity,
                                 detail = excluded.detail || jsonb_build_object('history',
                                          coalesce(equity.dq_issue.detail->'history', '[]'::jsonb)),
                                 last_seen = now(), status = 'open', resolved_at = null
                               returning (xmax = 0)""", (i.check, i.company_id, i.key, i.severity, Json(i.detail)))
                n_new += 1 if cur.fetchone()[0] else 0
        cur.execute("select check_code, company_id, key from equity.dq_issue where status='open' and check_code = any(%s)",
                    (list(checks_run),))
        stale = [r for r in cur.fetchall() if tuple(r) not in seen]
        for code, cid, key in stale:
            cur.execute("""update equity.dq_issue set status='resolved', resolved_at=now(),
                             detail = jsonb_set(detail, '{history}', coalesce(detail->'history','[]'::jsonb) ||
                                      jsonb_build_array(jsonb_build_object('at', now(), 'action', 'resolved',
                                                                           'note', 'no longer reproduces')))
                           where check_code=%s and company_id=%s and key=%s""", (code, cid, key))
    return {"new": n_new, "resolved": len(stale), "open": len(seen)}


ALL_CHECKS = sorted({"recon_sales", "recon_profit", "recon_eps", "identity_balance", "identity_cashflow", "identity_opm",
                     "quarter_gap", "latest_quarter_missing", "year_gap", "no_financials", "shareholding_gap",
                     "outlier_qoq", "price_stale", "price_gap", "price_ohlc", "price_return", "price_mismatch",
                     "docs_gap"})


# ───────────────────────── remediation ─────────────────────────
def remediate(conn, today: date, db_url_env: Optional[str] = None, max_companies: int = 60) -> Dict[str, int]:
    """Apply plan_action to open issues. Every action is appended to detail.history. Bounded by per-action schedules."""
    now = datetime.now(timezone.utc)
    cur_rows = rows(conn, "select check_code, company_id, key, detail from equity.dq_issue where status='open'")
    batch = "dq-" + today.isoformat()
    jobs_for: Dict[Tuple[int, str], list] = {}
    bhav: Dict[date, set] = defaultdict(set)
    docs_syms: List[Tuple[int, str]] = []
    todo: List[Tuple[str, int, str, str]] = []
    sym_of = {c: s for c, s in rows(conn, "select company_id, coalesce(nse_symbol, screener_slug) from equity.company")}
    n = defaultdict(int)
    for code, cid, key, detail in cur_rows:
        if cid == 0 or key == "__overflow__":
            continue
        plan = plan_action(code, (detail or {}).get("history") or [], now)
        if plan is None:
            continue
        action, kinds = plan
        if action == "bhavcopy":
            ds = [date.fromisoformat(d) for d in ((detail or {}).get("dates") or [x["date"] for x in (detail or {}).get("days", [])])]
            if not ds:
                continue
            for d in ds[-5:]:
                bhav[d].add(sym_of.get(cid, key))
            todo.append((code, cid, key, action))
        elif action == "docs_retry":
            if len(docs_syms) < 10:
                docs_syms.append((cid, sym_of.get(cid)))
                todo.append((code, cid, key, action))
        else:
            if len({c for c, _ in jobs_for}) >= max_companies and (cid, kinds[0]) not in jobs_for:
                continue
            for k in kinds:
                jobs_for[(cid, k)] = [code]
            todo.append((code, cid, key, action))
    # execute
    if jobs_for:
        with conn.cursor() as cur:
            for (cid, k), _ in jobs_for.items():
                cur.execute("""insert into equity.job (batch, kind, company_id, priority) values (%s,%s,%s,50)
                               on conflict (batch, kind, company_id) do update set status='pending', attempts=0,
                                 next_run_at=now() where equity.job.status in ('done','dead')""", (batch, k, cid))
                n["jobs_enqueued"] += 1
        spawn_worker(batch, db_url_env)
    if bhav:
        n["bhavcopy_dates"] = run_bhavcopy(bhav, db_url_env)
    if docs_syms:
        run_docs_retry([s for _, s in docs_syms if s], db_url_env)
        n["docs_companies"] = len(docs_syms)
    entry_at = now.isoformat(timespec="seconds")
    with conn.cursor() as cur:
        for code, cid, key, action in todo:
            cur.execute("""update equity.dq_issue set detail = jsonb_set(detail, '{history}',
                              coalesce(detail->'history','[]'::jsonb) || %s::jsonb) where check_code=%s and company_id=%s and key=%s""",
                        (json.dumps([{"at": entry_at, "action": action, "batch": batch}]), code, cid, key))
            n["actions"] += 1
    return dict(n)


def _env(db_url_env: Optional[str]) -> dict:
    env = dict(os.environ)
    if db_url_env:
        env["DATABASE_URL"] = db_url_env
    return env


def spawn_worker(batch: str, db_url_env: Optional[str]) -> None:
    """Detached local worker for the remediation batch (drains and exits; screener politeness applies)."""
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        lf = open(config.LOG_DIR / f"dq_worker_{batch}.log", "ab")
        subprocess.Popen([sys.executable, "-m", "screener_scraper", "worker", "--kinds", "screener,yf", "--class", "local",
                          "--slots", "1", "--max-runtime", "45", "--batch", batch], cwd=str(config.ROOT),
                         env=_env(db_url_env), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        log.warning("could not start dq worker: %s", e)


def run_bhavcopy(by_date: Dict[date, set], db_url_env: Optional[str], max_dates: int = 5) -> int:
    done = 0
    for d in sorted(by_date, reverse=True)[:max_dates]:
        try:
            r = subprocess.run([sys.executable, "-m", "ingestion.backfill_from_nse", "--date", d.isoformat(),
                                "--symbols", ",".join(sorted(by_date[d]))], cwd=str(config.ROOT), env=_env(db_url_env),
                               capture_output=True, text=True, timeout=300)
            done += r.returncode == 0
        except Exception as e:
            log.warning("bhavcopy backfill %s failed: %s", d, e)
    return done


def run_docs_retry(symbols: List[str], db_url_env: Optional[str]) -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        lf = open(config.LOG_DIR / f"dq_docs_{date.today()}.log", "ab")
        subprocess.Popen([sys.executable, "-m", "screener_scraper", "docs", "--retry-missing", "--symbols",
                          ",".join(symbols), "--slots", "1"], cwd=str(config.ROOT), env=_env(db_url_env),
                         stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        log.warning("could not start docs retry: %s", e)


# ───────────────────────── scorecard ─────────────────────────
def compute_scorecard(conn, today: date) -> Dict[str, float]:
    actives = rows(conn, "select company_id, coalesce(nse_symbol, screener_slug) from equity.company where is_active")
    kp = [r[0] for r in rows(conn, "select metric_id from equity.metric where parent_metric_id is null and name='Net Profit' "
                                   "and section in ('quarters','profit-loss')")]
    qn: Dict[int, set] = defaultdict(set)
    an: Dict[int, set] = defaultdict(set)
    for cid, pt, pe in rows(conn, "select distinct company_id, period_type, period_end from equity.financial_fact "
                                  "where metric_id = any(%s) and period_type in ('Q','A')", (kp,)):
        (qn if pt == "Q" else an)[cid].add(pe)
    lq = universe_latest({c: max(v) for c, v in qn.items() if v})
    last = {s: d for s, d in rows(conn, "select symbol, max(date) from equity.historical_prices group by 1")}
    cal_last = max(last.values()) if last else None
    iss: Dict[int, Dict[str, int]] = defaultdict(lambda: {"error": 0, "warn": 0})
    for cid, sev, n in rows(conn, "select company_id, severity, count(*) from equity.dq_issue where status='open' "
                                  "and severity in ('error','warn') and check_code not in ('docs_gap') group by 1,2"):
        iss[cid][sev] = n
    docs = {}
    try:
        for cid, ae, af, ce, cf in rows(conn, "select company_id, ar_expected, ar_found, cc_expected, cc_found "
                                              "from equity.v_document_completeness"):
            tot = (ae or 0) + (ce or 0)
            docs[cid] = ((af or 0) + (cf or 0)) / tot if tot else None
    except Exception:
        pass
    out = []
    for cid, sym in actives:
        behind = (cal_last - last[sym]).days if sym in last and cal_last else None
        sc = score_company(q_count=len(qn.get(cid, ())), a_count=len(an.get(cid, ())), price_days_behind=behind,
                           q_behind=bool(lq and (not qn.get(cid) or max(qn[cid]) < lq)),
                           open_errors=iss[cid]["error"], open_warns=iss[cid]["warn"], docs_ratio=docs.get(cid))
        out.append((cid, today, sc["coverage"], sc["freshness"], sc["consistency"], sc["documents"], sc["overall"]))
    with conn.cursor() as cur:
        cur.execute("delete from equity.dq_company_score where company_id not in (select company_id from equity.company where is_active)")
        for r in out:
            cur.execute("""insert into equity.dq_company_score (company_id, as_of, coverage, freshness, consistency, documents, overall)
                           values (%s,%s,%s,%s,%s,%s,%s) on conflict (company_id) do update set as_of=excluded.as_of,
                           coverage=excluded.coverage, freshness=excluded.freshness, consistency=excluded.consistency,
                           documents=excluded.documents, overall=excluded.overall""", r)
    return {"companies": len(out)}


def snapshot_daily(conn, today: date, resolved: int, remediations: int) -> dict:
    s = rows(conn, "select companies, avg_overall, avg_coverage, avg_freshness, avg_consistency, avg_documents, open_errors, "
                   "open_warns from equity.v_quality_summary")[0]
    by = {c: n for c, n in rows(conn, "select check_code, count(*) from equity.dq_issue where status='open' group by 1")}
    with conn.cursor() as cur:
        cur.execute("""insert into equity.dq_daily (as_of, companies, avg_overall, avg_coverage, avg_freshness,
                         avg_consistency, avg_documents, open_errors, open_warns, by_check, resolved, remediations)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (as_of) do update set companies=excluded.companies, avg_overall=excluded.avg_overall,
                         avg_coverage=excluded.avg_coverage, avg_freshness=excluded.avg_freshness,
                         avg_consistency=excluded.avg_consistency, avg_documents=excluded.avg_documents,
                         open_errors=excluded.open_errors, open_warns=excluded.open_warns, by_check=excluded.by_check,
                         resolved=excluded.resolved, remediations=excluded.remediations""",
                    (today, *[float(x) if x is not None else None for x in s[:6]], s[6], s[7], Json(by), resolved, remediations))
    return {"companies": s[0], "overall": float(s[1] or 0), "coverage": float(s[2] or 0), "freshness": float(s[3] or 0),
            "consistency": float(s[4] or 0), "documents": float(s[5] or 0), "errors": s[6], "warns": s[7], "by_check": by}


# ───────────────────────── quantity growth ─────────────────────────
EXTRA_WINDOW = 300


def plan_extra_windows(start_rank: int, last_rank: int, size: int = EXTRA_WINDOW) -> List[Tuple[int, int]]:
    return [(a, min(a + size - 1, last_rank)) for a in range(start_rank, last_rank + 1, size)]


def grow_universe(sup_conn, lake_conn) -> str:
    """After TOP1000 is fully loaded in the lake, rank the remaining NSE candidates 1001.. into the TOP1000_MCAP universe
    (identity rows only, tiny) and register the next window in lake_state 'extra_windows' for lake-drive to dispatch.
    One window at a time; stops when no candidates remain."""
    code = "TOP1000_MCAP"
    base = rows(lake_conn, "select from_rank, to_rank, status from public.lake_window where attempt=1 and ranks is null")
    if any(s in ("dispatched", "done") for _, _, s in base):
        return "growth waiting: lake windows still in flight"
    loaded = {(a, b) for a, b, s in base if s in ("loaded", "retried")}
    if not all(w in loaded for w in ((101, 400), (401, 700), (701, 1000))):
        return "growth waiting: base windows (101-1000) not all loaded"
    cand = rows(sup_conn, """select c.company_id, c.symbol from (select uc.company_id, uc.symbol, uc.market_cap_cr from equity.universe_candidate uc
                                where uc.as_of = (select max(as_of) from equity.universe_candidate)) c
                             where c.company_id not in (select company_id from equity.universe where universe_code=%s
                                   and as_of = (select max(as_of) from equity.universe where universe_code=%s))
                             order by (select market_cap_cr from equity.universe_candidate x where x.company_id=c.company_id
                                       order by as_of desc limit 1) desc nulls last""", (code, code))
    state = dict(rows(lake_conn, "select key, value from public.lake_state where key in ('extra_windows','extra_ranked')"))
    extra = [tuple(map(int, w.split("-"))) for w in (state.get("extra_windows") or "").split(",") if w]
    ranked_to = int(state.get("extra_ranked") or 1000)
    if cand:
        as_of = rows(sup_conn, "select max(as_of) from equity.universe where universe_code=%s", (code,))[0][0]
        with sup_conn.cursor() as cur:
            for i, (cid, _) in enumerate(cand, start=ranked_to + 1):
                cur.execute("insert into equity.universe (universe_code, as_of, company_id, rank) values (%s,%s,%s,%s) "
                            "on conflict do nothing", (code, as_of, cid, min(i, 32000)))
        ranked_to += len(cand)
    todo = [w for w in plan_extra_windows(1001, ranked_to) if w not in extra]
    if not todo:
        return "growth complete: no candidates remain" if not cand else "growth: nothing to add"
    pending = [w for w in extra if w not in loaded]
    if pending:
        return f"growth waiting: window {pending[0][0]}-{pending[0][1]} not loaded yet"
    nxt = todo[0]
    extra.append(nxt)
    with lake_conn.cursor() as cur:
        for k, v in (("extra_windows", ",".join(f"{a}-{b}" for a, b in extra)), ("extra_ranked", str(ranked_to))):
            cur.execute("insert into public.lake_state(key, value) values (%s,%s) on conflict (key) do update "
                        "set value=excluded.value, updated_at=now()", (k, v))
    return f"growth: registered window {nxt[0]}-{nxt[1]} ({len(cand)} new candidates ranked)"


# ───────────────────────── orchestration ─────────────────────────
def run_dq(conn, name: str, db_url_env: Optional[str], today: Optional[date] = None, force: bool = False,
           remediate_on: bool = True, live: bool = True) -> dict:
    today = today or date.today()
    conn.autocommit = True
    if not force and get_ctl(conn, "dq_last_run") == today.isoformat():
        return {"db": name, "skipped": "already ran today"}
    out: dict = {"db": name}
    out["deactivated_or_reactivated"] = sync_active(conn)
    issues = gather_issues(conn, today)
    if live:
        try:
            issues += close_vs_yf(conn, today)
        except Exception as e:
            log.warning("yf spot check failed: %s", e)
    p = persist(conn, issues, [c for c in ALL_CHECKS if c != "price_mismatch"])   # spot check is a rotating sample
    with conn.cursor() as cur:
        cur.execute("update equity.dq_issue set status='resolved', resolved_at=now() where check_code='price_mismatch' "
                    "and status='open' and last_seen < now() - interval '14 days'")
    out.update(p)
    out["remediation"] = remediate(conn, today, db_url_env) if remediate_on else {}
    compute_scorecard(conn, today)
    out["summary"] = snapshot_daily(conn, today, p["resolved"], out["remediation"].get("actions", 0))
    set_ctl(conn, "dq_last_run", today.isoformat(), "daily data-quality run")
    return out


def dq_cli(which: str = "both", force: bool = False) -> int:
    from .db import DB
    results = []
    if which in ("supabase", "both"):
        from .jobs import overflow_reason
        db = DB()
        try:
            why = overflow_reason(db.trail) if "supabase" in db.url else None   # full: write nothing more there
            results.append({"db": "supabase", "skipped": why} if why else run_dq(db.trail, "supabase", None, force=force))
            if which == "both":
                results.append(_lake_part(db, force))
        finally:
            db.close()
    elif which == "lake":
        results.append(_lake_part(None, force))
    for r in results:
        print(json.dumps(r, default=str)[:3000])
    return 0


def _lake_part(sup_db, force: bool) -> dict:
    try:
        from .lake import lake_connect, lake_url
        conn = lake_connect(autocommit=True)
    except Exception as e:
        return {"db": "lake", "skipped": f"lake unavailable: {type(e).__name__}"}
    try:
        r = run_dq(conn, "lake", lake_url(), force=force)
        if sup_db is not None:
            try:
                r["growth"] = grow_universe(sup_db.trail, conn)
            except Exception as e:
                r["growth"] = f"error {type(e).__name__}: {str(e)[:100]}"
        return r
    finally:
        conn.close()


def report_section(conn) -> str:
    """Markdown 'Data quality' block for reports/<date>.md."""
    try:
        s = rows(conn, "select companies, avg_overall, avg_coverage, avg_freshness, avg_consistency, avg_documents, "
                       "open_errors, open_warns, companies_below_60 from equity.v_quality_summary")[0]
        by = rows(conn, "select check_code, severity, count(*) from equity.dq_issue where status='open' group by 1,2 order by 3 desc")
        trend = rows(conn, "select as_of, avg_overall, open_errors, open_warns from equity.dq_daily order by as_of desc limit 7")
    except Exception as e:
        conn.rollback() if not conn.autocommit else None
        return f"\n## Data quality\nunavailable: {type(e).__name__}\n"
    L = ["", "## Data quality",
         f"{s[0]} companies, overall **{s[1]}**/100 (coverage {s[2]}, freshness {s[3]}, consistency {s[4]}, documents {s[5]}); "
         f"{s[8]} below 60; open issues: {s[6]} errors, {s[7]} warnings", "", "| check | severity | open |", "|---|---|---|"]
    L += [f"| {c} | {sv} | {n} |" for c, sv, n in by]
    if trend:
        L += ["", "Trend (date, overall, errors, warnings): " + "; ".join(f"{d} {o} {e} {w}" for d, o, e, w in trend)]
    return "\n".join(L) + "\n"
