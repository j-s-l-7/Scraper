"""Universe step: Nifty 500 candidates -> screener market cap -> top N -> company + universe rows."""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, parse
from .db import DB
from .http import Client, FetchError
from .pipeline import fetch_page, page_url, pseudo_id

log = logging.getLogger(__name__)


def load_candidates(client: Client, source: str) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """-> [(symbol, isin, company_name)] from the Nifty 500 CSV (url) or a local CSV path."""
    if source == "nifty500":
        text = client.get(config.NIFTY500_URL, "_universe", "nifty500list.csv").text
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        sym = r.get("Symbol")
        if sym:
            out.append((sym, r.get("ISIN Code") or r.get("ISIN") or None, r.get("Company Name")))
    return out


def run_universe(db: DB, top: int = 100, candidates: str = "nifty500", limit: Optional[int] = None,
                 universe_code: str = config.UNIVERSE_CODE) -> str:
    client = Client()
    run_id = db.start_run("universe", {"top": top, "candidates": candidates, "limit": limit, "code": universe_code})
    t0 = time.monotonic()
    stats = {"candidates": 0, "fetched": 0, "failed": 0, "ranked": 0}
    try:
        cands = load_candidates(client, candidates)
    except Exception as e:
        db.finish_run(run_id, "failed", {"error": f"{type(e).__name__}: {e}"})
        log.error("could not load candidates: %s", e)
        return "failed"
    if limit:
        cands = cands[:limit]
    stats["candidates"] = len(cands)
    results: List[Tuple[float, str, Optional[str], parse.PageData, bool]] = []
    for i, (sym, isin, _name) in enumerate(cands, 1):
        pc, t = fetch_page(client, sym, "C")
        ps = None
        if pc is None:
            db.record_task(run_id, pseudo_id(sym), "universe", "failed", http_status=t.http_status,
                           attempts=t.attempts, error=f"{sym}: {t.error}")
            stats["failed"] += 1
            log.warning("[%d/%d] %s: %s", i, len(cands), sym, t.error)
            continue
        page = pc
        has_c = pc.is_consolidated
        if not has_c or pc.top_ratios.market_cap_cr is None:      # empty consolidated shell -> standalone page
            ps, t2 = fetch_page(client, sym, "S")
            if ps is None:
                db.record_task(run_id, pseudo_id(sym), "universe", "failed", http_status=t2.http_status,
                               attempts=t2.attempts, error=f"{sym}: {t2.error}")
                stats["failed"] += 1
                continue
            page, has_c = ps, False
        mc = page.top_ratios.market_cap_cr
        if mc is None:
            db.record_task(run_id, pseudo_id(sym), "universe", "failed", error=f"{sym}: no market cap on page")
            stats["failed"] += 1
            continue
        db.record_task(run_id, page.info.company_id, "universe", "ok", rows_written=1, http_status=200)
        stats["fetched"] += 1
        results.append((mc, sym, isin, page, has_c))
        if i % 25 == 0:
            log.info("universe: %d/%d candidates fetched", i, len(cands))

    results.sort(key=lambda r: -r[0])
    as_of = date.today()
    uni_rows, ok_n = [], 0
    for rank, (mc, sym, isin, page, has_c) in enumerate(results[:top], 1):
        try:
            with db.conn:
                with db.conn.cursor() as cur:
                    db.upsert_company(cur, page.info, sym, has_c, isin=isin)
                    db.upsert_universe(cur, [(universe_code, as_of, page.info.company_id, rank, mc)])
            ok_n += 1
        except Exception as e:
            log.exception("universe upsert failed for %s", sym)
            db.record_task(run_id, page.info.company_id, "universe_write", "failed", error=str(e))
            stats["failed"] += 1
    stats["ranked"] = ok_n
    stats["requests"] = client.requests_made
    stats["cache_hits"] = client.cache_hits
    stats["seconds"] = round(time.monotonic() - t0, 1)
    stats["top"] = [(s, round(m)) for m, s, *_ in results[:top]][:15]
    status = "partial" if stats["failed"] else "success"
    db.finish_run(run_id, status, stats)
    log.info("universe done: %s %s", status, {k: v for k, v in stats.items() if k != "top"})
    return status
