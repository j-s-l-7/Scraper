"""Local Postgres "lake" (companies beyond the Supabase top 100): cluster management, export, remapping loader,
Supabase sync. The lake has the same `equity` schema as Supabase (all sql/*.sql applied) plus lake-only bookkeeping
tables from sql/lake/lake_tables.sql.

Artifact layout produced by a GitHub runner (see lake_runner.py):
    export/<table>.<seq>.csv.gz   COPY ... CSV HEADER, NULL as \\N, one set of files per company
    documents/...                 data/documents tree
"""
from __future__ import annotations

import csv
import getpass
import gzip
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values

from . import config

log = logging.getLogger(__name__)

LAKE_DIR = config.ROOT / "data" / "lake"
PGDATA = LAKE_DIR / "pgdata"
ARTIFACT_DIR = LAKE_DIR / "artifacts"
LAKE_PORT = 5433
LAKE_DB = "equity_lake"
DEFAULT_LAKE_URL = f"postgresql://localhost:{LAKE_PORT}/{LAKE_DB}"
NULL = "\\N"

# table -> (primary key columns, company filter column or None).  Order = load order.
TABLES: Dict[str, Tuple[Tuple[str, ...], Optional[str]]] = {
    "company": (("company_id",), "company_id"),
    "metric": ((), None),                       # special: natural-key remap
    "holder": ((), None),                       # special: natural-key remap
    "scrape_run": ((), None),                   # special: new ids
    "financial_fact": (("company_id", "basis", "metric_id", "period_type", "period_end"), "company_id"),
    "market_snapshot": (("company_id", "as_of"), "company_id"),
    "growth_summary": (("company_id", "basis", "as_of", "measure", "horizon"), "company_id"),
    "shareholding": (("company_id", "frequency", "category", "period_end"), "company_id"),
    "shareholding_holder": (("company_id", "frequency", "category", "holder_id", "period_end"), "company_id"),
    "price_history": (("company_id", "frequency", "trade_date"), "company_id"),
    "valuation_history": (("company_id", "basis", "obs_date"), "company_id"),
    "document_link": (("company_id", "doc_type", "url"), "company_id"),
    "document": (("company_id", "doc_type", "url"), "company_id"),
    "historical_prices": (("symbol", "date"), "symbol"),
    "corporate_action": (("company_id", "action_type", "ex_date"), "company_id"),
    "shares_outstanding": (("company_id", "as_of"), "company_id"),
    "yf_snapshot": (("company_id", "as_of"), "company_id"),
    "analyst_estimate": (("company_id", "as_of", "estimate_type", "period", "field"), "company_id"),
    "earnings_event": (("company_id", "event_date"), "company_id"),
    "insider_transaction": (("company_id", "row_hash"), "company_id"),
    "scrape_task": (("run_id", "company_id", "task"), "company_id"),
    "universe": (("universe_code", "as_of", "company_id"), "company_id"),
}
RUNNER_TABLES = [t for t in TABLES if t != "universe"]
RUN_ID_TABLES = {"financial_fact", "market_snapshot", "growth_summary", "shareholding", "shareholding_holder",
                 "yf_snapshot", "corporate_action", "scrape_task"}
KEEP_ON_UPDATE = {"first_seen_at"}


# ───────────────────────────── cluster management ─────────────────────────────
def pg_bin(name: str) -> str:
    # postgresql@17 preferred; @15 is the fallback (brew could not build 17 on this macOS: outdated CLT)
    cands = [f"{pre}/opt/postgresql@{v}/bin/{name}" for v in (17, 16, 15) for pre in ("/usr/local", "/opt/homebrew")]
    for c in cands:
        if os.path.exists(c):
            return c
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"{name} not found: brew install postgresql@17")


def lake_url() -> str:
    config.load_env()
    return os.environ.get("LAKE_DATABASE_URL") or DEFAULT_LAKE_URL


def _run(*cmd, check=True, **kw) -> subprocess.CompletedProcess:
    # postgres refuses to start ("postmaster became multithreaded") on macOS without a valid locale
    env = {**os.environ, "LC_ALL": os.environ.get("LC_ALL") or "en_US.UTF-8", **kw.pop("env", {})}
    return subprocess.run(list(cmd), check=check, capture_output=True, text=True, env=env, **kw)


def lake_running() -> bool:
    return PGDATA.exists() and _run(pg_bin("pg_ctl"), "-D", str(PGDATA), "status", check=False).returncode == 0


