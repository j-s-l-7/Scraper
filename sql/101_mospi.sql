-- MoSPI eSankhyiki official API (no key): monthly CPI / IIP / WPI. Base year is stored; CPI base 2012 and base 2024 are
-- separate codes (no splice is computed: see macro.series.name). Idempotent.
alter table macro.series drop constraint if exists series_freq_check;
alter table macro.series add constraint series_freq_check check (freq in ('D','M','A'));
alter table macro.series add column if not exists base_year text;
alter table macro.series add column if not exists fetched_at timestamptz;
create table if not exists macro.series_monthly (
    code        text not null,
    period      date not null,                -- first day of the month
    value       numeric,
    base_year   text,
    source      text not null default 'mospi_esankhyiki',
    fetched_at  timestamptz not null default now(),
    primary key (code, period)
);
alter table macro.series_monthly enable row level security;
