"""
NSE Bhavcopy Backfill / Gap-fill
================================

Authoritative end-of-day OHLCV straight from the NSE daily *bhavcopy*
(UDiFF format) — the exchange's own record — upserted into
`historical_prices` on the same `(symbol, date)` key as the yfinance
pipeline. Idempotent.

Why this exists
---------------
yfinance is a third-party scrape with known defects on NSE data:
phantom volume=0 holiday bars, synthetic pre-IPO/demerger history, and
occasional missing trading days (e.g. 2025-03-18, which yfinance never
serves but the bhavcopy has). The bhavcopy has none of those problems
and ships ISIN for stable identity. Its one gap is no adjusted close —
so this writes `adj_close = close` (raw), same fallback the yfinance
ingest uses when Adj Close is absent.

A bhavcopy exists *only for real trading days*, so a 404 is a reliable
"that date was a holiday / not yet published", not an error.

Usage
-----
    # single day (the original gap-fill use-case)
    python ingestion/backfill_from_nse.py --date 2025-03-18

    # an inclusive date range, full Nifty 50
    python ingestion/backfill_from_nse.py \
        --start 2025-03-01 --end 2025-03-31

    # limit which symbols get written (default: Nifty 50)
    python ingestion/backfill_from_nse.py \
        --date 2025-03-18 --symbols RELIANCE,TCS

    # write every EQ/BE security in the bhavcopy, not just Nifty 50
    python ingestion/backfill_from_nse.py --date 2025-03-18 --all

Note
----
NSE switched to the UDiFF bhavcopy format on 2024-07-08. Dates before
that used a different archive layout (cm{DDMMMYYYY}bhav.csv.zip) which
NSE has largely retired; this script intentionally does not fetch them
and will error with guidance. For deep history, yfinance remains the
source.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import logging
import math
import os
import sys
import time
import zipfile
from typing import Iterable

import requests
from dotenv import load_dotenv

load_dotenv(
    os.path.join(os.path.dirname(__file__), "..", ".env"),
    override=True,
)

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ingestion.symbol_universe import NIFTY_50, universe_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nseBackfill")

DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_SIZE = 1000

# NSE UDiFF bhavcopy: one zipped CSV per trading day, all CM securities.
UDIFF_START = dt.date(2024, 7, 8)
BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36")


# ----- DB helpers -----

def get_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set in environment")
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)


# ----- NSE fetch -----

def make_session() -> requests.Session:
    """Plain session for the static nsearchives files (no cookie warming, no spoofed headers)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_bhavcopy(session: requests.Session, date: dt.date):
    """Download + parse one day's bhavcopy.

    Returns a pandas DataFrame, or None when NSE has no bhavcopy for the
    date (holiday / not yet published — a normal, non-error condition).
    """
    import pandas as pd

    if date < UDIFF_START:
        raise NotImplementedError(
            f"{date} predates the UDiFF bhavcopy format ({UDIFF_START}). "
            "Old-format archives are retired on NSE; use yfinance for "
            "history this old."
        )

    url = BHAVCOPY_URL.format(ymd=date.strftime("%Y%m%d"))
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return None  # no session that day
    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            df = pd.read_csv(fh)
    df["TckrSymb"] = df["TckrSymb"].astype(str).str.strip()
    return df


# ----- value coercion (mirrors ingest_yfinance.py) -----

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


def build_rows(symbols: set[str] | None, date: dt.date, df) -> tuple[list[tuple], dict]:
    """Map a bhavcopy DataFrame to historical_prices upsert tuples.

    `symbols=None` writes every EQ/BE security; otherwise only the given
    set. Prefers the EQ series over BE when a ticker appears in both.
    Applies the same guards as the yfinance pipeline: drop volume==0,
    all-NaN OHLC, and OHLC-invariant violations (bad source rows).
    `adj_close` is set to `close` (bhavcopy carries no adjusted price).
    """
    eq = df[df["SctySrs"].isin(["EQ", "BE"])].copy()
    # Prefer EQ over BE when a symbol is dual-listed across series.
    eq["__pri"] = (eq["SctySrs"] != "EQ").astype(int)
    eq = eq.sort_values("__pri").drop_duplicates("TckrSymb", keep="first")

    rows: list[tuple] = []
    skipped = {"volume0": 0, "nan": 0, "invariant": 0, "not_listed": 0}

    wanted = symbols if symbols is not None else set(eq["TckrSymb"])
    by_ticker = eq.set_index("TckrSymb")

    for sym in sorted(wanted):
        if sym not in by_ticker.index:
            skipped["not_listed"] += 1
            continue
        r = by_ticker.loc[sym]
        o = _safe_float(r["OpnPric"])
        h = _safe_float(r["HghPric"])
        lo = _safe_float(r["LwPric"])
        c = _safe_float(r["ClsPric"])
        v = _safe_int(r["TtlTradgVol"])

        if all(x is None for x in (o, h, lo, c)):
            skipped["nan"] += 1
            continue
        if v == 0:
            skipped["volume0"] += 1
            continue
        if None not in (o, h, lo, c) and (
            h < lo or h < o or h < c or lo > o or lo > c
        ):
            skipped["invariant"] += 1
            logger.warning(f"[{sym} {date}] OHLC invariant violation, skipped: "
                           f"O={o} H={h} L={lo} C={c}")
            continue
        rows.append((sym, date, o, h, lo, c, c, v))

    return rows, skipped


