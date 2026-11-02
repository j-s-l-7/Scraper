"""2. BSE equity bhavcopy: new UDiFF CSV (from 2023-12-27), else the older EQ_ISINCODE_DDMMYY.zip."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from typing import List, Optional

from . import _common as c

NAME = "bse_bhav"
TABLE = "equity.bse_bhav"
BASE = "https://www.bseindia.com/download/BhavCopy/Equity/"
NEW_URL = BASE + "BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
OLD_URL = BASE + "EQ_ISINCODE_{d:%d%m%y}.zip"
NEW_FROM = date(2023, 12, 27)
FLOOR = date(2016, 12, 1)
DEFAULT_SINCE = date(2016, 12, 1)
COLS = ["sc_code", "isin", "date", "open", "high", "low", "close", "volume", "trades", "turnover"]
PK = ["sc_code", "date"]
READY_HOUR_IST = 19


def parse_new(text: str) -> List[tuple]:
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        if (r.get("Sgmt") or "CM") != "CM" or not r.get("FinInstrmId"):
            continue
        out.append((r["FinInstrmId"].strip(), (r.get("ISIN") or "").strip() or None,
                    datetime.strptime(r["TradDt"].strip(), "%Y-%m-%d").date(),
                    c.num(r["OpnPric"]), c.num(r["HghPric"]), c.num(r["LwPric"]), c.num(r["ClsPric"]),
                    c.intn(r["TtlTradgVol"]), c.intn(r["TtlNbOfTxsExctd"]), c.num(r["TtlTrfVal"])))
    return out


def parse_old(text: str, d: date) -> List[tuple]:
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        if None in r:           # a line that ran into the next one (07-Feb-22): skip it rather than misalign
            continue
        r = {k.strip(): (v or "").strip() for k, v in r.items()}
        if not r.get("SC_CODE", "").isdigit():      # ...and the broken tail it leaves behind
            continue
        out.append((r["SC_CODE"], r.get("ISIN_CODE") or None, d, c.num(r["OPEN"]), c.num(r["HIGH"]), c.num(r["LOW"]),
                    c.num(r["CLOSE"]), c.intn(r["NO_OF_SHRS"]), c.intn(r["NO_TRADES"]), c.num(r["NET_TURNOV"])))
    return out


def looks_missing(raw: bytes) -> bool:
    """BSE answers 200 with a small HTML 'not found' page for non-trading days."""
    return raw[:20].lstrip().lower().startswith(b"<")


def parse_bytes(raw: bytes, d: date) -> Optional[List[tuple]]:
    if looks_missing(raw):
        return None
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        return parse_old(z.read(z.namelist()[0]).decode("latin-1"), d)
    return parse_new(raw.decode("utf-8-sig"))


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    f = c.Fetcher()

    def load_day(d: date):
        try:
            r = f.get((NEW_URL if d >= NEW_FROM else OLD_URL).format(d=d))
        except c.NotFound:
            return None
        rows = parse_bytes(r.content, d)
        if rows is None:
            return None
        return c.upsert(db, TABLE, COLS, PK, [x for x in rows if x[2] == d])
    return c.run_days(db, NAME, since, until, DEFAULT_SINCE, FLOOR, TABLE, load_day)
