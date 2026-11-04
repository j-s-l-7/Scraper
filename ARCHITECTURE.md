# Screener.in → Supabase equity pipeline

Scrapes **all numerical data** screener.in exposes publicly (no login) for the **top 100 Indian
listed companies by current market cap**, and stores it in Supabase Postgres, schema **`equity`**
(isolated from the `mf` mutual-fund schema in the same project).

Annual reports / concall PDFs are **not downloaded**: only their links go into `equity.document_link`,
so a later NSE/BSE pipeline can fetch them.

OpenAI is not used. Every value is a parsed number, so no LLM is needed.

## 1. System design

```
             ┌──────────────┐   Nifty 500 CSV (niftyindices.com) = candidate set
 universe ──►│ universe.py  │── fetch screener page per candidate → parse Market Cap → rank → top 100
             └──────┬───────┘   → equity.company + equity.universe('TOP100_MCAP', as_of)
                    │
 scrape  ──► pipeline.py ── per company, per basis (C=consolidated, S=standalone)
                    │        ├─ page        /company/<slug>/consolidated/  and /company/<slug>/
                    │        ├─ schedules   /api/company/<cid>/schedules/?parent=..&section=..[&consolidated=]
                    │        ├─ investors   /api/3/<cid>/investors/<category>/<quarterly|yearly>/
                    │        └─ charts      /api/company/<cid>/chart/?q=...&days=..[&consolidated=true]
                    │
          http.py (one session, rate limit, retry/backoff, raw cache) ── data/raw/<YYYY-MM-DD>/<slug>/...gz
          parse.py (pure functions: html/json → dataclasses, no I/O)
          db.py    (psycopg2, upserts via execute_values, ON CONFLICT DO UPDATE)
          trail    equity.scrape_run (one per CLI invocation) + equity.scrape_task (one per company×task)
```

Design principles:
* **Long/narrow facts + metric dimension.** Line items differ by business model (banks have
  `Revenue / Financing Profit / Gross NPA %`, others have `Sales / Operating Profit`). Metrics are rows in
  `equity.metric`, so a new line item never needs a migration. `canonical_code` is reserved for later
  cross-industry harmonisation.
* **Idempotent upserts.** Every table has a natural primary key. Re-running a scrape overwrites values
  (screener restates history), and `market_snapshot` / `universe` / `growth_summary` accrue by `as_of` date.
* **Fetch and parse are separate.** Raw responses are cached gzip on disk, so the parser can be fixed and
  re-run (`reparse`) with no network traffic.
* **Resumable.** A company whose tasks all succeeded in the last 20h is skipped unless `--force`.
* **Polite.** One session with a single worker by default, at least 1.5s between requests plus jitter, exponential backoff on
  429/5xx honouring `Retry-After`. That is about 55 requests per company, so the full top-100 run takes 1.5–2h.

## 2. Tables (see `sql/001_equity_schema.sql`)

| table | grain (PK) | source |
|---|---|---|
| `company` | company_id (screener id) | page header, links, peers breadcrumb, about/pros/cons |
| `metric` | metric_id; unique(section,name,parent) | row labels of every statement + schedule keys |
| `financial_fact` | company, basis, metric, period_type, period_end | quarters, P&L, BS, CF, ratios tables + all schedules |
| `market_snapshot` | company, as_of | `#top-ratios` |
| `growth_summary` | company, basis, as_of, measure, horizon | `.ranges-table` boxes in P&L section |
| `shareholding` | company, Q/Y, category, period_end | shareholding tables (quarterly + yearly) |
| `holder`, `shareholding_holder` | holder-level % | investors API |
| `price_history` | company, D/W, date | chart API Price-DMA50-DMA200-Volume |
| `valuation_series` | company, basis, series, date | chart API PE / PBV / EV-EBITDA / MCap-Sales / margins |
| `document_link` | company, doc_type, url | `#documents` section (links only) |
| `universe` | universe_code, as_of, company | ranking step |
| `scrape_run`, `scrape_task` | audit trail | pipeline |

## 3. Parsing rules (exact)

