# Screener.in scraper -> Supabase (`equity` schema)

Scrapes the numbers screener.in exposes publicly (no login) and upserts them into Postgres schema `equity`.
Design and parsing rules: `ARCHITECTURE.md`. Schema: `sql/001_equity_schema.sql`.

## Setup
```
pip install requests beautifulsoup4 lxml psycopg2-binary     # already present on this machine
echo 'DATABASE_URL=postgresql://...' > .env                  # Supabase session pooler URL
python3 -m screener_scraper init-db                          # applies sql/*.sql (idempotent)
```

## CLI
```
python3 -m screener_scraper universe [--top 100] [--candidates nifty500|file.csv] [--limit-candidates N]
python3 -m screener_scraper scrape   [--symbols RELIANCE,TCS] [--universe TOP100_MCAP] [--force]
python3 -m screener_scraper reparse  [--date YYYY-MM-DD]     # rebuild DB from data/raw cache, zero network
python3 -m screener_scraper yf       [--symbols RELIANCE,TCS] [--universe TOP100_MCAP] [--shard k/n]
python3 -m screener_scraper coverage [--universe TOP100_MCAP]  # v_coverage + gap flags (NO_DAILY_PRICES, ...)
python3 -m screener_scraper status
python3 -m unittest tests.test_parse                         # parser tests on saved real pages
```
* `scrape --symbols ...` works standalone (company rows are created from the page itself).
* Without `--symbols` it scrapes the latest `universe` snapshot. A company whose tasks all succeeded in the
  last 20h is skipped; `--force` ignores that and the raw cache.
* Speed / sharding: `scrape --shard k/n` (universe rank `(rank-1)%n==k-1`; sorted symbols for `--symbols`),
  `--min-interval S --jitter J` (env `SCREENER_MIN_INTERVAL`, `SCREENER_JITTER`), `--workers N` threads that share ONE
  global rate limiter (they overlap latency, they cannot exceed the rate; each worker has its own DB connection and
  every company is one transaction). A 429 doubles the interval (cap 10s); it decays after 20 straight successes.
* `yf` (see ARCHITECTURE.md section 7) loads statements (sections yf-*, metric.source='yfinance'), daily OHLCV
  (`historical_prices`), dividends/splits, shares history, info snapshot, analyst estimates, earnings dates, insider
  trades; one `scrape_task` per dataset. If yfinance has no corporate actions for a company, NSE
  `corporates-corporateActions` is used (`source='nse'`). Companies must already exist in `equity.company`.
  About 30-100s per company (1s spacing between yfinance calls).
* Worker entry points: `pipeline.scrape_company(company_or_symbol, run_id, force=False) -> dict` and
  `yf.run_company(company_id, run_id) -> dict`. Both record `scrape_task` rows and then raise on failure
  (`requests.HTTPError` subclass with `.status_code`, `requests.ConnectionError`, `parse.ParseError`, DB errors).
* Storage: valuation charts go to the wide `equity.valuation_history`; the weekly price chart is no longer fetched
  (`price_history` holds only the 365-day daily series with DMA50/200 and delivery %; long history is
  `historical_prices` from yfinance). `pip install -r requirements.txt`; Python 3.11+.
* About 54 requests per company at >=1.5s each (~2.5 min/company). Raw responses are cached gzip in
  `data/raw/<date>/<slug>/`; logs go to `logs/<date>.log`.
* Audit: `equity.scrape_run` (one row per invocation) and `equity.scrape_task` (company x task, with status,
  http_status, attempts, rows_written, error, raw_path). Any failed task makes the run `partial`.

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
Notes: `basis` C = consolidated, S = standalone (`company.has_consolidated=false` means only S exists).
`period_type`: Q quarter, A fiscal year, TTM (period_end = latest quarter end), H half-year balance sheet column.
Schedules (drill-downs) appear as `Parent › Child` in `v_financials.line_item`.

## Console

An internal, read-only web app for browsing the top-100 data, annual reports and concalls (spec: `ARCHITECTURE.md` section 9).

```bash
.venv/bin/python -m library.index     # build/update data/library.sqlite (incremental; re-run as document files land)
.venv/bin/python -m console           # builds console/web if stale, serves http://127.0.0.1:8787
.venv/bin/python -m console --build   # force npm ci + build;  --dev runs next dev on :3000 with the API on :8787
```

Pages: Securities, Company (`/company/?s=TCS`, 7 tabs), Reader (`/doc/?key=...`), Library search, Ops. Shortcuts: `/`, `g s`, `g l`, `g o`, `t`, `?`.

## Top-1000 rollout (local lake)
`lake-up` (Postgres on :5433, db `equity_lake`, `LAKE_DATABASE_URL`), `lake-start` (rank via `equity-rank.yml`, then first window),
`lake-drive` (unattended tick, run by `scripts/refresh.sh`), `lake-load <run_id>`. Workflows: `equity-rank.yml`,
`equity-lake.yml` (20 runners, windows 101-400 / 401-700 / 701-1000). Lake bookkeeping: `lake_window`, `lake_load_log`, `lake_state`.
