"""5. Credit ratings: document_link rows (doc_type credit_rating, read-only from Supabase) -> public rationale page/PDF
-> text via documents.py extractors (data/documents/<SYM>/credit_rating/) -> equity.credit_rating (best-effort regex)."""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import List, Optional

import psycopg2

from .. import config, documents as docs
from . import _common as c

log = logging.getLogger(__name__)
NAME = "credit_ratings"
TABLE = "equity.credit_rating"
COLS = ["company_id", "url", "agency", "instrument", "rating", "outlook", "action", "date"]
PK = ["company_id", "url", "agency", "instrument", "rating"]
MAX_PER_RUN = 150          # links fetched per run; the daily run keeps working through the backlog

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
AGENCY_WORDS = [("crisil", "CRISIL"), ("icra", "ICRA"), ("care", "CARE"), ("india ratings", "India Ratings"),
                ("indiaratings", "India Ratings"), ("fitch", "India Ratings"), ("brickwork", "Brickwork"),
                ("acuite", "Acuite"), ("ind-ra", "India Ratings")]
LONG = r"(?:AAA|AA|A|BBB|BB|B|C|D)[+-]?"
SHORT = r"A[1-4]\+?|P[1-4]\+?"
RATING_RE = re.compile(
    r"(?P<ag>\[ICRA\]|ICRA|CRISIL|Crisil|CARE|IND|BWR|ACUITE|Acuite)\s*(?:PP-MLD|PP-MLD\s*)?\s*"
    r"(?P<r>" + SHORT + r"|" + LONG + r")(?![A-Za-z0-9])(?:\s*\((?:CE|SO|ind)\))?"
    r"(?:\s*[/;,(]\s*(?P<o>Stable|Positive|Negative|Developing|Watch[A-Za-z ]{0,20}))?")
FITCH_RE = re.compile(r"'(?P<r>" + LONG + r"|" + SHORT + r")\(ind\)'(?:[^.]{0,80}?Outlook\s+(?P<o>Stable|Positive|Negative))?", re.I)
INSTR = [("non-convertible debenture", "NCD"), ("ncd", "NCD"), ("commercial paper", "Commercial Paper"),
         ("bank facilit", "Bank Facilities"), ("term loan", "Term Loan"), ("external commercial borrowing", "ECB"),
         ("fixed deposit", "Fixed Deposit"), ("perpetual", "Perpetual Bonds"), ("bonds", "Bonds"),
         ("working capital", "Working Capital"), ("short term", "Short Term"), ("long term", "Long Term"),
         ("issuer rating", "Issuer Rating"), ("market linked", "MLD"), ("cash credit", "Cash Credit")]
ACTIONS = [("withdrawn", "withdrawn"), ("downgrade", "downgraded"), ("upgrade", "upgraded"),
           ("reaffirm", "reaffirmed"), ("assigned", "assigned"), ("revised", "revised"), ("placed on", "watch"),
           ("rating watch", "watch"), ("retained", "reaffirmed")]


def agency_from(label: Optional[str], url: str, text: str = "") -> str:
    for src in ((label or "").lower(), url.lower(), text[:600].lower()):
        for k, v in AGENCY_WORDS:
            if k in src:
                return v
    return ""


