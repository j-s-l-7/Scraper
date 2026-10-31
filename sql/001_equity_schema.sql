-- Equity data warehouse (screener.in numerical data, top-N Indian equities).
-- Lives in its own schema `equity`, fully isolated from the `mf` (mutual fund) schema.
-- Idempotent: safe to re-run.

create schema if not exists equity;

-- ───────────────────────────── Audit trail ─────────────────────────────
create table if not exists equity.scrape_run (
    run_id       bigserial primary key,
    command      text        not null,            -- e.g. 'universe', 'scrape', 'reparse'
    params       jsonb       not null default '{}',
    status       text        not null default 'running'
                 check (status in ('running','success','partial','failed')),
    stats        jsonb       not null default '{}', -- counters: companies ok/failed, rows written, requests
    host         text,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz
);

create table if not exists equity.scrape_task (
    run_id        bigint      not null references equity.scrape_run on delete cascade,
    company_id    int         not null,           -- no FK: task may fail before company row exists
    task          text        not null,           -- 'page:C', 'page:S', 'schedules:C', 'shareholders', 'charts:C', ...
    status        text        not null check (status in ('ok','failed','skipped')),
    http_status   smallint,
    attempts      smallint    not null default 1,
    rows_written  int         not null default 0,
    error         text,
    raw_path      text,                            -- local raw cache file (re-parse without re-fetch)
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    primary key (run_id, company_id, task)
);
create index if not exists scrape_task_failed_idx on equity.scrape_task (status) where status = 'failed';

-- ───────────────────────────── Dimensions ─────────────────────────────
create table if not exists equity.company (
    company_id       int primary key,              -- screener data-company-id (stable internal id)
    warehouse_id     int unique,                   -- screener data-warehouse-id (used by some APIs)
    screener_slug    text not null unique,         -- /company/<slug>/ (NSE symbol or BSE code)
    name             text not null,
    nse_symbol       text unique,
    bse_code         text unique,
    isin             text unique,
    sector           text,                         -- 4-level screener/industry classification
    industry_group   text,
    industry         text,
    sub_industry     text,
    industry_code    text,                         -- deepest code, e.g. IN030103001
    website          text,
    about            text,
    key_points       text,
    pros             text[],
    cons             text[],
    has_consolidated boolean,
    is_active        boolean not null default true,
    first_seen_at    timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);
create index if not exists company_industry_idx on equity.company (sector, industry);

-- Every line item that screener exposes, including drill-down "schedules" (parent_metric_id not null).
-- Labels differ by business model (banks: 'Revenue', 'Financing Profit'; others: 'Sales', 'Operating Profit'),
-- so metrics are rows, not columns: new line items never require a migration.
create table if not exists equity.metric (
    metric_id        smallserial primary key,
    section          text not null check (section in
                     ('quarters','profit-loss','balance-sheet','cash-flow','ratios')),
    name             text not null,                -- cleaned label: 'Sales', 'Material Cost %', 'EPS in Rs'
    parent_metric_id smallint references equity.metric,
    unit             text not null check (unit in ('cr','pct','rs','days','count','x')),
    canonical_code   text,                         -- optional harmonised code for cross-company comparison (filled later)
    display_order    smallint,
    unique nulls not distinct (section, name, parent_metric_id)
);

-- ───────────────────────────── Facts ─────────────────────────────
-- Long/narrow fact table: one value per (company, basis, line item, period).
create table if not exists equity.financial_fact (
    company_id   int      not null references equity.company on delete cascade,
    basis        char(1)  not null check (basis in ('C','S')),          -- Consolidated / Standalone
    metric_id    smallint not null references equity.metric,
    period_type  text     not null check (period_type in ('Q','A','TTM','H')),
                          -- Q quarter, A fiscal year, TTM trailing-12m (period_end = latest quarter end),
                          -- H interim/half-year balance sheet column
    period_end   date     not null,                                     -- last day of the period month
    value        numeric(24,4) not null,
    run_id       bigint   references equity.scrape_run,
    updated_at   timestamptz not null default now(),
    primary key (company_id, basis, metric_id, period_type, period_end)
);
-- cross-sectional queries: "metric X for all companies in period P"
create index if not exists financial_fact_metric_period_idx
    on equity.financial_fact (metric_id, period_type, period_end);

-- Point-in-time headline numbers (one row per company per scrape date → valuation history accrues over time).
create table if not exists equity.market_snapshot (
    company_id          int  not null references equity.company on delete cascade,
    as_of               date not null,
    market_cap_cr       numeric(20,2),
    price               numeric(14,2),
    high_52w            numeric(14,2),
    low_52w             numeric(14,2),
    pe                  numeric(12,2),
    book_value          numeric(14,2),
    dividend_yield_pct  numeric(8,2),
    roce_pct            numeric(8,2),
    roe_pct             numeric(8,2),
    face_value          numeric(10,2),
    extra               jsonb not null default '{}',   -- any other top-ratio label → value (bank-specific etc.)
    run_id              bigint references equity.scrape_run,
    primary key (company_id, as_of)
);

