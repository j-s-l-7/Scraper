"""Queue worker: claims jobs from equity.job and runs the per-company functions; failures go through rules.decide.

python -m screener_scraper worker --kinds screener,yf --class gh --slots 1 --max-runtime 110
(max-runtime is in minutes; the worker stops claiming after it and exits once its running jobs finish.)
"""
from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
from typing import Callable, Dict, Optional, Sequence

from . import config, jobs, rules
from .db import DB

log = logging.getLogger(__name__)

HEARTBEAT_S = 30
IDLE_SLEEP_S = 15
PAUSED_SLEEP_S = 30
SCREENER_RESUME_HOURS = config.RESUME_HOURS


# ───────── thin adapters over the per-company entry points ─────────
def _call_screener(db: DB, job: dict, run_id: int, force: bool):
    from . import pipeline
    return pipeline.scrape_company(job["company_id"], run_id, force, db=db)


def _call_yf(db: DB, job: dict, run_id: int, force: bool):
    from . import yf
    return yf.run_company(job["company_id"], run_id, db=db)


def _call_docs(db: DB, job: dict, run_id: int, force: bool):
    from . import documents
    return documents.run_company(db, job["company_id"], run_id, job["kind"], force=force)


HANDLERS: Dict[str, Callable] = {"screener": _call_screener, "yf": _call_yf,
                                 "docs_ar": _call_docs, "docs_concall": _call_docs, "docs_ppt": _call_docs}


def screener_recently_done(db: DB, company_id: int, hours: int = SCREENER_RESUME_HOURS) -> bool:
    """True when the company's latest screener scrape (non-yf tasks within `hours`) had no failed task."""
    with db.trail.cursor() as cur:
        cur.execute("""select max(run_id) from equity.scrape_task where company_id=%s and task not like 'yf:%%'
                       and task not like 'docs%%' and finished_at > now() - make_interval(hours => %s)""",
                    (company_id, hours))
        rid = cur.fetchone()[0]
        if rid is None:
            return False
        cur.execute("""select count(*) filter (where status='failed'), count(*) from equity.scrape_task
                       where run_id=%s and company_id=%s and task not like 'yf:%%' and task not like 'docs%%'""",
                    (rid, company_id))
        failed, total = cur.fetchone()
    return total > 0 and failed == 0


def worker_id(slot: int, cls: str) -> str:
    if cls == "gh" or os.environ.get("GITHUB_RUN_ID"):
        return f"gh:{os.environ.get('GITHUB_RUN_ID', 'x')}:{os.environ.get('GITHUB_JOB', 'job')}.{os.getpid()}.{slot}"
    return f"{socket.gethostname()}:{os.getpid()}:{slot}"


class Heartbeater(threading.Thread):
    def __init__(self, url: str):
        super().__init__(daemon=True, name="heartbeat")
        self.url = url
        self.ids = set()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()

    def add(self, job_id: int) -> None:
        with self.lock:
            self.ids.add(job_id)

    def discard(self, job_id: int) -> None:
        with self.lock:
            self.ids.discard(job_id)

    def run(self) -> None:
        import psycopg2
        conn = None
        while not self.stop_evt.wait(HEARTBEAT_S):
            try:
                if conn is None or conn.closed:
                    conn = psycopg2.connect(self.url, connect_timeout=20)
                    conn.autocommit = True
                with self.lock:
                    ids = list(self.ids)
                jobs.heartbeat(conn, ids)
            except Exception as e:
                log.warning("heartbeat failed: %s", e)
                try:
                    conn and conn.close()
                except Exception:
                    pass
                conn = None


