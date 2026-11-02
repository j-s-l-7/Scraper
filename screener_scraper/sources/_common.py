"""Shared plumbing for public-source loaders: polite GETs (cross-process per-host spacing, robots.txt, block
detection), control-row markers in equity.control, day iteration and upserts."""
from __future__ import annotations

import fcntl
import json
import logging
import re
import time
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import urlparse

import requests
import requests.adapters
from psycopg2.extras import execute_values

from .. import config
from ..rules import BLOCK_RE

log = logging.getLogger(__name__)
UA = config.USER_AGENT
MIN_HOST_INTERVAL = 1.0
LIMIT_DIR = config.ROOT / "data" / ".hostlimit"


class Blocked(Exception):
    """401/403/429 or a bot-check page: final for this run, retry another day."""


class NotFound(Exception):
    """404 (holiday / not published)."""


def _host_wait(host: str, interval: float) -> None:
    """Cross-process spacing: serialise on a lock file holding the last request time."""
    LIMIT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LIMIT_DIR / host.replace(":", "_"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            last = float(f.read().strip() or 0)
        except ValueError:
            last = 0.0
        wait = last + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))
        f.flush()


class LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """Some official hosts (api.mospi.gov.in) still use TLS legacy renegotiation, which OpenSSL 3 refuses by default.
    Re-enabling it is a client TLS compatibility setting only: no headers, cookies or access controls are touched."""

    def init_poolmanager(self, *a, **kw):
        import ssl
        ctx = ssl.create_default_context()
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kw["ssl_context"] = ctx
        super().init_poolmanager(*a, **kw)


class Fetcher:
    def __init__(self, interval: float = MIN_HOST_INTERVAL, legacy_tls_hosts: Sequence[str] = ()):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        for h in legacy_tls_hosts:
            self.s.mount(f"https://{h}", LegacyTLSAdapter())
        self.interval = max(interval, MIN_HOST_INTERVAL)
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def _allowed(self, url: str) -> bool:
        u = urlparse(url)
        host = u.netloc
        if host not in self._robots:
            rp = None
            try:
                _host_wait(host, self.interval)
                r = self.s.get(f"{u.scheme}://{host}/robots.txt", timeout=20)
                head = r.text.lstrip()[:20].lower()
                if r.status_code == 200 and not head.startswith("<"):
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(UA, url)

    def get(self, url: str, *, timeout=(15, 120), tries: int = 3, **kw) -> requests.Response:
        """Plain GET. 404 -> NotFound; 401/403/429 or a bot-check page -> Blocked; 5xx/network retried."""
        if not self._allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        last: Optional[Exception] = None
        for attempt in range(1, tries + 1):
            _host_wait(host, self.interval)
            try:
                r = self.s.get(url, timeout=timeout, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(3 * attempt)
                continue
            st = r.status_code
            if st in (401, 403, 429):
                raise Blocked(f"HTTP {st} for {host}")
            if st in (404, 410):
                raise NotFound(url)
            if st >= 500:
                last = RuntimeError(f"HTTP {st}")
                time.sleep(3 * attempt)
                continue
            if st >= 400:
                raise NotFound(f"HTTP {st} {url}")
            ct = r.headers.get("content-type", "")
            if "text/html" in ct and BLOCK_RE.search(r.text[:4000]):
                raise Blocked(f"bot-check page from {host}")
            return r
        raise RuntimeError(f"fetch failed {url}: {last}")


# ───────── numbers / dates ─────────
def num(x) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "--", "nan", "NaN", "None", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def intn(x) -> Optional[int]:
    v = num(x)
    return None if v is None else int(round(v))


def weekdays(since: date, until: date) -> Iterator[date]:
    d = since
    while d <= until:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def now_ist() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


# ───────── control markers ─────────
def get_marker(db, name: str) -> dict:
    with db.conn.cursor() as cur:
        cur.execute("select value from equity.control where key=%s", (f"source:{name}",))
        r = cur.fetchone()
    db.conn.commit()
    return dict(r[0]) if r and isinstance(r[0], dict) else {}


def set_marker(db, name: str, **kv) -> dict:
    m = get_marker(db, name)
    m.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in kv.items()})
    with db.conn, db.conn.cursor() as cur:
        cur.execute("""insert into equity.control (key, value, reason) values (%s, %s, 'sources')
                       on conflict (key) do update set value=excluded.value, updated_at=now()""",
                    (f"source:{name}", json.dumps(m)))
    return m


