"""Own corporate-action adjustment factors from equity.corporate_action (split, bonus, dividend) -> equity.adj_factor,
view equity.v_prices_adjusted, and a dq check of our adjusted close against yfinance adj_close (tolerance 0.5%).
corporate_action / historical_prices for the Supabase top-100 are copied into the lake (read-only on Supabase)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import psycopg2
from psycopg2.extras import execute_values

from .. import config
from . import _common as c

log = logging.getLogger(__name__)
NAME = "adj_factor"
TOLERANCE = 0.005
COMPARE_FROM = date(2019, 10, 1)


def factor_for(kind: str, value: Optional[float], prev_close: Optional[float] = None) -> Optional[float]:
    """Multiplier for prices before the ex-date. For dividends prev_close must be on the same split basis as the dividend
    value (yfinance dividends are split-adjusted, so the yfinance close is preferred over the raw NSE close). split value = new shares per old; bonus value = (a+b)/b as parsed;
    dividend = cash per share against the last close before the ex-date."""
    if value is None or value <= 0:
        return None
    if kind in ("split", "bonus"):
        return 1.0 / value
    if kind == "dividend":
        if not prev_close or value >= prev_close:
            return None
        return 1.0 - value / prev_close
    return None


def sync_from_supabase(db) -> dict:
    """Read-only copy of corporate_action and historical_prices (>= 2019-09-01) for companies in the lake. Skipped
    (logged) when this process has no Supabase url: config.supabase_url never hands back the lake itself."""
    url = config.supabase_url(session=True)
    if not url:
        log.warning("adj_factor: no Supabase url (SOURCE_DATABASE_URL unset and DATABASE_URL is the lake): sync skipped")
        return {"skipped": "no Supabase url"}
    c.ensure_companies(db)
    src = psycopg2.connect(url, connect_timeout=20)
    src.set_session(readonly=True, autocommit=True)
    out = {}
    with src.cursor() as cur:
        cur.execute("select company_id, action_type, ex_date, value, detail, source from equity.corporate_action")
        ca = cur.fetchall()
        cur.execute("select symbol, date, open, high, low, close, adj_close, volume from equity.historical_prices "
                    "where date >= '2019-09-01'")
        hp = cur.fetchall()
    src.close()
    with db.conn.cursor() as cur:
        cur.execute("select company_id from equity.company")
        known = {r[0] for r in cur.fetchall()}
    db.conn.commit()
    ca = [r for r in ca if r[0] in known]
    if ca:
        db.run_tx(lambda cur: execute_values(
            cur, "insert into equity.corporate_action (company_id, action_type, ex_date, value, detail, source) values %s "
                 "on conflict (company_id, action_type, ex_date) do update set value=excluded.value, detail=excluded.detail", ca))
    for i in range(0, len(hp), 20000):
        chunk = hp[i:i + 20000]
        db.run_tx(lambda cur: execute_values(
            cur, "insert into equity.historical_prices (symbol, date, open, high, low, close, adj_close, volume) values %s "
                 "on conflict (symbol, date) do update set open=excluded.open, high=excluded.high, low=excluded.low, "
                 "close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume", chunk))
    out["corporate_action"], out["historical_prices"] = len(ca), len(hp)
    return out


def compute_factors(db) -> int:
    with db.conn.cursor() as cur:
        cur.execute("""select co.nse_symbol, a.action_type, a.ex_date, a.value, a.source,
                              (select b.close from equity.nse_bhav_full b where b.symbol = co.nse_symbol and b.series = 'EQ'
                                 and b.date < a.ex_date order by b.date desc limit 1) as prev_close,
                              (select h.close from equity.historical_prices h where h.symbol = co.nse_symbol
                                 and h.date < a.ex_date order by h.date desc limit 1) as prev_close_yf
                       from equity.corporate_action a join equity.company co using (company_id)
                       where a.action_type in ('split','bonus','dividend') and co.nse_symbol is not null""")
        rows = cur.fetchall()
    db.conn.commit()
    out = []
    for sym, kind, ex, val, src, pc, pcy in rows:
        # yfinance dividends are split-adjusted (pair with yfinance's close); NSE-archive dividends are as declared
        # (pair with the raw NSE close)
        close = (pc if pc is not None else pcy) if src == "nse_archive" else (pcy if pcy is not None else pc)
        f = factor_for(kind, float(val) if val is not None else None, float(close) if close else None)
        # a consolidation (reverse split, value < 1) raises earlier prices: factor > 1, allowed for splits only
        if f is not None and (0 < f < 1.0000001 or (kind == "split" and 1 < f <= 100)):
            out.append((sym, ex, f, kind, "computed:corporate_action"))
    n = c.upsert(db, "equity.adj_factor", ["symbol", "ex_date", "factor", "kind", "source"],
                 ["symbol", "ex_date", "kind"], out, ts_col="fetched_at")
    keep = {(s, e, k) for s, e, _, k, _ in out}
    with db.conn.cursor() as cur:            # a factor whose corporate action is gone (deduplicated, corrected)
        cur.execute("select symbol, ex_date, kind from equity.adj_factor where source = 'computed:corporate_action'")
        stale = [r for r in cur.fetchall() if r not in keep]
    db.conn.commit()
    if stale:
        db.run_tx(lambda cur: cur.executemany(
            "delete from equity.adj_factor where symbol=%s and ex_date=%s and kind=%s and source='computed:corporate_action'",
            stale))
        log.info("adj_factor: removed %d factors whose corporate action no longer exists", len(stale))
    return n


def dq_check(db) -> dict:
    """Our adjusted close (raw NSE close x factors) vs yfinance adj_close; symbols off by > 0.5% become dq warnings."""
    with db.conn.cursor() as cur:
        cur.execute("""select symbol, count(*) n,
                              count(*) filter (where abs(adj_close / yf_adj_close - 1) > %s) bad,
                              max(abs(adj_close / yf_adj_close - 1)) mx
                       from equity.v_prices_adjusted
                       where date >= %s and yf_adj_close > 0 group by symbol""", (TOLERANCE, COMPARE_FROM))
        res = cur.fetchall()
    db.conn.commit()
    bad = [(s, n, b, float(m)) for s, n, b, m in res if b]
    now = datetime.now(timezone.utc).isoformat()

    def tx(cur):
        cur.execute("update equity.dq_issue set status='resolved', resolved_at=now() "
                    "where check_code='adj_close_vs_yf' and status='open'")
        for s, n, b, m in bad:
            cur.execute("""insert into equity.dq_issue (check_code, company_id, key, severity, detail)
                           values ('adj_close_vs_yf', 0, %s, 'warn', %s)
                           on conflict (check_code, company_id, key) do update set status='open', resolved_at=null,
                               last_seen=now(), detail=excluded.detail""",
                        (s, json.dumps({"compared": n, "off_gt_0_5pct": b, "max_rel_diff": round(m, 4), "at": now})))
    try:
        db.run_tx(tx)
    except psycopg2.errors.UndefinedTable:
        db.reconnect("conn")
    return {"symbols": len(res), "flagged": len(bad), "compared": sum(r[1] for r in res)}


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    from ..lake import top100_on_lake
    # once the lake is primary for the top 100 (Supabase full, R8) Supabase's frozen rows must not overwrite it
    synced = {} if top100_on_lake(db.trail) else sync_from_supabase(db)
    n = compute_factors(db)
    dq = dq_check(db)
    c.set_marker(db, NAME, status="ok", run_at=datetime.now(timezone.utc).isoformat(), last_date=c.today_ist(), dq=dq)
    return {"status": "ok", "rows": n, "synced": synced, "dq": dq}
