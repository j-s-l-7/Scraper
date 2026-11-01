-- Own corporate-action adjustment factors (split, bonus, dividend) and adjusted prices from raw NSE closes. Idempotent.
create table if not exists equity.adj_factor (
    symbol      text not null,
    ex_date     date not null,
    factor      numeric not null,             -- multiply raw prices BEFORE ex_date by this
    kind        text not null check (kind in ('split','bonus','dividend')),
    source      text not null default 'computed:corporate_action',
    fetched_at  timestamptz not null default now(),
    primary key (symbol, ex_date, kind)
);
create index if not exists adj_factor_symbol_idx on equity.adj_factor (symbol, ex_date);
alter table equity.adj_factor enable row level security;

create or replace view equity.v_prices_adjusted as
select b.symbol, b.date, b.open, b.high, b.low, b.close, b.ttl_trd_qnty as volume,
       c.cum as factor,
       b.close * c.cum as adj_close,
       b.open  * c.cum as adj_open,
       b.high  * c.cum as adj_high,
       b.low   * c.cum as adj_low,
       h.adj_close as yf_adj_close
from equity.nse_bhav_full b
cross join lateral (select coalesce(exp(sum(ln(f.factor))), 1) as cum from equity.adj_factor f
                    where f.symbol = b.symbol and f.ex_date > b.date) c
left join equity.historical_prices h on h.symbol = b.symbol and h.date = b.date
where b.series = 'EQ';
