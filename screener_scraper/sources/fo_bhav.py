"""4. NSE F&O UDiFF bhavcopy (zip in nsearchives): stock/index futures + options summary per symbol/expiry/CE-PE."""
from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from datetime import date, datetime
from typing import List, Optional

from . import _common as c

NAME = "fo_bhav"
TABLE = "equity.fo_bhav"
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
FLOOR = date(2023, 12, 27)
DEFAULT_SINCE = date(2024, 7, 8)
COLS = ["date", "instrument", "symbol", "expiry", "strike", "option_type", "open", "high", "low", "close", "settle",
        "contracts", "oi", "chg_oi"]
PK = ["date", "instrument", "symbol", "expiry", "strike", "option_type"]
READY_HOUR_IST = 19


def _d(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def parse(text: str) -> List[tuple]:
    out = []
    summ = defaultdict(lambda: [0, 0, 0])
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        tp = (r.get("FinInstrmTp") or "").strip()
        if tp not in ("STF", "IDF", "STO", "IDO"):
            continue
        d, sym, exp = _d(r["TradDt"]), r["TckrSymb"].strip(), _d(r["XpryDt"])
        vol, oi, chg = c.intn(r["TtlTradgVol"]) or 0, c.intn(r["OpnIntrst"]) or 0, c.intn(r["ChngInOpnIntrst"]) or 0
        if tp in ("STF", "IDF"):
            out.append((d, tp, sym, exp, 0, "", c.num(r["OpnPric"]), c.num(r["HghPric"]), c.num(r["LwPric"]),
                        c.num(r["ClsPric"]), c.num(r["SttlmPric"]), vol, oi, chg))
        else:
            s = summ[(d, tp, sym, exp, (r.get("OptnTp") or "").strip())]
            s[0] += vol
            s[1] += oi
            s[2] += chg
    for (d, tp, sym, exp, ot), (vol, oi, chg) in summ.items():
        out.append((d, tp, sym, exp, 0, ot, None, None, None, None, None, vol, oi, chg))
    return out


def parse_zip(raw: bytes) -> List[tuple]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    return parse(z.read(z.namelist()[0]).decode("utf-8-sig"))


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    f = c.Fetcher()

    def load_day(d: date):
        try:
            r = f.get(URL.format(d=d))
        except c.NotFound:
            return None
        if r.content[:2] != b"PK":
            return None
        return c.upsert(db, TABLE, COLS, PK, [x for x in parse_zip(r.content) if x[0] == d])
    return c.run_days(db, NAME, since, until, DEFAULT_SINCE, FLOOR, TABLE, load_day)