**Company identity** (from the page): `company_id` = `data-company-id`, `warehouse_id` = `data-warehouse-id`
(both on elements in the page; take the first occurrence). Name = `h1`. NSE symbol and BSE code come from the
`.company-links` anchors (`nseindia.com/get-quotes/equity?symbol=X`, BSE url path `/<code>/`). ISIN: if not
present on the page, take it from the Nifty 500 CSV. Classification comes from `#peers` breadcrumb
anchors `href=/market/<code>/...`: the first 4 market links are sector › industry_group › industry › sub_industry,
and the last market link's code is `industry_code`. About = `.company-profile .about`
(fallback `.about`), key points = `.company-profile .commentary`, pros = `.pros li`, cons = `.cons li`, website = first
external anchor in `.company-links` that isn't bse/nse.

**Basis.** Fetch `/company/<slug>/consolidated/` (C) and `/company/<slug>/` (S). If the consolidated page has
no consolidated data (screener then shows standalone figures; detect via the text in the
`#profit-loss` section header / sub-heading containing "Standalone" rather than "Consolidated", or the absence
of a "View Standalone" toggle), set `has_consolidated=false` and store only S.

**Statement tables**: `#quarters`, `#profit-loss`, `#balance-sheet`, `#cash-flow`, `#ratios`, each
`table.data-table`. Header `thead th[1:]` = period labels. For each `tbody tr`:
* label = text of the first `td` with tooltips/nested spans removed (take the `button` text if a
  schedule button exists, else the first direct text node), then strip a trailing `+` and whitespace.
  Example: `"CFO/EBITDA Share of operating profit..."` → `"CFO/EBITDA"`.
* skip the rows `Raw PDF`, and any row whose cells are all empty.
* a schedule exists when the first cell has `button[onclick*=showSchedule]`. Parse the parent name from
  `showSchedule('<parent>', '<section>', this)`.
* unit: label ends with `%` → pct; `EPS in Rs` → rs; contains `Days` or `Cash Conversion Cycle` → days;
  everything else → cr.
* value: strip `,`, `%`, `₹`, whitespace; `''`/`-` → skip (never store NULL facts).

**Periods**: `'Mar 2026'` → period_end = last day of that month. `'TTM'` → period_type `TTM`,
period_end = latest quarter-end in the `#quarters` table. P&L/CF/ratios/annual columns → `A`. Quarter table → `Q`.
Balance sheet: the fiscal-year-end month is the most common month among its columns. A trailing column with a
different month → `H`.

**Schedules** API returns `{child_label: {period_label: "12.3%"|"1,234"}}`. Child metric = (section,
child_label, parent_metric_id). Unit = pct if any value ends with `%` or the label ends with `%`, else cr.
Same period rules (the quarters section → Q, others → A, `TTM` key → TTM). Send `consolidated=` (empty value)
for basis C and omit it for S, with headers `X-Requested-With: XMLHttpRequest` and a Referer pointing at the page.

**Growth boxes**: `#profit-loss table.ranges-table`. The first `th` gives the measure (`Compounded Sales Growth`→sales_growth,
`Compounded Profit Growth`→profit_growth, `Stock Price CAGR`→price_cagr, `Return on Equity`→roe). Rows
`10 Years:`→10Y, `5 Years:`→5Y, `3 Years:`→3Y, `1 Year:`→1Y, `TTM:`→TTM, `Last Year:`→LAST.

**Top ratios** (`#top-ratios li`, `.name` + `.value`/`.number`): Market Cap→market_cap_cr, Current Price→price,
`High / Low`→high_52w/low_52w, Stock P/E→pe, Book Value, Dividend Yield, ROCE, ROE, Face Value. Every other
label goes into `extra` jsonb. Basis-independent: take it from the C page if it exists, else S.

**Shareholding**: `#shareholding` has two tables (quarterly first, yearly second; or ids `#quarterly-shp` /
`#yearly-shp`). Map labels Promoters→promoters, FIIs→fii, DIIs→dii, Government→government, Public→public,
Others→others, No. of Shareholders→num_shareholders. Holder detail comes from
`/api/3/<company_id>/investors/<cat>/<quarterly|yearly>/` for cat in promoters, foreign_institutions,
domestic_institutions, government, public, but only for categories that have a `showShareholders` button.
Response `{holder_name: {period: "11.20", ..., "setAttributes": {"data-person-url": "/people/.."}}}`. Ignore
non-period keys. holder_key = person url or `name:` + lowercase name.

