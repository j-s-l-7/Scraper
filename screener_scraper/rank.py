"""Ranking for the TOP1000_MCAP universe (runs on 20 GitHub runners, then one finalize step).

rank-candidates --shard k/n : NSE EQUITY_L.csv slice -> screener consolidated page -> market cap + identity ->
                              equity.company (identity columns only) + equity.universe_candidate
rank-finalize               : equity.universe_candidate -> equity.universe(TOP1000_MCAP, rank 1..1000)
"""
from __future__ import annotations

import copy
import csv
import io
import logging
import time
from datetime import date
from typing import List, Optional, Tuple

from . import config
from .db import DB
from .http import Client, FetchError
from .pipeline import fetch_page, pseudo_id, shard_items

log = logging.getLogger(__name__)

EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NIFTY_FALLBACKS = ["https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
                   "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"]
KEEP_SERIES = {"EQ", "BE", "BZ"}
TOP1000_CODE = "TOP1000_MCAP"


def parse_candidates(text: str) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """EQUITY_L.csv or a niftyindices list -> [(symbol, isin, name)] (main-board series only for EQUITY_L)."""
    out, seen = [], set()
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        sym = r.get("SYMBOL")
        if not sym or sym in seen:
            continue
        series = r.get("SERIES")
        if series is not None and series and series not in KEEP_SERIES:
            continue
        seen.add(sym)
        out.append((sym, r.get("ISIN NUMBER") or r.get("ISIN CODE") or r.get("ISIN") or None,
                    r.get("NAME OF COMPANY") or r.get("COMPANY NAME")))
    return out


def fetch_candidates(client: Client) -> List[Tuple[str, Optional[str], Optional[str]]]:
    errs = []
    for url in [EQUITY_L_URL] + NIFTY_FALLBACKS:
        try:
            r = client.get(url, "_universe", "cand_" + url.rsplit("/", 1)[-1])
            cands = parse_candidates(r.text)
            if len(cands) > 100:
                log.info("candidates: %d symbols from %s", len(cands), url)
                return cands
            errs.append(f"{url}: only {len(cands)} rows")
        except FetchError as e:
            errs.append(f"{url}: {e}")
    raise RuntimeError("no candidate list available: " + "; ".join(errs))


def identity_only(info):
    """company row without the bulky profile text (keeps Supabase small)."""
    i = copy.copy(info)
    i.about = i.key_points = None
    i.pros, i.cons = [], []
    return i


def run_rank_candidates(db: DB, shard: Optional[Tuple[int, int]], limit: Optional[int] = None) -> str:
    client = Client()
    cands = fetch_candidates(client)
    isin_of = {s: i for s, i, _ in cands}
    syms = shard_items([s for s, _, _ in cands], shard)
    if limit:
        syms = syms[:limit]
    as_of = date.today()
    run_id = db.start_run("rank", {"shard": f"{shard[0]}/{shard[1]}" if shard else None, "n": len(syms)})
    t0, ok, failed = time.monotonic(), 0, 0
    for i, sym in enumerate(syms, 1):
        try:
            pc, t = fetch_page(client, sym, "C")
            page, has_c = pc, bool(pc and pc.is_consolidated)
            if pc is None or not has_c or pc.top_ratios.market_cap_cr is None:
                ps, t2 = fetch_page(client, sym, "S")
                if ps is not None:
                    page, has_c = ps, False
                elif pc is None:
                    raise FetchError(t2.error or t.error or "page fetch failed", t2.http_status or t.http_status)
            mc = page.top_ratios.market_cap_cr
            if mc is None:
                raise ValueError("no market cap on page")
            info = identity_only(page.info)

            def write(cur):
                db.upsert_company(cur, info, sym, has_c, isin=isin_of.get(sym))
                cur.execute("""insert into equity.universe_candidate(symbol, company_id, market_cap_cr, as_of)
                               values (%s,%s,%s,%s) on conflict (symbol, as_of) do update set
                               company_id=excluded.company_id, market_cap_cr=excluded.market_cap_cr""",
                            (sym, info.company_id, mc, as_of))
            db.run_tx(write)
            ok += 1
        except Exception as e:
            failed += 1
            log.warning("[%d/%d] %s failed: %s: %s", i, len(syms), sym, type(e).__name__, e)
            try:
                db.record_task(run_id, pseudo_id(sym), "rank", "failed", error=f"{sym}: {type(e).__name__}: {e}",
                               http_status=getattr(e, "status_code", None))
            except Exception:
                pass
        if i % 20 == 0:
            log.info("rank: %d/%d (ok %d, failed %d)", i, len(syms), ok, failed)
    status = "partial" if failed else "success"
    db.finish_run(run_id, status, {"ok": ok, "failed": failed, "requests": client.requests_made,
                                   "seconds": round(time.monotonic() - t0, 1)})
    log.info("rank shard done: ok=%d failed=%d", ok, failed)
    return status


