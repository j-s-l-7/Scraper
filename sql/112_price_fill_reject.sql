-- 112: bhavcopy sessions the price fill refused to merge (its row stepped > 40% against a yfinance neighbour: a
-- basis the factors can't reconcile, e.g. a demerger yfinance adjusts for). The ledger labels them basis_conflict.
-- Rebuilt by every full fill pass. Idempotent.
create table if not exists equity.price_fill_reject (
    symbol     text    not null,
    date       date    not null,
    ratio      numeric,
    at         timestamptz not null default now(),
    primary key (symbol, date)
);
alter table equity.price_fill_reject enable row level security;
