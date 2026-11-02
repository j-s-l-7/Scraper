"""
yfinance Historical-Price Ingestion (generalized)
=================================================

Generalization of `ingest_reliance_yfinance.py` to any list of NSE
symbols. Same proven pipeline:

  fetch (yfinance)  →  build rows  →  ON CONFLICT upsert into
  historical_prices  →  verify

Idempotent — keyed on (symbol, date). Re-running refreshes today's bar
without duplicating history.

Usage
-----
    # Full Nifty 50 (uses NIFTY_50 from ingest_historical.py)
    python ingestion/ingest_yfinance.py --nifty50

    # Specific symbols
    python ingestion/ingest_yfinance.py --symbols RELIANCE,TCS

    # From a newline-separated file
    python ingestion/ingest_yfinance.py --symbols-file syms.txt

    # Shorter window (default is "max")
    python ingestion/ingest_yfinance.py --nifty50 --period 5y

Notes on Nifty 50 quirks (verified May 2026)
--------------------------------------------
- M&M.NS — yfinance accepts the ampersand verbatim.
- BAJAJ-AUTO.NS — hyphen is fine.
- ETERNAL.NS — formerly ZOMATO; yfinance carried the history forward.
- JIOFIN.NS — listed Aug 2023; ~660 rows is the full lifetime.
- TMPV.NS  — listed Oct 2025; ~130 rows is the full lifetime.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import math
import os
import sys
import time
from typing import Iterable
from dotenv import load_dotenv

load_dotenv(                    # no override: a caller's DATABASE_URL (e.g. the lake's dq remediation) wins
    os.path.join(os.path.dirname(__file__), "..", ".env"),
    override=False,
)

import yfinance as yf
import psycopg2
from psycopg2.extras import execute_values

# Reuse the canonical Nifty 50 list — single source of truth in
# data/nse_indices.json, loaded by symbol_universe.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.symbol_universe import NIFTY_50, universe_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yfIngest")

DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_SIZE = 1000
FETCH_WORKERS = 8


# ----- DB helpers -----

def get_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set in environment")
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)


# ----- yfinance helpers -----

def yfinance_ticker(symbol: str) -> str:
    """Map our internal symbol to a yfinance ticker (NSE = .NS)."""
    return f"{symbol}.NS"


def fetch_history(symbol: str, period: str) -> "yf.Ticker":
    """Fetch full OHLCV history from yfinance for a single symbol."""
    df = yf.Ticker(yfinance_ticker(symbol)).history(
        period=period, auto_adjust=False)
    if df.empty:
        return None
    df = df.reset_index()
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ----- value coercion (mirrors the proven Reliance script) -----

def _safe_float(val):
    try:
        f = float(val)
        if math.isfinite(f):
            return round(f, 2)
    except (TypeError, ValueError):
        pass
    return None


def _safe_int(val):
    try:
        f = float(val)
        if math.isfinite(f):
            return int(f)
    except (TypeError, ValueError):
        pass
    return None


def build_rows(symbol: str, df) -> list[tuple]:
    """Map a yfinance DataFrame to historical_prices upsert tuples.

    Columns: (symbol, date, open, high, low, close, adj_close, volume).
    Skipped:
      - rows where every OHLC value is NaN (no leakage)
      - rows where volume == 0 (yfinance phantom bars on NSE holidays —
        OHLC carries the prior close forward; not real trades)
    `adj_close` falls back to `close` when missing — Invariant I-7.
    """
    rows: list[tuple] = []
    skipped_nan = 0
    skipped_holiday = 0
    adj_col = "Adj Close" if "Adj Close" in df.columns else "Adj_Close"

    for _, r in df.iterrows():
        d = r["Date"]
        if hasattr(d, "date"):
            d = d.date()

        o = _safe_float(r.get("Open"))
        h = _safe_float(r.get("High"))
        lo = _safe_float(r.get("Low"))
        c = _safe_float(r.get("Close"))
        adj = _safe_float(r.get(adj_col))
        if adj is None:
            adj = c
        v = _safe_int(r.get("Volume"))

        if all(x is None for x in (o, h, lo, c)):
            skipped_nan += 1
            continue
        if v == 0:
            skipped_holiday += 1
            continue
        rows.append((symbol, d, o, h, lo, c, adj, v))

    if skipped_nan:
        logger.debug(f"[{symbol}] skipped {skipped_nan} all-NaN rows")
    if skipped_holiday:
        logger.debug(f"[{symbol}] skipped {skipped_holiday} volume=0 holiday rows")
    return rows


# ----- upsert -----

UPSERT_SQL = """
INSERT INTO equity.historical_prices
    (symbol, date, open, high, low, close, adj_close, volume)
VALUES %s
ON CONFLICT (symbol, date) DO UPDATE SET
    open      = EXCLUDED.open,
    high      = EXCLUDED.high,
    low       = EXCLUDED.low,
    close     = EXCLUDED.close,
    adj_close = EXCLUDED.adj_close,
    volume    = EXCLUDED.volume
