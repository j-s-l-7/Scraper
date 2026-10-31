---
name: scrape-screener
description: Run, resume, monitor or repair the equity data pipeline (screener.in numbers, yfinance, NSE/BSE annual reports and concalls) and its self-healing overnight job queue. Use for "refresh the data", "scrape X", "why did job Y fail", re-running the top-100 universe, reading the morning report, or adding companies.
---

# Equity pipeline operations

Package: `screener_scraper/` (run with `.venv/bin/python -m screener_scraper <cmd>`). The design is in `ARCHITECTURE.md`.

## Day-to-day
```bash
python -m screener_scraper universe --top 100            # re-rank Nifty 500 by market cap → equity.universe
python -m screener_scraper scrape --symbols TCS,INFY     # one-off screener scrape (both bases)
python -m screener_scraper yf --symbols TCS              # yfinance: daily OHLCV since listing, statements, actions, estimates
python -m screener_scraper docs --symbols TCS            # annual reports + concall transcripts → data/documents/
python -m screener_scraper coverage                      # per-company source coverage + gap flags
python -m screener_scraper status                        # recent runs, failed tasks, row counts
python -m screener_scraper reparse --date YYYY-MM-DD     # rebuild DB rows from the raw cache (no network)
```

## Full refresh (unattended)
```bash
scripts/overnight.sh            # enqueue today's batch, then start the supervisor under caffeinate + nohup
tail -f logs/supervisor_<batch>.log
cat reports/<batch>.md          # morning report: done/dead per kind, error classes, coverage gaps, DB size
```
The supervisor runs local workers (screener 1 slot per IP, yf 1, docs 6). It also dispatches GitHub Actions
`equity-workers.yml` (5 runners on separate IPs) for screener and yf jobs. Workers on every machine share
`equity.job` through `FOR UPDATE SKIP LOCKED`.

## When something breaks
The rule engine (`screener_scraper/rules.py`) handles failures by itself: it classifies errors (rate_limited, blocked,
network, server, not_found, parse, db_transient, db_full, pdf_no_text, stale, unknown), backs off, reroutes jobs
blocked on GitHub runners to local workers, and pauses everything if the DB nears the 500 MB cap. Every action is
recorded in `equity.job.history`.

Manual triage:
```sql
select kind, status, count(*) from equity.job where batch = '<batch>' group by 1,2 order by 1,2;
select c.nse_symbol, j.kind, j.error_class, j.last_error, j.history
from equity.job j join equity.company c using (company_id)
where j.batch = '<batch>' and j.status = 'dead';
select * from equity.control;                          -- paused? reason?
```
* **dead `parse`**: screener changed its markup. Open the raw file named in `scrape_task.raw_path`, fix `parse.py`,
  add a fixture test, run `reparse`, then set those jobs back to `pending`.
* **`paused` with reason db_full**: the DB is near the free-tier cap. Free space, or upgrade Supabase and raise
  `control.storage_limit_mb`, then set `paused=false`.
* **docs `no_text`**: scanned PDFs that need OCR later. They do not count as failures.
* **Requeue a job**: `update equity.job set status='pending', attempts=0, next_run_at=now() where ...`.

## Politeness limits (do not raise)
screener.in: ≥1.5s plus jitter between requests per IP, adaptive slow-down on 429. BSE: ≥0.5s per slot.
yfinance: ≥1s. Concurrency only comes from separate IPs or separate sources.
