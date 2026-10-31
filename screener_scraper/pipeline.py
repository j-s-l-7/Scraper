"""Per-company scrape pipeline: fetch (cached) -> parse -> one DB transaction -> audit trail."""
from __future__ import annotations

import json
import logging
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus

from . import config, parse
from psycopg2.extras import Json

from .db import DB
from .http import Client, FetchError

log = logging.getLogger(__name__)

CAT_CODE = {"promoters": "promoters", "foreign_institutions": "fii", "domestic_institutions": "dii",
            "government": "government", "public": "public"}
VAL_QUERIES = ["Price to Earning-Median PE-EPS", "Price to book value-Median PBV-Book value",
               "EV Multiple-Median EV Multiple-EBITDA", "Market Cap to Sales-Median Market Cap to Sales-Sales",
               "GPM-OPM-NPM-Quarter Sales"]
VAL_COLUMNS = ["pe", "median_pe", "eps", "pbv", "median_pbv", "book_value", "ev_multiple", "median_ev_multiple",
               "ebitda", "mcap_sales", "median_mcap_sales", "sales", "gpm", "opm", "npm", "quarter_sales"]
PRICE_Q = "Price-DMA50-DMA200-Volume"


def pseudo_id(key: str) -> int:
    """Negative stand-in company_id for tasks that fail before the real id is known."""
    return -(zlib.crc32(key.encode()) % (2 ** 31 - 1) + 1)


@dataclass
class TaskResult:
    task: str
    status: str = "ok"
    http_status: Optional[int] = None
    attempts: int = 1
    rows: int = 0
    error: Optional[str] = None
    raw_path: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: List[str] = field(default_factory=list)
    exc: Optional[BaseException] = None
    fatal: bool = True

    def note(self, resp=None, err: Optional[FetchError] = None):
        if resp is not None:
            self.http_status = resp.status
            self.attempts = max(self.attempts, resp.attempts)
            self.raw_path = str(resp.path.parent if self.raw_path else resp.path)
        if err is not None:
            self.http_status = err.status or self.http_status
            self.attempts = max(self.attempts, err.attempts)
            self.errors.append(str(err))
            self.exc = self.exc or err
            self.status = "skipped" if str(err).startswith("cache miss") else "failed"

    def finish(self):
        if self.errors:
            self.error = "; ".join(self.errors[:3]) + (f" (+{len(self.errors) - 3} more)" if len(self.errors) > 3 else "")
        return self


def xhr_headers(referer: str) -> dict:
    return {"X-Requested-With": "XMLHttpRequest", "Referer": referer, "Accept": "application/json, */*"}


def page_url(slug: str, basis: str) -> str:
    q = quote(slug, safe="")
    return f"{config.BASE_URL}/company/{q}/consolidated/" if basis == "C" else f"{config.BASE_URL}/company/{q}/"


@dataclass
class BasisData:
    basis: str
    page: parse.PageData
    schedules: List[Tuple[str, str, List[parse.Row]]] = field(default_factory=list)  # (parent, section, rows)
    valuation: Dict[str, List[Tuple[date, float]]] = field(default_factory=dict)


def fetch_page(client: Client, slug: str, basis: str) -> Tuple[Optional[parse.PageData], TaskResult]:
    t = TaskResult(f"page:{basis}")
    try:
        r = client.get(page_url(slug, basis), slug, f"page_{basis}")
        t.note(r)
        return parse.parse_page(r.text), t
    except FetchError as e:
        t.note(err=e)
    except Exception as e:      # parse failure
        t.status = "failed"
        t.exc = t.exc or e
        t.errors.append(f"parse: {type(e).__name__}: {e}")
        log.exception("%s page:%s parse failed", slug, basis)
    return None, t.finish()


