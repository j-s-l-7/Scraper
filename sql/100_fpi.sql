-- NSDL FPI Monitor: daily flows (latest-report snapshot, history accrues) and fortnightly sector-wise AUC/net investment
-- (static report pages). Provenance on every row. Idempotent.
create table if not exists macro.fpi_flow_daily (
    date             date not null,
    category         text not null,           -- Equity, Debt-General Limit, Debt-VRR, Debt-FAR, Hybrid, Mutual Funds, AIFs, Total
    route            text not null,           -- Stock Exchange, Primary market & others, Sub-total, scheme names, Total
    gross_purchases  numeric,                 -- Rs crore
    gross_sales      numeric,
    net_inr_cr       numeric,
    net_usd_mn       numeric,
    usd_inr          numeric,
    source           text not null default 'nsdl_fpi_latest',
    fetched_at       timestamptz not null default now(),
    primary key (date, category, route)
);
create table if not exists macro.fpi_sector_fortnightly (
    report_date  date not null,               -- fortnight end the report is for
    sector       text not null,
    measure      text not null,               -- auc_prev, net_prev, net_curr, auc_end
    route        text not null,               -- Equity, Debt General Limit, ..., AIF, Total
    inr_cr       numeric,
    usd_mn       numeric,
    source       text not null default 'nsdl_fpi_static',
    fetched_at   timestamptz not null default now(),
    primary key (report_date, sector, measure, route)
);
alter table macro.fpi_flow_daily enable row level security;
alter table macro.fpi_sector_fortnightly enable row level security;
