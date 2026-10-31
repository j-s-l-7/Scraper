"""Job queue on equity.job: enqueue, claim (SKIP LOCKED), heartbeat, complete/fail, stale reclaim, control switches.

All functions take a psycopg2 connection in AUTOCOMMIT mode (db.trail, or a dedicated connection).
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional, Sequence

from psycopg2.extras import Json, RealDictCursor

from . import config
from .rules import Decision, STALE

log = logging.getLogger(__name__)

KINDS = ("screener", "yf", "docs_ar", "docs_concall", "docs_ppt")
DOC_KINDS = ("docs_ar", "docs_concall", "docs_ppt")
DOC_TYPE_OF = {"docs_ar": "annual_report", "docs_concall": "concall_transcript", "docs_ppt": "concall_ppt"}
STALE_MINUTES = 10
OPEN = ("pending", "retry", "running")


def _hist(worker: Optional[str], error_class: str, action: str, **extra) -> str:
    return json.dumps([{"at": _now_iso(), "worker": worker, "error_class": error_class, "action": action, **extra}])


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ───────── control switches ─────────
def get_control(conn, key: str, default=None):
    with conn.cursor() as cur:
        cur.execute("select value from equity.control where key=%s", (key,))
        r = cur.fetchone()
    return default if r is None else r[0]


def set_control(conn, key: str, value, reason: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute("""insert into equity.control(key, value, reason) values (%s,%s,%s)
                       on conflict (key) do update set value=excluded.value, reason=excluded.reason, updated_at=now()""",
                    (key, Json(value), reason[:500]))


def is_paused(conn) -> bool:
    return bool(get_control(conn, "paused", False))


# ───────── enqueue ─────────
def enqueue(conn, batch: str, universe: str = config.UNIVERSE_CODE, kinds: Sequence[str] = KINDS,
            symbols: Optional[Sequence[str]] = None) -> int:
    """One job per kind per company in the latest `universe` (priority = rank). Idempotent. Returns rows inserted."""
    sql = """insert into equity.job (batch, kind, company_id, priority)
             select %s, k, u.company_id, least(coalesce(u.rank, 100), 32000)
             from equity.universe u join equity.company c using (company_id), unnest(%s::text[]) k
             where u.universe_code = %s
               and u.as_of = (select max(as_of) from equity.universe where universe_code = %s)"""
    args: list = [batch, list(kinds), universe, universe]
    if symbols:
        sql += " and (c.nse_symbol = any(%s) or c.screener_slug = any(%s))"
        args += [list(symbols), list(symbols)]
    sql += " on conflict (batch, kind, company_id) do nothing"
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


def enqueue_companies(conn, batch: str, company_ids: Sequence[int], kinds: Sequence[str] = KINDS,
                      priority: int = 100) -> int:
    """Enqueue explicit company ids (tests, ad-hoc)."""
    n = 0
    with conn.cursor() as cur:
        for cid in company_ids:
            for k in kinds:
                cur.execute("""insert into equity.job (batch, kind, company_id, priority) values (%s,%s,%s,%s)
                               on conflict (batch, kind, company_id) do nothing""", (batch, k, cid, priority))
                n += cur.rowcount
    return n


# ───────── claim ─────────
CLAIM_SQL = """
update equity.job set status='running', locked_by=%(worker)s, heartbeat_at=now(), attempts=attempts+1, updated_at=now()
where job_id = (
    select j.job_id from equity.job j
    where j.kind = any(%(kinds)s) and j.status in ('pending','retry') and j.next_run_at <= now()
      and j.route in ('any', %(cls)s)
      and (%(batch)s::text is null or j.batch = %(batch)s)
      and (j.kind not in ('docs_ar','docs_concall','docs_ppt')
           or exists (select 1 from equity.document_link dl where dl.company_id = j.company_id)
           or exists (select 1 from equity.job s where s.batch = j.batch and s.company_id = j.company_id
                        and s.kind = 'screener' and s.status = 'done'))
    order by j.priority, j.job_id for update skip locked limit 1)
returning *"""


def claim(conn, worker: str, kinds: Sequence[str], cls: str = "local", batch: Optional[str] = None) -> Optional[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(CLAIM_SQL, {"worker": worker, "kinds": list(kinds), "cls": cls, "batch": batch})
        r = cur.fetchone()
        return dict(r) if r else None


def heartbeat(conn, job_ids: Iterable[int]) -> None:
    ids = list(job_ids)
    if ids:
        with conn.cursor() as cur:
            cur.execute("update equity.job set heartbeat_at=now() where job_id = any(%s) and status='running'", (ids,))


def complete(conn, job_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""update equity.job set status='done', locked_by=null, error_class=null, last_error=null,
                       updated_at=now() where job_id=%s""", (job_id,))


