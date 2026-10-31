-- Completeness ledger: annual reports FY2015..latest published FY, concall quarters FY2017Q1..latest announced quarter.
-- A call held in Jul-Sep discusses the previous calendar quarter (Apr-Jun), so a transcript's reported quarter end is
-- (first day of the call's calendar quarter) - 1 day. Idempotent.
create or replace view equity.v_document_completeness as
with p as (
    select (case when extract(month from now()) >= 8 then extract(year from now())
                 else extract(year from now()) - 1 end)::int as latest_fy
), exp_ar as (
    select c.company_id, g as fy from equity.company c, p, generate_series(2015, p.latest_fy) g
), exp_q as (
    select c.company_id, q::date as qend
    from equity.company c,
         generate_series('2016-06-30'::date, (now() - interval '45 days')::date, interval '3 months') q
    where q::date <= (now() - interval '45 days')::date
), qs as (            -- normalise generated dates to true quarter ends
    select company_id, (date_trunc('quarter', qend) + interval '3 months' - interval '1 day')::date as qend from exp_q
), found_ar as (
    select distinct company_id, fiscal_year as fy from equity.document
    where doc_type = 'annual_report' and status = 'ok' and fiscal_year is not null
), found_q as (
    select distinct company_id, (date_trunc('quarter', doc_date) - interval '1 day')::date as qend
    from equity.document where doc_type = 'concall_transcript' and status = 'ok' and doc_date is not null
)
select c.company_id, c.nse_symbol,
       (select count(*) from exp_ar e where e.company_id = c.company_id)                                   as ar_expected,
       (select count(*) from exp_ar e join found_ar f using (company_id, fy) where e.company_id = c.company_id) as ar_found,
       (select coalesce(array_agg(e.fy order by e.fy), '{}') from exp_ar e
         where e.company_id = c.company_id
           and not exists (select 1 from found_ar f where f.company_id = e.company_id and f.fy = e.fy))    as ar_missing,
       (select count(*) from qs e where e.company_id = c.company_id)                                       as cc_expected,
       (select count(*) from qs e join found_q f using (company_id, qend) where e.company_id = c.company_id) as cc_found,
       (select coalesce(array_agg(e.qend order by e.qend), '{}') from qs e
         where e.company_id = c.company_id
           and not exists (select 1 from found_q f where f.company_id = e.company_id and f.qend = e.qend))  as cc_missing
from equity.company c;
