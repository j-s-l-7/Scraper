"""Survivorship-bias fix: NSE cash-market bhavcopies, old format (cmDDMONYYYYbhav.csv.zip, 2010 .. 2024-07-05) and
UDiFF (BhavCopy_NSE_CM_..., 2024-07-08 ..). Every (symbol, isin, series) ever seen -> equity.listing_history
(first_seen, last_seen); prices before the full-bhavcopy era (2019-09-20) also go into equity.nse_bhav_full
(no delivery data). Also builds point-in-time universe membership (equity.universe_pit)."""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import _common as c
from . import nse_bhav_full

log = logging.getLogger(__name__)
NAME = "nse_cm_bhav"
OLD_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
NEW_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
UDIFF_FROM = date(2024, 7, 8)
FLOOR = date(2010, 1, 1)
DEFAULT_SINCE = date(2010, 1, 1)
READY_HOUR_IST = 19
LISTED_NOW_DAYS = 10
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
LH_COLS = ["symbol", "isin", "series", "first_seen", "last_seen", "source"]


def parse_old(text: str) -> List[dict]:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        if not r.get("SYMBOL"):
            continue
        try:
            d = datetime.strptime(r["TIMESTAMP"].title(), "%d-%b-%Y").date()
        except ValueError:
            continue
        out.append({"symbol": r["SYMBOL"], "series": r["SERIES"], "isin": r.get("ISIN", ""), "date": d,
                    "prev": c.num(r.get("PREVCLOSE")), "open": c.num(r.get("OPEN")), "high": c.num(r.get("HIGH")),
                    "low": c.num(r.get("LOW")), "last": c.num(r.get("LAST")), "close": c.num(r.get("CLOSE")),
                    "qty": c.intn(r.get("TOTTRDQTY")), "val": c.num(r.get("TOTTRDVAL")),
                    "trades": c.intn(r.get("TOTALTRADES"))})
    return out


def parse_udiff(text: str) -> List[dict]:
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        if r.get("FinInstrmTp") != "STK" or not r.get("TckrSymb"):
            continue
        out.append({"symbol": r["TckrSymb"].strip(), "series": (r.get("SctySrs") or "").strip(),
                    "isin": (r.get("ISIN") or "").strip(), "date": datetime.strptime(r["TradDt"], "%Y-%m-%d").date(),
                    "prev": c.num(r["PrvsClsgPric"]), "open": c.num(r["OpnPric"]), "high": c.num(r["HghPric"]),
                    "low": c.num(r["LwPric"]), "last": c.num(r["LastPric"]), "close": c.num(r["ClsPric"]),
                    "qty": c.intn(r["TtlTradgVol"]), "val": c.num(r["TtlTrfVal"]), "trades": c.intn(r["TtlNbOfTxsExctd"])})
    return out


def parse_zip(raw: bytes, d: date) -> List[dict]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode("utf-8-sig", "ignore")
    return parse_udiff(text) if d >= UDIFF_FROM else parse_old(text)


def to_full_rows(rows: List[dict]) -> List[tuple]:
    """Old-format rows -> nse_bhav_full tuples (no delivery fields). turnover in lakh Rs."""
    out = []
    for r in rows:
        avg = r["val"] / r["qty"] if r["val"] and r["qty"] else None
        out.append((r["symbol"], r["series"], r["date"], r["prev"], r["open"], r["high"], r["low"], r["last"],
                    r["close"], avg, r["qty"], None if r["val"] is None else r["val"] / 1e5, r["trades"], None, None))
    return out


def upsert_listing(db, rows: List[dict]) -> None:
    keyed: Dict[tuple, tuple] = {}
    for r in rows:
        k = (r["symbol"], r["isin"], r["series"])
        keyed[k] = (*k, r["date"], r["date"], "nse_cm_bhav")
    if not keyed:
        return
    from psycopg2.extras import execute_values
    sql = ("insert into equity.listing_history (symbol, isin, series, first_seen, last_seen, source) values %s "
           "on conflict (symbol, isin, series) do update set first_seen=least(equity.listing_history.first_seen, excluded.first_seen), "
           "last_seen=greatest(equity.listing_history.last_seen, excluded.last_seen), fetched_at=now()")
    db.run_tx(lambda cur: execute_values(cur, sql, list(keyed.values())))


def refresh_listed_flag(db) -> None:
    def tx(cur):
        cur.execute("select max(last_seen) from equity.listing_history")
        mx = cur.fetchone()[0]
        if mx:
            cur.execute("update equity.listing_history set is_listed_now = (last_seen >= %s)",
                        (mx - timedelta(days=LISTED_NOW_DAYS),))
    db.run_tx(tx)


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    f = c.Fetcher()

    def load_day(d: date):
        url = NEW_URL.format(d=d) if d >= UDIFF_FROM else OLD_URL.format(d=d, mon=MON[d.month - 1])
        try:
            r = f.get(url)
        except c.NotFound:
            return None
        if r.content[:2] != b"PK":
            return None
        rows = [x for x in parse_zip(r.content, d) if x["date"] == d]
        upsert_listing(db, rows)
        if d < nse_bhav_full.FLOOR:
            c.upsert(db, "equity.nse_bhav_full", nse_bhav_full.COLS, nse_bhav_full.PK, to_full_rows(rows))
            db.run_tx(lambda cur: cur.execute(
                "update equity.nse_bhav_full set source='nsearchives:cm_old_bhav' where date=%s and deliv_qty is null", (d,)))
        return len(rows)

    # `have` skipping uses listing_history dates, which are sparse: track processed days with the marker instead
    m = c.get_marker(db, NAME)
    orig = since
    if since and m.get("bf_from") and m.get("bf_to") and date.fromisoformat(m["bf_from"]) <= since:
        since = max(since, date.fromisoformat(m["bf_to"]) + timedelta(days=1))      # resume an interrupted backfill
    res = c.run_days(db, NAME, since, until, DEFAULT_SINCE, FLOOR, "equity.nse_bhav_full", load_day, skip_have=False)
    if orig and res["status"] == "ok":
        last = c.get_marker(db, NAME).get("last_date")
        c.set_marker(db, NAME, bf_from=min(orig, date.fromisoformat(m["bf_from"])) if m.get("bf_from") else orig,
                     bf_to=last)
    refresh_listed_flag(db)
    return res


def build_pit_universe(db, as_of: date, top: int = 500, window_days: int = 90, code: Optional[str] = None) -> int:
    """Top-N EQ-series symbols by average daily turnover (price x volume, Rs lakh) over the window ending at as_of.
    Includes names delisted since. Stored in equity.universe_pit."""
    code = code or f"PIT_TOP{top}_TURNOVER"

    def tx(cur):
        cur.execute("""
            insert into equity.universe_pit (universe_code, as_of, rank, symbol, metric)
            select %s, %s, rank() over (order by m desc), symbol, m from (
                select symbol, avg(turnover_lacs) as m from equity.nse_bhav_full
                where series in ('EQ','BE') and date > %s::date - %s and date <= %s and turnover_lacs is not null
                group by symbol order by m desc limit %s) t
            on conflict (universe_code, as_of, symbol) do update set rank=excluded.rank, metric=excluded.metric,
                fetched_at=now()""", (code, as_of, as_of, window_days, as_of, top))
        return cur.rowcount
    return db.run_tx(tx)