def lake_up() -> str:
    """Idempotent: init the cluster, start it if down, create the database, apply every migration."""
    LAKE_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    bin_major = re.search(r"\)\s*(\d+)", _run(pg_bin("pg_ctl"), "--version").stdout).group(1)   # "pg_ctl (PostgreSQL) 15.4 (Homebrew)"
    if (PGDATA / "PG_VERSION").exists() and (PGDATA / "PG_VERSION").read_text().strip() != bin_major:
        if lake_running():
            _run(pg_bin("pg_ctl"), "-D", str(PGDATA), "stop", "-m", "fast", check=False)
        backup = PGDATA.with_name(f"pgdata.v{(PGDATA / 'PG_VERSION').read_text().strip()}.{int(time.time())}")
        log.warning("pgdata is PG %s but binaries are PG %s: moving it to %s and re-initialising",
                    (PGDATA / "PG_VERSION").read_text().strip(), bin_major, backup.name)
        PGDATA.rename(backup)
    if (PGDATA / "postmaster.pid").exists() and not lake_running():
        (PGDATA / "postmaster.pid").unlink()                  # stale pid file after a crash / reboot
    if not (PGDATA / "PG_VERSION").exists():
        log.info("initdb %s", PGDATA)
        _run(pg_bin("initdb"), "-D", str(PGDATA), "-A", "trust", "-U", getpass.getuser(), "-E", "UTF8")
        with open(PGDATA / "postgresql.conf", "a") as f:
            f.write(f"\n# equity lake\nport = {LAKE_PORT}\nlisten_addresses = 'localhost'\nshared_buffers = 512MB\n"
                    "maintenance_work_mem = 256MB\nwork_mem = 64MB\nsynchronous_commit = off\n"
                    "max_wal_size = 4GB\ncheckpoint_timeout = 15min\n")
    if not lake_running():
        log.info("starting lake postgres on :%d", LAKE_PORT)
        r = _run(pg_bin("pg_ctl"), "-D", str(PGDATA), "-l", str(config.LOG_DIR / "lake-pg.log"), "-w", "-t", "60",
                 "start", check=False)
        if r.returncode != 0:
            raise RuntimeError(f"pg_ctl start failed: {r.stdout} {r.stderr}")
    user = getpass.getuser()
    admin = psycopg2.connect(f"postgresql://{user}@localhost:{LAKE_PORT}/postgres", connect_timeout=10)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("select 1 from pg_database where datname=%s", (LAKE_DB,))
        if cur.fetchone() is None:
            cur.execute(f'create database "{LAKE_DB}"')
    admin.close()
    url = lake_url()
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute((config.SQL_DIR / "lake" / "lake_tables.sql").read_text())
        cur.execute("create table if not exists public.lake_migration(name text primary key, "
                    "applied_at timestamptz not null default now())")
        cur.execute("select name from public.lake_migration")
        applied = {r[0] for r in cur.fetchall()}
        # sql files redefine views across migrations, so each file must run exactly once, in order
        for f in sorted(config.SQL_DIR.glob("*.sql")):
            if f.name in applied:
                continue
            cur.execute(f.read_text())
            cur.execute("insert into public.lake_migration(name) values (%s)", (f.name,))
    conn.close()
    return url


def lake_connect(autocommit: bool = False):
    c = psycopg2.connect(lake_url(), connect_timeout=15, **{"keepalives": 1, "keepalives_idle": 30})
    c.autocommit = autocommit
    return c


# ───────────────────────────── export (runner side and Supabase sync) ─────────────────────────────
def _copy_out(cur, query: str, params: tuple) -> Tuple[bytes, int]:
    buf = io.StringIO()
    q = cur.mogrify(query, params).decode()
    cur.copy_expert(f"COPY ({q}) TO STDOUT WITH (FORMAT csv, HEADER true, NULL '{NULL}')", buf)
    text = buf.getvalue()
    return text.encode("utf-8"), max(text.count("\n") - 1, 0)