def fetch_schedules(client: Client, slug: str, cid: int, bd: BasisData) -> TaskResult:
    t = TaskResult(f"schedules:{bd.basis}")
    ref = page_url(slug, bd.basis)
    for parent, section in bd.page.schedule_buttons:
        url = f"{config.BASE_URL}/api/company/{cid}/schedules/?parent={quote_plus(parent)}&section={section}"
        if bd.basis == "C":
            url += "&consolidated="
        try:
            r = client.get(url, slug, f"sched_{bd.basis}_{section}_{parent}", headers=xhr_headers(ref))
            t.note(r)
            fy = bd.page.bs_fy_month if section == "balance-sheet" else None
            rows = parse.parse_schedule_json(r.text, section, parent, bd.page.ttm_end, fy)
            bd.schedules.append((parent, section, rows))
            t.rows += sum(len(x.values) for x in rows)
        except FetchError as e:
            t.note(err=e)
        except Exception as e:
            t.status = "failed"
            t.exc = t.exc or e
            t.errors.append(f"parse {section}/{parent}: {type(e).__name__}: {e}")
    return t.finish()


def fetch_chart(client: Client, slug: str, cid: int, q: str, days: int, consolidated: bool, name: str, ref: str):
    url = f"{config.BASE_URL}/api/company/{cid}/chart/?q={quote_plus(q)}&days={days}"
    if consolidated:
        url += "&consolidated=true"
    return client.get(url, slug, name, headers=xhr_headers(ref))


def process_company(db: DB, client: Client, run_id: int, symbol: str, isin: Optional[str] = None) -> Tuple[bool, dict]:
    """Scrape one company. Never raises. Returns (all_tasks_ok, rows_written_by_table)."""
    ok, rows, _tasks = process_company_tasks(db, client, run_id, symbol, isin)
    return ok, rows