**Charts**: `/api/company/<company_id>/chart/?q=<Q>&days=<N>` (+`&consolidated=true` for basis C)
returns `{"datasets":[{"metric":..,"label":..,"values":[[date, value(, {extra})], ...]}]}`.
* price (basis-independent, use C if available): `Price-DMA50-DMA200-Volume` with days=10000 → frequency W,
  days=365 → D. Volume values carry `{"delivery": pct}` → delivery_pct. Pivot into one `price_history` row per date.
* valuation (per basis): `Price to Earning-Median PE-EPS`, `Price to book value-Median PBV-Book value`,
  `EV Multiple-Median EV Multiple-EBITDA`, `Market Cap to Sales-Median Market Cap to Sales-Sales`,
  `GPM-OPM-NPM-Quarter Sales`, all with days=10000. series = slug of `metric` (lowercase, non-alnum→`_`).
  Drop null values. A dataset with no non-null values is fine (banks have no EV multiple).

**Documents** (`#documents`): annual reports (`.annual-reports a`), concalls (`.concalls li`: the date label plus
links whose text is Transcript / PPT / REC / Notes → concall_transcript / concall_ppt / concall_recording /
concall_notes), announcements, credit ratings. source = bse/nse/other by host. Links only.

## 4. Universe

1. Download `https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv` (Symbol, ISIN, Company Name).
2. For each symbol fetch the consolidated page (raw-cached, so the scrape step reuses it) and parse Market Cap.
   The full top 100 by total market cap is contained in the Nifty 500.
3. Rank by market cap, take the top N (default 100), upsert `company` and `universe('TOP100_MCAP', today, rank)`.
   Candidates that fail to fetch are logged in `scrape_task` with task `universe` and excluded.

## 5. CLI

```
python -m screener_scraper init-db                       # apply sql/*.sql
python -m screener_scraper universe [--top 100] [--candidates nifty500|<csv>] [--limit-candidates N]
python -m screener_scraper scrape  [--symbols RELIANCE,TCS] [--universe TOP100_MCAP] [--force] [--workers 1]
python -m screener_scraper reparse [--date YYYY-MM-DD]   # rebuild DB rows from raw cache, no network
python -m screener_scraper status                        # last runs, failures, row counts per table
```

## 6. Expansion path
* **More companies**: `universe --top 500` or a new `universe_code`. The schema is unchanged.
* **Daily refresh**: cron `scrape` → snapshots accrue by `as_of` and facts are upserted.
* **NSE/BSE documents**: a new module fills `document` content tables keyed by `document_link`.
* **Cross-company comparisons**: populate `metric.canonical_code` (e.g. Sales≈Revenue).

## 7. Source fusion: screener.in + yfinance + NSE/BSE fallback

Coverage target is the `TOP100_MCAP` universe, with **every year each source provides**. These are numbers only, so
history is never truncated. Concalls and annual-report text come later.

| dataset | primary | second source / fallback | table |
|---|---|---|---|
| Financial statements, 12y annual + 13 quarters + drill-down breakdowns | screener | yfinance granular line items (5y A, ~5 Q) | `financial_fact` (`metric.source`) |
| Ratios, CAGR boxes, PE/PBV/EV history | screener | – | `financial_fact`, `growth_summary`, `valuation_series` |
| Shareholding (category + holder) | screener | – | `shareholding`, `shareholding_holder` |
| Daily OHLCV + adj close, full history since listing | yfinance `history(period='max', auto_adjust=False)` | NSE bhavcopy (authoritative OHLCV ≥ 2024-07-08, `--keep-adj-close`); screener weekly stays in `price_history` | `historical_prices` |
| Dividends / splits | yfinance `actions` | NSE `api/corporates-corporateActions` when yfinance returns none | `corporate_action` |
| Shares outstanding history | yfinance `get_shares_full(start='1990-01-01')` | – (can be derived from screener Equity Capital / Face Value) | `shares_outstanding` |
| Valuation/profile snapshot (EV, float, beta, targets, recommendation) | yfinance `info` | – | `yf_snapshot` |
| Analyst estimates (eps, revenue, eps_trend, eps_revisions, growth, price targets, recommendations) | yfinance | – | `analyst_estimate` |
| Earnings dates, EPS surprise | yfinance `get_earnings_dates(limit=100)` | – | `earnings_event` |
| Insider transactions | yfinance | – | `insider_transaction` |
| Document links | screener | – | `document_link` |

