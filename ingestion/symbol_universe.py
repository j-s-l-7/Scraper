"""
Symbol-universe loader. Reads index constituents from
`data/nse_indices.json` so updates don't require a code change.

Currently exposes:
    NIFTY_50  — list[str]   the 50 NSE tickers in the index

Add more lists (NIFTY_100, NIFTY_500, custom watchlists) by extending
the JSON; update this loader to expose them.
"""
from __future__ import annotations

import json
from pathlib import Path

# Walk up from this file: ingestion -> database -> api -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDICES_PATH = PROJECT_ROOT / "data" / "nse_indices.json"


def _load() -> dict:
    if not INDICES_PATH.exists():
        raise FileNotFoundError(
            f"Symbol universe file missing: {INDICES_PATH}. "
            "It ships with the repo; check that you're at the project root."
        )
    return json.loads(INDICES_PATH.read_text())


_data = _load()
NIFTY_50: list[str] = list(_data["nifty_50"])

assert len(NIFTY_50) == 50, (
    f"NIFTY_50 must contain 50 symbols, found {len(NIFTY_50)} in "
    f"{INDICES_PATH.name}"
)
assert len(set(NIFTY_50)) == 50, "NIFTY_50 contains duplicates"


def universe_from_db(code: str = "TOP100_MCAP") -> list[str]:
    """Nifty 50 plus the latest `equity.universe` snapshot (e.g. top 100 by market cap)."""
    import os
    import psycopg2

    with psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute(
            """select c.nse_symbol from equity.universe u join equity.company c using (company_id)
               where u.universe_code = %s and c.nse_symbol is not null
                 and u.as_of = (select max(as_of) from equity.universe where universe_code = %s)""",
            (code, code))
        db_symbols = [r[0] for r in cur.fetchall()]
    return sorted(set(NIFTY_50) | set(db_symbols))