def process_company_tasks(db: DB, client: Client, run_id: int, symbol: str, isin: Optional[str] = None
                          ) -> Tuple[bool, dict, List[TaskResult]]:
    """Like process_company but also returns the TaskResults (with `.exc`). Never raises."""
    slug = symbol
    as_of = client.cache_date
    tasks: List[TaskResult] = []
    rows_out: Dict[str, int] = {}
    known = db.company_id_for(symbol)
    fallback_id = known if known is not None else pseudo_id(symbol)

    def record(cid: int):
        for t in tasks:
            try:
                db.record_task(run_id, cid, t.task, t.status, http_status=t.http_status, attempts=t.attempts,
                               rows_written=t.rows, error=t.error, raw_path=t.raw_path, started_at=t.started_at)
            except Exception:
                log.exception("could not record task %s %s", symbol, t.task)

    try:
        pc, tc = fetch_page(client, slug, "C")
        tasks.append(tc)
        ps, ts = fetch_page(client, slug, "S")
        tasks.append(ts)
        if pc is None and ps is None:
            record(fallback_id)
            return False, rows_out, tasks
        has_c: Optional[bool] = pc.is_consolidated if pc is not None else None
        primary = pc if has_c else (ps or pc)
        cid = primary.info.company_id
        bases: List[BasisData] = []
        if has_c:
            bases.append(BasisData("C", pc))
        if ps is not None:
            bases.append(BasisData("S", ps))
        if not bases:
            bases.append(BasisData("S", primary))
        for t in (tc, ts):
            b = t.task[-1]
            bd = next((x for x in bases if x.basis == b), None)
            t.rows = sum(len(r.values) for rows in bd.page.statements.values() for r in rows) if bd else 0

        for bd in bases:
            tasks.append(fetch_schedules(client, slug, cid, bd))

        # shareholders (basis independent -> primary page)
        th = TaskResult("shareholders")
        tasks.append(th)
        holder_rows: List[Tuple[str, str, str, Optional[str], date, float]] = []
        ref = page_url(slug, "C" if has_c else "S")
        for freq in ("quarterly", "yearly"):
            for cat in primary.holder_buttons.get(freq, []):
                url = f"{config.BASE_URL}/api/3/{cid}/investors/{cat}/{freq}/"
                try:
                    r = client.get(url, slug, f"inv_{freq}_{cat}", headers=xhr_headers(ref))
                    th.note(r)
                    for name, purl, d, v in parse.parse_investors_json(r.text):
                        holder_rows.append((freq[0].upper(), CAT_CODE.get(cat, cat), name, purl, d, v))
                except FetchError as e:
                    th.note(err=e)
                except Exception as e:
                    th.status = "failed"
                    th.exc = th.exc or e
                    th.errors.append(f"parse {cat}/{freq}: {type(e).__name__}: {e}")
        th.rows = len(holder_rows)
        th.finish()

        # charts: price (once, consolidated if available)
        tp = TaskResult("charts:price")
        tasks.append(tp)
        prices: List[Tuple[str, dict]] = []
        for freq, days in (("D", 365),):      # weekly chart dropped: yfinance daily history replaces it
            try:
                r = fetch_chart(client, slug, cid, PRICE_Q, days, bool(has_c), f"chart_price_{freq}", ref)
                tp.note(r)
                prices += [(freq, row) for row in parse.parse_price_chart(r.text)]
            except FetchError as e:
                tp.note(err=e)
            except Exception as e:
                tp.status = "failed"
                tp.exc = tp.exc or e
                tp.errors.append(f"parse price {freq}: {type(e).__name__}: {e}")
        tp.rows = len(prices)
        tp.finish()

        # charts: valuation per basis
        for bd in bases:
            tv = TaskResult(f"charts:{bd.basis}")
            tasks.append(tv)
            for q in VAL_QUERIES:
                try:
                    r = fetch_chart(client, slug, cid, q, 10000, bd.basis == "C", f"chart_{bd.basis}_{q[:40]}",
                                    page_url(slug, bd.basis))
                    tv.note(r)
                    for series, pts in parse.parse_chart_series(r.text).items():
                        bd.valuation.setdefault(series, []).extend(pts)
                except FetchError as e:
                    tv.note(err=e)
                except Exception as e:
                    tv.status = "failed"
                    tv.exc = tv.exc or e
                    tv.errors.append(f"parse chart {q}: {type(e).__name__}: {e}")
            tv.rows = sum(len(v) for v in bd.valuation.values())
            tv.finish()

        # ───── single transaction for this company ─────
        td = TaskResult("db")
        tasks.append(td)
        try:
            rows_out = db.run_tx(lambda cur: write_company(
                db, cur, run_id, as_of, slug, isin, has_c, primary, pc, ps, bases, holder_rows, prices))
            td.rows = sum(rows_out.values())
        except Exception as e:
            log.exception("%s: DB write failed (rolled back)", symbol)
            td.status = "failed"
            td.exc = td.exc or e
            td.errors.append(f"{type(e).__name__}: {e}")
            rows_out = {}
        td.finish()
        record(cid)
        ok = all(t.status != "failed" for t in tasks)
        return ok, rows_out, tasks
    except Exception as e:      # defensive: never abort the run
        log.exception("%s: unexpected failure", symbol)
        tasks.append(TaskResult("unexpected", "failed", error=f"{type(e).__name__}: {e}", exc=e))
        record(fallback_id)
        return False, rows_out, tasks


def pivot_valuation(valuation: Dict[str, List[Tuple[date, float]]]) -> List[tuple]:
    """{series: [(date, value)]} -> wide rows (obs_date, <VAL_COLUMNS...>, extra_json|None) for valuation_history."""
    wide: Dict[date, Dict[str, float]] = {}
    for series, pts in valuation.items():
        for d, v in pts:
            wide.setdefault(d, {})[series] = v
    out = []
    for d in sorted(wide):
        sv = wide[d]
        extra = {k: v for k, v in sv.items() if k not in VAL_COLUMNS}
        out.append((d,) + tuple(sv.get(c) for c in VAL_COLUMNS) + (Json(extra) if extra else None,))
    return out


