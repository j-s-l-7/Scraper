# CLAUDE.md

Repo `j-s-l-7/Scraper` (formerly Quant-God). Local path: `~/Scraper - Screener.in` (quote it, it contains spaces).

## What this repo is
Scraping, fetching and storing Indian equity data for the **top 100 companies by market cap** (plus Nifty 50)
into Supabase Postgres, schema **`equity`**. The same Supabase project also has a separate `mf` (mutual fund)
schema that belongs to another app. **Never read, write, or join to `mf` from here.**

### Public sources (local lake only, `screener_scraper/sources/`, migrations `sql/090-097`)
`python -m screener_scraper sources --only X [--backfill-from YYYY-MM-DD]` (X: nse_bhav_full, bse_bhav, bulk_block,
fo_bhav, credit_ratings, macro, gdelt_news); `refresh.sh` runs `sources --daily`, gated by `equity.control` key
`source:<name>`. Plain static/official GETs only, >=1s per host, a 401/403/429 or bot-check is final for the run
(`blocked_on` marker). Coverage: `equity.v_source_coverage`.

Full design: **ARCHITECTURE.md**. Project skills in `.claude/skills/`: `scrape-screener` (operations and
triage), `equity-db` (querying and storing conventions), `ingest-prices`, `ingest-concalls`.

## Storage constraint
Supabase **Free tier: 500 MB shared with `mf`**. Numbers go in Postgres. Annual report and concall **text lives in
`data/documents/**.jsonl.gz`** (gitignored, one JSON line per page), and `equity.document` catalogs each file. After
an upgrade, `python -m screener_scraper load-documents` moves the text into `equity.document_page`. The rule engine
pauses all writers at `equity.control.storage_limit_mb` (470).

## Commands
```bash
.venv/bin/python -m screener_scraper init-db           # apply sql/*.sql (idempotent, numbered)
.venv/bin/python -m screener_scraper universe --top 100
scripts/overnight.sh                                    # full unattended refresh (queue + supervisor + GH workers)
.venv/bin/python -m screener_scraper coverage | status | report
python3 -m unittest discover tests
python3 scripts/validate_system.py                      # price-table preflight
```

## Rules
1. Every table has a natural PK, and every write is `INSERT … ON CONFLICT DO UPDATE`. Re-runs must never duplicate rows.
2. Schema changes go in a new numbered idempotent `sql/NNN_*.sql`. Never edit an applied migration.
3. Line items are rows in `equity.metric` (source × section × name × parent), never new columns.
4. Amounts are in ₹ crore, EPS in ₹, percentages as percent numbers. Missing data is an absent row, not a NULL.
5. Politeness: screener ≥1.5s plus jitter per IP, yfinance ≥1s, BSE ≥0.5s per slot. Scale out across IPs, not by speeding up.
6. Parser changes need a fixture test in `tests/fixtures/` and a `reparse` from the raw cache. Don't re-scrape.
7. `historical_prices` PK `(symbol, date)`: yfinance owns `adj_close`; run bhavcopy with `--keep-adj-close` after yfinance.
