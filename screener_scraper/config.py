"""Configuration: paths, env loading, HTTP politeness settings."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "logs"
SQL_DIR = ROOT / "sql"

BASE_URL = "https://www.screener.in"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MIN_INTERVAL = 1.5      # seconds between requests, global across threads (env SCREENER_MIN_INTERVAL)
JITTER = 0.6            # extra random 0..JITTER seconds (env SCREENER_JITTER)
MAX_INTERVAL = 10.0     # cap for adaptive throttling after 429s
DECAY_AFTER = 20        # consecutive successes before the interval decays one step back
MAX_ATTEMPTS = 5
BACKOFF_BASE = 4.0      # 4, 8, 16, 32 ...
TIMEOUT = 40
RESUME_HOURS = 20

UNIVERSE_CODE = "TOP100_MCAP"


def parse_shard(spec):
    """'k/n' -> (k, n) with 1 <= k <= n; None -> None."""
    if not spec:
        return None
    try:
        k, n = (int(x) for x in str(spec).split("/"))
    except ValueError:
        raise ValueError(f"bad --shard {spec!r}, expected k/n")
    if not (1 <= k <= n):
        raise ValueError(f"bad --shard {spec!r}: need 1 <= k <= n")
    return k, n


def load_env() -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no overriding, no printing)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def database_url(session: bool = False) -> str:
    """session=True keeps Supabase's session pooler (5432): required by any client that sets
    session-level state (e.g. the read-only console), which would leak to other clients
    through the transaction pooler's shared server connections."""
    load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (see .env)")
    return _pooler(url, session)


def _pooler(url: str, session: bool) -> str:
    # Supabase's session pooler (5432) caps at 15 clients; 20 CI runners plus the local workers,
    # console and checks exceed it. The transaction pooler (6543) is sized for many short clients
    # and works with everything here (no session state, LISTEN or server-side prepared statements).
    # DB_SESSION_POOLER=1 opts back into 5432.
    if not session and os.environ.get("DB_SESSION_POOLER") != "1" and "pooler.supabase.com:5432" in url:
        url = url.replace("pooler.supabase.com:5432", "pooler.supabase.com:6543")
    return url


def supabase_url(session: bool = False) -> Optional[str]:
    """Supabase (the top-100 store) for code that reads it while writing the lake; None when this process has none.
    SOURCE_DATABASE_URL first: fleet processes run with DATABASE_URL = the lake and SOURCE_DATABASE_URL = Supabase.
    Else DATABASE_URL (a shell with .env), but never the lake itself: that "sync" copied the lake onto itself."""
    load_env()
    from .lake import DEFAULT_LAKE_URL             # lazy: lake imports config
    lakes = {u.rstrip("/") for u in (os.environ.get("LAKE_DATABASE_URL"), DEFAULT_LAKE_URL) if u}
    url = os.environ.get("SOURCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or url.rstrip("/") in lakes:
        return None
    return _pooler(url, session)


def _apply_env() -> None:
    global MIN_INTERVAL, JITTER
    load_env()
    if os.environ.get("SCREENER_MIN_INTERVAL"):
        MIN_INTERVAL = float(os.environ["SCREENER_MIN_INTERVAL"])
    if os.environ.get("SCREENER_JITTER"):
        JITTER = float(os.environ["SCREENER_JITTER"])


_apply_env()