def export_tables(conn, out_dir: Path, seq: int, *, company_id: Optional[int] = None, symbol: Optional[str] = None,
                  tables: Sequence[str] = RUNNER_TABLES) -> Dict[str, int]:
    """Write one gzipped CSV per non-empty table: <table>.<seq:06d>.csv.gz. With company_id/symbol only that
    company's rows (plus the global metric table, referenced holders and all scrape_run rows)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}
    with conn.cursor() as cur:
        for t in tables:
            _, colname = TABLES[t]
            if t == "holder":
                if company_id is None:
                    q, p = "select * from equity.holder", ()
                else:
                    q, p = ("select * from equity.holder where holder_id in "
                            "(select holder_id from equity.shareholding_holder where company_id=%s)", (company_id,))
            elif t in ("metric", "scrape_run") or (company_id is None and symbol is None):
                q, p = f"select * from equity.{t}", ()
            elif colname == "symbol":
                q, p = f"select * from equity.{t} where symbol=%s", (symbol,)
            else:
                q, p = f"select * from equity.{t} where {colname}=%s", (company_id,)
            data, n = _copy_out(cur, q + (" order by 1" if t in ("metric", "scrape_run") else ""), p)
            if n == 0:
                continue
            tmp = out_dir / f".{t}.{seq:06d}.csv.gz.tmp"        # not matched by the loader's *.csv.gz glob
            tmp.write_bytes(gzip.compress(data, 3))
            tmp.replace(out_dir / f"{t}.{seq:06d}.csv.gz")      # atomic: a runner killed mid-write leaves no torn file
            counts[t] = n
    conn.rollback()
    return counts


# ───────────────────────────── load / remap ─────────────────────────────
def read_csv_gz(path: Path) -> List[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return [{k: (None if v == NULL else v) for k, v in r.items()} for r in csv.DictReader(f)]


def group_files(export_dir: Path) -> Dict[str, List[Path]]:
    g: Dict[str, List[Path]] = {}
    for p in sorted(export_dir.glob("*.csv.gz")):
        m = re.match(r"^(?P<t>[a-z_]+)\.(?P<seq>\d+)\.csv\.gz$", p.name)
        if m and m["t"] in TABLES:
            g.setdefault(m["t"], []).append(p)
    return g


def order_metrics(rows: Dict[int, dict]) -> List[dict]:
    """Parents before children (metric.parent_metric_id)."""
    done, out, pending = set(), [], dict(rows)
    while pending:
        progressed = False
        for mid, r in list(pending.items()):
            p = r.get("parent_metric_id")
            if p is None or int(p) in done or int(p) not in rows:
                out.append(r)
                done.add(mid)
                del pending[mid]
                progressed = True
        if not progressed:
            raise ValueError(f"metric parent cycle / missing parents: {sorted(pending)[:5]}")
    return out


def build_metric_map(cur, files: List[Path]) -> Dict[int, int]:
    """Local metric_id -> lake metric_id by natural key (section, name, parent natural key); inserts missing."""
    rows: Dict[int, dict] = {}
    for p in files:
        for r in read_csv_gz(p):
            rows[int(r["metric_id"])] = r
    mapping: Dict[int, int] = {}
    for r in order_metrics(rows):
        parent = r.get("parent_metric_id")
        pid = mapping[int(parent)] if parent is not None and int(parent) in mapping else None
        cur.execute("select metric_id from equity.metric where section=%s and name=%s and "
                    "parent_metric_id is not distinct from %s", (r["section"], r["name"], pid))
        got = cur.fetchone()
        if got is None:
            cur.execute("""insert into equity.metric(section, name, parent_metric_id, unit, canonical_code,
                              display_order, source) values (%s,%s,%s,%s,%s,%s,%s) returning metric_id""",
                        (r["section"], r["name"], pid, r["unit"], r.get("canonical_code"), r.get("display_order"),
                         r.get("source") or "screener"))
            got = cur.fetchone()
        mapping[int(r["metric_id"])] = got[0]
    return mapping


def build_holder_map(cur, files: List[Path]) -> Dict[int, int]:
    rows: Dict[int, dict] = {}
    for p in files:
        for r in read_csv_gz(p):
            rows[int(r["holder_id"])] = r
    if rows:
        execute_values(cur, "insert into equity.holder(holder_key, name, person_url) values %s "
                            "on conflict (holder_key) do nothing",
                       [(r["holder_key"], r["name"], r.get("person_url")) for r in rows.values()])
        cur.execute("select holder_key, holder_id from equity.holder where holder_key = any(%s)",
                    ([r["holder_key"] for r in rows.values()],))
        by_key = dict(cur.fetchall())
    else:
        by_key = {}
    return {lid: by_key[r["holder_key"]] for lid, r in rows.items()}


def build_run_map(cur, files: List[Path]) -> Dict[int, int]:
    """Local scrape_run ids -> lake ids. Natural key = (command, started_at, host): reloading finds the same row."""
    rows: Dict[int, dict] = {}
    for p in files:
        for r in read_csv_gz(p):
            rows[int(r["run_id"])] = r            # later files carry the final status
    mapping: Dict[int, int] = {}
    for lid, r in sorted(rows.items()):
        cur.execute("select run_id from equity.scrape_run where command=%s and started_at=%s and "
                    "host is not distinct from %s", (r["command"], r["started_at"], r.get("host")))
        got = cur.fetchone()
        if got is None:
            cur.execute("""insert into equity.scrape_run(command, params, status, stats, host, started_at, finished_at)
                           values (%s,%s,%s,%s,%s,%s,%s) returning run_id""",
                        (r["command"], r.get("params") or "{}", r["status"], r.get("stats") or "{}", r.get("host"),
                         r["started_at"], r.get("finished_at")))
        else:
            cur.execute("update equity.scrape_run set status=%s, stats=%s, finished_at=%s where run_id=%s "
                        "returning run_id", (r["status"], r.get("stats") or "{}", r.get("finished_at"), got[0]))
        mapping[lid] = cur.fetchone()[0]
    return mapping


def _temp_map(cur, name: str, mapping: Dict[int, int]) -> None:
    cur.execute(f"create temp table if not exists {name} (local_id bigint primary key, lake_id bigint not null)")
    cur.execute(f"truncate {name}")
    if mapping:
        execute_values(cur, f"insert into {name} values %s", list(mapping.items()))


def load_file(cur, table: str, path: Path, maps: Dict[str, str]) -> int:
    """Stage one CSV into a typed temp table, remap surrogate ids, upsert on the table's natural PK."""
    pk, _ = TABLES[table]
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        header = next(csv.reader([f.readline()]))
    cols = header
    cur.execute("drop table if exists _st")
    cur.execute(f"create temp table _st (like equity.{table})")
    with gzip.open(path, "rb") as f:
        cur.copy_expert(f"COPY _st ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '{NULL}')", f)
    if "metric_id" in cols and table == "financial_fact":
        cur.execute("select count(*) from _st where metric_id not in (select local_id from _m_metric)")
        if cur.fetchone()[0]:
            raise ValueError(f"{path.name}: metric_id without mapping")
        cur.execute("update _st s set metric_id = m.lake_id from _m_metric m where s.metric_id = m.local_id")
    if "holder_id" in cols and table == "shareholding_holder":
        cur.execute("select count(*) from _st where holder_id not in (select local_id from _m_holder)")
        if cur.fetchone()[0]:
            raise ValueError(f"{path.name}: holder_id without mapping")
        cur.execute("update _st s set holder_id = m.lake_id from _m_holder m where s.holder_id = m.local_id")
    if "run_id" in cols and table in RUN_ID_TABLES:
        if table == "scrape_task":
            cur.execute("delete from _st where run_id not in (select local_id from _m_run)")
        else:
            cur.execute("update _st set run_id = null where run_id is not null and "
                        "run_id not in (select local_id from _m_run)")
        cur.execute("update _st s set run_id = m.lake_id from _m_run m where s.run_id = m.local_id")
    upd = [c for c in cols if c not in pk and c not in KEEP_ON_UPDATE]
    conflict = ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in upd)) if upd else "DO NOTHING"
    cur.execute(f"""insert into equity.{table} ({', '.join(cols)})
                    select distinct on ({', '.join(pk)}) {', '.join(cols)} from _st order by {', '.join(pk)}
                    on conflict ({', '.join(pk)}) {conflict}""")
    n = cur.rowcount
    cur.execute("drop table if exists _st")
    return n


def load_export_dir(conn, export_dir: Path) -> Dict[str, int]:
    """Load one artifact's export directory into the lake. Idempotent (natural-key upserts)."""
    groups = group_files(export_dir)
    totals: Dict[str, int] = {}
    with conn:
        with conn.cursor() as cur:
            _temp_map(cur, "_m_metric", build_metric_map(cur, groups.get("metric", [])))
            _temp_map(cur, "_m_holder", build_holder_map(cur, groups.get("holder", [])))
            _temp_map(cur, "_m_run", build_run_map(cur, groups.get("scrape_run", [])))
    for t in TABLES:
        if t in ("metric", "holder", "scrape_run"):
            continue
        for p in groups.get(t, []):
            with conn:
                with conn.cursor() as cur:
                    totals[t] = totals.get(t, 0) + load_file(cur, t, p, {})
    return totals
