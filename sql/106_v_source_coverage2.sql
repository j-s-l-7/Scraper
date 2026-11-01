-- v_source_coverage extended with the second wave of sources. Idempotent (drop + create: column list is unchanged).
create or replace view equity.v_source_coverage as
select 'nse_bhav_full' as source, count(*) as rows, min(date) as min_date, max(date) as max_date,
       max(loaded_at) as last_loaded, count(distinct symbol) as companies from equity.nse_bhav_full
union all select 'bse_bhav', count(*), min(date), max(date), max(loaded_at), count(distinct sc_code) from equity.bse_bhav
union all select 'bulk_block_deal', count(*), min(date), max(date), max(loaded_at), count(distinct symbol) from equity.bulk_block_deal
union all select 'fo_bhav', count(*), min(date), max(date), max(loaded_at), count(distinct symbol) from equity.fo_bhav
union all select 'credit_rating', count(*), min(date), max(date), max(loaded_at), count(distinct company_id) from equity.credit_rating
union all select 'macro_daily', count(*), min(date), max(date), max(loaded_at), count(distinct code) from macro.series_daily
union all select 'macro_annual', count(*), make_date(min(year),1,1), make_date(max(year),12,31), max(loaded_at), count(distinct code) from macro.series_annual
union all select 'news_article', count(*), min(seendate)::date, max(seendate)::date, max(loaded_at), count(distinct company_id) from equity.news_article
union all select 'fpi_flow_daily', count(*), min(date), max(date), max(fetched_at), count(distinct category) from macro.fpi_flow_daily
union all select 'fpi_sector_fortnightly', count(*), min(report_date), max(report_date), max(fetched_at), count(distinct sector) from macro.fpi_sector_fortnightly
union all select 'macro_monthly', count(*), min(period), max(period), max(fetched_at), count(distinct code) from macro.series_monthly
union all select 'mf_holding', count(*), min(month), max(month), max(fetched_at), count(distinct isin) from equity.mf_holding
union all select 'listing_history', count(*), min(first_seen), max(last_seen), max(fetched_at), count(distinct symbol) from equity.listing_history
union all select 'adj_factor', count(*), min(ex_date), max(ex_date), max(fetched_at), count(distinct symbol) from equity.adj_factor;
