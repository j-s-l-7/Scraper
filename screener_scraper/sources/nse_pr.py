"""NSE daily PR archive (static nsearchives zip, back to Jan 2010): two of its files.

  Bc<ddmmyy>.csv    book closures / record dates with the announced purpose -> equity.nse_corp_action (raw text)
  mcap<ddmmyyyy>.csv face value + issue size (shares listed) per security    -> equity.nse_issue_size (change log)

Corporate actions are announced ahead of the ex-date (a 2026 file lists the next ~10 days, a 2014 file ~6 weeks), so
one zip per trading day sees every action at least once. The source chain (screener_scraper/completeness.py) turns
the purpose text into equity.corporate_action rows and the issue size into equity.shares_outstanding.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from . import _common as c

NAME = "nse_pr"
TABLE = "equity.nse_corp_action"
URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"
FLOOR = date(2010, 1, 4)             # earliest zip the archive serves (probed 2026-11-04)
DEFAULT_SINCE = date(2026, 10, 1)   # daily runs start here; history comes from `sources --only nse_pr --backfill-from 2010-01-04`
READY_HOUR_IST = 19
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
CA_COLS = ["symbol", "ex_date", "purpose", "series", "record_date", "bc_start", "bc_end"]
IS_COLS = ["symbol", "date", "series", "face_value", "issue_size"]


def parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_bc(text: str) -> List[tuple]:
    """Equity-series rows with an ex-date; one row per (symbol, ex_date, purpose) (EQ and BE repeat the action)."""
    out: Dict[tuple, tuple] = {}
    for r in csv.DictReader(io.StringIO(text)):
        # a malformed row has extra fields: DictReader files them under key None as a list
        r = {k.strip().upper(): (v or "").strip() for k, v in r.items() if isinstance(k, str) and isinstance(v, (str, type(None)))}
        series, sym, purpose = r.get("SERIES", ""), r.get("SYMBOL", ""), " ".join(r.get("PURPOSE", "").split())
        ex = parse_date(r.get("EX_DT", ""))
        if series not in EQUITY_SERIES or not sym or not purpose or ex is None:
            continue
        key = (sym, ex, purpose)
        if key not in out or series == "EQ":
            out[key] = (sym, ex, purpose, series, parse_date(r.get("RECORD_DT", "")),
                        parse_date(r.get("BC_STRT_DT", "")), parse_date(r.get("BC_END_DT", "")))
    return list(out.values())


def parse_mcap(text: str, d: date) -> List[Tuple[str, str, Optional[float], Optional[int]]]:
    """(symbol, series, face_value, issue_size) for equity series. Header names vary in case and spacing."""
    rd = csv.reader(io.StringIO(text))
    try:
        hdr = [h.strip().lower() for h in next(rd)]
    except StopIteration:
        return []
    col = lambda *keys: next((i for i, h in enumerate(hdr) if any(k in h for k in keys)), None)
    i_sym, i_ser, i_fv, i_is = col("symbol"), col("series"), col("face value"), col("issue size")
    if None in (i_sym, i_ser, i_is):
        return []
    out = {}
    for r in rd:
        if len(r) <= max(i_sym, i_ser, i_is):
            continue
        sym, ser = r[i_sym].strip(), r[i_ser].strip()
        if ser not in EQUITY_SERIES or not sym:
            continue
        iss = c.intn(r[i_is].strip())
        if not iss:
            continue
        fv = c.num(r[i_fv].strip()) if i_fv is not None else None
        if sym not in out or ser == "EQ":
            out[sym] = (sym, ser, fv, iss)
    return list(out.values())


def read_zip(raw: bytes) -> Tuple[Optional[str], Optional[str]]:
    z = zipfile.ZipFile(io.BytesIO(raw))
    bc = mc = None
    for n in z.namelist():
        ln = n.lower().rsplit("/", 1)[-1]
        if ln.startswith("bc") and ln.endswith(".csv"):
            bc = z.read(n).decode("latin-1")
        elif ln.startswith("mcap") and ln.endswith(".csv"):
            mc = z.read(n).decode("latin-1")
    return bc, mc


def _last_issue(db, before: date) -> Dict[str, Tuple]:
    with db.conn.cursor() as cur:
        cur.execute("""select distinct on (symbol) symbol, face_value, issue_size from equity.nse_issue_size
                       where date < %s order by symbol, date desc""", (before,))
        out = {s: (float(fv) if fv is not None else None, iss) for s, fv, iss in cur.fetchall()}
    db.conn.commit()
    return out


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    f = c.Fetcher()
    marker = c.get_marker(db, NAME)
    start = since or (date.fromisoformat(marker["last_date"]) if marker.get("last_date") else DEFAULT_SINCE)
    last = _last_issue(db, start)            # change log: compare each day with the symbol's previous row

    def load_day(d: date) -> Optional[int]:
        try:
            r = f.get(URL.format(d=d))
        except c.NotFound:
            return None
        bc, mc = read_zip(r.content)
        n = c.upsert(db, TABLE, CA_COLS, ["symbol", "ex_date", "purpose"], parse_bc(bc) if bc else [],
                     ts_col="loaded_at")
        if mc:
            changed = []
            for sym, ser, fv, iss in parse_mcap(mc, d):
                if last.get(sym) != (fv, iss):
                    changed.append((sym, d, ser, fv, iss))
                    last[sym] = (fv, iss)
            n += c.upsert(db, "equity.nse_issue_size", IS_COLS, ["symbol", "date"], changed, ts_col="loaded_at")
        return n
    return c.run_days(db, NAME, since, until, DEFAULT_SINCE, FLOOR, TABLE, load_day, skip_have=False)