def requeue(conn, job_id: int, delay_s: int = 0, note: str = "") -> None:
    """Put a running job back without counting the attempt (e.g. worker shutting down, or paused)."""
    with conn.cursor() as cur:
        cur.execute("""update equity.job set status='retry', locked_by=null, attempts=greatest(attempts-1,0),
                       next_run_at=now()+make_interval(secs => %s), updated_at=now() where job_id=%s""", (delay_s, job_id))


def fail(conn, job: dict, exc: BaseException, decision: Decision) -> str:
    """Apply a rule-engine Decision to a running job. Returns the new status."""
    status = "dead" if decision.action == "dead" else "retry"
    entry = {"at": _now_iso(), "worker": job.get("locked_by"), "error_class": decision.error_class,
             "rule": decision.rule, "action": decision.action, "delay_s": decision.delay_s, "note": decision.note}
    if decision.force_fresh:
        entry["force_fresh"] = True
    msg = f"{type(exc).__name__}: {exc}"[:2000]
    with conn.cursor() as cur:
        cur.execute("""update equity.job set status=%s, error_class=%s, last_error=%s, locked_by=null,
                       next_run_at = now() + make_interval(secs => %s),
                       route = coalesce(%s, route),
                       history = history || %s::jsonb, updated_at=now() where job_id=%s""",
                    (status, decision.error_class, msg, decision.delay_s, decision.route,
                     json.dumps([entry]), job["job_id"]))
    for k, (v, why) in decision.controls.items():
        set_control(conn, k, v, why)
    return status


# ───────── R10: stale reclaim ─────────
def reclaim_stale(conn, minutes: int = STALE_MINUTES, batch: Optional[str] = None) -> List[dict]:
    """Running jobs whose heartbeat is older than `minutes` -> retry (dead if attempts exhausted)."""
    entry = json.dumps([{"at": _now_iso(), "worker": None, "error_class": STALE, "rule": "R10",
                         "action": "reclaim", "note": f"no heartbeat for {minutes}min"}])
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""update equity.job set
                         status = case when attempts >= max_attempts then 'dead' else 'retry' end,
                         error_class='stale', last_error='worker heartbeat lost (locked_by '||coalesce(locked_by,'?')||')',
                         locked_by=null, next_run_at=now(), history = history || %s::jsonb, updated_at=now()
                       where status='running' and coalesce(heartbeat_at, updated_at) < now() - make_interval(mins => %s)
                         and (%s::text is null or batch = %s)
                       returning job_id, batch, kind, company_id, status""", (entry, minutes, batch, batch))
        return [dict(r) for r in cur.fetchall()]


def block_orphan_docs(conn, batch: str) -> int:
    """docs_* jobs that can never run (screener job dead and no document_link rows) -> dead."""
    entry = json.dumps([{"at": _now_iso(), "worker": None, "error_class": "dependency", "rule": "-",
                         "action": "dead", "note": "screener job dead and company has no document_link"}])
    with conn.cursor() as cur:
        cur.execute("""update equity.job j set status='dead', error_class='dependency',
                         last_error='screener job dead and no document_link rows', history = history || %s::jsonb,
                         updated_at=now()
                       where j.batch=%s and j.kind in ('docs_ar','docs_concall','docs_ppt') and j.status in ('pending','retry')
                         and not exists (select 1 from equity.document_link dl where dl.company_id=j.company_id)
                         and exists (select 1 from equity.job s where s.batch=j.batch and s.company_id=j.company_id
                                       and s.kind='screener' and s.status='dead')""", (entry, batch))
        return cur.rowcount


# ───────── introspection ─────────
def counts(conn, batch: Optional[str] = None) -> dict:
    """{kind: {status: n}}"""
    with conn.cursor() as cur:
        cur.execute("select kind, status, count(*) from equity.job where (%s::text is null or batch=%s) group by 1,2",
                    (batch, batch))
        out: dict = {}
        for k, s, n in cur.fetchall():
            out.setdefault(k, {})[s] = n
    return out


def open_jobs(conn, batch: Optional[str], kinds: Optional[Sequence[str]] = None) -> int:
    with conn.cursor() as cur:
        cur.execute("""select count(*) from equity.job where status in ('pending','retry','running')
                       and (%s::text is null or batch=%s) and (%s::text[] is null or kind = any(%s))""",
                    (batch, batch, list(kinds) if kinds else None, list(kinds) if kinds else None))
        return cur.fetchone()[0]


def runnable_screener(conn, batch: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""select count(*) from equity.job where batch=%s and kind='screener' and status in ('pending','retry')
                       and route in ('any','gh')""", (batch,))
        return cur.fetchone()[0]


def db_size_mb(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("select pg_database_size(current_database())/1024.0/1024.0")
        return float(cur.fetchone()[0])
