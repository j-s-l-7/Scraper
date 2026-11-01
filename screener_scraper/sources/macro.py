"""6. Macro (schema `macro`): yfinance daily indices/FX/commodities, World Bank annual India indicators.
RBI DBIE has no plain static file URL (JS portal), so it is skipped."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from . import _common as c

log = logging.getLogger(__name__)
NAME = "macro"
YF = {  # code -> (name, unit)
    "^NSEI": ("Nifty 50", "index"), "^BSESN": ("BSE Sensex", "index"), "^NSEBANK": ("Nifty Bank", "index"),
    "^CNXIT": ("Nifty IT", "index"), "^CNXAUTO": ("Nifty Auto", "index"), "^CNXFMCG": ("Nifty FMCG", "index"),
    "^CNXPHARMA": ("Nifty Pharma", "index"), "^CNXMETAL": ("Nifty Metal", "index"),
    "^CNXENERGY": ("Nifty Energy", "index"), "^CNXREALTY": ("Nifty Realty", "index"),
    "^CNXPSUBANK": ("Nifty PSU Bank", "index"), "^CNXMEDIA": ("Nifty Media", "index"),
    "^CNXINFRA": ("Nifty Infrastructure", "index"), "^CNXPSE": ("Nifty PSE", "index"),
    "^CNXMNC": ("Nifty MNC", "index"), "^CNXSERVICE": ("Nifty Services", "index"),
    "^CNXCMDT": ("Nifty Commodities", "index"), "^CNXCONSUM": ("Nifty Consumption", "index"),
    "^NSMIDCP": ("Nifty Next 50", "index"), "^NSEMDCP50": ("Nifty Midcap 50", "index"),
    "^CNX100": ("Nifty 100", "index"), "^CNX200": ("Nifty 200", "index"), "^CRSLDX": ("Nifty 500", "index"),
    "NIFTY_FIN_SERVICE.NS": ("Nifty Financial Services", "index"),
    "INR=X": ("USD/INR", "INR per USD"), "CL=F": ("Crude oil WTI futures", "USD/bbl"),
    "GC=F": ("Gold futures", "USD/oz"), "^TNX": ("US 10Y Treasury yield", "pct"), "^VIX": ("CBOE VIX", "index"),
    "^INDIAVIX": ("India VIX", "index"),
}
MIN_ROWS = 20           # series with less history (delisted tickers) are skipped
WB = {  # World Bank indicator -> (name, unit)
    "NY.GDP.MKTP.CD": ("GDP, current US$", "USD"), "NY.GDP.MKTP.KD.ZG": ("GDP growth", "pct"),
    "NY.GDP.PCAP.CD": ("GDP per capita, current US$", "USD"), "FP.CPI.TOTL.ZG": ("CPI inflation", "pct"),
    "FP.CPI.TOTL": ("Consumer price index (2010=100)", "index"), "SL.UEM.TOTL.ZS": ("Unemployment rate", "pct"),
    "NE.EXP.GNFS.ZS": ("Exports of goods and services, % of GDP", "pct"),
    "NE.IMP.GNFS.ZS": ("Imports of goods and services, % of GDP", "pct"),
    "BN.CAB.XOKA.GD.ZS": ("Current account balance, % of GDP", "pct"), "PA.NUS.FCRF": ("Official exchange rate, INR per USD", "INR"),
    "FR.INR.RINR": ("Real interest rate", "pct"), "FR.INR.LEND": ("Lending interest rate", "pct"),
    "GC.DOD.TOTL.GD.ZS": ("Central government debt, % of GDP", "pct"),
    "BX.KLT.DINV.WD.GD.ZS": ("FDI net inflows, % of GDP", "pct"), "FM.LBL.BMNY.GD.ZS": ("Broad money, % of GDP", "pct"),
    "NY.GNS.ICTR.ZS": ("Gross savings, % of GDP", "pct"), "NE.GDI.TOTL.ZS": ("Gross capital formation, % of GDP", "pct"),
    "FS.AST.PRVT.GD.ZS": ("Domestic credit to private sector, % of GDP", "pct"),
}
WB_URL = "https://api.worldbank.org/v2/country/IND/indicator/{code}?format=json&per_page=20000"


def parse_worldbank(payload) -> List[tuple]:
    """[meta, [obs...]] -> [(year, value)] skipping nulls."""
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return []
    out = []
    for o in payload[1]:
        if o.get("value") is None:
            continue
        try:
            out.append((int(o["date"]), float(o["value"])))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def frame_to_rows(code: str, df) -> List[tuple]:
    rows = []
    for ts, v in df["Close"].dropna().items():
        rows.append((code, ts.date(), float(v)))
    return rows


def run_yf(db, since: Optional[date]) -> int:
    import warnings
    import yfinance as yf
    warnings.filterwarnings("ignore")
    total = 0
    for code, (name, unit) in YF.items():
        try:
            c._host_wait("query1.finance.yahoo.com", 1.0)
            t = yf.Ticker(code)
            df = t.history(start=str(since), auto_adjust=False) if since else t.history(period="max", auto_adjust=False)
        except Exception as e:
            log.warning("macro %s: %s", code, str(e)[:100])
            continue
        if df is None or len(df) == 0:
            continue
        if not since and len(df) < MIN_ROWS:
            continue
        rows = frame_to_rows(code, df)
        db.run_tx(lambda cur: cur.execute(
            "insert into macro.series (code,name,source,unit,freq) values (%s,%s,'yfinance',%s,'D') "
            "on conflict (code) do update set name=excluded.name, unit=excluded.unit", (code, name, unit)))
        total += c.upsert(db, "macro.series_daily", ["code", "date", "value"], ["code", "date"], rows)
    return total


def run_wb(db) -> int:
    f = c.Fetcher()
    total = 0
    for code, (name, unit) in WB.items():
        try:
            r = f.get(WB_URL.format(code=code))
            rows = parse_worldbank(r.json())
        except c.NotFound:
            continue
        if not rows:
            continue
        db.run_tx(lambda cur: cur.execute(
            "insert into macro.series (code,name,source,unit,freq) values (%s,%s,'worldbank',%s,'A') "
            "on conflict (code) do update set name=excluded.name, unit=excluded.unit", (code, name, unit)))
        total += c.upsert(db, "macro.series_annual", ["code", "year", "value"], ["code", "year"],
                          [(code, y, v) for y, v in rows])
    return total


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    status = "ok"
    n = 0
    try:
        resume = since or (date.fromisoformat(m["last_date"]) - timedelta(days=7) if m.get("last_date") else None)
        n += run_yf(db, resume)
        n += run_wb(db)
    except c.Blocked as e:
        status = "blocked"
        c.set_marker(db, NAME, blocked_on=today)
        log.error("macro BLOCKED (%s)", e)
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if status == "ok":
        kv["last_date"] = today
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": n}
