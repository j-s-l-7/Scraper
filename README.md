# Scraper: Indian equity data for research

Collects the numbers and documents behind Indian listed companies (financial statements, prices, shareholding,
valuation history, annual reports, earnings-call transcripts and presentations) from public sources, and stores them
in Postgres for **personal, non-commercial research**. Design: `ARCHITECTURE.md`. Conventions for coding agents: `CLAUDE.md`.

| scope | stored in | state |
|---|---|---|
| Top 100 by market cap (+ Nifty 50) | Supabase Postgres, schema `equity` (free tier, shared with an unrelated `mf` schema) | complete, refreshed daily/weekly |
| Top 1000, then every NSE stock | local Postgres "lake" (`equity_lake` on :5433), same schema | rolling out unattended |
| Annual report / concall / presentation text | `data/documents/<SYMBOL>/<type>/*.jsonl.gz`, one JSON line per page | grows nightly |
| Full-text search over that text | `data/library.sqlite` (SQLite FTS5) | top 100 indexed |

This public repo holds **code only**: `.env`, `data/`, `logs/` and `reports/` are gitignored.

```
 screener.in ──┐                                     ┌─> Supabase `equity`: top 100 (writers pause at 470 MB)
 yfinance ─────┼─> GitHub Actions runners ───────────┤
 NSE / BSE ────┤   (20 per rank window, artifacts)   └─> local lake :5433: top 1000 -> all NSE ─> console :8787
 company IR, ──┘   + 6 worker processes on the Mac ───> data/documents (text) ─> library.sqlite (search)
 Wayback, CC
```

## Sources
| source | what | code |
|---|---|---|
| screener.in | ~12y annual + quarterly statements with drill-downs, ratios, shareholding by holder, PE/PBV/EV history, document links | `screener_scraper/` (`scrape`) |
| yfinance | daily OHLCV since listing, granular line items, dividends/splits, shares outstanding, analyst estimates, earnings surprises | `screener_scraper/yf.py` (`yf`) |
| BSE / NSE filings, company sites | annual reports, concall transcripts and presentations as text; fallbacks: Wayback, Common Crawl, company site, OCR, capped audio transcription | `screener_scraper/documents.py`, `fallback.py` (`docs`) |
| NSE / BSE archives | full-market bhavcopies (back-filled from 2010 NSE / 2016 BSE), F&O bhavcopy, bulk/block deals, adjustment factors | `screener_scraper/sources/` (`sources`) |
| other public data | credit ratings, macro series, GDELT news, NSDL FPI flows, MOSPI, AMC portfolios | `screener_scraper/sources/` |

