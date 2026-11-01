-- NSE F&O UDiFF bhavcopy: stock and index futures, plus one summary row per (symbol, expiry, CE/PE) for options
-- (strike = 0, contracts and oi summed over strikes, prices null). Idempotent.
create table if not exists equity.fo_bhav (
    date         date    not null,
    instrument   text    not null,          -- STF, IDF (futures), STO, IDO (options summary)
    symbol       text    not null,
    expiry       date    not null,
    strike       numeric not null default 0,
    option_type  text    not null default '', -- '', CE, PE
    open         numeric,
    high         numeric,
    low          numeric,
    close        numeric,
    settle       numeric,
    contracts    bigint,
    oi           bigint,
    chg_oi       bigint,
    loaded_at    timestamptz not null default now(),
    primary key (date, instrument, symbol, expiry, strike, option_type)
);
create index if not exists fo_bhav_symbol_idx on equity.fo_bhav (symbol, date);
alter table equity.fo_bhav enable row level security;