**yfinance rules** (`screener_scraper/yf.py`, CLI `yf [--symbols ..] [--universe TOP100_MCAP]`):
* Ticker = `<nse_symbol>.NS`, else `<bse_code>.BO`. Read the company list from `equity.company` / `equity.universe`.
* Statements: `income_stmt`, `quarterly_income_stmt`, `balance_sheet`, `quarterly_balance_sheet`, `cashflow`,
  `quarterly_cashflow` go to sections `yf-income` / `yf-balance` / `yf-cashflow`, `metric.source='yfinance'`, basis `C`,
  period_type `A` or `Q`, period_end = column date. Units: names containing `EPS` are `rs`; `Shares`, `Share Issued` or
  `Ordinary Shares Number` are `count` (raw); `Tax Rate` is `x`; everything else is ₹ **crore** (raw ÷ 1e7). Skip NaN.
* Prices go to the existing `equity.historical_prices(symbol, date, open, high, low, close, adj_close, volume)`
  with the same row rules as `ingestion/ingest_yfinance.py` (skip volume=0 rows). Reuse its `build_rows`/upsert
  if importable, otherwise mirror them.
* `info`: drop `companyOfficers`/`executiveTeam` from the jsonb, keep everything else, and map typed columns.
* Each company gets a `scrape_task` per dataset (`yf:prices`, `yf:statements`, `yf:actions`, ...). One dataset failing
  never blocks the others. Space requests ≥1s apart and retry on rate-limit errors with backoff.
* **Fallback:** after yfinance, if a company has 0 rows in `corporate_action` from yfinance, try NSE corporate
  actions (warm up cookies by GETting https://www.nseindia.com first, then
  `/api/corporates-corporateActions?index=equities&symbol=<SYM>`). Parse dividend ₹/share and split/bonus from the
  `subject` text and record `source='nse'`.
* `coverage` CLI command: print `equity.v_coverage` and flag gaps (no daily prices, no screener facts, etc.).

## 8. Overnight control plane: job queue, workers, rule engine (no human in the loop)

Storage decision (free tier, 500 MB shared with `mf`): **numbers in Postgres, document text in local compressed
files** (`data/documents/<SYMBOL>/<doc_type>/<label>_<sha12>.jsonl.gz`, one JSON line `{"page": n, "text": ...}` per page).
`equity.document` catalogs every file. `equity.document_page` exists but stays empty until an upgrade, when
`load-documents` bulk-loads the files and flips `text_location` to `db`.

### Queue
`equity.job`: one row per (batch, kind, company). kinds are `screener`, `yf`, `docs_ar` and `docs_concall`. `enqueue --batch <date>`
creates one job per kind for every company in the latest `TOP100_MCAP` universe, with priority = rank.
Workers claim with:
```sql
update equity.job set status='running', locked_by=%s, heartbeat_at=now(), attempts=attempts+1, updated_at=now()
where job_id = (select job_id from equity.job
                where kind = any(%s) and status in ('pending','retry') and next_run_at <= now()
                  and route in ('any', %s)            -- %s = worker class: 'local' | 'gh'
                order by priority, job_id for update skip locked limit 1)
returning *;
```
Any number of workers on any machine can share the queue safely. A heartbeat thread updates `heartbeat_at` every 30s.
Every worker checks `equity.control.paused` before each claim.

### Workers (`python -m screener_scraper worker --kinds k1,k2 --class local|gh --slots N --max-runtime MIN`)
| kind | where | concurrency |
|---|---|---|
| screener | local (1 slot, the IP-polite limit) + GitHub Actions matrix (5 runners, separate IPs) | 1 per IP |
| yf | local, 1 slot | 1 |
| docs_ar, docs_concall | local, 6 slots (CPU-bound PDF extraction, PyMuPDF) | 6 |

Each job calls the existing per-company function (`pipeline.scrape_company`, `yf.run_company`,
`documents.run_company(kind)`), which already writes `scrape_task` rows. A job is `done` when its function returns.

