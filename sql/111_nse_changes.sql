-- 111: NSE symbol and company-name change files (nsearchives content/equities/symbolchange.csv, namechange.csv).
-- Former names let third-party pages (Tijori slugs) and documents of the old name match the company. Idempotent.
create table if not exists equity.nse_symbol_change (
    old_symbol     text not null,
    new_symbol     text not null,
    effective_date date not null,
    company_name   text,
    source         text not null default 'nsearchives:symbolchange',
    fetched_at     timestamptz not null default now(),
    loaded_at      timestamptz not null default now(),
    primary key (old_symbol, new_symbol, effective_date)
);
create table if not exists equity.nse_name_change (
    symbol         text not null,
    old_name       text not null,
    new_name       text not null,
    effective_date date not null,
    source         text not null default 'nsearchives:namechange',
    fetched_at     timestamptz not null default now(),
    loaded_at      timestamptz not null default now(),
    primary key (symbol, effective_date, old_name)
);
alter table equity.nse_symbol_change enable row level security;
alter table equity.nse_name_change enable row level security;
