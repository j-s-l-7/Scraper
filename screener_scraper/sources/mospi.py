"""MoSPI eSankhyiki official API (api.mospi.gov.in, no key, documented swagger): monthly CPI (base 2012 and, when
published, base 2024 as separate codes: no splice is computed), IIP and WPI headline. GDP comes from the World Bank
loader (the NAS endpoint parameters are not documented well enough to use safely)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from . import _common as c

log = logging.getLogger(__name__)
NAME = "mospi"
BASE = "https://api.mospi.gov.in/api/"
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], 1)}
CPI_BASES = ["2012", "2024"]
START_YEAR = 2012
CATALOG = {}      # code -> (name, unit, base_year)


def _code(*parts) -> str:
    return "_".join(p.upper().replace(" ", "").replace("-", "").replace("&", "AND") for p in parts)


def parse_cpi(payload: dict) -> List[tuple]:
    """All-India combined rows -> (code, period, value, base_year) for the index and the inflation rate."""
    out = []
    for r in payload.get("data") or []:
        if r.get("state") != "All India" or r.get("sector") != "Combined" or r.get("month") not in MONTHS:
            continue
        per = date(int(r["year"]), MONTHS[r["month"]], 1)
        base = str(r.get("baseyear"))
        for fld, kind in (("index", "INDEX"), ("inflation", "INFL")):
            v = c.num(r.get(fld))
            if v is not None:
                code = _code("MOSPI_CPI", base, r.get("group") or "", r.get("subgroup") or "", kind)
                out.append((code, per, v, base))
                CATALOG[code] = (f"CPI base {base}: {r.get('group')} / {r.get('subgroup')} ({kind.lower()}). "
                                 f"Base-2012 and base-2024 are distinct series, not spliced", "index" if kind == "INDEX" else "pct", base)
    return out


def parse_iip(payload: dict) -> List[tuple]:
    out = []
    for r in payload.get("data") or []:
        if r.get("month") not in MONTHS:
            continue
        per = date(int(r["year"]), MONTHS[r["month"]], 1)
        base = str(r.get("base_year"))
        for fld, kind in (("index", "INDEX"), ("growth_rate", "GROWTH")):
            v = c.num(r.get(fld))
            if v is not None:
                code = _code("MOSPI_IIP", base, r.get("type") or "", r.get("category") or "", r.get("sub_category") or "", kind)
                out.append((code, per, v, base))
                CATALOG[code] = (f"IIP base {base}: {r.get('type')} / {r.get('category')} {r.get('sub_category') or ''} ({kind.lower()})".strip(),
                                 "index" if kind == "INDEX" else "pct", base)
    return out


def parse_wpi(payload: dict) -> List[tuple]:
    out = []
    for r in payload.get("data") or []:
        if r.get("month") not in MONTHS or r.get("majorgroup") != "Wholesale price index" or r.get("group"):
            continue
        v = c.num(r.get("index_value"))
        if v is None:
            continue
        base = str(r.get("base_year"))
        code = _code("MOSPI_WPI", base, "ALL", "INDEX")
        out.append((code, date(int(r["year"]), MONTHS[r["month"]], 1), v, base))
        CATALOG[code] = (f"WPI all commodities index, base {base}", "index", base)
    return out


def _pages(f: c.Fetcher, path: str, params: dict, max_pages: int = 60, first_only: bool = False):
    page = 1
    while page <= max_pages:
        r = f.get(BASE + path, params={**params, "page": page}, timeout=(15, 60))
        if "json" not in r.headers.get("content-type", ""):
            return
        d = r.json()
        yield d
        tp = (d.get("meta_data") or {}).get("totalPages") or 1
        if first_only or page >= tp:
            return
        page += 1


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    start = since.year if since else (today.year - 1 if m.get("last_date") else START_YEAR)
    f = c.Fetcher(interval=1.5, legacy_tls_hosts=["api.mospi.gov.in"])
    rows = []
    status = "ok"
    try:
        for y in range(start, today.year + 1):
            for base in CPI_BASES:
                try:
                    for d in _pages(f, "cpi/getCPIIndex", {"base_year": base, "year": y, "series": "Current",
                                                           "state_code": 99, "sector_code": 3}):
                        rows += parse_cpi(d)
                except (c.NotFound, RuntimeError) as e:
                    log.warning('mospi skip: %s', str(e)[:90])
            try:
                for d in _pages(f, "iip/getIIPMonthly", {"base_year": "2011-12", "year": y}):
                    rows += parse_iip(d)
            except (c.NotFound, RuntimeError) as e:
                log.warning('mospi skip: %s', str(e)[:90])
            for mc in range(1, 13):
                if date(y, mc, 1) > today:
                    break
                try:
                    for d in _pages(f, "wpi/getWpiRecords", {"year": y, "month_code": mc}, first_only=True):
                        rows += parse_wpi(d)
                except (c.NotFound, RuntimeError) as e:
                    log.warning('mospi skip: %s', str(e)[:90])
            log.info("mospi: year %d done, rows=%d", y, len(rows))
    except c.Blocked as e:
        status = "blocked"
        c.set_marker(db, NAME, blocked_on=today)
        log.error("mospi BLOCKED (%s)", e)
    n = 0
    if rows:
        for code, (name, unit, base) in CATALOG.items():
            db.run_tx(lambda cur: cur.execute(
                "insert into macro.series (code,name,source,unit,freq,base_year,fetched_at) "
                "values (%s,%s,'mospi_esankhyiki',%s,'M',%s,now()) on conflict (code) do update set "
                "name=excluded.name, unit=excluded.unit, base_year=excluded.base_year, fetched_at=now()",
                (code, name[:300], unit, base)))
        n = c.upsert(db, "macro.series_monthly", ["code", "period", "value", "base_year"], ["code", "period"],
                     [r for r in rows], ts_col="fetched_at")
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if status == "ok":
        kv["last_date"] = today
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": n}
