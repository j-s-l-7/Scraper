-- AMC monthly portfolio disclosures, ISIN level (equity schema in the lake only; never the Supabase `mf` schema). Idempotent.
create table if not exists equity.mf_holding (
    month         date not null,              -- month end of the disclosure
    amc           text not null,
    scheme        text not null,
    isin          text not null,
    company_id    int,                        -- via company.isin when known
    instrument    text,
    qty           numeric,
    market_value  numeric,                    -- Rs crore (converted from lakh when the sheet says so)
    pct_nav       numeric,
    source        text not null default 'amc_portfolio',
    fetched_at    timestamptz not null default now(),
    primary key (month, amc, scheme, isin)
);
create index if not exists mf_holding_isin_idx on equity.mf_holding (isin, month);
alter table equity.mf_holding enable row level security;
