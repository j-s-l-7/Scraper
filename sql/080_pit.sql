-- Point-in-time history: every changed financial fact / close price keeps its previous value, so any past date can be
-- reconstructed as it was then known. Tiny by design (only changed values are stored). Idempotent.

create table if not exists equity.financial_fact_history (
    company_id    int          not null,
    basis         char(1)      not null,
    metric_id     smallint     not null,
    period_type   text         not null,
    period_end    date         not null,
    value         numeric(24,4) not null,
    recorded_at   timestamptz,                -- when this value was written (old.updated_at)
    superseded_at timestamptz  not null,      -- when it was replaced
    run_id        bigint
);
create index if not exists financial_fact_history_key_idx
    on equity.financial_fact_history (company_id, basis, metric_id, period_type, period_end, superseded_at);

create or replace function equity.trg_fact_history() returns trigger language plpgsql as $$
begin
    if new.value is distinct from old.value then
        insert into equity.financial_fact_history
            (company_id, basis, metric_id, period_type, period_end, value, recorded_at, superseded_at, run_id)
        values (old.company_id, old.basis, old.metric_id, old.period_type, old.period_end, old.value,
                old.updated_at, now(), old.run_id);
        new.updated_at := now();
    end if;
    return new;
end $$;
drop trigger if exists fact_history on equity.financial_fact;
create trigger fact_history before update on equity.financial_fact
    for each row execute function equity.trg_fact_history();

-- Prices: only close changes are kept (adj_close is rewritten for the whole history at every dividend/split, which
-- would explode the table; it is recorded only together with a changed close). recorded_at is null: prices carry no
-- write timestamp, null = "since before tracking began".
create table if not exists equity.historical_prices_history (
    symbol        varchar(32) not null,
    date          date        not null,
    close         numeric(14,4),
    adj_close     numeric(14,4),
    recorded_at   timestamptz,
    superseded_at timestamptz not null
);
create index if not exists historical_prices_history_idx on equity.historical_prices_history (symbol, date);

create or replace function equity.trg_price_history() returns trigger language plpgsql as $$
begin
    if new.close is distinct from old.close then
        insert into equity.historical_prices_history (symbol, date, close, adj_close, recorded_at, superseded_at)
        values (old.symbol, old.date, old.close, old.adj_close, null, now());
    end if;
    return new;
end $$;
drop trigger if exists price_history on equity.historical_prices;
create trigger price_history before update on equity.historical_prices
    for each row execute function equity.trg_price_history();

-- Values as known at `as_of`: the current row if it was written by then and nothing older was still valid,
-- else the history row whose validity interval contains as_of.
create or replace function equity.financials_asof(as_of timestamptz)
returns table (company_id int, basis char(1), metric_id smallint, period_type text, period_end date, value numeric,
               known_since timestamptz)
language sql stable as $$
    select f.company_id, f.basis, f.metric_id, f.period_type, f.period_end, f.value, f.updated_at
    from equity.financial_fact f
    where f.updated_at <= as_of
      and not exists (select 1 from equity.financial_fact_history h
                      where h.company_id = f.company_id and h.basis = f.basis and h.metric_id = f.metric_id
                        and h.period_type = f.period_type and h.period_end = f.period_end and h.superseded_at > as_of)
    union all
    select h.company_id, h.basis, h.metric_id, h.period_type, h.period_end, h.value, h.recorded_at
    from equity.financial_fact_history h
    where coalesce(h.recorded_at, '-infinity') <= as_of and h.superseded_at > as_of
$$;

-- readable form: select * from equity.v_financials_asof('2026-06-30') where nse_symbol='TCS'
create or replace function equity.v_financials_asof(as_of timestamptz)
returns table (nse_symbol text, company_id int, basis char(1), section text, line_item text, unit text,
               period_type text, period_end date, value numeric, known_since timestamptz)
language sql stable as $$
    select c.nse_symbol::text, a.company_id, a.basis, m.section, coalesce(p.name || ' › ', '') || m.name, m.unit,
           a.period_type, a.period_end, a.value, a.known_since
    from equity.financials_asof(as_of) a
    join equity.company c using (company_id)
    join equity.metric m on m.metric_id = a.metric_id
    left join equity.metric p on p.metric_id = m.parent_metric_id
$$;

alter table equity.financial_fact_history enable row level security;
alter table equity.historical_prices_history enable row level security;
