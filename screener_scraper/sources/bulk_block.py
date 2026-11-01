"""3. NSE bulk and block deals: static daily snapshot files bulk.csv / block.csv (no archive, history accrues daily)."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, timezone
from typing import List, Optional

from . import _common as c

NAME = "bulk_block"
TABLE = "equity.bulk_block_deal"
URLS = {"bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
        "block": "https://nsearchives.nseindia.com/content/equities/block.csv"}
COLS = ["deal_hash", "date", "symbol", "client", "side", "qty", "price", "kind"]
PK = ["deal_hash"]
READY_HOUR_IST = 19


def parse(text: str, kind: str) -> List[tuple]:
    rd = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        hdr = [h.strip().lower() for h in next(rd)]
    except StopIteration:
        return []
    ix = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rd:
        if len(r) < 7 or "symbol" not in ix:
            continue
        try:
            d = datetime.strptime(r[ix["date"]].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        sym, client = r[ix["symbol"]].strip(), r[ix["client name"]].strip()
        side = r[ix["buy/sell"]].strip().upper()
        qty, price = c.intn(r[ix["quantity traded"]]), c.num(r[ix["trade price / wght. avg. price"]])
        h = hashlib.md5("|".join(map(str, (d, sym, client, side, qty, price, kind))).encode()).hexdigest()
        out.append((h, d, sym, client, side, qty, price, kind))
    return out


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    """Only the current snapshot exists; since/until are ignored (a backfill is a no-op)."""
    f = c.Fetcher()
    rows = 0
    status = "ok"
    maxd = None
    try:
        for kind, url in URLS.items():
            try:
                r = f.get(url)
            except c.NotFound:
                continue
            parsed = parse(r.text, kind)
            rows += c.upsert(db, TABLE, COLS, PK, parsed)
            for p in parsed:
                maxd = max(maxd or p[1], p[1])
    except c.Blocked as e:
        status = "blocked"
        c.set_marker(db, NAME, blocked_on=c.today_ist())
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if maxd:
        kv["last_date"] = maxd
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": rows}