def upsert(db, table: str, cols: Sequence[str], pk: Sequence[str], rows: List[tuple], page: int = 5000,
           ts_col: str = "loaded_at") -> int:
    """INSERT ... ON CONFLICT (pk) DO UPDATE in one transaction; rows deduplicated on the pk (last wins)."""
    if not rows:
        return 0
    idx = [cols.index(c) for c in pk]
    d = {tuple(r[i] for i in idx): r for r in rows}
    rows = list(d.values())
    upd = [c for c in cols if c not in pk]
    sql = (f"insert into {table} ({', '.join(cols)}) values %s on conflict ({', '.join(pk)}) do update set "
           + ", ".join(f"{c}=excluded.{c}" for c in upd) + f", {ts_col}=now()")

    def tx(cur):
        for i in range(0, len(rows), page):
            execute_values(cur, sql, rows[i:i + page], page_size=page)
    db.run_tx(tx)
    return len(rows)


def loaded_dates(db, table: str, col: str = "date") -> set:
    with db.conn.cursor() as cur:
        cur.execute(f"select distinct {col} from {table}")
        s = {r[0] for r in cur.fetchall()}
    db.conn.commit()
    return s


def ensure_companies(db) -> int:
    """The lake may have no company rows yet: copy the master from Supabase (read-only) so joins work."""
    with db.conn.cursor() as cur:
        cur.execute("select count(*) from equity.company")
        n = cur.fetchone()[0]
    db.conn.commit()
    if n:
        return n
    import psycopg2
    src = psycopg2.connect(config.database_url(session=True), connect_timeout=20)
    src.set_session(readonly=True, autocommit=True)
    with src.cursor() as c:
        c.execute("select * from equity.company")
        cols = [d[0] for d in c.description]
        rows = c.fetchall()
    src.close()
    upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "company_id")

    def tx(cur):
        execute_values(cur, f"insert into equity.company ({', '.join(cols)}) values %s "
                            f"on conflict (company_id) do update set {upd}", rows)
    db.run_tx(tx)
    return len(rows)


# ───────── generic per-day runner for date-addressed files ─────────
def run_days(db, name: str, since: Optional[date], until: Optional[date], default_since: date, floor: date,
             table: str, load_day: Callable[[date], Optional[int]], fetch_cls_blocked=Blocked,
             force_all: bool = False, skip_have: bool = True) -> dict:
    """since=None resumes from the marker. load_day(d) returns rows loaded or None for 'not published' (holiday).
    The marker only advances over contiguous definitive days; today's 404 is not definitive."""
    marker = get_marker(db, name)
    today = today_ist()
    until = min(until or today, today)
    if marker.get("blocked_on") == today.isoformat():
        log.warning("%s: blocked earlier today, retry tomorrow", name)
        return {"status": "blocked", "rows": 0, "days": 0}
    backfill = since is not None
    if since is None:
        last = marker.get("last_date")
        since = date.fromisoformat(last) + timedelta(days=1) if last else default_since
    since = max(since, floor)
    have = set() if (force_all or not skip_have) else (loaded_dates(db, table) if backfill else set())
    rows = days = miss = 0
    status = "ok"
    last_ok = None
    try:
        for d in weekdays(since, until):
            if d in have:
                last_ok = d
                continue
            n = load_day(d)
            if n is None:
                miss += 1
                if d >= today:
                    break
            else:
                rows += n
                days += 1
            last_ok = d
            if days % 25 == 0 and days:
                log.info("%s: %s rows=%d days=%d", name, d, rows, days)
    except Blocked as e:
        status = "blocked"
        log.error("%s: BLOCKED (%s); final for this run", name, e)
        set_marker(db, name, blocked_on=today)
    except Exception:       # keep the progress so far: the next run resumes after it instead of from the start
        db.conn.rollback()
        if last_ok and (not marker.get("last_date") or last_ok.isoformat() > marker["last_date"]):
            set_marker(db, name, last_date=last_ok)
        raise
    fields = {"status": status, "run_at": datetime.now(timezone.utc).isoformat()}
    if last_ok and (not marker.get("last_date") or last_ok.isoformat() > marker["last_date"] or not backfill):
        # a backfill that started earlier than the marker must not move the marker backwards
        if not backfill or not marker.get("last_date") or last_ok.isoformat() > marker["last_date"]:
            fields["last_date"] = last_ok
    set_marker(db, name, **fields)
    log.info("%s: %s rows=%d days=%d holidays=%d", name, status, rows, days, miss)
    return {"status": status, "rows": rows, "days": days, "holidays": miss}
