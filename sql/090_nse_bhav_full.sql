-- NSE full bhavcopy with delivery (nsearchives sec_bhavdata_full_DDMMYYYY.csv). Native units: turnover in lakh Rs. Idempotent.
create table if not exists equity.nse_bhav_full (
    symbol         text    not null,
    series         text    not null,
    date           date    not null,
    prev_close     numeric,
    open           numeric,
    high           numeric,
    low            numeric,
    last           numeric,
    close          numeric,
    avg_price      numeric,
    ttl_trd_qnty   bigint,
    turnover_lacs  numeric,
    no_of_trades   bigint,
    deliv_qty      bigint,
    deliv_per      numeric,
    loaded_at      timestamptz not null default now(),
    primary key (symbol, series, date)
);
create index if not exists nse_bhav_full_date_idx on equity.nse_bhav_full (date);
alter table equity.nse_bhav_full enable row level security;
