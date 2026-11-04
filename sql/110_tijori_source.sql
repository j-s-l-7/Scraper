-- 110: documents found on Tijori Finance (company-published PDFs on its file server, verified against their own text
-- before storing; personal non-commercial use per its Terms). Idempotent.
alter table equity.document drop constraint if exists document_fetch_source_check;
alter table equity.document add constraint document_fetch_source_check check (fetch_source is null or fetch_source = any
    (array['original','original_retry','wayback','commoncrawl','company_site','audio','nse','bse','tijori']));