### Documents job
For each `document_link` of the matching doc_type (annual_report for docs_ar; concall_transcript for docs_concall):
upsert an `equity.document` row, skip it if already `ok` with the same url, download the PDF (BSE first; NSE archive url as an
alternate; `.zip` → first PDF inside), extract per-page text with PyMuPDF (fallback pypdf), write the jsonl.gz, set
status/page_count/char_count/sha/bytes. PDFs are not kept. Derive fiscal_year from the label ('Annual Report 2026' → 2026;
for concalls the call month → FY: Apr–Dec Y → FY Y+1, Jan–Mar Y → FY Y). Requests to bseindia.com need a browser UA and a
Referer `https://www.bseindia.com/`.

### Rule engine (`screener_scraper/rules.py`): error → class → action
Every exception is classified, and the class decides the action. Each failure appends `{at, worker, error_class, action}` to `job.history`.

| # | class | detection | action |
|---|---|---|---|
| R1 | rate_limited | HTTP 429 | this worker doubles its interval (cap 10s); retry in 10 min × attempt |
| R2 | blocked | 403, captcha or "access denied" HTML, Cloudflare page | on a gh worker: `route='local'`, retry now (another IP takes it); on local: retry in 30 min, and after 2 blocks set `control.screener_min_interval` to 4s |
| R3 | network | timeout, connection reset, DNS | retry in 2 min × 2^attempt |
| R4 | server | 5xx | retry in 5 min × attempt |
| R5 | not_found | 404 on a company page | dead (logged); a 404 on a document marks only that doc `missing` and tries the alternate url first, and the job continues |
| R6 | parse | ParseError / KeyError / AssertionError in parse | retry once with a fresh fetch (force, bypass cache); then dead with the raw path kept for diagnosis |
| R7 | db_transient | OperationalError, connection lost | reconnect; retry in 1 min |
| R8 | db_full | SQLSTATE 53100, "read-only", or DB size ≥ `control.storage_limit_mb` | set `control.paused=true` with a reason. All workers stop claiming. The morning report flags it |
| R9 | pdf_no_text | encrypted, scanned, zero text | that document is `no_text` (OCR later); the job continues |
| R10 | stale | `heartbeat_at` older than 10 min while running | the supervisor resets the job to `retry` (worker died) |
| R11 | unknown | anything else | retry in 15 min × attempt; dead after `max_attempts` |
A job past `max_attempts` becomes `dead` and is never retried within the batch.

### Supervisor (`python -m screener_scraper supervise --batch <date> --local-workers ... --dispatch-gh`)
Runs under `caffeinate -dimsu` with nohup, so it survives a closed terminal and keeps the Mac awake. Every 60s it:
1. applies R10 (reclaims stale jobs);
2. keeps the local worker subprocesses alive, restarting any that exited before their max-runtime (each restart is logged);
3. storage guard: checks `pg_database_size`, and applies R8 above the limit;
4. on the first tick, and again whenever runnable `screener` jobs are left with no gh runner active, dispatches the GitHub workflow
   `equity-workers.yml` (`gh workflow run`), at most 3 dispatches per batch;
5. ends when no jobs are pending, retry or running. It then runs the `coverage` check, enqueues one more `retry` round for gap
   companies (bounded to once), and writes `reports/<batch>.md`:
   - totals per kind and status
   - dead jobs with their error class and last error
   - documents by status
   - DB size
   - per-company coverage gaps
   It also stores the summary in `scrape_run.stats`.

The GitHub workflow `equity-workers.yml` (workflow_dispatch, input `runners` default 5) runs a matrix of
`worker --kinds screener --class gh --slots 1 --max-runtime 110` with secret `DATABASE_URL`.

## 9. Equity Console: inspection UI and document library

An internal, local-only app for browsing every security and document, and inspecting data quality.
**One command:** `.venv/bin/python -m console` builds or serves the app on http://127.0.0.1:8787. It binds to localhost only and
is **read-only**: it never writes to `equity.*`.
The Ops page loads each section independently (`_section` in `console/api/app.py`): a failed query yields an `errors` entry and
the last good value, not a 500. It shows storage headroom and the overflow state (`control.paused`, lake `top100_primary`),
fleet pause and per-worker log silence, rank bands past 1000, lake data-quality (`dq_daily`), and `v_source_coverage`, which is
slow and so is computed only by the warm loop (every ~15 ticks, 180 s timeout) and served from memory.

