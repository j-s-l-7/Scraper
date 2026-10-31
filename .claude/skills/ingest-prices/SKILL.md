---
name: ingest-prices
description: Load or repair daily OHLCV bars for NSE stocks in equity.historical_prices (NSE bhavcopy primary, yfinance fallback + adj_close). Use for backfills, gap-fills, a failed daily GitHub Action, adding symbols, or questions about price data freshness.
---

# Daily OHLCV ingestion

Two ingesters write the same table, `equity.historical_prices`, with PK `(symbol, date)`. Both upsert, so re-runs are safe.

| source | script | owns |
|---|---|---|
| NSE bhavcopy (primary) | `ingestion/backfill_from_nse.py` | traded open/high/low/close/volume |
| yfinance (fallback) | `ingestion/ingest_yfinance.py` | `adj_close` (split/dividend adjusted), deep history before 2024-07-08 |

## Commands

```bash
python3 scripts/validate_system.py                                     # preflight: env, table, PK, universe
python3 ingestion/ingest_yfinance.py --nifty50 --period 5d             # recent window, all Nifty 50
python3 ingestion/ingest_yfinance.py --symbols RELIANCE,TCS --period max
python3 ingestion/backfill_from_nse.py --recent 7 --keep-adj-close     # what the daily cron runs
python3 ingestion/backfill_from_nse.py --start 2026-10-01 --end 2026-10-31 --keep-adj-close
python3 ingestion/backfill_from_nse.py --date 2026-10-30 --all         # every EQ/BE security that day
```

## Rules
1. Order matters: run yfinance first, then bhavcopy with `--keep-adj-close`. A bhavcopy run without the
   flag sets `adj_close = close` and overwrites the adjusted value.
2. The bhavcopy only exists from 2024-07-08 (UDiFF format). Use yfinance for older history. A 404 means a holiday, not a failure.
3. Rows with `volume = 0` are yfinance holiday phantoms and are filtered out. Afterwards, check that
   `select count(*) from equity.historical_prices where volume = 0` is still 0.
4. The universe is `data/nse_indices.json` and nothing else. Edit that file after an NSE rebalance.
5. Tickers are passed without `.NS`. The yfinance helper adds the suffix.

## Automation
`.github/workflows/daily-prices.yml` runs Mon–Fri at 17:00 IST and needs the repo secret `DATABASE_URL`.
To debug a red run: `gh run list -w daily-prices.yml -L 5` then `gh run view <id> --log-failed`.

## Verify
```sql
select symbol, max(date), count(*) from equity.historical_prices group by 1 order by 2 limit 10;  -- stale symbols first
```
