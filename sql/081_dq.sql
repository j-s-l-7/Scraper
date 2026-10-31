-- Data-quality ledger, daily snapshots and the scorecard. Idempotent; all rows are small.
create table if not exists equity.dq_issue (
    check_code  text not null,
    company_id  int  not null default 0,          -- 0 = global issue
    key         text not null default '',         -- detail key (e.g. a symbol or a period) to keep issues distinct
    severity    text not null check (severity in ('info','warn','error')),
    detail      jsonb not null default '{}',      -- includes history: [{at, action, ...}] of remediations
    status      text not null default 'open' check (status in ('open','resolved')),
    first_seen  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    resolved_at timestamptz,
    primary key (check_code, company_id, key)
);
create index if not exists dq_issue_open_idx on equity.dq_issue (status, check_code);

create table if not exists equity.dq_company_score (
    company_id  int primary key,
    as_of       date not null,
    coverage    real not null, freshness real not null, consistency real not null, documents real not null,
    overall     real not null
);

create table if not exists equity.dq_daily (
    as_of        date primary key,
    companies    int,
    avg_overall  real, avg_coverage real, avg_freshness real, avg_consistency real, avg_documents real,
    open_errors  int, open_warns int,
    by_check     jsonb,
    resolved     int, remediations int
);

create or replace view equity.v_quality_scorecard as
select c.nse_symbol, c.name, s.company_id, s.as_of, s.overall, s.coverage, s.freshness, s.consistency, s.documents,
       (select count(*) from equity.dq_issue i where i.company_id = s.company_id and i.status = 'open'
          and i.severity = 'error') as open_errors,
       (select count(*) from equity.dq_issue i where i.company_id = s.company_id and i.status = 'open'
          and i.severity = 'warn') as open_warns
from equity.dq_company_score s join equity.company c using (company_id);

create or replace view equity.v_quality_summary as
select count(*) as companies, round(avg(overall)::numeric, 1) as avg_overall,
       round(avg(coverage)::numeric, 1) as avg_coverage, round(avg(freshness)::numeric, 1) as avg_freshness,
       round(avg(consistency)::numeric, 1) as avg_consistency, round(avg(documents)::numeric, 1) as avg_documents,
       count(*) filter (where overall < 60) as companies_below_60,
       (select count(*) from equity.dq_issue where status = 'open' and severity = 'error') as open_errors,
       (select count(*) from equity.dq_issue where status = 'open' and severity = 'warn') as open_warns,
       max(as_of) as as_of
from equity.dq_company_score;

alter table equity.dq_issue enable row level security;
alter table equity.dq_company_score enable row level security;
alter table equity.dq_daily enable row level security;
