"""Unattended rollout driver. `lake-drive` is safe to run every 15 minutes and never raises:

  1. secrets present?  (else log "waiting for secrets" and stop)
  2. lake-up (start the local lake PG if down)
  3. load every completed-but-unloaded equity-lake run; re-dispatch missing ranks once as a smaller window
  4. daily: sync Supabase top-100 -> lake;  daily: equity-docs (primary fetch; retry-missing at most weekly);
     weekly: equity-shards
  5. if no equity-lake run is in progress: dispatch the next window (101-400, 401-700, 701-1000)
Progress is tracked in the lake tables lake_window / lake_load_log / lake_state.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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
DOWNLOAD_PARALLEL = 8              # artifact downloads in flight at once
DOWNLOAD_TIMEOUT = 3 * 3600        # per artifact; ~50 MB at ~12 KB/s takes ~70 min
MAX_PART_FAILS = 6                 # a part that failed this often is given up; the window retry covers its ranks
PART_BACKOFF_S = 900               # after n failures a part waits 15m, 30m, 1h, 2h, 4h: giving up needs >= 7.75h
DOCS_SHARDS = 10                   # equity-docs.yml matrix size
DOCS_RETRY_DAYS = 7                # equity-docs runs the fallback chain (retry_missing) at most this often


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


def fails_of(status: Optional[str]) -> int:
    """lake_load_log status 'failed 2' -> 2; anything else -> 0."""
    head, _, n = (status or "").partition(" ")
    return int(n) if head == "failed" and n.isdigit() else 0


def plan_parts(names: List[str], logged: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """(still to download, given up) for a run's artifacts, from their lake_load_log statuses."""
    open_ = [n for n in names if logged.get(n) != "loaded"]
    gave_up = [n for n in open_ if fails_of(logged.get(n)) >= MAX_PART_FAILS]
    return [n for n in open_ if n not in gave_up], gave_up


def part_due(status: Optional[str], age_s: float) -> bool:
    """A part that failed n times is tried again PART_BACKOFF_S * 2**(n-1) after its last failure, so an internet
    outage of an hour or two can't use up its tries."""
    n = fails_of(status)
    return n == 0 or age_s >= PART_BACKOFF_S * 2 ** (n - 1)


def gave_up_ranks(ranks: List[int], gave_up: List[str]) -> List[int]:
    """The window ranks carried by given-up parts (lake-<run>-<k> holds runner k's slice)."""
    from .lake_runner import runner_ranks
    return sorted({r for n in gave_up for r in runner_ranks(ranks, int(n.rsplit("-", 1)[1]), RUNNERS)})


IDENTITY = ("company", "universe")     # all that still comes from Supabase once the lake is primary for the top 100


def plan_top100(full: Optional[str], err: bool, primary: bool, today: date, last_sync: Optional[str],
                last_docs: Optional[str], last_shards: Optional[str]) -> Tuple[Optional[Tuple[str, ...]], bool, bool, bool]:
    """Step 4: (tables to sync from Supabase or None, dispatch equity-docs?, dispatch equity-shards?, make the lake
    primary for the top 100?). `full`: Supabase's db_full reason; `err`: Supabase couldn't be checked this tick.
    Once the lake is primary only identity rows come down (Supabase stays the ranking authority): its frozen copies
    must never overwrite fresher lake rows. A full Supabase gets no more GitHub writers."""
    sync = None if err or not due(last_sync, today.isoformat()) else IDENTITY if primary or full else tuple(TABLES)
    ok = not full and not err
    return (sync, ok and due(last_docs, today.isoformat()),
            ok and due(last_shards, week_key(today)) and today.weekday() >= 5, bool(full) and not err)


def docs_inputs(today: date, last_retry: Optional[str]) -> Dict[str, str]:
    """Inputs of the daily equity-docs dispatch. retry_missing makes the run skip the primary fetch, so it runs at most
    every DOCS_RETRY_DAYS; the other days fetch new links, pending documents and failed ones past their cooldown.
    (Dispatched retry_missing every day, it left 3,127 top-100 presentations never fetched.)"""
    retry = last_retry is None or (today - date.fromisoformat(last_retry[:10])).days >= DOCS_RETRY_DAYS
    return {"total": str(DOCS_SHARDS), "retry_missing": "true" if retry else "false"}


