"""Public data sources loaded into the local lake (equity.* / macro.*). Each module: run(db, since, until)."""
from __future__ import annotations

import fcntl
import importlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from .. import config
from . import _common as c

log = logging.getLogger(__name__)
# CLI name -> module
SOURCES = {"nse_bhav_full": "nse_bhav_full", "bse_bhav": "bse_bhav", "bulk_block": "bulk_block", "fo_bhav": "fo_bhav",
           "credit_ratings": "credit_ratings", "macro": "macro", "gdelt_news": "gdelt_news"}
EXCHANGE_READY_HOUR_IST = 19      # bhavcopies appear in the evening
RETRY_MINUTES = 90


def load(name: str):
    return importlib.import_module(f"{__name__}.{SOURCES[name]}")


def due(db, name: str) -> bool:
    """Daily gate from the equity.control marker: one successful run per IST day; exchange files wait for the evening."""
    m = c.get_marker(db, name)
    today = c.today_ist()
    if m.get("blocked_on") == today.isoformat():
        return False
    if m.get("run_date") == today.isoformat():
        return False
    mod = load(name)
    if hasattr(mod, "READY_HOUR_IST") and c.now_ist().hour < mod.READY_HOUR_IST and today.weekday() < 5:
        return False
    if m.get("attempt_at"):
        if datetime.now(timezone.utc) - datetime.fromisoformat(m["attempt_at"]) < timedelta(minutes=RETRY_MINUTES):
            return False
    return True


def run_sources(db, only: Optional[List[str]] = None, backfill_from: Optional[date] = None, daily: bool = False) -> Dict[str, dict]:
    names = only or list(SOURCES)
    out = {}
    for n in names:
        if n not in SOURCES:
            raise SystemExit(f"unknown source {n!r}; choose from {', '.join(SOURCES)}")
        if daily and not due(db, n):
            out[n] = {"status": "skipped"}
            continue
        c.set_marker(db, n, attempt_at=datetime.now(timezone.utc).isoformat())
        try:
            res = load(n).run(db, backfill_from, None)
        except Exception as e:           # one source failing must not stop the others
            log.exception("source %s failed", n)
            res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200]}
            db.reconnect("both")
        out[n] = res
        if res.get("status") == "ok" and not res.get("remaining"):
            mod = load(n)
            if hasattr(mod, "READY_HOUR_IST"):
                # exchange files: done for the day only once the latest weekday's file is in (holidays retry till midnight)
                t = c.today_ist()
                while t.weekday() >= 5:
                    t -= timedelta(days=1)
                if (c.get_marker(db, n).get("last_date") or "") >= t.isoformat():
                    c.set_marker(db, n, run_date=c.today_ist())
            else:
                c.set_marker(db, n, run_date=c.today_ist())
        log.info("source %s -> %s", n, res)
    return out


class single_instance:
    def __init__(self, name: str):
        self.path = config.ROOT / "data" / f".{name}.lock"

    def __enter__(self):
        self.path.parent.mkdir(exist_ok=True)
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def __exit__(self, *a):
        self.f.close()
