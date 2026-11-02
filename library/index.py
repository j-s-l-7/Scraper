"""Build / update the local FTS5 index `data/library.sqlite` from equity.document (status ok) + data/documents files.

Incremental: a document is re-indexed only when its sha (pdf_sha256 + file size) changed; documents whose text file is not
on disk yet are skipped (and counted); documents no longer `ok` in Postgres are dropped. Re-running is cheap.

    .venv/bin/python -m library.index [--rebuild] [--workers N] [--symbols TCS,INFY] [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .chunk import chunk_pages

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "library.sqlite"

DTYPE_TOKEN = {"annual_report": "dtar", "concall_transcript": "dtcc", "concall_ppt": "dtppt"}

SCHEMA = """
create table if not exists doc(
    id integer primary key, key text unique not null, company_id int, symbol text, name text, doc_type text,
    fiscal_year int, period_label text, doc_date text, url text, text_path text unique, pages int, chars int,
    sha text, fsize int
);
create index if not exists doc_symbol_idx on doc(symbol);
create table if not exists chunk(
    id integer primary key, doc_id int not null references doc(id) on delete cascade, ord int not null,
    page_from int, page_to int, heading text, text text not null,
    symtok text, dtok text, fytok text
);
create index if not exists chunk_doc_idx on chunk(doc_id, ord);
create virtual table if not exists chunk_fts using fts5(
    heading, text, symtok, dtok, fytok,
    content='chunk', content_rowid='id', tokenize='porter unicode61'
);
"""


def sym_token(symbol: str) -> str:
    return "s" + re.sub(r"[^a-z0-9]", "", symbol.lower())


def dtype_token(doc_type: str) -> str:
    return DTYPE_TOKEN.get(doc_type, "dtother")


def fy_token(fy) -> str:
    return f"fy{int(fy)}" if fy else "fyna"


def doc_key(company_id: int, doc_type: str, url: str) -> str:
    return hashlib.sha1(f"{company_id}|{doc_type}|{url}".encode()).hexdigest()[:14]


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    con.execute("pragma foreign_keys=on")
    con.executescript(SCHEMA)
    return con


def read_pages(path: Path) -> list[str]:
    pages: list[tuple[int, str]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                pages.append((int(d.get("page", len(pages) + 1)), d.get("text") or ""))
    pages.sort(key=lambda t: t[0])
    return [t for _, t in pages]


def _work(args: tuple) -> tuple:
    """Worker (separate process): read + chunk one file. Returns (key, chunks, pages, chars) or (key, error)."""
    key, path = args
    try:
        pages = read_pages(Path(path))
        chunks = chunk_pages(pages)
        return key, chunks, len(pages), sum(len(p) for p in pages)
    except Exception as e:  # corrupt gzip, partial download ...
        return key, f"{type(e).__name__}: {e}"


def catalog(symbols: list[str] | None = None) -> list[dict]:
    """Documents with status ok, from Postgres (read-only): Supabase, or the lake's top 100 once the lake is primary for
    them (Supabase full, R8). A lake that is down raises: falling back to Supabase then would make build() drop every
    lake-only document."""
    import psycopg2
    import psycopg2.extras
    from screener_scraper import config
    from screener_scraper.lake import lake_connect, lake_url, top100_on_lake

    lc = lake_connect()
    try:
        on_lake = top100_on_lake(lc)
    finally:
        lc.close()
    sql = """select d.company_id, c.nse_symbol as symbol, c.name, d.doc_type, d.fiscal_year, d.period_label, d.doc_date,
                    d.url, d.text_path, d.page_count, d.char_count, d.pdf_sha256
             from equity.document d join equity.company c using (company_id)
             where d.status = 'ok' and d.text_path is not null and c.nse_symbol is not null"""
    if on_lake:                     # the index stays the top 100's
        sql += " and d.company_id in (select company_id from equity.universe where universe_code='TOP100_MCAP')"
    params: list = []
    if symbols:
        sql += " and c.nse_symbol = any(%s)"
        params.append(symbols)
    con = psycopg2.connect(lake_url() if on_lake else config.database_url(session=True), keepalives=1, keepalives_idle=30)
    try:
        con.set_session(readonly=True, autocommit=True)
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def delete_doc(con: sqlite3.Connection, doc_id: int) -> None:
    con.execute(
        "insert into chunk_fts(chunk_fts, rowid, heading, text, symtok, dtok, fytok) "
        "select 'delete', id, heading, text, symtok, dtok, fytok from chunk where doc_id=?", (doc_id,))
    con.execute("delete from chunk where doc_id=?", (doc_id,))
    con.execute("delete from doc where id=?", (doc_id,))


def insert_doc(con: sqlite3.Connection, d: dict, key: str, chunks: list[dict], pages: int, chars: int, sha: str, fsize: int) -> int:
    cur = con.execute(
        "insert into doc(key, company_id, symbol, name, doc_type, fiscal_year, period_label, doc_date, url, text_path, pages, chars, sha, fsize)"
        " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, d["company_id"], d["symbol"], d["name"], d["doc_type"], d["fiscal_year"], d["period_label"],
         d["doc_date"].isoformat() if d.get("doc_date") else None, d["url"], d["text_path"], pages, chars, sha, fsize))
    doc_id = cur.lastrowid
    st, dt, ft = sym_token(d["symbol"]), dtype_token(d["doc_type"]), fy_token(d["fiscal_year"])
    start = con.execute("select coalesce(max(id),0) from chunk").fetchone()[0] + 1
    con.executemany(
        "insert into chunk(id, doc_id, ord, page_from, page_to, heading, text, symtok, dtok, fytok) values (?,?,?,?,?,?,?,?,?,?)",
        [(start + i, doc_id, c["seq"], c["page_from"], c["page_to"], c["heading"], c["text"], st, dt, ft) for i, c in enumerate(chunks)])
    con.execute(
        "insert into chunk_fts(rowid, heading, text, symtok, dtok, fytok) "
        "select id, heading, text, symtok, dtok, fytok from chunk where doc_id=?", (doc_id,))
    return len(chunks)


def build(db_path: Path | str = DEFAULT_DB, *, rebuild: bool = False, workers: int | None = None,
          symbols: list[str] | None = None, limit: int | None = None, docs: list[dict] | None = None,
          root: Path = ROOT, quiet: bool = False) -> dict:
    """Index the catalog. `docs` overrides the Postgres catalog (used by tests)."""
    t0 = time.time()
    if rebuild and Path(db_path).exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    con = connect(db_path)
    docs = docs if docs is not None else catalog(symbols)
    stats = dict(catalog=len(docs), indexed=0, unchanged=0, missing_file=0, failed=0, removed=0, chunks_added=0)

    existing = {r[1]: (r[0], r[2]) for r in con.execute("select id, key, sha from doc")}
    todo: list[tuple[dict, str, str, int, Path]] = []
    seen: set[str] = set()
    for d in docs:
        key = doc_key(d["company_id"], d["doc_type"], d["url"])
        seen.add(key)
        path = root / d["text_path"]
        if not path.is_file():
            stats["missing_file"] += 1
            continue
        fsize = path.stat().st_size
        sha = f"{d.get('pdf_sha256') or ''}:{fsize}"
        if key in existing and existing[key][1] == sha:
            stats["unchanged"] += 1
            continue
        todo.append((d, key, sha, fsize, path))
    if limit:
        todo = todo[:limit]

    if not symbols and not limit:  # drop docs that vanished or are no longer ok
        for key, (doc_id, _) in existing.items():
            if key not in seen:
                delete_doc(con, doc_id)
                stats["removed"] += 1
        con.commit()

    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    jobs = [(key, str(path)) for _, key, _, _, path in todo]
    meta = {key: (d, sha, fsize) for d, key, sha, fsize, _ in todo}
    done = 0

    def handle(res: tuple) -> None:
        nonlocal done
        key = res[0]
        d, sha, fsize = meta[key]
        done += 1
        if isinstance(res[1], str):
            stats["failed"] += 1
            print(f"  ! {d['symbol']} {d['period_label']}: {res[1]}", file=sys.stderr)
            return
        _, chunks, pages, chars = res
        if key in existing:
            delete_doc(con, existing[key][0])
        stats["chunks_added"] += insert_doc(con, d, key, chunks, pages, chars, sha, fsize)
        stats["indexed"] += 1
        if done % 25 == 0:
            con.commit()
            if not quiet:
                print(f"  {done}/{len(jobs)} documents ({time.time() - t0:.0f}s)", flush=True)

    if workers > 1 and len(jobs) > 2:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(_work, jobs, chunksize=1):
                handle(res)
    else:
        for j in jobs:
            handle(_work(j))
    con.commit()

    stats["docs_total"] = con.execute("select count(*) from doc").fetchone()[0]
    stats["chunks_total"] = con.execute("select count(*) from chunk").fetchone()[0]
    if todo:
        con.execute("insert into chunk_fts(chunk_fts) values('optimize')")
        con.commit()
    con.close()
    p = Path(db_path)
    stats["index_mb"] = round(sum(f.stat().st_size for f in p.parent.glob(p.name + "*")) / 1e6, 1)
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build/update data/library.sqlite")
    ap.add_argument("--rebuild", action="store_true", help="delete the index and start over")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--symbols", default=None, help="comma-separated NSE symbols")
    ap.add_argument("--limit", type=int, default=None, help="index at most N new/changed documents")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    a = ap.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None
    st = build(a.db, rebuild=a.rebuild, workers=a.workers, symbols=syms, limit=a.limit)
    print("library index:")
    for k, v in st.items():
        print(f"  {k:14} {v}")
    if st["missing_file"]:
        print(f"  ({st['missing_file']} documents have no local file yet; re-run after the files land)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