### Library (`library/`, ported from rebirth-assistant `backend/app/docs/`, same author)
* `library/chunk.py`: rebirth's `chunk.py` ported as is. Chunks are an exact partition of the page text, cut at ranked break
  points (heading > paragraph > sentence > item > line), and each carries its heading. Target is about 1800 chars.
  Page boundaries are kept so every chunk has `page_from` and `page_to`.
* `library/index.py`: builds **`data/library.sqlite`**, an FTS5 index over every `equity.document` with status ok, read from
  `data/documents/**.jsonl.gz`. It is incremental, keyed by text_path and sha. The free-tier DB holds no text, so the index is local.
  Tables: `doc(id, company_id, symbol, name, doc_type, fiscal_year, period_label, doc_date, url, text_path, pages, chars)`,
  `chunk(id, doc_id, ord, page_from, page_to, heading, text)`, and `chunk_fts` (fts5 over heading and text,
  `tokenize='porter unicode61'`, weighted with bm25: heading 3×, text 1×).
* `library/search.py`: `search(q, symbols?, doc_types?, fy_from?, fy_to?, limit, offset)`. It follows rebirth's semantics: a strict all-terms
  match first (FTS5 implicit AND, phrase support via quotes), topped up from a relaxed any-term (OR) query. Results carry a
  `snippet()` highlight, a doc citation (symbol · type · period · p.N) and a score. When the DB is upgraded, `search_pg` can switch to
  rebirth's Postgres `tsv` / `websearch_to_tsquery` / `ts_headline` path without changing the API.

### API (`console/api/`, FastAPI, psycopg2 read-only, parameterised SQL only)
`/api/securities` (universe list plus snapshot metrics and coverage), `/api/securities/{sym}` (profile, snapshot, about, pros/cons),
`/financials?basis=C|S&source=screener|yfinance&section=` (pivot: rows are line items with children nested, columns are periods),
`/prices?range=1y|5y|max` (downsampled to ≤1500 points), `/valuation`, `/shareholding` (categories plus top holders),
`/actions`, `/estimates`, `/documents` (catalog with status and fetch_source), `/quality` (v_coverage row, scrape_task trail, missing docs
with attempts_log), `/api/documents/{key}` (meta plus pages, paged), `/api/library/search`, `/api/ops` (runs, jobs by status,
DB size, document completeness).

### Web (`console/web/`, Next.js 16 + React 19 + Tailwind 4, the same stack and design tokens as rebirth-assistant)
Static export (`output: 'export'`) served by FastAPI, so one process serves everything. There is no chart library: charts are hand-rolled SVG.
* **Securities**: a dense, sortable, filterable table of the top 100. Columns: rank, symbol, name, sector, mcap, price, P/E, ROE,
  ROCE, 1Y %, plus coverage pills (screener, yf, AR n, concall n). Instant client-side search.
* **Company** (`/s/[symbol]`), with tabs:
  * Overview: headline ratios, price chart with range toggle, about, pros/cons.
  * Financials: statement tables with a sticky first column, C/S and screener/yfinance toggles, expandable breakdowns, units
    shown, negative values in red, a CSV download button.
  * Valuation: PE/PBV/EV history against the median.
  * Shareholding: stacked trend plus holders.
  * Actions & estimates.
  * Documents: grouped by FY; status badge, pages, source, open reader.
  * Data quality: coverage, task trail, missing docs with every fallback attempted.
* **Reader** (`/doc/[key]`): page-by-page text, a page jump, find-in-document with highlights, the original-URL link, metadata, and
  a "cite" copy button.
* **Library** (`/library`): full-text search across every annual report and concall, with facets (company, doc type, FY range) and
  snippets with highlights. Clicking a result opens the reader at that page with the terms highlighted.
* **Ops** (`/ops`): pipeline runs, job queue by kind and status, failures by error class, DB size against the 500 MB cap, and document completeness.
* Keyboard: `/` focuses search, `g s` goes to securities, `g l` goes to the library. Light and dark themes. Number formatting uses Indian
  grouping (₹ 1,23,456 Cr).

## 12. Public sources in the local lake (second wave)
Loaded by `python -m screener_scraper sources` into the LAKE only (never Supabase), one module per source in
`screener_scraper/sources/`, migrations `sql/090-106`, daily via `refresh.sh` (`sources --daily`, gated by the
`equity.control` row `source:<name>`). Plain static or officially documented GETs only; a 401/403/429/bot-check is final
for the run (`blocked_on` marker). Every table carries `source` and `fetched_at`.

