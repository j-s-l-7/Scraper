---
name: equity-db
description: Query, store into, or extend the Supabase `equity` schema (Indian stock data from screener.in, yfinance, NSE/BSE). Use when fetching numbers for analysis, writing SQL against equity.*, adding a table/source, or checking data coverage. Never touches the separate `mf` (mutual fund) schema.
---

# equity schema: fetching & storing

Connection: `DATABASE_URL` in `.env` (Supabase session pooler, port 5432). Everything stock-related lives in
schema **`equity`**. The `mf` schema in the same project belongs to the mutual-fund platform: do not read it,
write it, or join to it from this repo.

Ranks 101+ live in the local lake (`postgresql://localhost:5433/equity_lake`, same `equity` layout). Once Supabase is
full (`equity.control` `paused` with reason `db_full`, or lake `public.lake_state` key `top100_primary` set), the lake
is primary for the top 100 too: query the lake for top-100 data.

## Map
| need | table / view | key |
|---|---|---|
| company master, sector, ids | `company` | company_id (screener id); nse_symbol, bse_code, isin unique |
| top-N membership by date | `universe` | (universe_code, as_of, company_id) |
| any statement number | `financial_fact` ⨝ `metric`, or the view **`v_financials`** | (company_id, basis C/S, metric_id, period_type Q/A/TTM/H, period_end) |
| headline ratios over time | `market_snapshot`, `v_latest_snapshot` | (company_id, as_of) |
| CAGR boxes | `growth_summary` | measure × horizon |
| shareholding | `shareholding`, `shareholding_holder` ⨝ `holder` | frequency Q/Y |
| daily OHLCV | `historical_prices` | (symbol, date): joins on `company.nse_symbol` |
| weekly price + DMA + delivery % | `price_history` | frequency W/D |
| PE / PBV / EV-EBITDA / margin history | `valuation_series` | series slug |
| dividends / splits | `corporate_action` | source yfinance or nse |
| shares outstanding | `shares_outstanding` | |
| yfinance profile, targets | `yf_snapshot` (typed cols + `info` jsonb) | |
| analyst estimates | `analyst_estimate` (long) | estimate_type × period × field |
| earnings surprises | `earnings_event` | |
| annual report / concall links | `document_link` | links only |
| concall transcripts | `concalls` | (symbol, fiscal_year, quarter) |
| per-company source coverage | **`v_coverage`** | |
| what is missing and why (lake) | **`data_gap`** (reason, state open/explained, next_source, attempts), `completeness`, `completeness_daily` | (company_id, dataset, period) |
| NSE PR archive (lake) | `nse_corp_action` (raw purposes), `nse_issue_size` (issue size change log) | |
| run audit trail | `scrape_run`, `scrape_task` | |

## Conventions
* Amounts are in **₹ crore** for both screener and yfinance (yfinance raw ÷ 1e7). EPS is in ₹. Percentages are stored as
  percent numbers (15 means 15%). Check `metric.unit` (`cr`, `pct`, `rs`, `days`, `count`, `x`).
* `basis`: C = consolidated, S = standalone. Prefer C when `company.has_consolidated`.
* Line items are rows, not columns: banks use `Revenue` / `Financing Profit`, everyone else uses `Sales` /
  `Operating Profit`. Filter on `metric.source` (screener or yfinance) and `metric.section`.
* Breakdown children have `parent_metric_id` set (e.g. `Expenses › Material Cost %`).
* Missing values are absent rows, never NULLs.

## Recipes
```sql
-- Annual sales, 12 years, one company (consolidated)
select period_end, value from equity.v_financials
where nse_symbol='TCS' and basis='C' and section='profit-loss' and line_item='Sales' and period_type='A'
order by period_end;

-- Cross-section: latest FY OPM % for the top-100 universe
select c.nse_symbol, f.period_end, f.value
from equity.financial_fact f join equity.metric m using (metric_id) join equity.company c using (company_id)
where m.source='screener' and m.section='profit-loss' and m.name='OPM %' and f.basis='C' and f.period_type='A'
  and f.period_end = (select max(period_end) from equity.financial_fact f2
                      where f2.company_id=f.company_id and f2.metric_id=f.metric_id and f2.period_type='A');

-- Wide pivot in Python
--   pd.read_sql(query, conn).pivot_table(index='period_end', columns='line_item', values='value')
```

## Storing / extending
1. Add a numbered, idempotent migration in `sql/NNN_*.sql` (`create ... if not exists`, enable RLS). Apply it with
   `python -m screener_scraper init-db`.
2. Every table gets a natural primary key and writes use `INSERT ... ON CONFLICT DO UPDATE`, so re-runs never
   duplicate rows.
3. Point-in-time data (snapshots, estimates) carries `as_of`, so history accrues. Restated history overwrites.
4. Every write path records a `scrape_run` and one `scrape_task` per company × dataset, with failures and their error text.
5. A new statement source goes into `metric` with its own `source` and section values, not new fact tables.
