"""NSE symbol-change and company-name-change files (static nsearchives CSVs, full history since 1996-99, re-read
daily: ~300 KB). Loaded into equity.nse_symbol_change / equity.nse_name_change. A symbol change can be a merger with
a different ISIN, not a rename: consumers confirm by ISIN or date window before stitching series."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import List, Optional

from . import _common as c

NAME = "nse_changes"
SYMBOL_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
NAME_URL = "https://nsearchives.nseindia.com/content/equities/namechange.csv"


def _date(s: str):
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def parse_symbols(text: str) -> List[tuple]:
    """(old_symbol, new_symbol, effective_date, company_name)."""
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 4 or r[1].strip().upper() == "OLD SYMBOL":
            continue
        d = _date(r[3])
        if d and r[1].strip() and r[2].strip():
            out.append((r[1].strip(), r[2].strip(), d, " ".join(r[0].split())))
    return out


def parse_names(text: str) -> List[tuple]:
    """(symbol, old_name, new_name, effective_date)."""
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 4 or r[0].strip().upper() == "SYMBOL":
            continue
        d = _date(r[3])
        if d and r[0].strip() and r[1].strip():
            out.append((r[0].strip(), " ".join(r[1].split()), " ".join(r[2].split()), d))
    return out


def run(db, since=None, until=None) -> dict:
    f = c.Fetcher()
    sym = parse_symbols(f.get(SYMBOL_URL).text)
    names = parse_names(f.get(NAME_URL).text)
    n1 = c.upsert(db, "equity.nse_symbol_change", ["old_symbol", "new_symbol", "effective_date", "company_name"],
                  ["old_symbol", "new_symbol", "effective_date"], sym)
    n2 = c.upsert(db, "equity.nse_name_change", ["symbol", "old_name", "new_name", "effective_date"],
                  ["symbol", "effective_date", "old_name"], names)
    c.set_marker(db, NAME, status="ok", run_at=datetime.now(timezone.utc).isoformat())
    return {"status": "ok", "rows": n1 + n2, "symbol_changes": n1, "name_changes": n2}
