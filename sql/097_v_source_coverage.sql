-- Coverage of the public-source tables: rows, date range, last load, entities covered. Idempotent.
create or replace view equity.v_source_coverage as
select 'nse_bhav_full' as source, count(*) as rows, min(date) as min_date, max(date) as max_date,
       max(loaded_at) as last_loaded, count(distinct symbol) as companies from equity.nse_bhav_full
union all
select 'bse_bhav', count(*), min(date), max(date), max(loaded_at), count(distinct sc_code) from equity.bse_bhav
union all
select 'bulk_block_deal', count(*), min(date), max(date), max(loaded_at), count(distinct symbol) from equity.bulk_block_deal
union all
select 'fo_bhav', count(*), min(date), max(date), max(loaded_at), count(distinct symbol) from equity.fo_bhav
union all
select 'credit_rating', count(*), min(date), max(date), max(loaded_at), count(distinct company_id) from equity.credit_rating
union all
select 'macro_daily', count(*), min(date), max(date), max(loaded_at), count(distinct code) from macro.series_daily
union all
select 'macro_annual', count(*), make_date(min(year),1,1), make_date(max(year),12,31), max(loaded_at), count(distinct code)
from macro.series_annual
union all
select 'news_article', count(*), min(seendate)::date, max(seendate)::date, max(loaded_at), count(distinct company_id)
from equity.news_article;