def write_company(db: DB, cur, run_id: int, as_of: date, slug: str, isin: Optional[str], has_c: Optional[bool],
                  primary: parse.PageData, pc, ps, bases: List[BasisData], holder_rows, prices) -> Dict[str, int]:
    info = primary.info
    cid = info.company_id
    wh = (pc.info.warehouse_id if pc is not None and has_c else None) or (ps.info.warehouse_id if ps else None) \
        or info.warehouse_id
    db.upsert_company(cur, info, slug, has_c, isin=isin, warehouse_id=wh)
    n: Dict[str, int] = {"company": 1}

    facts: List[tuple] = []
    growth: List[tuple] = []
    val: List[tuple] = []
    for bd in bases:
        b = bd.basis
        for section, rows in bd.page.statements.items():
            for r in rows:
                mid = db.metric_id(cur, section, r.label, None, r.unit, r.order)
                facts += [(cid, b, mid, pt, pe, v, run_id) for pt, pe, v in r.values]
        for parent, section, rows in bd.schedules:
            pid = db.metric_id(cur, section, parent, None, parse.unit_for(parent, []))
            for r in rows:
                mid = db.metric_id(cur, section, r.label, pid, r.unit, r.order)
                facts += [(cid, b, mid, pt, pe, v, run_id) for pt, pe, v in r.values]
        growth += [(cid, b, as_of, g.measure, g.horizon, g.value_pct, run_id) for g in bd.page.growth]
        val += [(cid, b) + r for r in pivot_valuation(bd.valuation)]
    n["financial_fact"] = db.upsert_facts(cur, facts)
    n["growth_summary"] = db.upsert_growth(cur, growth)
    n["valuation_history"] = db.upsert_valuation(cur, val)
    n["market_snapshot"] = db.upsert_snapshot(cur, cid, as_of, primary.top_ratios, run_id)
    n["shareholding"] = db.upsert_shareholding(
        cur, [(cid, s.frequency, s.category, s.period_end, s.value, run_id) for s in primary.shareholding])
    if holder_rows:
        ids = db.holder_ids(cur, [(parse.holder_key(nm, u), nm, u) for _, _, nm, u, _, _ in holder_rows])
        n["shareholding_holder"] = db.upsert_shareholding_holder(
            cur, [(cid, f, cat, ids[parse.holder_key(nm, u)], d, v, run_id) for f, cat, nm, u, d, v in holder_rows])
    n["price_history"] = db.upsert_prices(
        cur, [(cid, f, r["trade_date"], r["close"], r["dma50"], r["dma200"], r["volume"], r["delivery_pct"])
              for f, r in prices])
    n["document_link"] = db.upsert_documents(
        cur, [(cid, d.doc_type, d.url, d.title, d.period_label, d.source) for d in primary.documents])
    return n


# ───────────────────────────── drivers ─────────────────────────────
def _finish(db: DB, run_id: int, stats: dict, client: Client, t0: float, failed: int) -> str:
    stats["requests"] = client.requests_made
    stats["cache_hits"] = client.cache_hits
    stats["seconds"] = round(time.monotonic() - t0, 1)
    status = "partial" if failed else "success"
    db.finish_run(run_id, status, stats)
    return status


# ───────────────────────────── worker entry point ─────────────────────────────
_shared: Dict[str, Client] = {}
_shared_lock = threading.Lock()


def shared_client(force: bool = False) -> Client:
    """Process-wide Client (so every job in a worker shares ONE rate limiter and its adaptive interval).
    force=True returns a cache-bypassing view that shares the same limiter."""
    with _shared_lock:
        base = _shared.get("base")
        if base is None or base.cache_date != date.today():
            old = base.limiter if base else None
            base = _shared["base"] = Client()
            if old is not None:
                base.limiter = old
        if not force:
            return base
        c = Client(use_cache=False)
        c.limiter = base.limiter
        return c


