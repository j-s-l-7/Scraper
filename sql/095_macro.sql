-- Macro series (indices, FX, commodities, World Bank annual indicators). Separate schema `macro`. Idempotent.
create schema if not exists macro;
create table if not exists macro.series (
    code    text primary key,
    name    text,
    source  text not null,       -- yfinance, worldbank
    unit    text,
    freq    text not null check (freq in ('D','A'))
);
create table if not exists macro.series_daily (
    code       text not null,
    date       date not null,
    value      numeric,
    loaded_at  timestamptz not null default now(),
    primary key (code, date)
);
create table if not exists macro.series_annual (
    code       text not null,
    year       int  not null,
    value      numeric,
    loaded_at  timestamptz not null default now(),
    primary key (code, year)
);
alter table macro.series        enable row level security;
alter table macro.series_daily  enable row level security;
alter table macro.series_annual enable row level security;
