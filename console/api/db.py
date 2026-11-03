"""Read-only Postgres access: small pool, keepalives, reconnect on dropped connections, TTL cache.

Two targets: "lake" (local Postgres, every company incl. the top 100; the default for data pages) and
"supabase" (the top-100 store plus the job queue and storage guard; used by Ops). CONSOLE_DB=supabase
points the data pages back at Supabase."""
from __future__ import annotations

import datetime as dt
import decimal
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

import psycopg2
import psycopg2.extras
import psycopg2.pool

from screener_scraper import config
from screener_scraper.db import is_conn_error

log = logging.getLogger("console.db")
KEEPALIVES = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
CACHE_TTL = 60.0

_pools: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
_pool_lock = threading.Lock()


def default_target() -> str:
    config.load_env()
    return "supabase" if os.environ.get("CONSOLE_DB") == "supabase" else "lake"


def _url(target: str) -> str:
    if target == "lake":
        from screener_scraper.lake import lake_url
        return lake_url()
    return config.database_url(session=True)


def _new_pool(target: str) -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        1, 4, _url(target), connect_timeout=15,
        options="-c default_transaction_read_only=on", **KEEPALIVES)


def _get_pool(target: str) -> psycopg2.pool.ThreadedConnectionPool:
    with _pool_lock:
        if target not in _pools:
            _pools[target] = _new_pool(target)
        return _pools[target]


def reset_pool(target: str) -> None:
    with _pool_lock:
        pool = _pools.pop(target, None)
    if pool is not None:
        try:
            pool.closeall()
        except Exception:
            pass


def clean(v: Any) -> Any:
    """Make a DB value JSON-ready: Decimal -> float, dates -> ISO strings."""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, list):
        return [clean(x) for x in v]
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    return v


@contextmanager
def _conn(target: str, timeout_ms: int | None = None):
    pool = _get_pool(target)
    conn = pool.getconn()
    ok = True
    try:
        if conn.closed:
            raise psycopg2.InterfaceError("connection already closed")
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:  # Supabase's pooler drops startup options, so set the timeout per session
            cur.execute("set statement_timeout = %d" % int(timeout_ms or 60000))
        yield conn
    except psycopg2.Error as e:   # a dead pooled connection can surface as a bare DatabaseError
        ok = not (is_conn_error(e) or conn.closed)
        raise
    finally:
        try:
            pool.putconn(conn, close=not ok or conn.closed)
        except Exception:
            pass


def query(sql: str, params: tuple | list | dict | None = None, *, raw: bool = False, target: str | None = None,
          timeout_ms: int | None = None) -> list[dict]:
    """Run a parameterised SELECT. Connection-level errors reset the pool and retry (3 attempts); SQL errors
    (incl. statement timeout) raise at once."""
    target = target or default_target()
    last: Exception | None = None
    for attempt in range(3):
        try:
            with _conn(target, timeout_ms) as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
            return rows if raw else [clean(dict(r)) for r in rows]
        except psycopg2.Error as e:
            if not is_conn_error(e):
                raise
            last = e
            log.warning("db connection problem (%s), retry %d", type(e).__name__, attempt + 1)
            if attempt == 0:
                reset_pool(target)   # one idle connection dead means the rest are too
            time.sleep(0.3 * (attempt + 1))
    assert last is not None
    raise last


def one(sql: str, params=None, *, target: str | None = None, timeout_ms: int | None = None) -> dict | None:
    rows = query(sql, params, target=target, timeout_ms=timeout_ms)
    return rows[0] if rows else None


_cache: dict[Any, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def cached(key: Any, fn: Callable[[], Any], ttl: float = CACHE_TTL) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        if len(_cache) > 800:
            _cache.clear()
        _cache[key] = (time.monotonic(), val)
    return val


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def put(key: Any, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), val)
