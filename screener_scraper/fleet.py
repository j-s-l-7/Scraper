"""Local fleet worker: one process per role, writing DIRECTLY to the local lake (DATABASE_URL = lake).
Symbol lists come from Supabase TOP1000_MCAP (SOURCE_DATABASE_URL, read-only). Every pass is idempotent
(skips work the lake already has); `scripts/local-fleet.sh` restarts a crashed process with backoff, and its
`pause` stops every worker between companies (PAUSE_FLAG) until `resume`.

ONE process per source host so every site sees a single polite client:
  screener   screener.in, ranks 1000 -> 101 descending (also yields document_link)
  yf         Yahoo, ranks 101-1000
  bse        BSE-hosted annual reports      nse   NSE-hosted (and other-host) annual reports, after BSE was tried
  concalls   concall_transcript + concall_ppt from any host, then the wayback / company-site fallback chain
  sources    NSE/BSE archives (bhavcopy, deals, F&O ...): `python -m screener_scraper sources --daily` (see script)
Doc roles only process companies that already have document_link rows (re-scanned every 10 min).
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import List, Optional, Tuple

from . import config
from .db import DB
from .fallback import RETRY_BACKOFF_DAYS
from .lake_runner import seed
from .rank import TOP1000_CODE

log = logging.getLogger(__name__)
FIRST_RANK, LAST_RANK = 101, 32000   # every ranked company: grow_universe ranks NSE past 1000 (capped at 32000)
YF_LAST_RANK = 1000           # yf refetches each company's full price history; past 1000 prices come from the bhavcopy
SCREENER_FRESH_DAYS = 7       # one IP at ~106 s a company: a daily refresh of ~1,800 companies never finishes
RESCAN_S = 600
COMPANY_MAX_S = 40 * 60       # under the fleet watchdog's 60 min, which restarts the whole pass from rank 101
PAUSE_FLAG = config.ROOT / "data" / "fleet" / "paused"   # `scripts/local-fleet.sh pause|resume`


class CompanyTimeout(BaseException):
    """SIGALRM after COMPANY_MAX_S. A BaseException so per-document `except Exception` handlers can't swallow it."""


def _company_timeout(signum, frame):
    raise CompanyTimeout(f"over {COMPANY_MAX_S // 60} min (a download that never finishes)")


def paused() -> bool:
    return PAUSE_FLAG.exists()


def nap(seconds: float) -> None:
    """The sleep between passes, cut short by a pause."""
    end = time.monotonic() + seconds
    while not paused() and (left := end - time.monotonic()) > 0:
        time.sleep(min(15.0, left))


def source_ranks(src: DB, first: int = FIRST_RANK) -> List[Tuple[int, int, str]]:
    with src.trail.cursor() as cur:
        cur.execute("""select u.rank, c.company_id, coalesce(c.nse_symbol, c.screener_slug)
                       from equity.universe u join equity.company c using (company_id)
                       where u.universe_code=%s and u.as_of=(select max(as_of) from equity.universe
                             where universe_code=%s) and u.rank between %s and %s order by u.rank""",
                    (TOP1000_CODE, TOP1000_CODE, first, LAST_RANK))
        return cur.fetchall()


def first_rank(db: DB) -> int:
    """1 once the lake is primary for the top 100 (Supabase full, R8): the fleet then keeps them fresh too."""
    from .lake import top100_on_lake
    return 1 if top100_on_lake(db.trail) else FIRST_RANK


def select_for(role: str, ranked: List[Tuple[int, int, str]], index: int, of: int) -> List[Tuple[int, int, str]]:
    """Which (rank, company_id, symbol) a process owns. screener: descending; yf: up to YF_LAST_RANK, ascending;
    every other role: all ranks, ascending."""
    if role == "screener":
        return sorted(ranked, key=lambda r: -r[0])
    return sorted(r for r in ranked if role != "yf" or r[0] <= YF_LAST_RANK)


def lake_has_fresh_screener(db: DB, company_id: int) -> bool:
    """Scraped within SCREENER_FRESH_DAYS, or within 20 h while the latest quarter is still missing (results season)."""
    with db.trail.cursor() as cur:
        cur.execute("""select exists (select 1 from equity.financial_fact f join equity.metric m using (metric_id)
                       where f.company_id=%(c)s and m.source='screener' and f.updated_at > now() - case
                         when exists (select 1 from equity.dq_issue i where i.company_id=%(c)s and i.status='open'
                                      and i.check_code='latest_quarter_missing') then interval '20 hours'
                         else make_interval(days => %(d)s) end)""",
                    {"c": company_id, "d": SCREENER_FRESH_DAYS})
        return cur.fetchone()[0]


def has_links(db: DB, company_id: int, doc_types: List[str]) -> bool:
    with db.trail.cursor() as cur:
        cur.execute("select exists (select 1 from equity.document_link where company_id=%s and doc_type = any(%s))",
                    (company_id, doc_types))
        return cur.fetchone()[0]