def date_from_label(label: Optional[str], today: Optional[date] = None) -> Optional[date]:
    """'19 Aug from fitch' (no year): latest year such that the date is not in the future."""
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})", label or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    today = today or date.today()
    for y in (today.year, today.year - 1):
        try:
            d = date(y, MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
        if d <= today:
            return d
    return None


def date_from_text(text: str) -> Optional[date]:
    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                  r"\s+(\d{1,2}),\s*(20\d\d)", text[:3000])
    if m:
        return date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    m = re.search(r"\b(\d{1,2})[ -](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ -,]+(20\d\d)", text[:3000], re.I)
    if m:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_text(text: str, label: Optional[str] = None, url: str = "", today: Optional[date] = None) -> List[dict]:
    """Best-effort extraction of (agency, instrument, rating, outlook, action, date) rows from rationale text."""
    text = re.sub(r"\s+", " ", text)
    action = next((v for k, v in ACTIONS if k in text[:4000].lower()), None)
    d = date_from_text(text) or date_from_label(label, today)
    default_ag = agency_from(label, url, text)
    out, seen = [], set()
    for rx in (RATING_RE, FITCH_RE):
        for m in rx.finditer(text):
            gd = m.groupdict()
            ag = default_ag
            if rx is RATING_RE:
                a = gd["ag"].upper().strip("[]")
                ag = {"IND": "India Ratings", "BWR": "Brickwork", "ACUITE": "Acuite"}.get(a, a)
            elif not ag:
                ag = "India Ratings"
            near = text[max(0, m.start() - 140):m.start()].lower()
            instr = ""
            best = -1
            for k, v in INSTR:
                p = near.rfind(k)
                if p > best:
                    best, instr = p, v
            rating = gd["r"].upper()
            key = (ag, instr, rating)
            if key in seen:
                continue
            seen.add(key)
            out.append({"agency": ag, "instrument": instr, "rating": rating,
                        "outlook": (gd.get("o") or "").strip().title() or None, "action": action, "date": d})
            if len(out) >= 40:
                return out
    return out


def read_text(rel_path: str) -> str:
    with gzip.open(config.ROOT / rel_path, "rt", encoding="utf-8") as f:
        return "\n".join(json.loads(l)["text"] for l in f)


def source_links():
    src = psycopg2.connect(config.database_url(), connect_timeout=20)
    src.set_session(readonly=True, autocommit=True)
    with src.cursor() as cur:
        cur.execute("select company_id, url, period_label, source from equity.document_link where doc_type='credit_rating' "
                    "order by company_id, url")
        rows = cur.fetchall()
    src.close()
    return rows


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    c.ensure_companies(db)
    links = source_links()
    with db.conn.cursor() as cur:
        cur.execute("select company_id, nse_symbol from equity.company")
        sym = dict(cur.fetchall())
        cur.execute("select company_id, url, status from equity.document where doc_type='credit_rating'")
        done = {(a, b) for a, b, s in cur.fetchall() if s in docs.TERMINAL}
    db.conn.commit()
    f = c.Fetcher()
    n_rows = n_docs = 0
    status = "ok"
    todo = [l for l in links if (l[0], l[1]) not in done and l[0] in sym]
    log.info("credit_ratings: %d links, %d to do", len(links), len(todo))
    try:
        for cid, url, label, _src in todo[:MAX_PER_RUN]:
            symbol = sym[cid] or str(cid)
            db.run_tx(lambda cur: docs._upsert_pending(cur, cid, "credit_rating", url, label, "original"))
            try:
                r = f.get(url)
                res = docs.process_bytes(r.content, url, symbol, "credit_rating", label)
            except c.NotFound:
                res = {"status": "missing", "error": "404"}
            except c.Blocked:
                raise
            except Exception as e:      # parse/extract problems must not stop the run
                res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            rows = []
            if res.get("status") == "ok":
                for p in parse_text(read_text(res["text_path"]), label, url, today):
                    rows.append((cid, url, p["agency"], p["instrument"], p["rating"], p["outlook"], p["action"], p["date"]))
                if not rows:
                    res = {**res, "status": "no_text", "error": "no rating pattern found"}
            if rows:
                n_rows += c.upsert(db, TABLE, COLS, PK, rows)
            db.run_tx(lambda cur: docs._set_status(cur, cid, "credit_rating", url, res["status"],
                                                    **{k: v for k, v in res.items() if k != "status"}))
            n_docs += 1
    except c.Blocked as e:
        status = "blocked"
        log.error("credit_ratings BLOCKED (%s)", e)
        c.set_marker(db, NAME, blocked_on=today)
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat(), "remaining": max(0, len(todo) - n_docs)}
    if len(todo) - n_docs <= 0:
        kv["complete_on"] = today
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": n_rows, "docs": n_docs, "remaining": kv["remaining"]}
