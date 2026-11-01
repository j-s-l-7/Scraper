-- Provenance on the earlier source tables: source + fetched_at (loaded_at stays). Idempotent.
alter table equity.nse_bhav_full  add column if not exists source text not null default 'nsearchives:sec_bhavdata_full';
alter table equity.nse_bhav_full  add column if not exists fetched_at timestamptz not null default now();
alter table equity.bse_bhav       add column if not exists source text not null default 'bseindia:download/BhavCopy';
alter table equity.bse_bhav       add column if not exists fetched_at timestamptz not null default now();
alter table equity.bulk_block_deal add column if not exists source text not null default 'nsearchives:bulk_block';
alter table equity.bulk_block_deal add column if not exists fetched_at timestamptz not null default now();
alter table equity.fo_bhav        add column if not exists source text not null default 'nsearchives:fo_udiff';
alter table equity.fo_bhav        add column if not exists fetched_at timestamptz not null default now();
alter table equity.credit_rating  add column if not exists source text not null default 'rating_agency_page';
alter table equity.credit_rating  add column if not exists fetched_at timestamptz not null default now();
alter table equity.news_article   add column if not exists source text not null default 'gdelt_doc2';
alter table equity.news_article   add column if not exists fetched_at timestamptz not null default now();
alter table macro.series_daily    add column if not exists source text not null default 'yfinance';
alter table macro.series_daily    add column if not exists fetched_at timestamptz not null default now();
alter table macro.series_annual   add column if not exists source text not null default 'worldbank';
alter table macro.series_annual   add column if not exists fetched_at timestamptz not null default now();