def retry_action(miss: List[int], attempt: int, retried: bool, busy: bool) -> str:
    """A loaded window's missing ranks get ONE retry window: 'dispatch' it now, 'wait' while another lake run is in
    progress (a newer queued run would cancel a pending one), or 'none'."""
    if not miss or attempt > 1 or retried:
        return "none"
    return "wait" if busy else "dispatch"


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
def secrets_ready() -> Optional[bool]:
    """Whether DATABASE_URL is set on the repo; None when GitHub can't be asked (e.g. the internet is down)."""
    try:
        out = gh("secret", "list", "-R", repo(), "--json", "name")
    except Exception as e:
        log.warning("secret check failed: %s", e)
        return None
    return "DATABASE_URL" in {s["name"] for s in json.loads(out or "[]")}


def list_runs(workflow: str, limit: int = 30) -> List[dict]:
    out = gh("run", "list", "-R", repo(), "-w", workflow, "-L", str(limit), "--json",
             "databaseId,status,conclusion,createdAt,displayTitle,event")
    return json.loads(out or "[]")


def dispatch(workflow: str, fields: Dict[str, str], ref: str = "main", title: Optional[str] = None) -> Optional[int]:
    """Dispatch a workflow and return the new run id (found by polling; with `title`, only a run of that name)."""
    t0 = datetime.now(timezone.utc) - timedelta(seconds=10)
    before = {r["databaseId"] for r in list_runs(workflow, 5)}
    args = ["workflow", "run", workflow, "-R", repo(), "--ref", ref]
    for k, v in fields.items():
        args += ["-f", f"{k}={v}"]
    gh(*args)
    for _ in range(20):
        time.sleep(4)
        try:
            runs = list_runs(workflow, 5)
        except Exception:               # the run exists already: a network blip here must not lose its id
            continue
        for r in runs:
            created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
            if r["databaseId"] not in before and created >= t0 and title in (None, r["displayTitle"]):
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


class Loaded(NamedTuple):
    rows: Dict[str, int]          # rows loaded per table by this call
    gave_up: List[str]            # parts given up after MAX_PART_FAILS failed downloads or loads
    complete: bool                # the run has finished and every part is loaded or given up


