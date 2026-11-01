"""Unattended rollout driver. `lake-drive` is safe to run every 15 minutes and never raises:

  1. secrets present?  (else log "waiting for secrets" and stop)
  2. lake-up (start the local lake PG if down)
  3. load every completed-but-unloaded equity-lake run; re-dispatch missing ranks once as a smaller window
  4. daily: sync Supabase top-100 -> lake;  daily: equity-docs (retry-missing);  weekly: equity-shards
  5. if no equity-lake run is in progress: dispatch the next window (101-400, 401-700, 701-1000)
Progress is tracked in the lake tables lake_window / lake_load_log / lake_state.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config
from .lake import ARTIFACT_DIR, TABLES, export_tables, lake_connect, lake_up, load_export_dir
from .rank import TOP1000_CODE

log = logging.getLogger(__name__)

import os as _os
_os.environ.setdefault("SUPABASE_TXN_POOL", "1")      # parallel runners + console exhaust the session pooler (15)
REPO = "j-s-l-7/Scraper"
WINDOWS = [(101, 400), (401, 700), (701, 1000)]
RUNNERS = 20
BUSY = {"queued", "in_progress", "waiting", "pending", "requested"}


def repo() -> str:
    import os
    return os.environ.get("GH_REPO") or REPO


def gh(*args: str, check: bool = True, timeout: int = 600) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


# ───────────────────────────── pure helpers (unit-tested) ─────────────────────────────
def next_window(done: List[Tuple[int, int]], windows=WINDOWS) -> Optional[Tuple[int, int]]:
    done_set = set(done)
    return next((w for w in windows if w not in done_set), None)


def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def due(last: Optional[str], key: str) -> bool:
    return last != key


def ranks_str(ranks: List[int]) -> str:
    return ",".join(str(r) for r in sorted(ranks))


def extra_windows(conn) -> List[Tuple[int, int]]:
    """Windows beyond rank 1000 registered by the data-quality engine (lake_state 'extra_windows' = '1001-1300,...')."""
    try:
        with conn.cursor() as cur:
            cur.execute("select value from public.lake_state where key='extra_windows'")
            r = cur.fetchone()
        conn.rollback()
        return [tuple(map(int, w.split("-"))) for w in (r[0] if r and r[0] else "").split(",") if w]
    except Exception:
        return []


# ───────────────────────────── gh wrappers ─────────────────────────────
def secrets_ready() -> bool:
    try:
        out = gh("secret", "list", "-R", repo(), "--json", "name")
        return "DATABASE_URL" in {s["name"] for s in json.loads(out or "[]")}
    except Exception as e:
        log.warning("secret check failed: %s", e)
        return False


def list_runs(workflow: str, limit: int = 30) -> List[dict]:
    out = gh("run", "list", "-R", repo(), "-w", workflow, "-L", str(limit), "--json",
             "databaseId,status,conclusion,createdAt,displayTitle,event")
    return json.loads(out or "[]")


def dispatch(workflow: str, fields: Dict[str, str], ref: str = "main") -> Optional[int]:
    """Dispatch a workflow and return the new run id (found by polling)."""
    t0 = datetime.now(timezone.utc) - timedelta(seconds=10)
    before = {r["databaseId"] for r in list_runs(workflow, 5)}
    args = ["workflow", "run", workflow, "-R", repo(), "--ref", ref]
    for k, v in fields.items():
        args += ["-f", f"{k}={v}"]
    gh(*args)
    for _ in range(20):
        time.sleep(4)
        for r in list_runs(workflow, 5):
            created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
            if r["databaseId"] not in before and created >= t0:
                return r["databaseId"]
    return None


# ───────────────────────────── lake state ─────────────────────────────
def state_get(cur, key: str) -> Optional[str]:
    cur.execute("select value from public.lake_state where key=%s", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def state_set(cur, key: str, value: str) -> None:
    cur.execute("insert into public.lake_state(key, value) values (%s,%s) on conflict (key) do update "
                "set value=excluded.value, updated_at=now()", (key, value))


def load_run(run_id: int, conn=None) -> Dict[str, int]:
    """Download and load every lake-<run_id>-* artifact. Idempotent. Returns total rows per table."""
    from .documents import merge_tree
    own = conn is None
    conn = conn or lake_connect()
    dest = ARTIFACT_DIR / str(run_id)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    totals: Dict[str, int] = {}
    try:
        r = subprocess.run(["gh", "run", "download", str(run_id), "-R", repo(), "-p", f"lake-{run_id}-*", "-D",
                            str(dest)], capture_output=True, text=True, timeout=3600)
        arts = sorted(p for p in dest.iterdir() if p.is_dir())
        if r.returncode != 0 and not arts:
            with conn, conn.cursor() as cur:
                cur.execute("insert into public.lake_load_log(run_id, artifact, files, rows_loaded, status) "
                            "values (%s,'-',0,0,%s) on conflict (run_id, artifact) do update set status=excluded.status,"
                            " loaded_at=now()", (run_id, "no_artifacts: " + r.stderr.strip()[:120]))
            return totals
        for art in arts:
            n_files = len(list((art / "export").glob("*.csv.gz"))) if (art / "export").exists() else 0
            rows = load_export_dir(conn, art / "export") if n_files else {}
            if (art / "documents").exists():
                merge_tree(art / "documents", config.ROOT / "data" / "documents")
            for k, v in rows.items():
                totals[k] = totals.get(k, 0) + v
            with conn, conn.cursor() as cur:
                cur.execute("insert into public.lake_load_log(run_id, artifact, files, rows_loaded, status) "
                            "values (%s,%s,%s,%s,'loaded') on conflict (run_id, artifact) do update set "
                            "files=excluded.files, rows_loaded=excluded.rows_loaded, status='loaded', loaded_at=now()",
                            (run_id, art.name, n_files, sum(rows.values())))
        shutil.rmtree(dest, ignore_errors=True)
        return totals
    finally:
        if own:
            conn.close()


def missing_ranks(conn, ranks: List[int]) -> List[int]:
    """Ranks whose company has no screener facts or no daily prices in the lake."""
    with conn.cursor() as cur:
        cur.execute("""select u.rank from equity.universe u join equity.company c using (company_id)
                       where u.universe_code=%s and u.as_of=(select max(as_of) from equity.universe where universe_code=%s)
                         and u.rank = any(%s)
                         and (not exists (select 1 from equity.financial_fact f join equity.metric m using (metric_id)
                                          where f.company_id=c.company_id and m.source='screener')
                              or not exists (select 1 from equity.historical_prices p
                                             where p.symbol=coalesce(c.nse_symbol, c.screener_slug)))
                       order by 1""", (TOP1000_CODE, TOP1000_CODE, ranks))
        out = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return out


def window_ranks_of(row) -> List[int]:
    from .lake_runner import window_ranks
    return window_ranks(row[1], row[2], row[3])


# ───────────────────────────── tick ─────────────────────────────
def drive_once(allow_dispatch: bool = True) -> str:
    """One idempotent tick. Returns a one-line summary. Raises nothing the caller must handle (see lake_drive_cli)."""
    summary: List[str] = []
    if not secrets_ready():
        return "waiting for secrets (DATABASE_URL not set on the repo)"
    lake_up()
    conn = lake_connect()
    try:
        runs = list_runs("equity-lake.yml", 40)
        busy = [r for r in runs if r["status"] in BUSY]
        # 3. load completed runs
        with conn.cursor() as cur:
            cur.execute("select distinct run_id from public.lake_load_log where status='loaded'")
            loaded = {r[0] for r in cur.fetchall()}
        conn.rollback()
        for r in sorted(runs, key=lambda x: x["databaseId"]):
            rid = r["databaseId"]
            if r["status"] != "completed" or rid in loaded:
                continue
            t0 = time.monotonic()
            tot = load_run(rid, conn)
            with conn, conn.cursor() as cur:
                cur.execute("select window_id, from_rank, to_rank, ranks, attempt from public.lake_window "
                            "where run_id=%s", (rid,))
                w = cur.fetchone()
            retry = ""
            if w:
                miss = missing_ranks(conn, window_ranks_of(w))
                with conn, conn.cursor() as cur:
                    cur.execute("update public.lake_window set status=%s, loaded_at=now() where window_id=%s",
                                ("loaded" if not miss or w[4] > 1 else "retried", w[0]))
                if miss and w[4] == 1 and not busy:
                    new = dispatch("equity-lake.yml", {"from_rank": str(min(miss)), "to_rank": str(max(miss)),
                                                       "ranks": ranks_str(miss)})
                    with conn, conn.cursor() as cur:
                        cur.execute("insert into public.lake_window(from_rank,to_rank,ranks,attempt,run_id,note) "
                                    "values (%s,%s,%s,2,%s,%s)", (min(miss), max(miss), ranks_str(miss), new,
                                                                  f"retry of window {w[0]}"))
                    busy = [{"databaseId": new, "status": "queued"}]
                    retry = f" retry={len(miss)} ranks"
            summary.append(f"loaded run {rid} ({sum(tot.values())} rows, {time.monotonic() - t0:.0f}s){retry}")
        # 4. daily/weekly top-100 jobs
        today = date.today()
        with conn.cursor() as cur:
            last_sync, last_docs, last_shards = (state_get(cur, k) for k in
                                                 ("top100_sync", "docs_dispatch", "shards_dispatch"))
        conn.rollback()
        if due(last_sync, today.isoformat()):
            from .db import DB
            src = DB()
            try:
                n = sync_top100(src, conn)
            finally:
                src.close()
            with conn, conn.cursor() as cur:
                state_set(cur, "top100_sync", today.isoformat())
            summary.append(f"synced top100 ({n} rows)")
        if due(last_docs, today.isoformat()):
            if not any(r["status"] in BUSY for r in list_runs("equity-docs.yml", 5)):
                rid = dispatch("equity-docs.yml", {"total": "10", "retry_missing": "true"})
                with conn, conn.cursor() as cur:
                    state_set(cur, "docs_dispatch", today.isoformat())
                summary.append(f"dispatched equity-docs run {rid}")
        if due(last_shards, week_key(today)) and today.weekday() >= 5:      # weekend refresh of the top 100
            if not any(r["status"] in BUSY for r in list_runs("equity-shards.yml", 5)):
                rid = dispatch("equity-shards.yml", {"total": "10", "universe": "TOP100_MCAP"})
                with conn, conn.cursor() as cur:
                    state_set(cur, "shards_dispatch", week_key(today))
                summary.append(f"dispatched equity-shards run {rid}")
        # 5. next window
        if allow_dispatch and not busy:
            with conn.cursor() as cur:
                cur.execute("select from_rank, to_rank from public.lake_window where attempt=1 and ranks is null "
                            "and status in ('dispatched','done','loaded','retried')")
                done = [(a, b) for a, b in cur.fetchall()]
            conn.rollback()
            w = next_window(done, WINDOWS + extra_windows(conn))
            if w:
                rid = dispatch("equity-lake.yml", {"from_rank": str(w[0]), "to_rank": str(w[1])})
                with conn, conn.cursor() as cur:
                    cur.execute("insert into public.lake_window(from_rank,to_rank,run_id) values (%s,%s,%s)",
                                (w[0], w[1], rid))
                summary.append(f"dispatched window {w[0]}-{w[1]} run {rid}")
            else:
                summary.append("all windows dispatched")
        elif busy:
            summary.append(f"{len(busy)} lake run(s) in progress")
        return "; ".join(summary) or "nothing to do"
    finally:
        conn.close()


def lake_drive_cli() -> int:
    """Never fails: any error is logged as the tick summary and the exit code is 0."""
    try:
        line = drive_once()
    except Exception as e:                                   # noqa: BLE001 - unattended: never raise
        log.exception("lake-drive error")
        line = f"error {type(e).__name__}: {str(e)[:200]}"
    print(f"lake-drive {datetime.now():%F %T}: {line}", flush=True)
    return 0


# ───────────────────────────── Supabase top-100 sync ─────────────────────────────
def sync_top100(src, lake_conn) -> int:
    """COPY all equity data tables out of Supabase, then load them into the lake with the same remapping."""
    tmp = config.ROOT / "data" / "lake" / "sync"
    shutil.rmtree(tmp, ignore_errors=True)
    counts = export_tables(src.conn, tmp / "export", 1, tables=list(TABLES))
    load_export_dir(lake_conn, tmp / "export")
    shutil.rmtree(tmp, ignore_errors=True)
    return sum(counts.values())


# ───────────────────────────── kick-off ─────────────────────────────
def lake_start() -> int:
    from .db import DB
    if not secrets_ready():
        print("waiting for secrets: DATABASE_URL is not set on the repo; lake-drive will continue when it is")
        lake_up()
        return 0
    print("lake:", lake_up())
    db = DB()
    try:
        def ranked() -> int:
            with db.trail.cursor() as cur:
                cur.execute("select count(*) from equity.universe where universe_code=%s and as_of="
                            "(select max(as_of) from equity.universe where universe_code=%s)",
                            (TOP1000_CODE, TOP1000_CODE))
                return cur.fetchone()[0]
        if ranked() < 900:
            print("ranking TOP1000_MCAP via equity-rank.yml ...")
            rid = dispatch("equity-rank.yml", {})
            while True:
                time.sleep(30)
                r = next((x for x in list_runs("equity-rank.yml", 5) if x["databaseId"] == rid), None)
                if r and r["status"] == "completed":
                    print("rank run", rid, r["conclusion"])
                    break
        print("TOP1000_MCAP companies:", ranked())
    finally:
        db.close()
    print(drive_once())
    return 0
