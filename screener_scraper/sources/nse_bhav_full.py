"""1. NSE full bhavcopy with delivery: nsearchives sec_bhavdata_full_DDMMYYYY.csv (static)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import List, Optional

from . import _common as c

NAME = "nse_bhav_full"
TABLE = "equity.nse_bhav_full"
URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
FLOOR = date(2019, 9, 20)            # earliest file the archive serves (probed)
DEFAULT_SINCE = date(2019, 9, 20)
COLS = ["symbol", "series", "date", "prev_close", "open", "high", "low", "last", "close", "avg_price",
        "ttl_trd_qnty", "turnover_lacs", "no_of_trades", "deliv_qty", "deliv_per"]
PK = ["symbol", "series", "date"]
READY_HOUR_IST = 19


def parse(text: str) -> List[tuple]:
    return parse_rows(csv.reader(io.StringIO(text)))


def parse_xlsx(raw: bytes) -> List[tuple]:
    """Some days the archive serves an Excel workbook under the .csv name (08-Aug-2022), with the same columns."""
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).worksheets[0]
    return parse_rows(["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True))


def parse_rows(rd) -> List[tuple]:
    hdr = [h.strip().upper() for h in next(rd)]
    ix = {h: i for i, h in enumerate(hdr)}
    if "SYMBOL" not in ix or "DATE1" not in ix:
        raise ValueError("not a full bhavcopy")
    out = []
    for r in rd:
        if len(r) < len(hdr):
            continue
        g = lambda k: r[ix[k]].strip()
        try:
            dt = datetime.strptime(g("DATE1"), "%d-%b-%Y").date()
        except ValueError:
            continue
        out.append((g("SYMBOL"), g("SERIES"), dt, c.num(g("PREV_CLOSE")), c.num(g("OPEN_PRICE")),
                    c.num(g("HIGH_PRICE")), c.num(g("LOW_PRICE")), c.num(g("LAST_PRICE")), c.num(g("CLOSE_PRICE")),
                    c.num(g("AVG_PRICE")), c.intn(g("TTL_TRD_QNTY")), c.num(g("TURNOVER_LACS")),
                    c.intn(g("NO_OF_TRADES")), c.intn(g("DELIV_QTY")), c.num(g("DELIV_PER"))))
    return out


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    f = c.Fetcher()

    def load_day(d: date):
        try:
            r = f.get(URL.format(d=d))
        except c.NotFound:
            return None
        rows = [x for x in (parse_xlsx(r.content) if r.content[:2] == b"PK" else parse(r.text)) if x[2] == d]
        return c.upsert(db, TABLE, COLS, PK, rows)
    return c.run_days(db, NAME, since, until, DEFAULT_SINCE, FLOOR, TABLE, load_day)
