-- NSE bulk and block deals (daily snapshot files). PK is an md5 of the row. Idempotent.
create table if not exists equity.bulk_block_deal (
    deal_hash  text    primary key,
    date       date    not null,
    symbol     text    not null,
    client     text,
    side       text,
    qty        bigint,
    price      numeric,
    kind       text    not null check (kind in ('bulk','block')),
    loaded_at  timestamptz not null default now()
);
create index if not exists bulk_block_deal_date_idx on equity.bulk_block_deal (date, symbol);
alter table equity.bulk_block_deal enable row level security;