-- "Compounded Sales Growth / Profit Growth / Stock Price CAGR / Return on Equity" boxes.
create table if not exists equity.growth_summary (
    company_id  int     not null references equity.company on delete cascade,
    basis       char(1) not null check (basis in ('C','S')),
    as_of       date    not null,
    measure     text    not null check (measure in ('sales_growth','profit_growth','price_cagr','roe')),
    horizon     text    not null check (horizon in ('10Y','5Y','3Y','1Y','TTM','LAST')),
    value_pct   numeric(10,2),
    run_id      bigint references equity.scrape_run,
    primary key (company_id, basis, as_of, measure, horizon)
);

-- Shareholding: category level (Promoters/FIIs/DIIs/Government/Public/Others + No. of Shareholders).
create table if not exists equity.shareholding (
    company_id  int     not null references equity.company on delete cascade,
    frequency   char(1) not null check (frequency in ('Q','Y')),
    category    text    not null check (category in
                ('promoters','fii','dii','government','public','others','num_shareholders')),
    period_end  date    not null,
    value       numeric(18,2) not null,                 -- % holding, or count for num_shareholders
    run_id      bigint references equity.scrape_run,
    primary key (company_id, frequency, category, period_end)
);

create table if not exists equity.holder (
    holder_id   serial primary key,
    holder_key  text not null unique,     -- screener person url, else 'name:'||lower(name)
    name        text not null,
    person_url  text
);

create table if not exists equity.shareholding_holder (
    company_id  int     not null references equity.company on delete cascade,
    frequency   char(1) not null check (frequency in ('Q','Y')),
    category    text    not null,
    holder_id   int     not null references equity.holder,
    period_end  date    not null,
    pct         numeric(8,2) not null,
    run_id      bigint references equity.scrape_run,
    primary key (company_id, frequency, category, holder_id, period_end)
);
create index if not exists shareholding_holder_holder_idx on equity.shareholding_holder (holder_id);

-- Price/volume history from screener chart API. W = full history weekly, D = last ~1y daily.
create table if not exists equity.price_history (
    company_id    int     not null references equity.company on delete cascade,
    frequency     char(1) not null check (frequency in ('D','W')),
    trade_date    date    not null,
    close         numeric(14,2),
    dma50         numeric(14,2),
    dma200        numeric(14,2),
    volume        bigint,
    delivery_pct  numeric(6,2),
    primary key (company_id, frequency, trade_date)
);

-- Valuation / margin time series from chart API (PE, median PE, TTM EPS, P/BV, EV/EBITDA, MCap/Sales, GPM/OPM/NPM ...).
create table if not exists equity.valuation_series (
    company_id  int     not null references equity.company on delete cascade,
    basis       char(1) not null check (basis in ('C','S')),
    series      text    not null,          -- slug of screener metric: 'pe','median_pe','eps','pbv','ev_multiple','opm',...
    obs_date    date    not null,
    value       numeric(20,4) not null,
    primary key (company_id, basis, series, obs_date)
);

-- Pointers only (no download): annual reports / concall docs live on BSE/NSE and are fetched by a later pipeline.
create table if not exists equity.document_link (
    company_id   int  not null references equity.company on delete cascade,
    doc_type     text not null check (doc_type in
                 ('annual_report','concall_transcript','concall_ppt','concall_recording','concall_notes',
                  'announcement','credit_rating','other')),
    url          text not null,
    title        text,
    period_label text,                    -- 'Annual Report 2026', 'Oct 2026'
    source       text,                    -- 'bse','nse','other'
    first_seen_at timestamptz not null default now(),
    primary key (company_id, doc_type, url)
);

-- Universe membership (top-100 by mcap today; add NIFTY500 etc. later without schema change).
create table if not exists equity.universe (
    universe_code  text not null,         -- 'TOP100_MCAP'
    as_of          date not null,
    company_id     int  not null references equity.company on delete cascade,
    rank           smallint,
    market_cap_cr  numeric(20,2),
    primary key (universe_code, as_of, company_id)
);

-- ───────────────────────────── Convenience views ─────────────────────────────
create or replace view equity.v_financials as
select c.nse_symbol, c.name, f.company_id, f.basis, m.section,
       coalesce(p.name || ' › ', '') || m.name as line_item, m.unit,
       f.period_type, f.period_end, f.value, f.updated_at
from equity.financial_fact f
join equity.company c using (company_id)
join equity.metric  m using (metric_id)
left join equity.metric p on p.metric_id = m.parent_metric_id;

create or replace view equity.v_latest_snapshot as
select distinct on (s.company_id) c.nse_symbol, c.name, c.sector, s.*
from equity.market_snapshot s join equity.company c using (company_id)
order by s.company_id, s.as_of desc;

-- Lock down from the public Data API (anon/publishable key); the pipeline connects as postgres.
do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'equity' loop
    execute format('alter table equity.%I enable row level security', t);
  end loop;
end $$;
