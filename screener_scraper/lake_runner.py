"""Runs on a GitHub runner against a runner-local Postgres: seed the slice from Supabase (read-only), then for each
company scrape + yf (parallel) -> docs -> incremental CSV export. A timeout loses at most one company."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Sequence

from psycopg2.extras import execute_values

from . import config
from .db import DB
from .lake import export_tables
from .rank import TOP1000_CODE

log = logging.getLogger(__name__)
KINDS = ["annual_report", "concall_transcript", "concall_ppt"]


def runner_ranks(ranks: Sequence[int], k: int, n: int) -> List[int]:
    """Runner k (1..n) takes every n-th rank of the sorted window: for a contiguous window this is
    (r - from_rank) % n == k - 1, so the n runners always work on about n consecutive securities."""
    return [r for i, r in enumerate(sorted(ranks)) if i % n == k - 1]


def window_ranks(from_rank: int, to_rank: int, explicit: Optional[str] = None) -> List[int]:
    if explicit:
        return sorted({int(x) for x in explicit.replace(",", " ").split()})
    return list(range(from_rank, to_rank + 1))


def _copy_row_set(src_cur, dst_cur, table: str, where: str, params: tuple) -> int:
    src_cur.execute(f"select * from equity.{table} where {where}", params)
    cols = [d[0] for d in src_cur.description]
    rows = src_cur.fetchall()
    if rows:
        execute_values(dst_cur, f"insert into equity.{table} ({', '.join(cols)}) values %s on conflict do nothing", rows)
    return len(rows)


def seed(src: DB, dst: DB, ranks: List[int], universe: str = TOP1000_CODE) -> List[tuple]:
    """Copy identity + universe rows for `ranks` from Supabase into the runner-local PG.
    Returns [(rank, company_id, symbol)] in rank order."""
    with src.trail.cursor() as cur:
        cur.execute("""select u.rank, c.company_id, coalesce(c.nse_symbol, c.screener_slug)
                       from equity.universe u join equity.company c using (company_id)
                       where u.universe_code=%s and u.as_of=(select max(as_of) from equity.universe
                             where universe_code=%s) and u.rank = any(%s) order by u.rank""",
                    (universe, universe, ranks))
        chosen = cur.fetchall()
        with dst.conn:
            with dst.conn.cursor() as dcur:
                for _, cid, _ in chosen:
                    _copy_row_set(cur, dcur, "company", "company_id=%s", (cid,))
                    _copy_row_set(cur, dcur, "universe", "company_id=%s and universe_code=%s", (cid, universe))
    return chosen


def run_lake_runner(from_rank: int, to_rank: int, k: int, n: int, explicit: Optional[str], out_dir: Path,
                    max_minutes: float = 310, kinds: Sequence[str] = KINDS) -> int:
    from .documents import run_docs_cli
    from .pipeline import scrape_company
    from .yf import run_company as yf_company

    src = DB(os.environ["SOURCE_DATABASE_URL"])
    dst = DB()                                   # DATABASE_URL = runner-local PG
    # migrations were applied by the workflow's init-db step (re-applying fails: views are redefined across files)
    mine = runner_ranks(window_ranks(from_rank, to_rank, explicit), k, n)
    chosen = seed(src, dst, mine)
    src.close()
    log.info("runner %d/%d: ranks %s -> %d companies in universe", k, n, mine, len(chosen))
    if not chosen:
        raise SystemExit("none of this runner's ranks are in TOP1000_MCAP (run the rank workflow first)")
    export_dir = out_dir / "export"
    run_id = dst.start_run("lake-runner", {"from": from_rank, "to": to_rank, "k": k, "n": n, "ranks": mine})
    seq, failures, t0 = 0, 0, time.monotonic()
    for rank, cid, sym in chosen:
        if time.monotonic() - t0 > max_minutes * 60:
            log.warning("time budget reached; stopping before rank %d", rank)
            break
        t1 = time.monotonic()
        errs = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            fs = {"scrape": ex.submit(scrape_company, sym, run_id), "yf": ex.submit(yf_company, cid, run_id)}
            for name, f in fs.items():
                try:
                    f.result()
                except Exception as e:
                    errs[name] = f"{type(e).__name__}: {e}"
        try:
            run_docs_cli(dst, [sym], list(kinds), False, None, TOP1000_CODE, None, 1, False, False)
        except BaseException as e:               # SystemExit from the CLI helper must not kill the slice
            errs["docs"] = f"{type(e).__name__}: {e}"
        failures += bool(errs)
        seq += 1
        counts = export_tables(dst.conn, export_dir, seq, company_id=cid, symbol=sym)
        log.info("rank %d %s done in %.0fs errors=%s exported=%s", rank, sym, time.monotonic() - t1, errs or "-",
                 sum(counts.values()))
    dst.finish_run(run_id, "partial" if failures else "success", {"companies": seq, "failures": failures})
    seq += 1
    export_tables(dst.conn, export_dir, seq, tables=["scrape_run"])        # final run status (all runs)
    dst.close()
    return 0
