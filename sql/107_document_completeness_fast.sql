-- 107: same v_document_completeness (columns, order, types) computed with joins + group by instead of
-- per-company correlated subqueries over the expected-period CTEs. The old shape is quadratic in the number
-- of companies (2,618 after the top-1000 rollout) and took >60 s on Supabase; this one is linear.
create or replace view equity.v_document_completeness as
with p as (
    select (case when extract(month from now()) >= 8 then extract(year from now())
                 else extract(year from now()) - 1 end)::int as latest_fy
), years as (
    select g as fy from p, generate_series(2015, p.latest_fy) g
), quarters as (      -- generated dates normalised to true quarter ends
    select (date_trunc('quarter', q) + interval '3 months' - interval '1 day')::date as qend
    from generate_series('2016-06-30'::date, (now() - interval '45 days')::date, interval '3 months') q
    where q::date <= (now() - interval '45 days')::date
), found_ar as (
    select distinct company_id, fiscal_year as fy from equity.document
    where doc_type = 'annual_report' and status = 'ok' and fiscal_year is not null
), found_q as (
    select distinct company_id, (date_trunc('quarter', doc_date) - interval '1 day')::date as qend
    from equity.document where doc_type = 'concall_transcript' and status = 'ok' and doc_date is not null
), found_p as (
    select distinct company_id, (date_trunc('quarter', doc_date) - interval '1 day')::date as qend
    from equity.document where doc_type = 'concall_ppt' and status = 'ok' and doc_date is not null
), ar as (
    select c.company_id, count(*) as ar_expected, count(f.fy) as ar_found,
           coalesce(array_agg(y.fy order by y.fy) filter (where f.fy is null), '{}') as ar_missing
    from equity.company c cross join years y
    left join found_ar f on f.company_id = c.company_id and f.fy = y.fy
    group by c.company_id
), cc as (
    select c.company_id, count(*) as cc_expected, count(fq.qend) as cc_found,
           coalesce(array_agg(q.qend order by q.qend) filter (where fq.qend is null), '{}') as cc_missing,
           count(fp.qend) as ppt_found,
           coalesce(array_agg(q.qend order by q.qend) filter (where fp.qend is null), '{}') as ppt_missing
    from equity.company c cross join quarters q
    left join found_q fq on fq.company_id = c.company_id and fq.qend = q.qend
    left join found_p fp on fp.company_id = c.company_id and fp.qend = q.qend
    group by c.company_id
)
select c.company_id, c.nse_symbol,
       ar.ar_expected, ar.ar_found, ar.ar_missing,
       cc.cc_expected, cc.cc_found, cc.cc_missing,
       cc.ppt_found, cc.cc_expected as ppt_expected, cc.ppt_missing
from equity.company c
join ar on ar.company_id = c.company_id
join cc on cc.company_id = c.company_id;
