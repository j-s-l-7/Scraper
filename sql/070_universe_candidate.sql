-- Ranking candidates for the TOP1000_MCAP universe (tiny rows: one per listed symbol per ranking day). Idempotent.
create table if not exists equity.universe_candidate (
    symbol         text    not null,
    company_id     int     not null,
    market_cap_cr  numeric(20,2) not null,
    as_of          date    not null,
    primary key (symbol, as_of)
);
create index if not exists universe_candidate_mcap_idx on equity.universe_candidate (as_of, market_cap_cr desc);

alter table equity.universe_candidate enable row level security;
