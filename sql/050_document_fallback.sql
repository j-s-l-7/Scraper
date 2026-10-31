-- Where a document's text actually came from when the screener-listed url was dead or blocked.
-- Idempotent.
alter table equity.document add column if not exists fetched_url  text;   -- url that finally served the PDF
alter table equity.document add column if not exists fetch_source text;   -- original | original_retry | wayback | company_site | nse | bse
alter table equity.document add column if not exists attempts_log jsonb not null default '[]';  -- [{source, url, result}] per fallback tried
alter table equity.document drop constraint if exists document_fetch_source_check;
alter table equity.document add constraint document_fetch_source_check check
    (fetch_source is null or fetch_source in ('original','original_retry','wayback','company_site','nse','bse'));