class Worker:
    def __init__(self, kinds: Sequence[str], cls: str = "local", slots: int = 1, max_runtime_min: float = 600,
                 batch: Optional[str] = None, idle_exit: bool = True):
        self.kinds, self.cls, self.slots = list(kinds), cls, max(1, slots)
        self.deadline = time.monotonic() + max_runtime_min * 60
        self.batch, self.idle_exit = batch, idle_exit
        self.stop = threading.Event()
        self.stats = {"done": 0, "failed": 0, "skipped": 0, "by_class": {}}
        self.lock = threading.Lock()
        self.run_id: Optional[int] = None
        self.url = config.database_url()

    # ── helpers ──
    def _apply_controls(self, db: DB) -> None:
        if "screener" in self.kinds:
            v = jobs.get_control(db.trail, "screener_min_interval")
            if v:
                try:
                    from . import pipeline
                    lim = pipeline.shared_client().limiter
                    lim.base = max(lim.base, float(v))
                    lim.interval = max(lim.interval, float(v))
                except Exception:
                    log.debug("could not apply screener_min_interval", exc_info=True)

    def _apply_decision(self, d: rules.Decision, kind: str) -> None:
        if d.interval_x2:
            try:
                if kind in jobs.DOC_KINDS:
                    from . import documents
                    documents._spacer.on_429()
                else:
                    from . import pipeline
                    pipeline.shared_client().limiter.on_429()
            except Exception:
                log.debug("could not slow down", exc_info=True)

    def _count(self, key: str, cls: Optional[str] = None) -> None:
        with self.lock:
            self.stats[key] += 1
            if cls:
                self.stats["by_class"][cls] = self.stats["by_class"].get(cls, 0) + 1

    # ── one job ──
    def _run_job(self, db: DB, job: dict, hb: Heartbeater) -> DB:
        label = f"{job['kind']} company={job['company_id']} job={job['job_id']} attempt={job['attempts']}"
        hb.add(job["job_id"])
        t0 = time.monotonic()
        try:
            hist = job.get("history") or []
            force = bool(hist and hist[-1].get("force_fresh"))
            if job["kind"] == "screener" and not force and screener_recently_done(db, job["company_id"]):
                jobs.complete(db.trail, job["job_id"])
                self._count("skipped")
                log.info("%s skipped (screener scrape ok within %dh)", label, SCREENER_RESUME_HOURS)
                return db
            HANDLERS[job["kind"]](db, job, self.run_id, force)
            jobs.complete(db.trail, job["job_id"])
            self._count("done")
            log.info("%s done in %.0fs", label, time.monotonic() - t0)
        except Exception as e:
            try:
                db.conn.rollback()
            except Exception:
                pass
            d = rules.decide(e, attempts=job["attempts"], max_attempts=job["max_attempts"], worker_class=self.cls,
                             history=job.get("history"), kind=job["kind"])
            log.warning("%s FAILED %s -> %s %s (%s)", label, f"{type(e).__name__}: {str(e)[:200]}", d.rule, d.action,
                        d.note)
            self._apply_decision(d, job["kind"])
            for attempt in range(3):         # recording the failure must survive a dropped connection
                try:
                    jobs.fail(db.trail, job, e, d)
                    break
                except Exception:
                    log.exception("could not record failure, reconnecting")
                    db = self._reconnect(db)
            self._count("failed", d.error_class)
        finally:
            hb.discard(job["job_id"])
        return db

    def _reconnect(self, db: DB) -> DB:
        try:
            db.close()
        except Exception:
            pass
        time.sleep(2)
        return DB(self.url)

    # ── slot loop ──
    def _slot(self, slot: int, hb: Heartbeater) -> None:
        wid = worker_id(slot, self.cls)
        db = DB(self.url)
        try:
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                try:
                    if jobs.is_paused(db.trail):
                        log.warning("[%s] control.paused is set: not claiming", wid)
                        self.stop.wait(PAUSED_SLEEP_S)
                        continue
                    self._apply_controls(db)
                    job = jobs.claim(db.trail, wid, self.kinds, self.cls, self.batch)
                    if job is None:
                        if self.idle_exit and jobs.open_jobs(db.trail, self.batch, self.kinds) == 0:
                            log.info("[%s] no open jobs left: exiting", wid)
                            return
                        self.stop.wait(IDLE_SLEEP_S + random.uniform(0, 5))
                        continue
                except Exception as e:
                    log.warning("[%s] queue error (%s: %s): reconnecting", wid, type(e).__name__, str(e)[:150])
                    db = self._reconnect(db)
                    self.stop.wait(30)
                    continue
                db = self._run_job(db, job, hb)
        finally:
            db.close()

    def run(self) -> dict:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self.stop.set())
            except ValueError:
                pass
        main_db = DB(self.url)
        self.run_id = main_db.start_run("worker", {"kinds": self.kinds, "class": self.cls, "slots": self.slots,
                                                   "batch": self.batch, "host": socket.gethostname()})
        hb = Heartbeater(self.url)
        hb.start()
        log.info("worker up: kinds=%s class=%s slots=%d run=%d", self.kinds, self.cls, self.slots, self.run_id)
        threads = [threading.Thread(target=self._slot, args=(i, hb), name=f"slot{i}") for i in range(self.slots)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        hb.stop_evt.set()
        status = "partial" if self.stats["failed"] else "success"
        main_db.finish_run(self.run_id, status, self.stats)
        main_db.close()
        log.info("worker exit: %s", self.stats)
        return self.stats


def run_worker(kinds: Sequence[str], cls: str = "local", slots: int = 1, max_runtime_min: float = 600,
               batch: Optional[str] = None) -> int:
    bad = [k for k in kinds if k not in jobs.KINDS]
    if bad:
        raise SystemExit(f"unknown kinds {bad}; choose from {jobs.KINDS}")
    Worker(kinds, cls, slots, max_runtime_min, batch).run()
    return 0
