#!/usr/bin/env python3
"""
Preflight validator for the slim Nifty 50 ingestion pipeline.

Checks:
  - DATABASE_URL is set
  - yfinance / psycopg2 / dotenv import cleanly
  - India DB reachable
  - historical_prices table exists with the (symbol, date) unique key
  - Nifty 50 universe file is present and 50-strong
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; RESET = "\033[0m"


def _ok(name: str) -> tuple[bool, str]:
    return True, f"{GREEN}OK{RESET}   {name}"


def _fail(name: str, why: str) -> tuple[bool, str]:
    return False, f"{RED}FAIL{RESET} {name}: {why}"


def check_env() -> list[tuple[bool, str]]:
    out = []
    if os.getenv("DATABASE_URL"):
        out.append(_ok("DATABASE_URL set"))
    else:
        out.append(_fail("DATABASE_URL", "missing from environment"))
    return out


def check_packages() -> list[tuple[bool, str]]:
    out = []
    for pkg in ("yfinance", "psycopg2", "dotenv"):
        try:
            __import__(pkg)
            out.append(_ok(f"import {pkg}"))
        except ImportError as e:
            out.append(_fail(f"import {pkg}", str(e)))
    return out


def check_db() -> list[tuple[bool, str]]:
    out = []
    url = os.getenv("DATABASE_URL")
    if not url:
        return [_fail("DB connect", "DATABASE_URL not set")]
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=10)
        cur = conn.cursor()

        cur.execute("""SELECT 1 FROM information_schema.tables
                       WHERE table_schema='equity' AND table_name='historical_prices'""")
        if cur.fetchone():
            out.append(_ok("table historical_prices exists"))
        else:
            out.append(_fail("table historical_prices", "missing"))

        cur.execute("""SELECT 1 FROM pg_indexes
                       WHERE schemaname='equity' AND tablename='historical_prices'
                         AND indexdef LIKE '%UNIQUE%symbol%date%'""")
        if cur.fetchone():
            out.append(_ok("unique index on (symbol, date)"))
        else:
            out.append(_fail("unique index", "(symbol, date) unique index missing — ON CONFLICT will fail"))

        cur.execute("""SELECT COUNT(*), MAX(date), COUNT(DISTINCT symbol)
                       FROM equity.historical_prices""")
        n, last, syms = cur.fetchone()
        out.append(_ok(f"historical_prices: {n:,} rows, {syms} symbols, last bar {last}"))

        cur.close(); conn.close()
    except Exception as e:
        out.append(_fail("DB connect", str(e)[:120]))
    return out


def check_universe() -> list[tuple[bool, str]]:
    out = []
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "data" / "nse_indices.json"
    if not path.exists():
        return [_fail("nse_indices.json", f"missing at {path}")]
    try:
        sys.path.insert(0, str(repo_root))
        from ingestion.symbol_universe import NIFTY_50
        if len(NIFTY_50) == 50 and len(set(NIFTY_50)) == 50:
            out.append(_ok(f"NIFTY_50 loaded ({len(NIFTY_50)} unique symbols)"))
        else:
            out.append(_fail("NIFTY_50", f"expected 50 unique, got {len(NIFTY_50)} ({len(set(NIFTY_50))} unique)"))
    except Exception as e:
        out.append(_fail("NIFTY_50 import", str(e)[:120]))
    return out


def main() -> int:
    try:
        from dotenv import load_dotenv
        repo_root = Path(__file__).resolve().parents[1]
        load_dotenv(repo_root / ".env")
    except ImportError:
        pass

    print(f"\n=== Preflight  {datetime.now():%Y-%m-%d %H:%M %Z} ===\n")
    all_results: list[tuple[bool, str]] = []
    for section_name, fn in (
        ("Environment", check_env),
        ("Packages",    check_packages),
        ("Database",    check_db),
        ("Universe",    check_universe),
    ):
        print(f"[{section_name}]")
        for r in fn():
            print(f"  {r[1]}")
            all_results.append(r)
        print()

    passed = sum(1 for ok, _ in all_results if ok)
    total = len(all_results)
    print("=" * 52)
    if passed == total:
        print(f"{GREEN}ALL CHECKS PASSED ({passed}/{total}){RESET}")
        return 0
    print(f"{YELLOW}{passed}/{total} passed{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
