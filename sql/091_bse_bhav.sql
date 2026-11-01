-- BSE equity bhavcopy (static download folder). Native units: turnover in Rs. Join to company via bse_code / isin. Idempotent.
create table if not exists equity.bse_bhav (
    sc_code    text    not null,
    isin       text,
    date       date    not null,
    open       numeric,
    high       numeric,
    low        numeric,
    close      numeric,
    volume     bigint,
    trades     bigint,
    turnover   numeric,
    loaded_at  timestamptz not null default now(),
    primary key (sc_code, date)
);
create index if not exists bse_bhav_date_idx on equity.bse_bhav (date);
create index if not exists bse_bhav_isin_idx on equity.bse_bhav (isin);
alter table equity.bse_bhav enable row level security;