| source | tables | notes |
|---|---|---|
| NSE/BSE/F&O bhavcopies, bulk/block | `nse_bhav_full`, `bse_bhav`, `fo_bhav`, `bulk_block_deal` | static archive files |
| NSE old-format + UDiFF CM bhavcopies | `listing_history`, `universe_pit` | survivorship-bias fix; `universe --pit-date D --top N` |
| corporate-action adjustment | `adj_factor`, view `v_prices_adjusted` | split/bonus/dividend factors; dq check vs yfinance adj_close (0.5%) |
| NSDL FPI Monitor | `macro.fpi_flow_daily`, `macro.fpi_sector_fortnightly` | daily history accrues from the Latest page |
| MoSPI eSankhyiki | `macro.series_monthly` | CPI base 2012 and 2024 are separate codes, not spliced |
| AMC monthly portfolios | `equity.mf_holding` | tolerant header-matching parser; most AMC pages are JS-rendered or block bots |
| yfinance / World Bank | `macro.series_daily`, `macro.series_annual` | RBI DBIE has no static file URL; rbi.org.in answers bots with 418 |

The undocumented `nseindia.com/api` corporate-actions fallback (cookie warming) was removed from `yf.py` (log line
`nse_fallback_removed`); corporate actions come from yfinance only.

**Future, licensed item: XBRL reconciliation.** Reconciling Screener's standardised fundamentals to the exchange XBRL
filings is skipped: there is no free bulk XBRL feed. It needs a licensed NSE/BSE corporate-data feed (or CMIE/C-MOTS)
if the use ever turns commercial.

## 13. Completeness ledger and source chains (`screener_scraper/completeness.py`, migrations `sql/108-109`)
Goal: every expected item for every ranked company (all of `TOP1000_MCAP`, 2,618 companies; growth frozen by
`rank-repair`) is either present or absent **with a reason**. `python -m screener_scraper gaps` (lake only, never
Supabase) runs the zero-network chain steps, then rebuilds the ledger from evidence already in the lake (~35 s).
`refresh.sh` runs it once a day after 20:00 (`gaps --daily`).

