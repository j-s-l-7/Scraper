"""CLI: python -m screener_scraper {init-db,universe,scrape,reparse,status}"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import config


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(config.LOG_DIR / f"{date.today().isoformat()}.log")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def coverage_flags(row: dict) -> list:
    flags = []
    if not row.get("daily_from"):
        flags.append("NO_DAILY_PRICES")
    if not row.get("screener_facts"):
        flags.append("NO_SCREENER_FACTS")
    if not row.get("yf_facts"):
        flags.append("NO_YF_FACTS")
    if not row.get("shareholding_rows"):
        flags.append("NO_SHAREHOLDING")
    if not row.get("corp_actions"):
        flags.append("NO_CORP_ACTIONS")
    if not row.get("shares_rows"):
        flags.append("NO_SHARES")
    if not row.get("yf_snapshot_as_of"):
        flags.append("NO_YF_SNAPSHOT")
    if not row.get("analyst_rows"):
        flags.append("NO_ESTIMATES")
    return flags


def print_coverage(cols, rows) -> None:
    short = [c for c in cols if c not in ("company_id", "name")]
    print("\t".join(short + ["gaps"]))
    gaps = 0
    for r in rows:
        d = dict(zip(cols, r))
        f = coverage_flags(d)
        gaps += bool(f)
        print("\t".join(str(d[c]) for c in short) + "\t" + (",".join(f) or "-"))
    print(f"-- {len(rows)} companies, {gaps} with gaps")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="screener_scraper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="apply sql/*.sql (idempotent)")
    u = sub.add_parser("universe", help="rank candidates by market cap and store the top N")
    u.add_argument("--top", type=int, default=100)
    u.add_argument("--candidates", default="nifty500", help="'nifty500' or path to a CSV with a Symbol column")
    u.add_argument("--limit-candidates", type=int, default=None)
    u.add_argument("--universe", default=config.UNIVERSE_CODE)
    s = sub.add_parser("scrape", help="scrape companies")
    s.add_argument("--symbols", help="comma separated NSE symbols / screener slugs")
    s.add_argument("--universe", default=config.UNIVERSE_CODE, help="universe code (latest as_of) when no --symbols")
    s.add_argument("--force", action="store_true", help="ignore resume window and raw cache")
    s.add_argument("--workers", type=int, default=1,
                   help="threads sharing ONE global rate limiter (overlaps latency, never exceeds the rate)")
    s.add_argument("--shard", help="k/n: deterministic slice (universe rank (rank-1)%%n==k-1, or sorted --symbols)")
    s.add_argument("--min-interval", type=float, help="min seconds between requests (env SCREENER_MIN_INTERVAL)")
    s.add_argument("--jitter", type=float, help="extra random 0..J seconds (env SCREENER_JITTER)")
    y = sub.add_parser("yf", help="yfinance datasets (+ NSE corporate-actions fallback) for companies in equity.company")
    y.add_argument("--symbols", help="comma separated NSE symbols (must already exist in equity.company)")
    y.add_argument("--universe", default=config.UNIVERSE_CODE)
    y.add_argument("--shard", help="k/n")
    cv = sub.add_parser("coverage", help="print equity.v_coverage with gap flags")
    cv.add_argument("--universe", help="restrict to a universe code, e.g. TOP100_MCAP")
    r = sub.add_parser("reparse", help="rebuild DB rows from the raw cache (no network)")
    r.add_argument("--date", help="YYYY-MM-DD cache folder (default: latest)")
    sub.add_parser("status", help="last runs, failures, row counts")
    en = sub.add_parser("enqueue", help="create jobs (one per kind per company) for a batch")
    en.add_argument("--batch", required=True, help="e.g. 2026-11-01")
    en.add_argument("--universe", default=config.UNIVERSE_CODE)
    en.add_argument("--kinds", default="screener,yf,docs_ar,docs_concall,docs_ppt")
    en.add_argument("--symbols", help="restrict to these symbols (testing)")
    wk = sub.add_parser("worker", help="claim and run queue jobs")
    wk.add_argument("--kinds", required=True, help="comma list of screener,yf,docs_ar,docs_concall")
    wk.add_argument("--class", dest="cls", choices=["local", "gh"], default="local")
    wk.add_argument("--slots", type=int, default=1)
    wk.add_argument("--max-runtime", type=float, default=600, help="minutes; stop claiming after this")
    wk.add_argument("--batch", help="only claim jobs of this batch (default: any)")
    sv = sub.add_parser("supervise", help="overnight control plane: workers, stale reclaim, storage guard, gh dispatch")
    sv.add_argument("--batch", required=True)
    sv.add_argument("--local", default="screener:1,yf:1,docs_ar:4,docs_concall:2")
    sv.add_argument("--dispatch-gh", action="store_true")
    sv.add_argument("--max-hours", type=float, default=14)
    sv.add_argument("--tick", type=float, default=60)
    dc = sub.add_parser("docs", help="run the documents pipeline directly (no queue) for --symbols or a universe shard")
    dc.add_argument("--symbols", help="comma separated symbols (default: the whole universe)")
    dc.add_argument("--universe", default=config.UNIVERSE_CODE)
    dc.add_argument("--shard", help="k/n slice of the universe by rank")
    dc.add_argument("--kinds", default="annual_report,concall_transcript,concall_ppt",
                    help="annual_report,concall_transcript,concall_ppt (aliases docs_ar,docs_concall,docs_ppt)")
    dc.add_argument("--slots", type=int, default=1, help="parallel companies (threads)")
    dc.add_argument("--force", action="store_true", help="also retry no_text / missing documents")
    dc.add_argument("--limit", type=int, help="max documents per kind per company (testing)")
    dc.add_argument("--retry-missing", action="store_true",
                    help="only missing/failed documents, through the wayback -> company-site fallback chain")
    dc.add_argument("--discover", action="store_true",
                    help="concall transcripts only: find quarters with no transcript on the company site")
    sub.add_parser("docs-coverage", help="print equity.v_document_completeness (annual reports + concall quarters)")
    cd = sub.add_parser("collect-documents", help="merge documents-shard-* artifacts of a workflow run into data/documents")
    cd.add_argument("run_id")
    cd.add_argument("--repo", help="default env GH_REPO or j-s-l-7/Scraper")
    ld = sub.add_parser("load-documents", help="bulk-load local jsonl.gz into equity.document_page (needs a bigger DB)")
    ld.add_argument("--limit", type=int)
    rp = sub.add_parser("report", help="write reports/<batch>.md from the job tables")
    rp.add_argument("--batch", required=True)
    a = ap.parse_args(argv)
    setup_logging()

    from .db import DB
    db = DB()
    try:
        if a.cmd == "init-db":
            print("applied:", ", ".join(db.init_db()))
        elif a.cmd == "universe":
            from .universe import run_universe
            return 0 if run_universe(db, a.top, a.candidates, a.limit_candidates, a.universe) != "failed" else 1
        elif a.cmd == "scrape":
            from .pipeline import run_scrape
            from .pipeline import shard_items, shard_ranked
            if a.min_interval is not None:
                config.MIN_INTERVAL = a.min_interval
            if a.jitter is not None:
                config.JITTER = a.jitter
            shard = config.parse_shard(a.shard)
            isins = {}
            if a.symbols:
                symbols = shard_items([x.strip() for x in a.symbols.split(",") if x.strip()], shard)
            else:
                rows = shard_ranked(db.universe_companies(a.universe), shard)
                symbols = [r[2] for r in rows]
                isins = {r[2]: r[5] for r in rows if r[5]}
                if not symbols:
                    print("universe is empty (or shard has no companies): run `universe` first or pass --symbols",
                          file=sys.stderr)
                    return 2
            run_scrape(db, symbols, force=a.force, isins=isins, workers=a.workers,
                       params={"shard": a.shard, "min_interval": config.MIN_INTERVAL, "jitter": config.JITTER})
        elif a.cmd == "yf":
            from .yf import run_yf
            syms = [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
            return 0 if run_yf(db, symbols=syms, universe=a.universe, shard=config.parse_shard(a.shard)) != "failed" else 1
        elif a.cmd == "coverage":
            cols, rows = db.coverage(a.universe)
            print_coverage(cols, rows)
        elif a.cmd == "reparse":
            from .pipeline import run_reparse
            run_reparse(db, date.fromisoformat(a.date) if a.date else None)
        elif a.cmd == "enqueue":
            from . import jobs
            kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
            syms = [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
            n = jobs.enqueue(db.trail, a.batch, a.universe, kinds, syms)
            print(f"enqueued {n} new jobs for batch {a.batch}: {jobs.counts(db.trail, a.batch)}")
        elif a.cmd == "worker":
            from .worker import run_worker
            return run_worker([k.strip() for k in a.kinds.split(",") if k.strip()], a.cls, a.slots, a.max_runtime, a.batch)
        elif a.cmd == "supervise":
            from .supervisor import run_supervise
            return run_supervise(db, a.batch, a.local, a.dispatch_gh, max_hours=a.max_hours, tick_s=a.tick)
        elif a.cmd == "docs":
            from .documents import run_docs_cli
            syms = [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
            return run_docs_cli(db, syms, [k.strip() for k in a.kinds.split(",") if k.strip()], a.force, a.limit,
                                a.universe, config.parse_shard(a.shard), a.slots, a.retry_missing, a.discover)
        elif a.cmd == "docs-coverage":
            from .documents import docs_coverage
            docs_coverage(db)
        elif a.cmd == "collect-documents":
            from .documents import collect_documents
            r = collect_documents(db, a.run_id, a.repo)
            print(r)
            return 1 if r["missing"] else 0
        elif a.cmd == "load-documents":
            from .documents import load_documents
            print(load_documents(db, a.limit))
        elif a.cmd == "report":
            from .supervisor import write_report
            print(write_report(db, a.batch))
        elif a.cmd == "status":
            st = db.status()
            print("== recent runs"); [print(" ", r) for r in st["runs"]]
            print("== recent failed tasks (run, company, task, http, error)"); [print(" ", r) for r in st["failures"]]
            print("== row counts"); [print(f"  {k:22s}{v:>10,}") for k, v in st["counts"].items()]
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
