"""NSDL FPI Monitor (fpi.nsdl.co.in): daily flows from the Latest report page (plain GET; history accrues one day per
run, the older daily reports need ASP.NET form posts so they are not fetched) and the fortnightly sector-wise static
report pages (FIIInvestSector_<Mon><DD><YYYY>.html)."""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from . import _common as c

log = logging.getLogger(__name__)
NAME = "nsdl_fpi"
LATEST = "https://www.fpi.nsdl.co.in/web/Reports/Latest.aspx"
SECTOR = "https://www.fpi.nsdl.co.in/web/StaticReports/Fortnightly_Sector_wise_FII_Investment_Data/FIIInvestSector_{d:%b}{d:%d}{d:%Y}.html"
DAILY_COLS = ["date", "category", "route", "gross_purchases", "gross_sales", "net_inr_cr", "net_usd_mn", "usd_inr",
              "source"]
SECT_COLS = ["report_date", "sector", "measure", "route", "inr_cr", "usd_mn", "source"]
SECTOR_FLOOR = date(2014, 4, 1)
MEASURES = ["auc_prev", "net_prev", "net_curr", "auc_end"]


def _n(x) -> Optional[float]:
    s = (x or "").strip().replace(",", "").replace("Rs.", "")
    if not s or s == "-":
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def parse_latest(html: str) -> List[tuple]:
    s = BeautifulSoup(html, "lxml")
    out, d, cat, usd_inr = [], None, None, None
    for tr in s.find_all("tr"):
        cells = [x.get_text(" ", strip=True) for x in tr.find_all(["td", "th"])]
        if cells and "Contracts" in cells[0]:
            break                                    # derivatives table starts: not part of the flows
        if len(cells) == 8 and re.match(r"\d{1,2}-[A-Za-z]{3}-\d{4}$", cells[0]):
            d = datetime.strptime(cells[0], "%d-%b-%Y").date()
            cat, route, nums = cells[1], cells[2], cells[3:7]
            usd_inr = _n(cells[7])
        elif d is None:
            continue
        elif len(cells) == 6:
            cat, route, nums = cells[0], cells[1], cells[2:6]
        elif len(cells) == 5 and _n(cells[1]) is not None:
            route, nums = cells[0], cells[1:5]
            if route == "Total":
                cat = "Total"
        else:
            if len(cells) == 6 and "Contracts" in cells[0]:
                break
            continue
        if "Contracts" in route:
            break
        v = [_n(x) for x in nums]
        out.append((d, cat, route, v[0], v[1], v[2], v[3], usd_inr, "nsdl_fpi_latest"))
    return out


def parse_sector(html: str, report_date: date) -> List[tuple]:
    s = BeautifulSoup(html, "lxml")
    rows = s.find_all("tr")
    names, data = None, []
    for tr in rows:
        cells = [x.get_text(" ", strip=True) for x in tr.find_all(["td", "th"])]
        if cells and cells[0].startswith("Sr") and len(cells) > 40 and names is None:
            names = cells[2:]
            names = names[:len(names) // 8] if len(names) % 8 == 0 else names
        elif cells and cells[0].isdigit() and len(cells) > 10:
            data.append(cells)
    if not names or not data:
        return []
    seen = set()
    for i, nm in enumerate(names):          # the mutual-fund block repeats Equity / Debt General Limit / Hybrid
        if nm in seen:
            names[i] = "MF " + nm
        seen.add(nm)
    k = len(names)                         # route columns per group (12 in the current layout)
    out = []
    for cells in data:
        sector, vals = cells[1], cells[2:]
        groups = len(vals) // k
        if groups != 8:
            continue
        for g in range(8):
            measure, ccy = MEASURES[g // 2], g % 2
            for j in range(k):
                v = _n(vals[g * k + j])
                if v is None:
                    continue
                out.append((report_date, sector, measure, names[j], v if ccy == 0 else None, v if ccy else None,
                            "nsdl_fpi_static"))
    # merge INR / USD rows of the same key
    merged = {}
    for r in out:
        key = r[:4]
        cur = merged.get(key)
        merged[key] = r if cur is None else (*cur[:4], cur[4] if cur[4] is not None else r[4],
                                             cur[5] if cur[5] is not None else r[5], r[6])
    return list(merged.values())


def fortnight_ends(since: date, until: date):
    y, m = since.year, since.month
    while date(y, m, 1) <= until:
        for d in (date(y, m, 15), date(y, m, calendar.monthrange(y, m)[1])):
            if since <= d <= until:
                yield d
        m += 1
        if m == 13:
            y, m = y + 1, 1


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    f = c.Fetcher()
    rows = 0
    status = "ok"
    try:
        r = f.get(LATEST)
        rows += c.upsert(db, "macro.fpi_flow_daily", DAILY_COLS, ["date", "category", "route"], parse_latest(r.text),
                         ts_col="fetched_at")
        start = max(since or (date.fromisoformat(m["sector_last"]) - timedelta(days=45) if m.get("sector_last")
                              else SECTOR_FLOOR), SECTOR_FLOOR)
        have = c.loaded_dates(db, "macro.fpi_sector_fortnightly", "report_date") if since else set()
        last = None
        for d in fortnight_ends(start, until or today):
            if d in have:
                last = d
                continue
            found = False
            for dd in (d, d + timedelta(days=1)) if d.day == 15 else (d,):
                try:
                    r = f.get(SECTOR.format(d=dd))
                except c.NotFound:
                    continue
                if len(r.content) < 5000:          # soft-404 page
                    continue
                parsed = parse_sector(r.text, d)
                if parsed:
                    rows += c.upsert(db, "macro.fpi_sector_fortnightly", SECT_COLS,
                                     ["report_date", "sector", "measure", "route"], parsed, ts_col="fetched_at")
                    found = True
                    break
            if found:
                last = d
    except c.Blocked as e:
        status = "blocked"
        c.set_marker(db, NAME, blocked_on=today)
        log.error("nsdl_fpi BLOCKED (%s)", e)
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if status == "ok":
        kv["last_date"] = today
        if last:
            kv["sector_last"] = last
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": rows}
