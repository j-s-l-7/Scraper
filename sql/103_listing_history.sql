-- Survivorship-bias fix: every symbol/series ever seen in NSE bhavcopies (old format 2010+, UDiFF 2024+), and
-- point-in-time universe membership. Idempotent.
create table if not exists equity.listing_history (
    symbol         text not null,
    isin           text not null default '',
    series         text not null,
    first_seen     date not null,
    last_seen      date not null,
    is_listed_now  boolean not null default false,
    source         text not null default 'nse_cm_bhav',
    fetched_at     timestamptz not null default now(),
    primary key (symbol, isin, series)
);
create index if not exists listing_history_seen_idx on equity.listing_history (last_seen);
create table if not exists equity.universe_pit (
    universe_code  text not null,             -- e.g. PIT_TOP500_TURNOVER
    as_of          date not null,
    rank           int  not null,
    symbol         text not null,
    metric         numeric,                   -- trailing average daily turnover (Rs lakh)
    source         text not null default 'nse_bhav_full',
    fetched_at     timestamptz not null default now(),
    primary key (universe_code, as_of, symbol)
);
alter table equity.listing_history enable row level security;
alter table equity.universe_pit enable row level security;
