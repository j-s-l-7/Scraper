"""Read-only Postgres access: small pool, keepalives, reconnect on dropped connections, TTL cache."""
from __future__ import annotations

import datetime as dt
import decimal
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

import psycopg2
import psycopg2.extras
import psycopg2.pool

from screener_scraper import config

log = logging.getLogger("console.db")
KEEPALIVES = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
CACHE_TTL = 60.0

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _new_pool() -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        1, 4, config.database_url(), connect_timeout=15,
        options="-c statement_timeout=25000 -c default_transaction_read_only=on", **KEEPALIVES)


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _new_pool()
        return _pool


def reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
        _pool = None


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
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    ok = True
    try:
        if conn.closed:
            raise psycopg2.InterfaceError("connection already closed")
        conn.set_session(readonly=True, autocommit=True)
        yield conn
    except (psycopg2.InterfaceError, psycopg2.OperationalError):
        ok = False
        raise
    finally:
        try:
            pool.putconn(conn, close=not ok or conn.closed)
        except Exception:
            pass


def query(sql: str, params: tuple | list | dict | None = None, *, raw: bool = False) -> list[dict]:
    """Run a parameterised SELECT; reconnect (twice) on InterfaceError / OperationalError."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            with _conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
            return rows if raw else [clean(dict(r)) for r in rows]
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            last = e
            log.warning("db connection problem (%s), retry %d", type(e).__name__, attempt + 1)
            if attempt == 1:
                reset_pool()
            time.sleep(0.3 * (attempt + 1))
    assert last is not None
    raise last


def one(sql: str, params=None) -> dict | None:
    rows = query(sql, params)
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