def _raise_for_tasks(tasks: List[TaskResult]) -> None:
    """Raise the first failed task's exception in a form the rule engine can classify:
    HTTP -> HTTPFailure/FetchError (.status_code), network -> requests.ConnectionError, structural -> ParseError,
    DB errors -> the original psycopg2 exception."""
    import requests
    for t in tasks:
        if t.status != "failed":
            continue
        e = t.exc
        if isinstance(e, FetchError):
            if e.status_code is None:
                raise requests.ConnectionError(str(e)) from e
            raise e
        if isinstance(e, (parse.ParseError, requests.RequestException)):
            raise e
        if t.task == "db" and e is not None:
            raise e
        if e is not None and t.task != "unexpected":
            raise parse.ParseError(f"{t.task}: {t.error}") from e
        if e is not None:
            raise e
        raise RuntimeError(f"{t.task} failed: {t.error}")


def scrape_company(company_or_symbol, run_id: int, force: bool = False, *, db: Optional[DB] = None,
                   client: Optional[Client] = None) -> dict:
    """Per-company entry point for the job worker. `company_or_symbol` is a company_id (int) or an NSE symbol /
    screener slug (str). Fetches both bases, schedules, shareholders, charts, writes one transaction per company
    and records scrape_task rows under `run_id`. `force` bypasses the raw cache (resume checks are the queue's job).

    Returns a stats dict. RAISES on any failed task (after recording it): requests.HTTPError subclasses
    (FetchError / HTTPFailure, `.status_code` 429/403/404/5xx), requests.ConnectionError for network failures,
    parse.ParseError for structural parse problems, psycopg2 errors for DB problems."""
    own_db = db is None
    db = db or DB()
    try:
        if isinstance(company_or_symbol, int) or str(company_or_symbol).isdigit():
            with db.trail.cursor() as cur:
                cur.execute("select screener_slug, isin from equity.company where company_id=%s",
                            (int(company_or_symbol),))
                r = cur.fetchone()
            if r is None:
                raise LookupError(f"company_id {company_or_symbol} not in equity.company")
            symbol, isin = r
        else:
            symbol = str(company_or_symbol)
            with db.trail.cursor() as cur:
                cur.execute("select isin from equity.company where screener_slug=%s or nse_symbol=%s", (symbol, symbol))
                r = cur.fetchone()
            isin = r[0] if r else None
        client = client or shared_client(force)
        req0, t0 = client.requests_made, time.monotonic()
        ok, rows, tasks = process_company_tasks(db, client, run_id, symbol, isin)
        stats = {"symbol": symbol, "ok": ok, "rows": rows, "requests": client.requests_made - req0,
                 "seconds": round(time.monotonic() - t0, 1), "tasks": {t.task: t.status for t in tasks}}
        if not ok:
            _raise_for_tasks(tasks)
        return stats
    finally:
        if own_db:
            db.close()


def shard_items(items: List[str], shard: Optional[Tuple[int, int]]) -> List[str]:
    """--symbols sharding: sorted symbol i belongs to shard (i % n) + 1."""
    items = sorted(items)
    if not shard:
        return items
    k, n = shard
    return [s for i, s in enumerate(items) if i % n == k - 1]


def shard_ranked(rows, shard: Optional[Tuple[int, int]]):
    """Universe sharding: rows are (rank, ...); keep (rank - 1) % n == k - 1."""
    if not shard:
        return list(rows)
    k, n = shard
    return [r for r in rows if (r[0] - 1) % n == k - 1]