# ----- upsert (identical contract to ingest_yfinance.py) -----

# Default: bhavcopy is authoritative for everything, incl. adj_close=close.
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

# --keep-adj-close: bhavcopy owns traded OHLCV+volume, but an existing
# adj_close (e.g. yfinance's split/dividend-adjusted value) is preserved.
# On a fresh insert adj_close still seeds to close — correct for the
# trading day itself, before any future corporate action.
UPSERT_SQL_KEEP_ADJ = """
INSERT INTO equity.historical_prices
    (symbol, date, open, high, low, close, adj_close, volume)
VALUES %s
ON CONFLICT (symbol, date) DO UPDATE SET
    open      = EXCLUDED.open,
    high      = EXCLUDED.high,
    low       = EXCLUDED.low,
    close     = EXCLUDED.close,
    volume    = EXCLUDED.volume
"""


def upsert_rows(conn, rows: list[tuple], keep_adj_close: bool = False) -> None:
    if not rows:
        return
    sql = UPSERT_SQL_KEEP_ADJ if keep_adj_close else UPSERT_SQL
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=BATCH_SIZE)


# ----- driver -----

def daterange(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        if d.weekday() < 5:  # skip weekends — never a bhavcopy
            yield d
        d += dt.timedelta(days=1)


def run(dates: list[dt.date], symbols: set[str] | None,
        keep_adj_close: bool = False) -> dict:
    label = "ALL EQ/BE" if symbols is None else f"{len(symbols)} symbols"
    logger.info(f"Backfilling {len(dates)} date(s) ({label}) from NSE bhavcopy"
                f"{' [keep adj_close]' if keep_adj_close else ''}")

    session = make_session()
    conn = get_connection()
    per_date: dict[str, int] = {}
    holidays: list[str] = []
    failures: dict[str, str] = {}
    total = 0
    try:
        for d in dates:
            key = d.isoformat()
            try:
                df = fetch_bhavcopy(session, d)
            except Exception as exc:
                failures[key] = str(exc)[:140]
                logger.error(f"{key}: fetch failed: {exc}")
                continue
            if df is None:
                holidays.append(key)
                logger.info(f"{key}: no bhavcopy (holiday / not published) — skipped")
                continue
            rows, skipped = build_rows(symbols, d, df)
            try:
                upsert_rows(conn, rows, keep_adj_close=keep_adj_close)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                failures[key] = f"upsert: {exc}"[:140]
                logger.error(f"{key}: upsert failed: {exc}")
                continue
            per_date[key] = len(rows)
            total += len(rows)
            logger.info(f"{key}: upserted {len(rows)} rows  "
                        f"(skipped vol0={skipped['volume0']} "
                        f"nan={skipped['nan']} inv={skipped['invariant']} "
                        f"not_listed={skipped['not_listed']})")
            time.sleep(0.3)  # be polite to NSE
    finally:
        conn.close()

    return {"per_date": per_date, "holidays": holidays,
            "failures": failures, "total": total, "dates": len(dates)}


def _print_report(r: dict) -> None:
    print()
    print("=" * 60)
    print(f"dates processed : {r['dates']}")
    print(f"rows upserted   : {r['total']:,}")
    print(f"trading days    : {len(r['per_date'])}")
    print(f"holidays skipped: {len(r['holidays'])}  {r['holidays'] or ''}")
    if r["failures"]:
        print(f"failures        : {len(r['failures'])}")
        for k, why in r["failures"].items():
            print(f"  {k}: {why}")
    print("=" * 60)


def _parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", type=str, help="single date YYYY-MM-DD")
    p.add_argument("--start", type=str, help="range start YYYY-MM-DD (with --end)")
    p.add_argument("--end", type=str, help="range end YYYY-MM-DD (with --start)")
    p.add_argument("--recent", type=int, metavar="N",
                   help="backfill the last N calendar days ending today "
                        "(weekends/holidays auto-skipped) — for the daily cron")
    p.add_argument("--universe", type=str, metavar="CODE",
                   help="Nifty 50 + latest equity.universe snapshot, e.g. TOP100_MCAP")
    p.add_argument("--symbols", type=str,
                   help="comma-separated symbols (default: Nifty 50)")
    p.add_argument("--all", action="store_true",
                   help="write every EQ/BE security, not just Nifty 50")
    p.add_argument("--keep-adj-close", action="store_true",
                   help="update OHLCV+volume but preserve any existing "
                        "adj_close (let yfinance own the adjusted series)")
    args = p.parse_args(argv[1:])

    if args.date:
        dates = [_parse_date(args.date)]
    elif args.start and args.end:
        dates = list(daterange(_parse_date(args.start), _parse_date(args.end)))
        if not dates:
            p.error("empty date range")
    elif args.recent:
        today = dt.date.today()
        dates = list(daterange(today - dt.timedelta(days=args.recent - 1), today))
        if not dates:
            p.error("empty --recent window")
    else:
        p.error("specify --date, --start/--end, or --recent N")

    if args.all:
        symbols = None
    elif args.universe:
        symbols = set(universe_from_db(args.universe))
    elif args.symbols:
        symbols = {s.strip() for s in args.symbols.split(",") if s.strip()}
    else:
        symbols = set(NIFTY_50)

    result = run(dates, symbols, keep_adj_close=args.keep_adj_close)
    _print_report(result)
    return 0 if not result["failures"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