def run_rank_finalize(db: DB, top: int = 1000, code: str = TOP1000_CODE, min_candidates: int = 0) -> int:
    """Latest candidate per symbol -> universe(code, today). Returns number of ranked companies."""
    as_of = date.today()

    def write(cur):
        cur.execute("""select symbol, company_id, market_cap_cr from (
                         select distinct on (symbol) symbol, company_id, market_cap_cr, as_of
                         from equity.universe_candidate where as_of >= %s - 7 order by symbol, as_of desc) c
                       order by market_cap_cr desc limit %s""", (as_of, top))
        rows = cur.fetchall()
        if len(rows) < min_candidates:
            raise RuntimeError(f"only {len(rows)} candidates ranked (< {min_candidates})")
        seen, uniq = set(), []
        for row in rows:
            if row[1] not in seen:
                seen.add(row[1]); uniq.append(row)
        rows = uniq
        # the universe grew past `top` (grow_universe, now frozen): carry those companies forward after the new
        # top, by their latest candidate market cap, so a re-rank never shrinks it back to `top`
        cur.execute("""select p.company_id, c.symbol, c.market_cap_cr from equity.universe p
                       left join lateral (select symbol, market_cap_cr from equity.universe_candidate x
                                          where x.company_id = p.company_id order by as_of desc limit 1) c on true
                       where p.universe_code=%s and p.as_of = (select max(as_of) from equity.universe
                             where universe_code=%s and as_of < %s) and not (p.company_id = any(%s))
                       order by c.market_cap_cr desc nulls last, p.rank""", (code, code, as_of, list(seen)))
        rows += [(sym, cid, mc) for cid, sym, mc in cur.fetchall()]
        cur.execute("delete from equity.universe where universe_code=%s and as_of=%s", (code, as_of))
        db.upsert_universe(cur, [(code, as_of, cid, rank, mc) for rank, (_, cid, mc) in enumerate(rows, 1)])
        return rows
    rows = db.run_tx(write)
    log.info("%s: ranked %d companies; #1 %s, #%d %s", code, len(rows), rows[0][0] if rows else None,
             len(rows), rows[-1][0] if rows else None)
    return len(rows)


GROWTH_FROZEN_KEY = "growth_frozen"     # public.lake_state: grow_universe adds no candidates or windows while set


def run_rank_repair(code: str = TOP1000_CODE, keep_top: int = 1000) -> dict:
    """Unique ranks for the latest `code` universe: ranks 1..keep_top stay (one rank-finalize pass, consistent with
    TOP100_MCAP); every company ranked past keep_top is re-ranked keep_top+1.. by its latest market cap in the lake
    (market_snapshot, the freshest), NULLs last. Growth past keep_top double-assigned ranks 1001-1799 (fixed in
    grow_universe; this repairs the stored rows). Freezes growth first, so the new top rank never registers new
    lake windows, then writes Supabase (the ranking authority; skipped while it is full) and the lake (the fleet's
    seed is insert-only and would never update a lake rank). Idempotent."""
    import psycopg2
    from . import config
    from .jobs import overflow_for_url
    from .lake import lake_connect
    lake = lake_connect()
    try:
        with lake, lake.cursor() as cur:
            cur.execute("insert into public.lake_state(key, value) values (%s, %s) on conflict (key) do update "
                        "set value = excluded.value, updated_at = now()",
                        (GROWTH_FROZEN_KEY, f"{date.today()}: universe complete; rank-repair"))
            cur.execute("""select u.as_of, u.company_id, u.rank, (select s.market_cap_cr from equity.market_snapshot s
                                   where s.company_id = u.company_id order by s.as_of desc limit 1)
                           from equity.universe u where u.universe_code = %s
                             and u.as_of = (select max(as_of) from equity.universe where universe_code = %s)""",
                        (code, code))
            rows = cur.fetchall()
        if not rows:
            return {"status": "no universe"}
        as_of = rows[0][0]
        tail = sorted((r for r in rows if r[2] is None or r[2] > keep_top),
                      key=lambda r: (r[3] is None, -(float(r[3]) if r[3] is not None else 0), r[1]))
        new = [(cid, keep_top + i, mc) for i, (_, cid, _, mc) in enumerate(tail, 1)]
        sql = """update equity.universe u set rank = v.rank, market_cap_cr = coalesce(v.mc, u.market_cap_cr)
                 from (values %s) v(company_id, rank, mc)
                 where u.universe_code = %s and u.as_of = %s and u.company_id = v.company_id
                   and (u.rank is distinct from v.rank or u.market_cap_cr is distinct from coalesce(v.mc, u.market_cap_cr))"""
        out = {"as_of": str(as_of), "reranked": len(new), "max_rank": keep_top + len(new)}
        if not new:
            return out
        sup = config.supabase_url()
        if sup and not overflow_for_url(sup, latch=False):
            s = psycopg2.connect(sup, connect_timeout=20)
            try:
                with s, s.cursor() as cur:
                    cur.execute(*_values_update(cur, sql, new, code, as_of))
                    out["supabase_updated"] = cur.rowcount
            finally:
                s.close()
        else:
            out["supabase_updated"] = "skipped (no Supabase url, or it is full)"
        with lake, lake.cursor() as cur:
            cur.execute(*_values_update(cur, sql, new, code, as_of))
            out["lake_updated"] = cur.rowcount
        return out
    finally:
        lake.close()


def _values_update(cur, sql: str, vals, code: str, as_of) -> tuple:
    """(query, params) for an UPDATE ... FROM (VALUES ...) with one placeholder for the whole VALUES list."""
    body = b",".join(cur.mogrify("(%s, %s::smallint, %s::numeric)", v) for v in vals).decode()
    return sql.replace("(values %s)", f"(values {body})", 1), (code, as_of)