"""


def upsert_rows(conn, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur, UPSERT_SQL, rows, page_size=BATCH_SIZE)


# ----- per-symbol summary -----

def db_summary(conn, symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, COUNT(*), MIN(date), MAX(date),
                   COUNT(*) FILTER
                       (WHERE open IS NULL OR high IS NULL
                          OR low IS NULL OR close IS NULL)
            FROM equity.historical_prices
            WHERE symbol = ANY(%s)
            GROUP BY symbol
            """,
            (symbols,),
        )
        for sym, n, mn, mx, null_ohlc in cur.fetchall():
            out[sym] = {"rows": n, "first": mn, "last": mx,
                        "null_ohlc": null_ohlc}
    return out


# ----- driver -----

def run(symbols: Iterable[str], period: str) -> dict:
    symbols = list(symbols)
    logger.info(f"Ingesting {len(symbols)} symbols (period={period}) "
                f"via yfinance into historical_prices")

    fetched: dict[str, object] = {}
    failures: dict[str, str] = {}

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futs = {ex.submit(fetch_history, s, period): s for s in symbols}
        for i, fut in enumerate(cf.as_completed(futs), start=1):
            sym = futs[fut]
            try:
                df = fut.result()
                if df is None or len(df) == 0:
                    failures[sym] = "empty"
                    logger.warning(f"[{i}/{len(symbols)}] {sym}: empty")
                    continue
                fetched[sym] = df
                logger.info(f"[{i}/{len(symbols)}] {sym}: {len(df):,} rows "
                            f"({df['Date'].min().date()} → "
                            f"{df['Date'].max().date()})")
            except Exception as exc:
                failures[sym] = str(exc)[:120]
                logger.error(f"[{i}/{len(symbols)}] {sym}: {exc}")
    fetch_secs = time.time() - t0

    # Serial upsert through one connection — keeps DB tx surface small
    # and gives us deterministic per-symbol error handling.
    t0 = time.time()
    inserted: dict[str, int] = {}
    conn = get_connection()
    try:
        for sym, df in fetched.items():
            try:
                rows = build_rows(sym, df)
                upsert_rows(conn, rows)
                conn.commit()
                inserted[sym] = len(rows)
            except Exception as exc:
                conn.rollback()
                failures[sym] = f"upsert: {exc}"
                logger.error(f"[{sym}] upsert failed: {exc}")
        # Verify final state in the DB for everything we tried.
        summary = db_summary(conn, list(symbols))
    finally:
        conn.close()
    upsert_secs = time.time() - t0

    return {
        "symbols":   symbols,
        "fetched":   list(fetched.keys()),
        "inserted":  inserted,
        "failures":  failures,
        "summary":   summary,
        "fetch_s":   round(fetch_secs, 1),
        "upsert_s":  round(upsert_secs, 1),
    }


def _print_report(r: dict) -> None:
    print()
    print("=" * 78)
    print(f"{'symbol':<12} {'rows in DB':>10} {'first':<11} "
          f"{'last':<11} {'null OHLC':>9}  status")
    print("-" * 78)
    for sym in r["symbols"]:
        s = r["summary"].get(sym)
        if not s:
            status = r["failures"].get(sym, "no rows")
            print(f"{sym:<12} {'-':>10} {'-':<11} {'-':<11} {'-':>9}  "
                  f"FAIL ({status})")
            continue
        ok = "OK" if s["null_ohlc"] == 0 else "WARN: NULLs"
        print(f"{sym:<12} {s['rows']:>10,} {str(s['first']):<11} "
              f"{str(s['last']):<11} {s['null_ohlc']:>9}  {ok}")
    print("-" * 78)
    print(f"fetched: {len(r['fetched'])}/{len(r['symbols'])}, "
          f"failures: {len(r['failures'])}, "
          f"fetch={r['fetch_s']}s, upsert={r['upsert_s']}s")
    if r["failures"]:
        print("failures:")
        for sym, why in r["failures"].items():
            print(f"  {sym}: {why}")
    print("=" * 78)


def main(argv: list[str]) -> int:
    from screener_scraper.jobs import overflow_for_url
    why = overflow_for_url(os.getenv("DATABASE_URL"))
    if why:
        logger.warning("Supabase overflow (%s): nothing written, the lake is primary for the top 100", why)
        return 0
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nifty50", action="store_true",
                   help="ingest the canonical Nifty 50 list")
    p.add_argument("--universe", type=str, metavar="CODE",
                   help="Nifty 50 + latest equity.universe snapshot, e.g. TOP100_MCAP")
    p.add_argument("--symbols", type=str,
                   help="comma-separated symbol list, e.g. RELIANCE,TCS")
    p.add_argument("--symbols-file", type=str,
                   help="path to newline-separated symbol file")
    p.add_argument("--period", type=str, default="max",
                   help='yfinance period (default "max"; also "10y","5y",…)')
    args = p.parse_args(argv[1:])

    if args.universe:
        symbols = universe_from_db(args.universe)
    elif args.nifty50:
        symbols = list(NIFTY_50)
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbols_file:
        with open(args.symbols_file) as f:
            symbols = [ln.strip() for ln in f if ln.strip()
                        and not ln.lstrip().startswith("#")]
    else:
        p.error("specify one of --universe / --nifty50 / --symbols / --symbols-file")

    result = run(symbols, period=args.period)
    _print_report(result)
    return 0 if not result["failures"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