**Expectation rules** (pure functions, `tests/test_completeness.py`). The listing date comes from the exchange
archives' first sightings by ISIN: a sighting in NSE's first file (Jan 2010) means "listed before 2010"; one in BSE's
first file (Dec 2016) only bounds it, so an earlier NSE date wins and a later one is a move from BSE (INDTONER).
Daily prices use the NSE listing date (the series is NSE's). Items outside the expectation are not counted.

| dataset | expected items | absent-item reasons |
|---|---|---|
| annual_report | FY2013 (start of the screener/BSE links) .. latest FY whose AGM deadline + 1 month has passed, from the first FY filed as a listed company (listed before that FY's AGM deadline) | listed_recently, suspended, queued, dead_link, download_failed, scanned_no_text, no_link, exhausted |
| concall_transcript | quarters from the first call on record (calls are voluntary) to the last quarter whose results + 15 days are due | no_concalls, deck_only (a deck alone does not prove a call), call_held_no_transcript (recording or notes on record), no_call_that_quarter, queued, the document reasons |
| concall_ppt | every linked deck | document reasons |
| fin_quarterly / fin_annual / shareholding | screener's window (13 Q / 12 FY / 12 Q) ending at the later of the last due period (LODR 33: 45 days, 60 for the year; LODR 31: 21 days) and the newest one published; SME boards: half-years only | listed_recently, suspended, missing_in_source (present in yfinance counts as present) |
| prices | NSE sessions (calendar = bhavcopy days) from the NSE listing or 2010 to the last NSE session | no_trade (exchange record shows no trade after the fill), suspended |
| dividends | fiscal years with screener Dividend Payout % > 0: an ex-date between the year's start and 9 months after its end | no_dividends, no_record, exhausted (yfinance and the NSE archive both read) |
| shares_outstanding, bse_code | one per company | exhausted, not_on_bse, fillable |

**Source chains** (`CHAINS`; `:local` steps run inside `gaps`, set-based SQL):
* prices: yfinance → NSE bhavcopy (local; sessions mapped to the current symbol through ISIN renames). The basis is
  measured, not assumed: yfinance close / adj_close over the raw exchange close on the nearest overlapping session
  (≤ 45 days) before and after, carried across any factor in between; no overlap → `adj_factor` alone. A filled row
  still stepping > 40% against a yfinance neighbour (a demerger or consolidation the factors don't know) is deleted and
  kept in `equity.price_fill_reject` (ledger reason `basis_conflict`). Rows carry `historical_prices.source =
  'nse_bhavcopy:fill'`; only those are ever updated by the fill (rule 7), and a trigger (sql/113) hands a filled row
  to any other writer that updates it. 8 symbol chunks, one transaction each; full pass weekly (~4 min), else 14 days.
  Audit 2026-11-04: steps > 40% at fill boundaries fell from 320 (73 symbols, factor-only basis) to 5.
* dividends / splits / bonuses / rights: yfinance → NSE PR archive (`sources --only nse_pr`: `Bc*.csv` purposes parsed by
  `parse_purpose` into `corporate_action` source `nse_archive`; an event within 3 days of one another source already has
  is skipped) → `adj_factor` recomputed (NSE dividends pair with the raw NSE close).
* shares outstanding: yfinance → NSE PR archive issue size (`mcap*.csv`, `equity.nse_issue_size` change log) → screener
  Equity Capital / Face Value.
* BSE code: screener → BSE bhavcopy ISIN.
* documents: screener link → original → retry → Wayback → Common Crawl → company site → OCR / capped audio (unchanged),
  plus ledger-driven company-site discovery (`fallback.discover_ledger`, run by the `concalls` fleet role, one crawl per
  company): annual reports no source links and calls on record without a transcript. Each search appends to
  `data_gap.attempts` (7/14/28-day backoff); after 3 fruitless searches the gap is `exhausted`. A site that answers
  403 or a bot check is recorded as refusing automated access (final, per the ground rules).

**Third-party document sources (checked 2026-11-04 by research agents; evidence in the session scratchpad).**
* **Tijori Finance** (`screener_scraper/tijori.py`, `python -m screener_scraper tijori [--limit N]`, also run per company
  by the `concalls` role before the company-site search): robots.txt allows all; its Terms allow copying/storing for
  personal, non-commercial use (no redistribution). Company pages are server-rendered and link company-published PDFs
  on `files.tijorifinance.com` (an S3-style host whose robots.txt 403 is read as "no restrictions", RFC 9309
  2.3.1.3). A company maps to a page only by an exact name -> slug match (current or former name, NSE
  `namechange.csv`); a file's quarter comes from its name (quarter-end month, sometimes the call month) and every PDF
  must prove itself from its text before it is stored (`fetch_source='tijori'`): the company's name phrase in the
  title block with no group company ahead of it (ICICI Prudential's call sat on ICICI Bank's page), the document kind,
  and the quarter or fiscal year. One page GET per company; PDFs only for open gaps; 2 s per host; retried monthly.
  Segment / KPI history is premium (paywall) and not requested.
* **Trendlyne: not used.** Its Terms (s6) forbid storing its information "in a searchable, machine-readable database"
  without written permission, its document links sit behind a robots-disallowed `/get-document/` proxy, and its
  transcript history starts in 2022. Its dividend tables would match the BSE-era gaps, which the yfinance `.BO` step
  covers instead.
* **NSE rename files** (`sources --only nse_changes`): `content/equities/symbolchange.csv` and `namechange.csv` ->
  `equity.nse_symbol_change`, `equity.nse_name_change` (a symbol change can be a merger with a new ISIN: confirm by ISIN).

**Tables.** `equity.data_gap` (company, dataset, period; state open|explained, reason, detail, next_source, attempts,
first_seen), `equity.completeness` (company x dataset: expected, present, explained, open), `equity.completeness_daily`
(universe totals + reason counts per day). Console: Ops → "Dataset completeness"; Company → Data quality → "Completeness"
(every absent item with its reason, next source and evidence); Securities → "Complete" column (mean over datasets of
(present + explained) / expected). API: `/api/completeness`, `/api/securities/{sym}/gaps`.

**Ranks.** `rank-repair` (idempotent) froze growth (`lake_state.growth_frozen`) and re-ranked everything past 1000 by
latest market cap: growth had handed out ranks 1001-1799 twice (2,618 companies, 1,915 distinct ranks).