def run_scrape(db: DB, symbols: List[str], *, force: bool = False, isins: Optional[Dict[str, str]] = None,
               params: Optional[dict] = None, workers: int = 1) -> str:
    """Scrape companies. `workers` threads share ONE rate limiter (global min interval), so extra threads only
    overlap latency. Each worker thread has its own DB connections; each company is one transaction."""
    client = Client(use_cache=not force)
    workers = max(1, workers)
    run_id = db.start_run("scrape", {"symbols": symbols, "force": force, "workers": workers, **(params or {})})
    stats = {"companies_ok": 0, "companies_failed": 0, "companies_skipped": 0, "rows": {}, "failed": []}
    t0 = time.monotonic()
    log.info("run %d: scraping %d companies with %d worker(s)", run_id, len(symbols), workers)
    lock = threading.Lock()
    local = threading.local()
    opened: List[DB] = []
    done = [0]

    def thread_db() -> DB:
        if workers == 1:
            return db
        d = getattr(local, "db", None)
        if d is None:
            d = local.db = DB(db.url)
            with lock:
                opened.append(d)
        return d

    def work(sym: str) -> None:
        try:
            tdb = thread_db()
            cid = tdb.company_id_for(sym)
            if not force and cid is not None and tdb.recently_done(cid, config.RESUME_HOURS):
                with lock:
                    stats["companies_skipped"] += 1
                    done[0] += 1
                    log.info("[%d/%d] %s skipped (scraped within %dh)", done[0], len(symbols), sym, config.RESUME_HOURS)
                return
            req0, t1 = client.requests_made, time.monotonic()
            ok, rows = process_company(tdb, client, run_id, sym, (isins or {}).get(sym))
        except Exception:
            log.exception("%s: worker crashed", sym)
            ok, rows, req0, t1 = False, {}, client.requests_made, time.monotonic()
        with lock:
            done[0] += 1
            for k, v in rows.items():
                stats["rows"][k] = stats["rows"].get(k, 0) + v
            stats["companies_ok" if ok else "companies_failed"] += 1
            if not ok:
                stats["failed"].append(sym)
            log.info("[%d/%d] %s %s: %.0fs, rows=%s (requests so far %d, interval %.1fs)", done[0], len(symbols), sym,
                     "ok" if ok else "PARTIAL/FAILED", time.monotonic() - t1, sum(rows.values()),
                     client.requests_made, client.limiter.interval)

    try:
        if workers == 1:
            for sym in symbols:
                work(sym)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(work, symbols))
    finally:
        for d in opened:
            d.close()
    status = _finish(db, run_id, stats, client, t0, stats["companies_failed"])
    log.info("run %d finished: %s %s", run_id, status, json.dumps({k: v for k, v in stats.items() if k != "rows"}))
    return status


def run_reparse(db: DB, day: Optional[date] = None) -> str:
    if day is None:
        days = sorted(p.name for p in config.RAW_DIR.glob("????-??-??") if p.is_dir())
        if not days:
            raise SystemExit("no raw cache found under data/raw")
        day = date.fromisoformat(days[-1])
    root = config.RAW_DIR / day.isoformat()
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
                  and ((p / "page_C.gz").exists() or (p / "page_S.gz").exists()))
    known = db.slug_map()           # safe_name(slug) -> real slug (e.g. 'M_M' -> 'M&M')
    slugs = [known.get(d, d) for d in dirs]
    client = Client(cache_date=day, offline=True)
    run_id = db.start_run("reparse", {"date": day.isoformat(), "slugs": slugs})
    stats = {"companies_ok": 0, "companies_failed": 0, "rows": {}, "failed": []}
    t0 = time.monotonic()
    for sym in slugs:
        ok, rows = process_company(db, client, run_id, sym)
        for k, v in rows.items():
            stats["rows"][k] = stats["rows"].get(k, 0) + v
        stats["companies_ok" if ok else "companies_failed"] += 1
        if not ok:
            stats["failed"].append(sym)
        log.info("reparse %s: %s rows=%s", sym, "ok" if ok else "FAILED", sum(rows.values()))
    assert client.requests_made == 0, "reparse must not touch the network"
    return _finish(db, run_id, stats, client, t0, stats["companies_failed"])