A source that answers 401/403/429 or shows a bot check is skipped for that run, never worked around.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.11+
.venv/bin/python -m screener_scraper init-db                         # apply sql/*.sql (numbered, idempotent)
brew install postgresql@17                                           # local lake (16 or 15 also work)
LC_ALL=en_US.UTF-8 .venv/bin/python -m screener_scraper lake-up      # start / init / migrate the lake on :5433
```
`.env` (never committed):
```
DATABASE_URL=postgresql://...pooler.supabase.com:5432/postgres   # Supabase session pooler URL
LAKE_DATABASE_URL=postgresql://localhost:5433/equity_lake        # optional, this is the default
OPENAI_API_KEY=...                                               # optional, audio fallback for concalls
```
Writers switch to Supabase's transaction pooler (6543) automatically; read-only clients stay on the session pooler
(`DB_SESSION_POOLER=1` keeps 5432 everywhere). Postgres on macOS needs a UTF-8 locale, hence `LC_ALL`.

## Running unattended (macOS)
```bash
scripts/install-services.sh                # LaunchAgents: com.scraper.refresh (every 15 min) + com.scraper.console
scripts/local-fleet.sh start|stop|status   # 6 local workers, one per source host, writing to the lake
scripts/local-fleet.sh pause|resume        # each worker stops after its current company; refresh.sh won't restart it
```
`scripts/refresh.sh` runs every 15 minutes, is idempotent and logs to `logs/refresh_<date>.log`:
1. collect finished `equity-docs` runs into `data/documents/`
2. `lake-drive` in the background (own lock): load finished `equity-lake` runs, retry gaps, dispatch the next rank
   window; daily top-100 sync and `equity-docs`, weekly `equity-shards` (both top-100 dispatches stop while the lake is
   primary, see overflow below)
3. restart any dead fleet worker
4. `dq --db both` (once a day); public sources run in the fleet's `sources` worker
5. `library.index` (incremental) and the batch report
6. git autosync: commit and push code only when the full test suite passes and no secret-like string is staged;
   `touch data/.no-autosync` pauses it

Fleet workers (`logs/fleet_<name>.log`): `screener` (every ranked company from rank 101 up, ascending; refreshes a
company weekly, or within 20 h while its latest quarter is missing), `yf` (to rank 1000; past that, prices come from the
NSE bhavcopy), `bse` (BSE-hosted annual reports), `nse` (NSE- and other-hosted annual reports), `concalls` (transcripts +
presentations, with the fallback chain), `sources` (exchange archives). A company is capped at 40 minutes inside a worker,
and a watchdog in `scripts/local-fleet.sh` restarts a worker whose log has been silent for 60 minutes (stack dumped first).

Safety switches live in `equity.control`: `paused`, `storage_limit_mb` (470, below Supabase's 500 MB cap),
`openai_audio_budget_usd`, and one `source:<name>` marker per public source per day.

GitHub Actions (repo secrets `DATABASE_URL`, optional `OPENAI_API_KEY`):

| workflow | what |
|---|---|
| `daily-prices.yml` | Mon–Fri 17:00 IST: yfinance last 5 days, then NSE bhavcopy `--keep-adj-close`, for Nifty 50 ∪ top 100 |
| `equity-shards.yml` | top-100 screener + yfinance refresh across shards (weekly, dispatched by `lake-drive`) |
| `equity-docs.yml` | top-100 document text: primary fetch daily across 10 shards, fallback chain for missing documents at most weekly; dispatched by `lake-drive`, not while the lake is primary |
| `equity-workers.yml` | job-queue workers on 5 runners (weekly cron) |
| `equity-rank.yml` | rank every NSE equity by market cap into `TOP1000_MCAP` |
| `equity-lake.yml` | 20 runners per rank window: scrape + yf + docs per company into a runner-local Postgres, exported as artifacts |

## Rollout past the top 100 (local lake)
`lake-start` ranks NSE (`equity-rank.yml`) and dispatches the first window. From then on `lake-drive` loads each finished
`equity-lake` run, re-dispatches missing ranks once, and dispatches the next window. The base windows are 101–400,
401–700 and 701–1000. Once all three are loaded, the daily `dq` run's `grow_universe` ranks the remaining NSE candidates
past 1000 into `TOP1000_MCAP` and registers one 300-rank window at a time in `lake_state.extra_windows`, which
`lake-drive` dispatches, until no candidates remain. Artifacts download 3 at a time (GitHub's storage serves this Mac at
~12–40 KB/s per connection, and more parallel downloads starve the slow link) and each one is loaded as it lands, so an
interruption keeps the progress. Manual load: `lake-load <run_id>`. Bookkeeping tables: `lake_window`, `lake_load_log`,
`lake_state`. Progress by rank band: console → Ops → "Top N rollout" (bands continue to the deepest ranked company).

## When Supabase fills up (overflow)
When Supabase's size reaches `equity.control.storage_limit_mb` (470 MB, below the 500 MB free-tier cap), the first writer
to see it latches `control.paused` with a `db_full: ...` reason. Nothing is deleted from Supabase. While the latch holds,
every top-100 writer skips Supabase (CLI gate on scrape, yf, reparse, docs, load-documents, enqueue, worker, supervise;
the three ingestion scripts; the Supabase part of `dq`; `daily-prices` exits green). `lake-drive` marks the lake primary
for the top 100 (`lake_state` key `top100_primary`), stops dispatching `equity-docs` / `equity-shards`, and syncs only
company/universe rows down; the fleet covers the top 100 too and the library indexes the lake's top 100. Only a human
clears the latch, after copying the top 100 back; the order is in `.claude/skills/scrape-screener/SKILL.md`. Console →
Ops shows the MB left before the pause, the pause reason and the lake-primary marker.

## CLI
```bash
python -m screener_scraper universe [--top 100] [--candidates nifty500|file.csv]
python -m screener_scraper scrape   [--symbols RELIANCE,TCS] [--universe TOP100_MCAP] [--force] [--shard k/n]
python -m screener_scraper reparse  [--date YYYY-MM-DD]       # rebuild rows from data/raw, zero network
python -m screener_scraper yf       [--symbols ...] [--universe ...] [--shard k/n]
python -m screener_scraper docs     ...                       # documents pipeline for --symbols or a universe shard
python -m screener_scraper sources  --only X [--backfill-from YYYY-MM-DD] | --daily
python -m screener_scraper dq       [--db supabase|lake|both] [--force]
python -m screener_scraper coverage | docs-coverage | status | report
python -m screener_scraper --help                             # everything else (queue, lake, fleet, rank)
.venv/bin/python -m unittest discover -s tests -q             # tests (parser fixtures are saved real pages)
.venv/bin/python -m pytest -q tests/test_console_api.py       # console API: live pytest-style tests, not collected above
```
The console API tests need pytest, a dev install that is not in `requirements.txt`.
* `scrape` skips a company whose tasks all succeeded in the last 20h (`--force` ignores that and the raw cache). Workers
  share one global rate limiter: >=1.5s plus jitter per IP (`SCREENER_MIN_INTERVAL`, `SCREENER_JITTER`); a 429 doubles the
  interval (cap 10s), which decays after 20 straight successes. About 54 requests and ~2.5 min per company.
* Raw responses are cached gzipped in `data/raw/<date>/<slug>/`, so parser fixes are a `reparse`, not a re-scrape.
* `yf` loads statements (`metric.source='yfinance'`), daily OHLCV (`historical_prices`), dividends/splits, shares
  history, analyst estimates, earnings dates and insider trades, ~30–100s per company; NSE corporate actions fill in
  when yfinance has none.
* Audit: `equity.scrape_run` (one row per invocation) and `equity.scrape_task` (company × task, with status, HTTP code,
  attempts, rows written, error, raw path).

## Querying
```sql
select * from equity.v_financials where nse_symbol='TCS' and basis='C' and period_type='A' order by line_item, period_end;
select line_item, period_end, value from equity.v_financials
 where nse_symbol='RELIANCE' and basis='C' and line_item like 'Expenses ›%' and period_type='A';
