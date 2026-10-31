-- Daily OHLCV (NSE bhavcopy + yfinance) and full-verbatim concall transcripts.
-- Written by ingestion/*.py. Lives in the `equity` schema alongside the screener tables. Idempotent.

create schema if not exists equity;

create table if not exists equity.historical_prices (
    symbol     varchar(32) not null,      -- NSE ticker, no .NS suffix
    date       date        not null,
    open       numeric(14,4),
    high       numeric(14,4),
    low        numeric(14,4),
    close      numeric(14,4),
    adj_close  numeric(14,4),             -- yfinance-owned; = close when unavailable
    volume     bigint,
    primary key (symbol, date)            -- drives ON CONFLICT upsert in both ingesters; never drop
);
create index if not exists idx_historical_prices_symbol_date
    on equity.historical_prices (symbol, date desc);

create table if not exists equity.concalls (
    symbol       varchar(32) not null,
    quarter      text        not null check (quarter in ('Q1','Q2','Q3','Q4')),
    fiscal_year  text        not null,    -- 'FY2026' = Apr 2025–Mar 2026
    call_date    date,
    transcript   text        not null,    -- full verbatim text, never a summary
    url          text        not null unique,
    source       text,                    -- bse | ir_site | wayback
    word_count   int,
    page_count   int,
    pdf_sha256   text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    primary key (symbol, fiscal_year, quarter)
);

alter table equity.historical_prices enable row level security;
alter table equity.concalls          enable row level security;
