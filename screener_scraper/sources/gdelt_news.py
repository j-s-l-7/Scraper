"""7. GDELT DOC 2.0 news per active company (ArtList). >=6s between calls; a rate-limit reply is final for the run.
tone is not part of ArtList output and stays NULL."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from . import _common as c

log = logging.getLogger(__name__)
NAME = "gdelt_news"
TABLE = "equity.news_article"
COLS = ["company_id", "url", "title", "seendate", "domain", "tone", "lang"]
PK = ["company_id", "url"]
API = "https://api.gdeltproject.org/api/v2/doc/doc"
INTERVAL = 6.0
BACKFILL_DAYS = 90
CHUNK_DAYS = 15
SUFFIX = re.compile(r"\b(limited|ltd\.?|corporation|corp\.?|company|co\.|\(india\))\s*$", re.I)


def clean_name(name: str) -> str:
    n = name.strip()
    for _ in range(2):
        n = SUFFIX.sub("", n).strip(" ,.")
    return n


def parse(payload, company_id: int) -> List[tuple]:
    out = []
    for a in (payload or {}).get("articles", []) or []:
        url = a.get("url")
        if not url:
            continue
        try:
            seen = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            seen = None
        out.append((company_id, url, a.get("title"), seen, a.get("domain"), None, a.get("language")))
    return out


def fetch(f: c.Fetcher, name: str, start: datetime, end: datetime):
    params = {"query": f'"{name}" sourcecountry:IN', "mode": "ArtList", "format": "json", "maxrecords": 250,
              "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": end.strftime("%Y%m%d%H%M%S")}
    r = f.get(API, params=params, timeout=(15, 60))
    body = r.text.strip()
    if body.lower().startswith("please limit") or "limit requests" in body[:200].lower():
        raise c.Blocked("GDELT rate limit reply")
    if not body or body == "{}":
        return {}
    try:
        return r.json()
    except ValueError:
        log.warning("gdelt: non-JSON reply %r", body[:80])
        return {}


def run(db, since: Optional[date] = None, until: Optional[date] = None) -> dict:
    today = c.today_ist()
    m = c.get_marker(db, NAME)
    if m.get("blocked_on") == today.isoformat():
        return {"status": "blocked", "rows": 0}
    c.ensure_companies(db)
    with db.conn.cursor() as cur:
        cur.execute("select company_id, name from equity.company where coalesce(is_active, true) order by company_id")
        comps = cur.fetchall()
    db.conn.commit()
    if since is None:
        since = today - timedelta(days=BACKFILL_DAYS) if not m.get("backfill_done") else today - timedelta(days=1)
    full = (today - since).days > 2
    label = f"{since}->{today}"
    done = set(map(tuple, m["done"])) if m.get("label") == label else set()
    end_dt = datetime.now(timezone.utc)
    chunks, d = [], since
    while d <= today:
        e = min(d + timedelta(days=CHUNK_DAYS), today + timedelta(days=1))
        chunks.append((datetime.combine(d, datetime.min.time()), min(datetime.combine(e, datetime.min.time()), end_dt.replace(tzinfo=None))))
        d += timedelta(days=CHUNK_DAYS)
    f = c.Fetcher(interval=INTERVAL)
    rows = calls = 0
    status = "ok"
    try:
        for cid, name in comps:
            for i, (s, e) in enumerate(chunks):
                if (cid, i) in done:
                    continue
                payload = fetch(f, clean_name(name), s, e)
                calls += 1
                rows += c.upsert(db, TABLE, COLS, PK, parse(payload, cid))
                done.add((cid, i))
                if calls % 20 == 0:
                    c.set_marker(db, NAME, label=label, done=sorted(done))
    except c.Blocked as e:
        status = "blocked"
        log.error("gdelt BLOCKED (%s); retry another day", e)
        c.set_marker(db, NAME, blocked_on=today, label=label, done=sorted(done))
    kv = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if status == "ok":
        kv.update(label=label, done=[], last_date=today)
        if full:
            kv["backfill_done"] = True
    c.set_marker(db, NAME, **kv)
    return {"status": status, "rows": rows, "calls": calls}