select * from equity.v_latest_snapshot where nse_symbol='TCS';
select obs_date, pe, median_pe, pbv, ev_multiple from equity.valuation_history vh join equity.company c using(company_id)
 where c.nse_symbol='TCS' and basis='C' order by obs_date desc limit 20;
select h.name, sh.period_end, sh.pct from equity.shareholding_holder sh join equity.holder h using(holder_id)
 join equity.company c using(company_id) where c.nse_symbol='TCS' and sh.frequency='Q' and sh.category='promoters'
 order by sh.period_end desc, sh.pct desc;
```
`basis`: C consolidated, S standalone (`company.has_consolidated=false` means only S exists). `period_type`: Q quarter,
A fiscal year, TTM (period_end = latest quarter end), H half-year balance sheet. Drill-downs appear as `Parent › Child`.
Point-in-time: every change to a fact is kept in `equity.financial_fact_history`, so
`select * from equity.v_financials_asof('2026-06-30') where nse_symbol='TCS'` shows what was known on that date.
Split-, bonus- and dividend-adjusted prices computed from raw NSE closes: `v_prices_adjusted`. Source coverage:
`v_source_coverage`.

## Data quality
`dq` runs once a day per database: screener ↔ yfinance reconciliation, balance-sheet and cash-flow identities,
freshness (latest quarter present) and document gaps. It re-queues a bounded number of companies for repair and writes
a scorecard: `equity.dq_issue`, `dq_daily`, `dq_company_score`, `v_quality_summary`.

## Console
```bash
.venv/bin/python -m console            # http://127.0.0.1:8787 (builds console/web if stale; needs Node)
.venv/bin/python -m console --build    # force npm ci + build;  --dev runs next dev on :3000 with the API on :8787
.venv/bin/python -m library.index      # update the document search index (incremental)
```
A read-only internal app (spec: `ARCHITECTURE.md` §9), local only: http://127.0.0.1:8787 (Ops at
http://127.0.0.1:8787/ops/), bound to localhost. To view it from another device use an SSH tunnel
(`ssh -L 8787:127.0.0.1:8787 <mac>`); never bind it publicly (personal, non-commercial data).
Pages: Securities (every ranked company from the lake; `CONSOLE_DB=supabase` shows the Supabase top 100 instead),
Company (`/company/?s=TCS`, 7 tabs), Reader, Library search, and Ops (rollout progress by rank band to the deepest
ranked company, Mac workers, GitHub wave loading, Supabase size, document completeness, job queue, failures).
Ops opens with a storage banner (MB left before writers pause at `storage_limit_mb`, pause reason, lake-primary
marker), flags workers silent over the watchdog's 60 min, and adds public-source coverage (refreshed in the
background, slow view) and the latest data-quality day against the one before. Each panel loads on its own: a failed
one shows an inline error and its last good value.
Shortcuts: `/`, `g s`, `g l`, `g o`, `t`, `?`.

## Storage
Measured 2026-11-03:

| | now |
|---|---|
| local lake (numbers) | 7.5 GB |
| document text | 4.9 GB |
| search index | 2.4 GB (top 100) |
| raw screener cache (`data/raw`) | 272 MB |
| Supabase | 457 MB of the 500 MB cap |

Supabase is close to the 470 MB latch; see "When Supabase fills up (overflow)". Keep the live lake on the internal SSD.
Use an external drive for nightly backups (and for an archive of original PDFs, if wanted: roughly 150–300 GB), not a
USB stick.

## Ground rules
* Personal, non-commercial research only. Don't redistribute the data.
* Polite by design: screener >=1.5s plus jitter per IP, yfinance >=1s, exchange archives >=1s per host. Scale out across
  machines, never by going faster.
* Blocks are final: no header spoofing, cookie tricks or undocumented exchange APIs.
* Never read, write or join the `mf` schema.
* Schema changes go in a new numbered `sql/NNN_*.sql`; every write is an upsert on a natural key (more in `CLAUDE.md`).
