"""Postgres access (psycopg2): upserts via execute_values, metric / holder get-or-create, run trail."""
from __future__ import annotations

import json
import logging
import functools
import socket
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json, execute_values

from . import config

log = logging.getLogger(__name__)


KEEPALIVES = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
RECONNECT_BACKOFF = (2, 5, 15)          # seconds before reconnect attempt 1, 2, 3
_CONN_MSGS = ("connection already closed", "server closed the connection", "ssl connection has been closed",
              "terminating connection", "connection reset", "connection timed out", "could not connect",
              "no connection to the server", "eof detected", "connection not open", "broken pipe",
              "could not receive data", "could not send data")


def is_conn_error(e: BaseException) -> bool:
    """True for a dropped / broken connection (worth reconnecting), False for SQL errors (disk full, constraint, ...)."""
    if isinstance(e, psycopg2.InterfaceError):
        return True
    if type(e).__module__ == "psycopg2.errors":      # specific SQLSTATE class: only shutdown/connection ones
        E = psycopg2.errors
        return isinstance(e, (E.AdminShutdown, E.CrashShutdown, E.CannotConnectNow, E.ConnectionException,
                              E.ConnectionFailure, E.ConnectionDoesNotExist))
    if isinstance(e, psycopg2.OperationalError):
        code = getattr(e, "pgcode", None)
        if code and not (code.startswith("08") or code.startswith("57P")):
            return False                 # e.g. 53100 disk full, 25006 read-only: reconnecting will not help
        return True
    return any(m in str(e).lower() for m in _CONN_MSGS) and isinstance(e, (OSError, psycopg2.Error))