def load_run(run_id: int, conn=None) -> Loaded:
    """Download and load every lake-<run_id>-* artifact. Idempotent: loaded parts are skipped, and the run is marked
    __complete__ once it has finished and every part is loaded or given up."""
    from .documents import merge_tree
    own = conn is None
    conn = conn or lake_connect()
    dest = ARTIFACT_DIR / str(run_id)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    totals: Dict[str, int] = {}
    try:
        # one artifact per gh call with its own timeout: a single stalled transfer of the whole run
        # once hung the refresher for hours. Artifacts already loaded are skipped.
        try:
            names = [n for n in gh("api", f"repos/{repo()}/actions/runs/{run_id}/artifacts", "--paginate",
                                   "--jq", ".artifacts[].name").split() if n.startswith(f"lake-{run_id}-")]
        except Exception as e:          # unknown is not empty: a failed listing never completes a run
            with conn, conn.cursor() as cur:
                cur.execute("insert into public.lake_load_log(run_id, artifact, files, rows_loaded, status) "
                            "values (%s,'-',0,0,%s) on conflict (run_id, artifact) do update set status=excluded.status,"
                            " loaded_at=now()", (run_id, "no_artifacts: " + str(e)[:120]))
            return Loaded(totals, [], False)
        with conn.cursor() as cur:
            cur.execute("select artifact, status, extract(epoch from now() - loaded_at) from public.lake_load_log "
                        "where run_id=%s", (run_id,))
            got = cur.fetchall()
        conn.rollback()                 # no transaction held open across hours of downloads
        logged = {a: s for a, s, _ in got}
        age = {a: float(t) for a, _, t in got}          # seconds since each part's last attempt, on the lake's clock
        todo = [n for n in plan_parts(names, logged)[0] if part_due(logged.get(n), age.get(n, 0.0))]
        # each artifact is loaded (and logged) as soon as it lands, so a slow link or a killed process keeps the
        # progress made so far; the next lake-drive skips what is already loaded. Downloads run in parallel:
        # GitHub's artifact storage serves this Mac at ~12-40 KB/s per connection, so one ~50 MB part at a time
        # never finished inside its timeout.

        def fetch(name: str) -> Tuple[bool, str]:
            try:
                r = subprocess.run(["gh", "run", "download", str(run_id), "-R", repo(), "-n", name, "-D",
                                    str(dest / name)], capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
                return r.returncode == 0, r.stderr.strip()
            except subprocess.TimeoutExpired:
                return False, f"timeout {name}"

        def load_one(name: str) -> None:
            art = dest / name
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
                            (run_id, name, n_files, sum(rows.values())))
            logged[name] = "loaded"
            shutil.rmtree(art, ignore_errors=True)

        def fail_one(name: str, why: str) -> None:      # counted per part: MAX_PART_FAILS failures give it up
            logged[name] = f"failed {fails_of(logged.get(name)) + 1}"
            with conn, conn.cursor() as cur:
                cur.execute("insert into public.lake_load_log(run_id, artifact, files, rows_loaded, status) "
                            "values (%s,%s,0,0,%s) on conflict (run_id, artifact) do update set "
                            "status=excluded.status, loaded_at=now()", (run_id, name, logged[name]))
            log.warning("run %s %s: %s: %s", run_id, name, logged[name], why[:200])
            if fails_of(logged[name]) >= MAX_PART_FAILS:
                log.warning("run %s %s: given up after %d failures", run_id, name, MAX_PART_FAILS)
            shutil.rmtree(dest / name, ignore_errors=True)

        pool = ThreadPoolExecutor(max_workers=DOWNLOAD_PARALLEL)
        try:
            futures = {pool.submit(fetch, n): n for n in todo}
            for fut in as_completed(futures):   # loads stay sequential on one connection, overlapping the downloads
                name = futures[fut]
                ok, e = fut.result()
                if ok:
                    try:
                        load_one(name)
                    except Exception as x:      # a part that won't load counts as a failure: it never blocks the run
                        conn.rollback()
                        fail_one(name, f"load {type(x).__name__}: {x}")
                else:
                    fail_one(name, e)
        finally:
            pool.shutdown(cancel_futures=True)
        todo, gave_up = plan_parts(names, logged)
        try:        # a run counts as fully loaded only once it has completed AND no part is left to download
            st = gh("run", "view", str(run_id), "-R", repo(), "--json", "status", "--jq", ".status").strip()
        except Exception:
            st = ""
        complete = st == "completed" and not todo
        if complete:
            with conn, conn.cursor() as cur:
                cur.execute("insert into public.lake_load_log(run_id, artifact, files, rows_loaded, status) "
                            "values (%s,'__complete__',%s,%s,'loaded') on conflict (run_id, artifact) do update "
                            "set loaded_at=now()", (run_id, len(names), sum(totals.values())))
        return Loaded(totals, gave_up, complete)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
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
    ready = secrets_ready()
    if not ready:
        return ("GitHub unreachable, skipped" if ready is None
                else "waiting for secrets (DATABASE_URL not set on the repo)")
    lake_up()
    conn = lake_connect()
    try:
        runs = list_runs("equity-lake.yml", 40)
        busy = [r for r in runs if r["status"] in BUSY]
        # 3. load completed runs
        with conn.cursor() as cur:
            cur.execute("select distinct run_id from public.lake_load_log where artifact='__complete__'")
            loaded = {r[0] for r in cur.fetchall()}
        conn.rollback()
        for r in sorted(runs, key=lambda x: x["databaseId"]):
            rid = r["databaseId"]
            if r["status"] != "completed" or rid in loaded:
                continue
            t0 = time.monotonic()
            res = load_run(rid, conn)
            line = f"loaded run {rid} ({sum(res.rows.values())} rows, {time.monotonic() - t0:.0f}s)"
            if res.gave_up:
                line += f" gave up on {', '.join(res.gave_up)}"
            with conn, conn.cursor() as cur:
                cur.execute("select window_id, from_rank, to_rank, ranks, attempt from public.lake_window "
                            "where run_id=%s", (rid,))
                w = cur.fetchone()
                # a run can carry several window rows: a retry of any of them counts
                cur.execute("select exists (select 1 from public.lake_window a join public.lake_window b "
                            "on a.note = 'retry of window ' || b.window_id where b.run_id=%s and a.attempt=2)", (rid,))
                retried = cur.fetchone()[0]
            if w and res.complete:          # gaps are judged once every part is loaded or given up
                ranks = window_ranks_of(w)
                # given-up parts count as missing: the fleet may have filled their facts and prices, not their documents
                miss = sorted(set(missing_ranks(conn, ranks)) | set(gave_up_ranks(ranks, res.gave_up)))
                act = retry_action(miss, w[4], retried, bool(busy))
                if act == "dispatch":
                    try:
                        new = dispatch("equity-lake.yml", {"from_rank": str(min(miss)), "to_rank": str(max(miss)),
                                                           "ranks": ranks_str(miss)},
                                       title=f"lake {min(miss)}-{max(miss)} (retry)")
                    except Exception as e:  # e.g. GitHub unreachable: re-open the run, a later drive dispatches it
                        log.warning("run %s: retry dispatch failed: %s", rid, e)
                        act = "wait"
                if act == "dispatch":
                    with conn, conn.cursor() as cur:
                        cur.execute("insert into public.lake_window(from_rank,to_rank,ranks,attempt,run_id,note) "
                                    "values (%s,%s,%s,2,%s,%s)", (min(miss), max(miss), ranks_str(miss), new,
                                                                  f"retry of window {w[0]}"))
                    busy = [{"databaseId": new, "status": "queued"}]
                    line += f" retry={len(miss)} ranks"
                if act == "wait":           # re-open the run so a later drive dispatches its retry
                    with conn, conn.cursor() as cur:
                        cur.execute("delete from public.lake_load_log where run_id=%s and artifact='__complete__'",
                                    (rid,))
                    line += f" retry of {len(miss)} ranks deferred to a later drive"
                else:
                    with conn, conn.cursor() as cur:
                        cur.execute("update public.lake_window set status=%s, loaded_at=now() where window_id=%s",
                                    ("retried" if act == "dispatch" or retried else "loaded", w[0]))
            summary.append(line)
        # 4. daily/weekly top-100 jobs; once Supabase is full (R8) the lake becomes primary for the top 100
        from .jobs import overflow_for_url
        today = date.today()
        try:
            full, err = overflow_for_url(config.database_url()), False
        except Exception as e:          # Supabase unreachable: skip its steps this tick, the lake work goes on
            log.warning("supabase overflow check failed: %s", e)
            full, err = None, True
        with conn.cursor() as cur:
            last_sync, last_docs, last_shards, primary, last_retry = (state_get(cur, k) for k in (
                "top100_sync", "docs_dispatch", "shards_dispatch", "top100_primary", "docs_retry_dispatch"))
        conn.rollback()
        sync, docs, shards, set_primary = plan_top100(full, err, primary is not None, today,
                                                      last_sync, last_docs, last_shards)
        if set_primary and primary is None:
            with conn, conn.cursor() as cur:
                state_set(cur, "top100_primary", f"lake since {today.isoformat()}")
            summary.append(f"Supabase full ({full}): the lake is now primary for the top 100")
        if sync:
            from .db import DB
            src = DB()
            try:
                n = sync_top100(src, conn, sync)
            finally:
                src.close()
            with conn, conn.cursor() as cur:
                state_set(cur, "top100_sync", today.isoformat())
            summary.append(f"synced top100 ({n} rows{', identity only' if sync == IDENTITY else ''})")
        if docs:
            if not any(r["status"] in BUSY for r in list_runs("equity-docs.yml", 5)):
                # every dispatch before docs_retry_dispatch existed was a retry_missing one
                inputs = docs_inputs(today, last_retry or last_docs)
                retry = inputs["retry_missing"] == "true"
                rid = dispatch("equity-docs.yml", inputs)
                with conn, conn.cursor() as cur:
                    state_set(cur, "docs_dispatch", today.isoformat())
                    if retry:
                        state_set(cur, "docs_retry_dispatch", today.isoformat())
                summary.append(f"dispatched equity-docs run {rid} ({'retry-missing' if retry else 'primary fetch'})")
        if shards:                      # weekend refresh of the top 100
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
                rid = dispatch("equity-lake.yml", {"from_rank": str(w[0]), "to_rank": str(w[1])},
                               title=f"lake {w[0]}-{w[1]}")
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
def sync_top100(src, lake_conn, tables=None) -> int:
    """COPY equity data tables (default: all) out of Supabase, then load them into the lake with the same remapping."""
    tmp = config.ROOT / "data" / "lake" / "sync"
    shutil.rmtree(tmp, ignore_errors=True)
    counts = export_tables(src.conn, tmp / "export", 1, tables=list(tables or TABLES))
    load_export_dir(lake_conn, tmp / "export")
    shutil.rmtree(tmp, ignore_errors=True)
    return sum(counts.values())


# ───────────────────────────── kick-off ─────────────────────────────
def lake_start() -> int:
    from .db import DB
    ready = secrets_ready()
    if not ready:
        print("GitHub unreachable; lake-drive will continue when it is back" if ready is None else
              "waiting for secrets: DATABASE_URL is not set on the repo; lake-drive will continue when it is")
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