def retry_due(db: DB, company_id: int, doc_types: List[str]) -> bool:
    """A missing/failed/no_text document whose fallback chain is due: fallback.chain_due in SQL. A chain run stamps all
    its attempts_log entries with one `at`; after n runs the next waits RETRY_BACKOFF_DAYS[min(n, 4) - 1] days
    (7, 14, 28, 56). 'primary' entries are the normal download's cooldown stamp (documents.py), not chain runs."""
    with db.trail.cursor() as cur:
        cur.execute("""select exists (select 1 from equity.document d
                         cross join lateral (select count(distinct e->>'at') as runs, max((e->>'at')::timestamptz) as last_at
                                             from jsonb_array_elements(d.attempts_log) e
                                             where e->>'at' is not null and e->>'source' is distinct from 'primary') a
                         where d.company_id=%s and d.doc_type = any(%s) and d.status in ('missing','failed','no_text')
                           and (a.runs = 0 or a.last_at <= now() - make_interval(
                                days => (%s::int[])[least(a.runs, %s)::int])))""",
                    (company_id, doc_types, RETRY_BACKOFF_DAYS, len(RETRY_BACKOFF_DAYS)))
        return cur.fetchone()[0]


def yf_done_today(db: DB, company_id: int) -> bool:
    with db.trail.cursor() as cur:
        cur.execute("select exists (select 1 from equity.yf_snapshot where company_id=%s and as_of=current_date)",
                    (company_id,))
        return cur.fetchone()[0]


def run_pass(role: str, index: int, of: int, db: DB, src: DB) -> dict:
    from .documents import run_docs_cli
    from .pipeline import scrape_company
    from .yf import run_company as yf_company
    ranked = source_ranks(src, first_rank(db))
    seed(src, db, [r[0] for r in ranked])   # identity + universe rows so every worker can resolve companies
    mine = select_for(role, ranked, index, of)
    run_id = db.start_run(f"fleet-{role}", {"index": index, "of": of, "companies": len(mine)})
    st = {"done": 0, "skipped": 0, "failed": 0, "ranked": len(ranked)}
    signal.signal(signal.SIGALRM, _company_timeout)
    for rank, cid, sym in mine:
        if paused():                         # between companies, never inside one
            st["paused"] = True
            log.info("[%s] paused before rank %d %s", role, rank, sym)
            break
        signal.alarm(COMPANY_MAX_S)          # a stuck socket read fails this company instead of hanging the pass
        try:
            if role == "screener":
                if lake_has_fresh_screener(db, cid):
                    st["skipped"] += 1
                    continue
                scrape_company(sym, run_id, db=db)
            elif role == "yf":
                if yf_done_today(db, cid):
                    st["skipped"] += 1
                    continue
                yf_company(cid, run_id, db=db)
            else:
                kinds = ["concall_transcript", "concall_ppt"] if role == "concalls" else ["annual_report"]
                if not has_links(db, cid, kinds):
                    st["skipped"] += 1
                    continue
                hosts = {"bse": {"bse"}, "nse": {"nse", "other"}}.get(role)
                run_docs_cli(db, [sym], kinds, False, None, TOP1000_CODE, None, 1, False, False, hosts)
                if role == "concalls" and retry_due(db, cid, kinds):   # fallback chain for docs still missing
                    run_docs_cli(db, [sym], kinds, False, None, TOP1000_CODE, None, 1, True, False, None)
            st["done"] += 1
            log.info("[%s] rank %d %s ok", role, rank, sym)
        except BaseException as e:           # one company never stops the pass (SystemExit, CompanyTimeout included)
            if isinstance(e, KeyboardInterrupt):
                raise
            st["failed"] += 1
            log.warning("[%s] rank %d %s failed: %s: %s", role, rank, sym, type(e).__name__, str(e)[:200])
        finally:
            signal.alarm(0)
    db.finish_run(run_id, "partial" if st["failed"] or st.get("paused") else "success", st)
    return st


def run_fleet_worker(role: str, index: int, of: int, once: bool = False) -> int:
    import faulthandler
    faulthandler.register(signal.SIGUSR1, all_threads=True)   # `kill -USR1 <pid>`: every thread's stack -> the log
    os.environ.setdefault("SUPABASE_TXN_POOL", "1")
    src_url = os.environ.get("SOURCE_DATABASE_URL")
    if not src_url:
        raise SystemExit("SOURCE_DATABASE_URL (Supabase, read-only) is required")
    while not paused():
        db = DB()                              # DATABASE_URL = lake
        src = DB(src_url)
        try:
            st = run_pass(role, index, of, db, src)
            log.info("[%s] pass finished: %s", role, st)
        finally:
            db.close()
            src.close()
        if once:
            return 0
        # docs roles rescan every 10 min (new document_link rows appear as scrapes land); others every 30 min
        nap(120 if not st.get("ranked") else 1800 if role in ("screener", "yf") else RESCAN_S)
    log.info("[%s] paused: exiting until scripts/local-fleet.sh resume", role)
    return 0