def _trail_retry(fn):
    """Run a trail-connection method; on a broken connection reconnect (2s, 5s, 15s backoff) and call it again."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        for i in range(len(RECONNECT_BACKOFF) + 1):
            try:
                return fn(self, *a, **kw)
            except Exception as e:
                if not is_conn_error(e) or i == len(RECONNECT_BACKOFF):
                    raise
                log.warning("trail connection lost in %s (%s): reconnecting in %ds", fn.__name__,
                            type(e).__name__, RECONNECT_BACKOFF[i])
                time.sleep(RECONNECT_BACKOFF[i])
                self.reconnect("trail")
    return wrapper


def _dedupe(rows: Sequence[tuple], pk_idx: Sequence[int]) -> List[tuple]:
    """execute_values + ON CONFLICT DO UPDATE fails if one batch hits the same key twice; keep the last."""
    d = {}
    for r in rows:
        d[tuple(r[i] for i in pk_idx)] = r
    return list(d.values())


class DB:
    def __init__(self, url: Optional[str] = None):
        self.url = url or config.database_url()
        self._conn = self._connect(autocommit=False)    # data connection: one tx per company
        self._trail = self._connect(autocommit=True)    # audit connection: autocommit
        self._metric: Dict[Tuple[str, str, Optional[int]], int] = {}
        self._holder: Dict[str, int] = {}

    def _connect(self, autocommit: bool):
        last = None
        for i in range(len(RECONNECT_BACKOFF) + 1):
            try:
                c = psycopg2.connect(self.url, connect_timeout=20, **KEEPALIVES)
                c.autocommit = autocommit
                return c
            except psycopg2.OperationalError as e:
                last = e
                if i < len(RECONNECT_BACKOFF):
                    log.warning("DB connect failed (%s): retry in %ds", e, RECONNECT_BACKOFF[i])
                    time.sleep(RECONNECT_BACKOFF[i])
        raise last

    def reconnect(self, which: str = "both") -> None:
        """Replace a dead connection ('conn', 'trail' or 'both'). Metric/holder caches are dropped because ids
        created inside a rolled-back transaction would be stale."""
        if which in ("conn", "both"):
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._connect(autocommit=False)
            self._metric.clear()
            self._holder.clear()
        if which in ("trail", "both"):
            try:
                self._trail.close()
            except Exception:
                pass
            self._trail = self._connect(autocommit=True)

    @property
    def conn(self):
        """Data connection; transparently replaced if it was closed."""
        if self._conn.closed:
            self.reconnect("conn")
        return self._conn

    @property
    def trail(self):
        """Autocommit audit connection; transparently replaced if it was closed."""
        if self._trail.closed:
            self.reconnect("trail")
        return self._trail

    def run_tx(self, fn, retries: int = 1):
        """Run fn(cur) in one transaction on the data connection. If the connection breaks mid-write the whole
        transaction is re-run from the start on a fresh connection (at most `retries` times, with backoff)."""
        for attempt in range(retries + 1):
            try:
                with self.conn:
                    with self.conn.cursor() as cur:
                        return fn(cur)
            except Exception as e:
                if not is_conn_error(e) or attempt == retries:
                    raise
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                log.warning("data connection lost mid-transaction (%s): reconnecting in %ds and retrying",
                            type(e).__name__, delay)
                time.sleep(delay)
                self.reconnect("conn")

    def close(self) -> None:
        for c in (self._conn, self._trail):
            try:
                c.close()
            except Exception:
                pass

    # ───────── schema ─────────
    def init_db(self) -> List[str]:
        applied = []
        for f in sorted(config.SQL_DIR.glob("*.sql")):
            with self.conn, self.conn.cursor() as cur:
                cur.execute(f.read_text())
            applied.append(f.name)
        return applied

    # ───────── run trail (autocommit connection, survives company rollbacks) ─────────
    @_trail_retry
    def start_run(self, command: str, params: dict) -> int:
        with self.trail.cursor() as cur:
            cur.execute("insert into equity.scrape_run(command, params, host) values (%s,%s,%s) returning run_id",
                        (command, Json(params), socket.gethostname()))
            return cur.fetchone()[0]

    @_trail_retry
    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        with self.trail.cursor() as cur:
            cur.execute("update equity.scrape_run set status=%s, stats=%s, finished_at=now() where run_id=%s",
                        (status, Json(stats), run_id))

    @_trail_retry
    def record_task(self, run_id: int, company_id: int, task: str, status: str, *, http_status=None,
                    attempts: int = 1, rows_written: int = 0, error: Optional[str] = None,
                    raw_path: Optional[str] = None, started_at: Optional[datetime] = None) -> None:
        with self.trail.cursor() as cur:
            cur.execute(
                """insert into equity.scrape_task(run_id, company_id, task, status, http_status, attempts,
                       rows_written, error, raw_path, started_at, finished_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, now()),now())
                   on conflict (run_id, company_id, task) do update set status=excluded.status,
                       http_status=excluded.http_status, attempts=excluded.attempts,
                       rows_written=excluded.rows_written, error=excluded.error, raw_path=excluded.raw_path,
                       finished_at=now()""",
                (run_id, company_id, task, status, http_status, max(attempts, 1), rows_written,
                 (error or None) and error[:2000], raw_path, started_at))

    @_trail_retry
    def recently_done(self, company_id: int, hours: int) -> bool:
        """True if the company's latest 'scrape' run within `hours` had only ok/skipped tasks."""
        with self.trail.cursor() as cur:
            cur.execute(
                """with last as (
                       select t.run_id from equity.scrape_task t join equity.scrape_run r using (run_id)
                       where t.company_id=%s and r.command='scrape'
                         and t.started_at > now() - make_interval(hours => %s)
                       order by t.run_id desc limit 1)
                   select count(*) filter (where status='failed'), count(*)
                   from equity.scrape_task where company_id=%s and run_id=(select run_id from last)""",
                (company_id, hours, company_id))
            failed, total = cur.fetchone()
            return total > 0 and failed == 0

    @_trail_retry
    def company_id_for(self, symbol: str) -> Optional[int]:
        with self.trail.cursor() as cur:
            cur.execute("select company_id from equity.company where screener_slug=%s or nse_symbol=%s",
                        (symbol, symbol))
            r = cur.fetchone()
            return r[0] if r else None

    @_trail_retry
    def universe_companies(self, code: str):
        """Latest snapshot of a universe: [(rank, company_id, slug, nse_symbol, bse_code, isin)] by rank."""
        with self.trail.cursor() as cur:
            cur.execute("""select u.rank, c.company_id, c.screener_slug, c.nse_symbol, c.bse_code, c.isin
                           from equity.universe u join equity.company c using (company_id)
                           where u.universe_code=%s and u.as_of=(select max(as_of) from equity.universe
                                 where universe_code=%s) order by u.rank""", (code, code))
            return cur.fetchall()

    @_trail_retry
    def slug_map(self) -> Dict[str, str]:
        from .http import safe_name
        with self.trail.cursor() as cur:
            cur.execute("select screener_slug from equity.company")
            return {safe_name(s): s for (s,) in cur.fetchall()}

    # ───────── dimensions ─────────
    def upsert_company(self, cur, info, slug: str, has_consolidated: Optional[bool],
                       isin: Optional[str] = None, warehouse_id: Optional[int] = None) -> None:
        cur.execute(
            """insert into equity.company(company_id, warehouse_id, screener_slug, name, nse_symbol, bse_code, isin,
                   sector, industry_group, industry, sub_industry, industry_code, website, about, key_points,
                   pros, cons, has_consolidated)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (company_id) do update set
                   warehouse_id = coalesce(excluded.warehouse_id, equity.company.warehouse_id),
                   screener_slug = excluded.screener_slug,
                   name = coalesce(excluded.name, equity.company.name),
                   nse_symbol = coalesce(excluded.nse_symbol, equity.company.nse_symbol),
                   bse_code = coalesce(excluded.bse_code, equity.company.bse_code),
                   isin = coalesce(excluded.isin, equity.company.isin),
                   sector = coalesce(excluded.sector, equity.company.sector),
                   industry_group = coalesce(excluded.industry_group, equity.company.industry_group),
                   industry = coalesce(excluded.industry, equity.company.industry),
                   sub_industry = coalesce(excluded.sub_industry, equity.company.sub_industry),
                   industry_code = coalesce(excluded.industry_code, equity.company.industry_code),
                   website = coalesce(excluded.website, equity.company.website),
                   about = coalesce(excluded.about, equity.company.about),
                   key_points = coalesce(excluded.key_points, equity.company.key_points),
                   pros = coalesce(excluded.pros, equity.company.pros),
                   cons = coalesce(excluded.cons, equity.company.cons),
                   has_consolidated = coalesce(excluded.has_consolidated, equity.company.has_consolidated),
                   updated_at = now()""",
            (info.company_id, warehouse_id if warehouse_id is not None else info.warehouse_id, slug, info.name,
             info.nse_symbol, info.bse_code, isin, info.sector, info.industry_group, info.industry,
             info.sub_industry, info.industry_code, info.website, info.about, info.key_points,
             info.pros or None, info.cons or None, has_consolidated))

    def metric_id(self, cur, section: str, name: str, parent_id: Optional[int], unit: str,
                  order: Optional[int] = None, source: str = "screener") -> int:
        key = (section, name, parent_id)
        if key in self._metric:
            return self._metric[key]
        sel = ("select metric_id from equity.metric where section=%s and name=%s and "
               "parent_metric_id is not distinct from %s")
        cur.execute(sel, (section, name, parent_id))
        r = cur.fetchone()
        if r is None:
            cur.execute("""insert into equity.metric(section, name, parent_metric_id, unit, display_order, source)
                           values (%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                        (section, name, parent_id, unit, order, source))
            cur.execute(sel, (section, name, parent_id))
            r = cur.fetchone()
        self._metric[key] = r[0]
        return r[0]

    def holder_ids(self, cur, holders: Iterable[Tuple[str, str, Optional[str]]]) -> Dict[str, int]:
        """holders: (holder_key, name, person_url). Returns holder_key -> holder_id (get-or-create, cached)."""
        new = {k: (k, n, u) for k, n, u in holders if k not in self._holder}
        if new:
            execute_values(cur, "insert into equity.holder(holder_key, name, person_url) values %s "
                                "on conflict (holder_key) do nothing", list(new.values()))
            cur.execute("select holder_key, holder_id from equity.holder where holder_key = any(%s)", (list(new),))
            self._holder.update(dict(cur.fetchall()))
        return self._holder

    # ───────── facts ─────────
    def _upsert(self, cur, sql: str, rows: List[tuple], pk_idx: Sequence[int], page_size: int = 2000) -> int:
        rows = _dedupe(rows, pk_idx)
        if rows:
            execute_values(cur, sql, rows, page_size=page_size)
        return len(rows)

    def upsert_facts(self, cur, rows: List[tuple]) -> int:
        """rows: (company_id, basis, metric_id, period_type, period_end, value, run_id)"""
        return self._upsert(cur, """insert into equity.financial_fact
            (company_id, basis, metric_id, period_type, period_end, value, run_id) values %s
            on conflict (company_id, basis, metric_id, period_type, period_end)
            do update set value=excluded.value, run_id=excluded.run_id, updated_at=now()""", rows, (0, 1, 2, 3, 4))

    def upsert_snapshot(self, cur, company_id: int, as_of, tr, run_id: int) -> int:
        cur.execute(
            """insert into equity.market_snapshot(company_id, as_of, market_cap_cr, price, high_52w, low_52w, pe,
                   book_value, dividend_yield_pct, roce_pct, roe_pct, face_value, extra, run_id)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (company_id, as_of) do update set market_cap_cr=excluded.market_cap_cr,
                   price=excluded.price, high_52w=excluded.high_52w, low_52w=excluded.low_52w, pe=excluded.pe,
                   book_value=excluded.book_value, dividend_yield_pct=excluded.dividend_yield_pct,
                   roce_pct=excluded.roce_pct, roe_pct=excluded.roe_pct, face_value=excluded.face_value,
                   extra=excluded.extra, run_id=excluded.run_id""",
            (company_id, as_of, tr.market_cap_cr, tr.price, tr.high_52w, tr.low_52w, tr.pe, tr.book_value,
             tr.dividend_yield_pct, tr.roce_pct, tr.roe_pct, tr.face_value, Json(tr.extra), run_id))
        return 1

    def upsert_growth(self, cur, rows: List[tuple]) -> int:
        """(company_id, basis, as_of, measure, horizon, value_pct, run_id)"""
        return self._upsert(cur, """insert into equity.growth_summary
            (company_id, basis, as_of, measure, horizon, value_pct, run_id) values %s
            on conflict (company_id, basis, as_of, measure, horizon)
            do update set value_pct=excluded.value_pct, run_id=excluded.run_id""", rows, (0, 1, 2, 3, 4))

    def upsert_shareholding(self, cur, rows: List[tuple]) -> int:
        """(company_id, frequency, category, period_end, value, run_id)"""
        return self._upsert(cur, """insert into equity.shareholding
            (company_id, frequency, category, period_end, value, run_id) values %s
            on conflict (company_id, frequency, category, period_end)
            do update set value=excluded.value, run_id=excluded.run_id""", rows, (0, 1, 2, 3))

    def upsert_shareholding_holder(self, cur, rows: List[tuple]) -> int:
        """(company_id, frequency, category, holder_id, period_end, pct, run_id)"""
        return self._upsert(cur, """insert into equity.shareholding_holder
            (company_id, frequency, category, holder_id, period_end, pct, run_id) values %s
            on conflict (company_id, frequency, category, holder_id, period_end)
            do update set pct=excluded.pct, run_id=excluded.run_id""", rows, (0, 1, 2, 3, 4))

    def upsert_prices(self, cur, rows: List[tuple]) -> int:
        """(company_id, frequency, trade_date, close, dma50, dma200, volume, delivery_pct)"""
        return self._upsert(cur, """insert into equity.price_history
            (company_id, frequency, trade_date, close, dma50, dma200, volume, delivery_pct) values %s
            on conflict (company_id, frequency, trade_date) do update set close=excluded.close,
                dma50=excluded.dma50, dma200=excluded.dma200, volume=excluded.volume,
                delivery_pct=excluded.delivery_pct""", rows, (0, 1, 2), page_size=3000)

    def upsert_valuation(self, cur, rows: List[tuple]) -> int:
        """Wide rows into equity.valuation_history:
        (company_id, basis, obs_date, <16 series columns in VAL_COLUMNS order>, extra_jsonb|None).
        A NULL in a batch never overwrites an existing value (coalesce), so partial re-fetches are safe."""
        cols = ["pe", "median_pe", "eps", "pbv", "median_pbv", "book_value", "ev_multiple", "median_ev_multiple",
                "ebitda", "mcap_sales", "median_mcap_sales", "sales", "gpm", "opm", "npm", "quarter_sales"]
        upd = ", ".join(f"{c}=coalesce(excluded.{c}, equity.valuation_history.{c})" for c in cols)
        return self._upsert(cur, f"""insert into equity.valuation_history
            (company_id, basis, obs_date, {', '.join(cols)}, extra) values %s
            on conflict (company_id, basis, obs_date) do update set {upd},
                extra=coalesce(excluded.extra, equity.valuation_history.extra)""",
                            rows, (0, 1, 2), page_size=3000)

    def upsert_documents(self, cur, rows: List[tuple]) -> int:
        """(company_id, doc_type, url, title, period_label, source)"""
        return self._upsert(cur, """insert into equity.document_link
            (company_id, doc_type, url, title, period_label, source) values %s
            on conflict (company_id, doc_type, url) do update set title=excluded.title,
                period_label=excluded.period_label, source=excluded.source""", rows, (0, 1, 2))

    def upsert_universe(self, cur, rows: List[tuple]) -> int:
        """(universe_code, as_of, company_id, rank, market_cap_cr)"""
        return self._upsert(cur, """insert into equity.universe
            (universe_code, as_of, company_id, rank, market_cap_cr) values %s
            on conflict (universe_code, as_of, company_id) do update set rank=excluded.rank,
                market_cap_cr=excluded.market_cap_cr""", rows, (0, 1, 2))

    # ───────── status ─────────
    @_trail_retry
    def status(self) -> dict:
        out: dict = {}
        with self.trail.cursor() as cur:
            cur.execute("select run_id, command, status, started_at, finished_at, stats from equity.scrape_run "
                        "order by run_id desc limit 8")
            out["runs"] = cur.fetchall()
            cur.execute("""select t.run_id, t.company_id, t.task, t.http_status, left(t.error, 120)
                           from equity.scrape_task t where t.status='failed'
                           order by t.run_id desc, t.company_id limit 25""")
            out["failures"] = cur.fetchall()
            counts = {}
            cur.execute("select tablename from pg_tables where schemaname='equity' order by 1")
            for (t,) in cur.fetchall():
                cur.execute(f'select count(*) from equity."{t}"')
                counts[t] = cur.fetchone()[0]
            out["counts"] = counts
        return out

    # ───────── yfinance layer ─────────
    def upsert_historical_prices(self, cur, rows: List[tuple]) -> int:
        """(symbol, date, open, high, low, close, adj_close, volume)"""
        return self._upsert(cur, """insert into equity.historical_prices
            (symbol, date, open, high, low, close, adj_close, volume) values %s
            on conflict (symbol, date) do update set open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, adj_close=excluded.adj_close, volume=excluded.volume""",
                            rows, (0, 1), page_size=5000)

    def upsert_corporate_actions(self, cur, rows: List[tuple]) -> int:
        """(company_id, action_type, ex_date, value, detail, source). yfinance rows win over NSE rows."""
        return self._upsert(cur, """insert into equity.corporate_action
            (company_id, action_type, ex_date, value, detail, source) values %s
            on conflict (company_id, action_type, ex_date) do update set value=excluded.value,
                detail=coalesce(excluded.detail, equity.corporate_action.detail), source=excluded.source""",
                            rows, (0, 1, 2))

    def upsert_shares(self, cur, rows: List[tuple]) -> int:
        """(company_id, as_of, shares, source)"""
        return self._upsert(cur, """insert into equity.shares_outstanding (company_id, as_of, shares, source)
            values %s on conflict (company_id, as_of) do update set shares=excluded.shares, source=excluded.source""",
                            rows, (0, 1), page_size=3000)

    def upsert_yf_snapshot(self, cur, company_id: int, as_of, ticker: str, v: dict, run_id: int) -> int:
        cols = [c for c in v if c != "info"]
        names = ["company_id", "as_of", "yf_ticker"] + cols + ["info", "run_id"]
        vals = [company_id, as_of, ticker] + [v[c] for c in cols] + [Json(v["info"]), run_id]
        upd = ", ".join(f"{c}=excluded.{c}" for c in names[3:])
        cur.execute(f"insert into equity.yf_snapshot ({', '.join(names)}) values ({', '.join(['%s'] * len(vals))}) "
                    f"on conflict (company_id, as_of) do update set {upd}", vals)
        return 1

    def upsert_estimates(self, cur, rows: List[tuple]) -> int:
        """(company_id, as_of, estimate_type, period, field, value)"""
        return self._upsert(cur, """insert into equity.analyst_estimate
            (company_id, as_of, estimate_type, period, field, value) values %s
            on conflict (company_id, as_of, estimate_type, period, field) do update set value=excluded.value""",
                            rows, (0, 1, 2, 3, 4))

    def upsert_earnings(self, cur, rows: List[tuple]) -> int:
        """(company_id, event_date, eps_estimate, eps_reported, surprise_pct, source)"""
        return self._upsert(cur, """insert into equity.earnings_event
            (company_id, event_date, eps_estimate, eps_reported, surprise_pct, source) values %s
            on conflict (company_id, event_date) do update set eps_estimate=excluded.eps_estimate,
                eps_reported=excluded.eps_reported, surprise_pct=excluded.surprise_pct""", rows, (0, 1))

    def upsert_insiders(self, cur, rows: List[tuple]) -> int:
        """(company_id, row_hash, start_date, insider, position, transaction, text, shares, value, url)"""
        return self._upsert(cur, """insert into equity.insider_transaction
            (company_id, row_hash, start_date, insider, position, transaction, text, shares, value, url) values %s
            on conflict (company_id, row_hash) do nothing""", rows, (0, 1))

    @_trail_retry
    def coverage(self, universe: Optional[str] = None):
        with self.trail.cursor() as cur:
            if universe:
                cur.execute("""select v.* from equity.v_coverage v where v.company_id in (
                                 select company_id from equity.universe where universe_code=%s and as_of=(
                                   select max(as_of) from equity.universe where universe_code=%s))
                               order by v.nse_symbol""", (universe, universe))
            else:
                cur.execute("select * from equity.v_coverage order by nse_symbol")
            return [d[0] for d in cur.description], cur.fetchall()
