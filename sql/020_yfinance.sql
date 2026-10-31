-- yfinance layer: everything yfinance has that screener.in does not.
-- Statement line items reuse equity.metric / equity.financial_fact (metric.source distinguishes origin).
-- Idempotent.

-- 1. metric gains a source; yfinance statements get their own sections (no name collisions with screener).
alter table equity.metric add column if not exists source text not null default 'screener';
alter table equity.metric drop constraint if exists metric_source_check;
alter table equity.metric add constraint metric_source_check
    check (source in ('screener','yfinance','nse','bse'));
alter table equity.metric drop constraint if exists metric_section_check;
alter table equity.metric add constraint metric_section_check check (section in
    ('quarters','profit-loss','balance-sheet','cash-flow','ratios',          -- screener
     'yf-income','yf-balance','yf-cashflow'));                              -- yfinance (amounts stored in ₹ crore)
alter table equity.metric drop constraint if exists metric_unit_check;
alter table equity.metric add constraint metric_unit_check
    check (unit in ('cr','pct','rs','days','count','x'));

-- 2. Dividends / splits / bonus. yfinance primary; NSE corporate-actions API only when yfinance has none.
create table if not exists equity.corporate_action (
    company_id   int  not null references equity.company on delete cascade,
    action_type  text not null check (action_type in ('dividend','split','bonus','rights','other')),
    ex_date      date not null,
    value        numeric(18,6),      -- dividend ₹/share; split = new shares per old (2.0 = 1:2 split)
    detail       text,               -- free-text purpose from NSE when sourced there
    source       text not null check (source in ('yfinance','nse','bse')),
    run_id       bigint references equity.scrape_run,
    primary key (company_id, action_type, ex_date)
);

-- 3. Shares outstanding history (yfinance get_shares_full).
create table if not exists equity.shares_outstanding (
    company_id  int  not null references equity.company on delete cascade,
    as_of       date not null,
    shares      bigint not null,
    source      text not null default 'yfinance',
    primary key (company_id, as_of)
);

-- 4. yfinance quote/profile snapshot: typed columns for the commonly used fields, full payload in jsonb.
create table if not exists equity.yf_snapshot (
    company_id             int  not null references equity.company on delete cascade,
    as_of                  date not null,
    yf_ticker              text not null,
    market_cap             numeric(22,0),
    enterprise_value       numeric(22,0),
    shares_outstanding     bigint,
    float_shares           bigint,
    held_pct_insiders      numeric(8,4),
    held_pct_institutions  numeric(8,4),
    beta                   numeric(8,4),
    trailing_pe            numeric(12,4),
    forward_pe             numeric(12,4),
    forward_eps            numeric(14,4),
    peg_ratio              numeric(12,4),
    price_to_book          numeric(12,4),
    ev_to_ebitda           numeric(12,4),
    ev_to_revenue          numeric(12,4),
    target_mean_price      numeric(14,2),
    target_median_price    numeric(14,2),
    target_high_price      numeric(14,2),
    target_low_price       numeric(14,2),
    recommendation_key     text,
    recommendation_mean    numeric(6,3),
    num_analyst_opinions   int,
    all_time_high          numeric(14,2),
    all_time_low           numeric(14,2),
    first_trade_date       date,
    full_time_employees    int,
    info                   jsonb not null default '{}',   -- full yfinance info dict (minus officers)
    run_id                 bigint references equity.scrape_run,
    primary key (company_id, as_of)
);

-- 5. Analyst estimates, long format; snapshots accrue by as_of so revisions are tracked over time.
--    estimate_type: eps | revenue | eps_trend | eps_revisions | growth | price_target | recommendation
--    period: yfinance period code ('0q','+1q','0y','+1y','0m','-1m',..., 'current' for price targets)
create table if not exists equity.analyst_estimate (
    company_id     int  not null references equity.company on delete cascade,
    as_of          date not null,
    estimate_type  text not null,
    period         text not null,
    field          text not null,     -- avg | low | high | numberOfAnalysts | growth | 7daysAgo | strongBuy ...
    value          numeric(22,4),
    primary key (company_id, as_of, estimate_type, period, field)
);

-- 6. Earnings events: reported vs estimated EPS.
create table if not exists equity.earnings_event (
    company_id    int  not null references equity.company on delete cascade,
    event_date    date not null,
    eps_estimate  numeric(14,4),
    eps_reported  numeric(14,4),
    surprise_pct  numeric(10,4),
    source        text not null default 'yfinance',
    primary key (company_id, event_date)
);

-- 7. Insider transactions (yfinance; sparse for India).
create table if not exists equity.insider_transaction (
    company_id   int  not null references equity.company on delete cascade,
    row_hash     text not null,       -- md5 of the source row (no natural key upstream)
    start_date   date,
    insider      text,
    position     text,
    transaction  text,
    text         text,
    shares       bigint,
    value        numeric(22,2),
    url          text,
    primary key (company_id, row_hash)
);

-- 8. Coverage: which source supplies what, per company (drives fallback decisions + audits).
create or replace view equity.v_coverage as
select c.company_id, c.nse_symbol, c.name,
  (select count(*) from equity.financial_fact f join equity.metric m using (metric_id)
     where f.company_id=c.company_id and m.source='screener')                       as screener_facts,
  (select min(period_end) from equity.financial_fact f join equity.metric m using (metric_id)
     where f.company_id=c.company_id and m.source='screener' and f.period_type='A') as screener_first_fy,
  (select count(*) from equity.financial_fact f join equity.metric m using (metric_id)
     where f.company_id=c.company_id and m.source='yfinance')                       as yf_facts,
  (select count(*) from equity.shareholding s where s.company_id=c.company_id)      as shareholding_rows,
  (select min(date) from equity.historical_prices p where p.symbol=c.nse_symbol)    as daily_from,
  (select max(date) from equity.historical_prices p where p.symbol=c.nse_symbol)    as daily_to,
  (select count(*) from equity.corporate_action a where a.company_id=c.company_id)  as corp_actions,
  (select string_agg(distinct source, ',') from equity.corporate_action a where a.company_id=c.company_id) as corp_action_sources,
  (select count(*) from equity.shares_outstanding s where s.company_id=c.company_id) as shares_rows,
  (select max(as_of) from equity.yf_snapshot y where y.company_id=c.company_id)     as yf_snapshot_as_of,
  (select count(*) from equity.analyst_estimate e where e.company_id=c.company_id)  as analyst_rows
from equity.company c;

do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'equity' loop
    execute format('alter table equity.%I enable row level security', t);
  end loop;
end $$;
