"""Overnight supervisor: spawns local workers, ticks every 60s (R10 reclaim, restarts, storage guard, gh dispatch),
runs one bounded coverage-gap retry round, then writes reports/<batch>.md and the scrape_run stats.

python -m screener_scraper supervise --batch 2026-11-01 --local screener:1,yf:1,docs_ar:4,docs_concall:2 --dispatch-gh
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import config, jobs
from .db import DB

log = logging.getLogger(__name__)

REPORT_DIR = config.ROOT / "reports"
TICK_S = 60
MAX_GH_DISPATCHES = 3
MAX_RESTARTS = 20
SCREENER_GAPS = {"NO_SCREENER_FACTS", "NO_SHAREHOLDING"}
YF_GAPS = {"NO_DAILY_PRICES", "NO_YF_FACTS", "NO_YF_SNAPSHOT", "NO_ESTIMATES", "NO_SHARES", "NO_CORP_ACTIONS"}


def parse_local(spec: str) -> List[Tuple[str, int]]:
    """'screener:1,yf:1,docs_ar:4' -> [('screener', 1), ...]"""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, n = part.partition(":")
        if kind not in jobs.KINDS:
            raise ValueError(f"unknown kind {kind!r} in --local {spec!r}")
        out.append((kind, int(n or 1)))
    return out


def gap_kinds(flags) -> set:
    kinds = set()
    if SCREENER_GAPS & set(flags):
        kinds.add("screener")
    if YF_GAPS & set(flags):
        kinds.add("yf")
    return kinds


class Supervisor:
    def __init__(self, db: DB, batch: str, local: List[Tuple[str, int]], dispatch_gh: bool = False,
                 tick_s: float = TICK_S, max_hours: float = 14, worker_minutes: Optional[float] = None,
                 gh_runners: int = 5, universe: str = config.UNIVERSE_CODE):
        self.db, self.batch, self.local = db, batch, local
        self.dispatch_gh, self.tick_s = dispatch_gh, tick_s
        self.deadline = time.monotonic() + max_hours * 3600
        self.worker_minutes = worker_minutes or max_hours * 60
        self.gh_runners, self.universe = gh_runners, universe
        self.procs: List[dict] = []
        self.events: List[str] = []
        self.gh_dispatches = 0
        self.last_dispatch = 0.0
        self.gap_round_done = False
        self.paused_ticks = 0
        self.conn = db.trail

    # ── events ──
    def event(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self.events.append(line)
        log.info("supervisor: %s", msg)

    # ── workers ──
    def spawn(self, kind: str, idx: int, restarts: int = 0) -> dict:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(config.LOG_DIR / f"worker_{self.batch}_{kind}{idx}.log", "ab")
        cmd = [sys.executable, "-m", "screener_scraper", "worker", "--kinds", kind, "--class", "local",
               "--slots", "1", "--max-runtime", str(self.worker_minutes), "--batch", self.batch]
        p = subprocess.Popen(cmd, cwd=str(config.ROOT), stdout=logf, stderr=subprocess.STDOUT)
        self.event(f"spawned {kind}#{idx} pid={p.pid}" + (f" (restart {restarts})" if restarts else ""))
        return {"kind": kind, "idx": idx, "proc": p, "restarts": restarts}

    def start_workers(self) -> None:
        for kind, n in self.local:
            for i in range(n):
                self.procs.append(self.spawn(kind, i))

    def restart_dead(self) -> None:
        for w in self.procs:
            if w["proc"] is None or w["proc"].poll() is None:
                continue
            code = w["proc"].returncode
            if jobs.open_jobs(self.conn, self.batch, [w["kind"]]) == 0:
                w["proc"] = None            # drained: leave it down (re-spawned if a gap round adds work)
                continue
            if w["restarts"] >= MAX_RESTARTS:
                self.event(f"{w['kind']}#{w['idx']} exited (code {code}) and hit the restart cap: left dead")
                w["proc"] = None
                w["capped"] = True
                continue
            self.event(f"{w['kind']}#{w['idx']} exited with code {code} while jobs remain: restarting")
            w.update(self.spawn(w["kind"], w["idx"], w["restarts"] + 1))

    def revive_drained(self) -> None:
        for w in self.procs:
            if w["proc"] is None and not w.get("capped") and jobs.open_jobs(self.conn, self.batch, [w["kind"]]) > 0:
                w.update(self.spawn(w["kind"], w["idx"], w["restarts"]))

    def alive(self) -> int:
        return sum(1 for w in self.procs if w["proc"] is not None and w["proc"].poll() is None)

    def stop_workers(self) -> None:
        for w in self.procs:
            if w["proc"] is not None and w["proc"].poll() is None:
                w["proc"].terminate()
        end = time.monotonic() + 60
        for w in self.procs:
            p = w["proc"]
            if p is None:
                continue
            try:
                p.wait(max(1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()

    # ── gh ──
    def gh_running(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("""select count(distinct locked_by) from equity.job where batch=%s and status='running'
                           and locked_by like 'gh:%%' and heartbeat_at > now() - interval '3 minutes'""", (self.batch,))
            return cur.fetchone()[0]

    def maybe_dispatch(self) -> None:
        if not self.dispatch_gh or self.gh_dispatches >= MAX_GH_DISPATCHES:
            return
        if jobs.runnable_screener(self.conn, self.batch) == 0 or self.gh_running() > 0:
            return
        if self.gh_dispatches and time.monotonic() - self.last_dispatch < 600:
            return
        self.gh_dispatches += 1
        self.last_dispatch = time.monotonic()
        repo = os.environ.get("GH_REPO", "j-s-l-7/Scraper")
        cmd = ["gh", "workflow", "run", "equity-workers.yml", "-R", repo, "-f", f"runners={self.gh_runners}",
               "-f", f"batch={self.batch}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                self.event(f"gh dispatch {self.gh_dispatches}/{MAX_GH_DISPATCHES} ok ({repo})")
            else:
                self.event(f"gh dispatch {self.gh_dispatches}/{MAX_GH_DISPATCHES} FAILED: "
                           f"{(r.stderr or r.stdout).strip()[:200]} (continuing with local workers)")
        except Exception as e:
            self.event(f"gh dispatch {self.gh_dispatches}/{MAX_GH_DISPATCHES} FAILED: {type(e).__name__}: {e} "
                       f"(continuing with local workers)")

    # ── per-tick rules ──
    def storage_guard(self) -> None:
        if "supabase" not in self.db.url:      # the quota is Supabase's: the lake is never full
            return
        was = jobs.is_paused(self.conn)
        why = jobs.overflow_reason(self.conn)
        if why and not was:
            self.event(f"R8: {why}: control.paused=true, the lake is primary for the top 100")

    def tick(self) -> None:
        for r in jobs.reclaim_stale(self.conn, jobs.STALE_MINUTES, self.batch):
            self.event(f"R10: reclaimed stale job {r['job_id']} ({r['kind']} company {r['company_id']}) -> {r['status']}")
        n = jobs.block_orphan_docs(self.conn, self.batch)
        if n:
            self.event(f"{n} docs jobs marked dead: their screener job is dead and no document_link exists")
        self.restart_dead()
        self.storage_guard()
        self.maybe_dispatch()

    # ── coverage-gap round ──
    def gap_round(self) -> int:
        """Reset done/dead jobs of batch companies that still have coverage gaps (bounded: once)."""
        self.gap_round_done = True
        cols, rows = self.db.coverage(self.universe)
        with self.conn.cursor() as cur:
            cur.execute("select distinct company_id from equity.job where batch=%s", (self.batch,))
            in_batch = {r[0] for r in cur.fetchall()}
        from .__main__ import coverage_flags
        n = 0
        for r in rows:
            d = dict(zip(cols, r))
            if d["company_id"] not in in_batch:
                continue
            kinds = gap_kinds(coverage_flags(d))
            if not kinds:
                continue
            with self.conn.cursor() as cur:
                cur.execute("""update equity.job set status='pending', attempts=0, next_run_at=now(), route='any',
                               history = history || %s::jsonb, updated_at=now()
                               where batch=%s and company_id=%s and kind = any(%s) and status in ('done','dead')""",
                            (json.dumps([{"at": datetime.now().isoformat(timespec="seconds"), "worker": None,
                                          "error_class": "coverage_gap", "action": "retry_round"}]),
                             self.batch, d["company_id"], sorted(kinds)))
                n += cur.rowcount
        self.event(f"coverage-gap retry round: {n} jobs re-queued")
        return n

    def retry_missing_round(self) -> None:
        """Gap-fill for documents: missing/failed/no_text ones go through the wayback/commoncrawl/company-site chain."""
        cmd = [sys.executable, "-m", "screener_scraper", "docs", "--retry-missing", "--universe", self.universe,
               "--slots", "2"]
        self.event("docs gap-fill: " + " ".join(cmd[2:]))
        try:
            r = subprocess.run(cmd, cwd=str(config.ROOT), capture_output=True, text=True, timeout=4 * 3600)
            tail = (r.stdout.strip().splitlines() or [""])[-1]
            self.event(f"docs gap-fill finished (exit {r.returncode}): {tail[:200]}")
        except Exception as e:
            self.event(f"docs gap-fill failed: {type(e).__name__}: {e}")

    # ── main loop ──
    def run(self) -> dict:
        run_id = self.db.start_run("supervise", {"batch": self.batch, "local": self.local,
                                                 "dispatch_gh": self.dispatch_gh})
        t0 = time.monotonic()
        self.event(f"start batch={self.batch} local={self.local} gh={self.dispatch_gh}")
        self.start_workers()
        outcome = "complete"
        try:
            while True:
                try:
                    self.tick()
                    if jobs.is_paused(self.conn):
                        self.paused_ticks += 1
                        if self.paused_ticks >= 3:
                            outcome = "paused"
                            self.event("control.paused stayed set for 3 ticks: ending the run")
                            break
                    else:
                        self.paused_ticks = 0
                    if jobs.open_jobs(self.conn, self.batch) == 0:
                        if not self.gap_round_done and self.gap_round() > 0:
                            self.revive_drained()
                            continue
                        self.retry_missing_round()
                        break
                    if self.alive() == 0 and self.gh_running() == 0 and not self.can_revive():
                        outcome = "workers_dead"
                        self.event("no workers alive and none can be restarted: ending the run")
                        break
                    if time.monotonic() > self.deadline:
                        outcome = "timeout"
                        self.event("max-hours reached: ending the run")
                        break
                except Exception as e:
                    log.exception("tick failed")
                    self.event(f"tick error {type(e).__name__}: {str(e)[:150]}")
                    try:
                        self.db.close()
                        self.db = DB()
                        self.conn = self.db.trail
                    except Exception:
                        pass
                self.revive_drained()
                time.sleep(self.tick_s)
        finally:
            self.stop_workers()
        stats = self.summary()
        stats.update({"outcome": outcome, "gh_dispatches": self.gh_dispatches, "seconds": round(time.monotonic() - t0),
                      "events": self.events[-200:]})
        path = write_report(self.db, self.batch, self.events, outcome)
        stats["report"] = str(path)
        self.db.finish_run(run_id, "success" if outcome == "complete" and not stats["dead"] else "partial", stats)
        self.event(f"report written: {path}")
        return stats

    def can_revive(self) -> bool:
        return any(not w.get("capped") for w in self.procs)

    def summary(self) -> dict:
        c = jobs.counts(self.conn, self.batch)
        dead = sum(v.get("dead", 0) for v in c.values())
        return {"jobs": c, "dead": dead, "db_mb": round(jobs.db_size_mb(self.conn), 1)}


# ───────── report ─────────
def _fmt_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


def build_report(db: DB, batch: str, events: Optional[List[str]] = None, outcome: str = "") -> str:
    conn = db.trail
    L = [f"# Overnight report {batch}", ""]
    if outcome:
        L.append(f"Outcome: **{outcome}**")
    paused = jobs.get_control(conn, "paused", False)
    if paused:
        with conn.cursor() as cur:
            cur.execute("select reason from equity.control where key='paused'")
            L.append(f"**PAUSED**: {cur.fetchone()[0]}")
    L += ["", "## Jobs per kind and status"]
    c = jobs.counts(conn, batch)
    sts = ["pending", "running", "retry", "done", "dead"]
    L.append(_fmt_table(["kind"] + sts, [[k] + [c.get(k, {}).get(s, 0) for s in sts] for k in jobs.KINDS if k in c]))
    with conn.cursor() as cur:
        cur.execute("""select c.nse_symbol, j.kind, j.error_class, j.attempts, left(j.last_error, 160)
                       from equity.job j join equity.company c using (company_id)
                       where j.batch=%s and j.status='dead' order by j.kind, j.priority""", (batch,))
        dead = cur.fetchall()
        cur.execute("""select d.doc_type, d.status, count(*), coalesce(sum(page_count),0), coalesce(sum(char_count),0)
                       from equity.document d where d.company_id in (select company_id from equity.job where batch=%s)
                       group by 1,2 order by 1,2""", (batch,))
        docs = cur.fetchall()
    L += ["", f"## Dead jobs ({len(dead)})"]
    L.append(_fmt_table(["symbol", "kind", "error_class", "attempts", "last_error"], dead) if dead else "none")
    L += ["", "## Failures by error class (all attempts, from job.history)"]
    with conn.cursor() as cur:
        cur.execute("""select h->>'error_class', h->>'rule', count(*) from equity.job j, jsonb_array_elements(j.history) h
                       where j.batch=%s group by 1,2 order by 3 desc""", (batch,))
        hist = cur.fetchall()
    L.append(_fmt_table(["error_class", "rule", "count"], hist) if hist else "none")
    L += ["", "## Documents by status"]
    L.append(_fmt_table(["doc_type", "status", "docs", "pages", "chars"], docs) if docs else "none")
    L += ["", "## Database", f"{jobs.db_size_mb(conn):.1f} MB (limit {jobs.get_control(conn, 'storage_limit_mb', 470)} MB)"]
    L += ["", "## Coverage gaps"]
    try:
        cols, rows = db.coverage(config.UNIVERSE_CODE)
        from .__main__ import coverage_flags
        with conn.cursor() as cur:
            cur.execute("select distinct company_id from equity.job where batch=%s", (batch,))
            ids = {r[0] for r in cur.fetchall()}
        gaps = []
        for r in rows:
            d = dict(zip(cols, r))
            f = coverage_flags(d)
            if f and d["company_id"] in ids:
                gaps.append([d.get("nse_symbol"), ", ".join(f)])
        L.append(_fmt_table(["symbol", "gaps"], gaps) if gaps else "none")
    except Exception as e:
        L.append(f"coverage unavailable: {type(e).__name__}: {e}")
    try:
        from .quality import report_section
        L.append(report_section(conn))
    except Exception:
        pass
    if events:
        L += ["", "## Supervisor events", "```"] + events[-300:] + ["```"]
    return "\n".join(L) + "\n"


def write_report(db: DB, batch: str, events: Optional[List[str]] = None, outcome: str = ""):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"{batch}.md"
    p.write_text(build_report(db, batch, events, outcome))
    return p


def run_supervise(db: DB, batch: str, local_spec: str, dispatch_gh: bool = False, **kw) -> int:
    sup = Supervisor(db, batch, parse_local(local_spec), dispatch_gh, **kw)
    stats = sup.run()
    return 0 if stats["outcome"] == "complete" else 1
