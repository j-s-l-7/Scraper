-- 108: completeness ledger + NSE PR archive (corporate actions, issue size). Lake data; Supabase gets the empty
-- tables only if init-db is run there. Idempotent.

-- NSE daily PR archive (nsearchives .../archives/equities/bhavcopy/pr/PRddmmyy.zip), Bc*.csv: book closures and
-- record dates with the announced purpose (dividend, bonus, split, rights, AGM ...). Raw purpose text kept as filed.
create table if not exists equity.nse_corp_action (
    symbol      text not null,
    ex_date     date not null,
    purpose     text not null,
    series      text,
    record_date date,
    bc_start    date,
    bc_end      date,
    source      text not null default 'nsearchives:pr/bc',
    fetched_at  timestamptz not null default now(),
    loaded_at   timestamptz not null default now(),
    primary key (symbol, ex_date, purpose)
);
alter table equity.nse_corp_action enable row level security;

-- PR archive mcap*.csv: face value and issue size (shares listed). Stored as a change log: a row only when the
-- (face_value, issue_size) pair differs from the symbol's previous row.
create table if not exists equity.nse_issue_size (
    symbol      text not null,
    date        date not null,
    series      text,
    face_value  numeric,
    issue_size  bigint,
    source      text not null default 'nsearchives:pr/mcap',
    fetched_at  timestamptz not null default now(),
    loaded_at   timestamptz not null default now(),
    primary key (symbol, date)
);
alter table equity.nse_issue_size enable row level security;

alter table equity.corporate_action drop constraint if exists corporate_action_source_check;
alter table equity.corporate_action add constraint corporate_action_source_check
    check (source in ('yfinance', 'nse', 'bse', 'nse_archive'));

-- One row per expected-but-absent item (or per empty dataset), each with the reason it is absent.
--   state 'open'      : data should exist; next_source is the next step in the dataset's source chain
--   state 'explained' : data does not exist (listed later, no trade, no call held, ...) or every source was tried
-- Items outside the expectation (pre-listing years, not yet due) are not rows: completeness counts only expected items.
create table if not exists equity.data_gap (
    company_id  integer not null references equity.company (company_id) on delete cascade,
    dataset     text    not null,
    period      text    not null default '',
    state       text    not null check (state in ('open', 'explained')),
    reason      text    not null,
    detail      jsonb   not null default '{}'::jsonb,
    next_source text,
    attempts    jsonb   not null default '[]'::jsonb,
    first_seen  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    primary key (company_id, dataset, period)
);
create index if not exists data_gap_reason_idx on equity.data_gap (dataset, state, reason);
alter table equity.data_gap enable row level security;

-- Per company x dataset: expected items, present, absent-with-reason (explained), absent-and-actionable (open).
create table if not exists equity.completeness (
    company_id  integer not null references equity.company (company_id) on delete cascade,
    dataset     text    not null,
    expected    integer not null,
    present     integer not null,
    explained   integer not null,
    open        integer not null,
    computed_at timestamptz not null default now(),
    primary key (company_id, dataset)
);
alter table equity.completeness enable row level security;

create table if not exists equity.completeness_daily (
    as_of     date not null,
    dataset   text not null,
    companies integer not null,
    expected  bigint not null,
    present   bigint not null,
    explained bigint not null,
    open      bigint not null,
    reasons   jsonb not null default '{}'::jsonb,
    primary key (as_of, dataset)
);
alter table equity.completeness_daily enable row level security;
