-- Allow the extra fetch sources (commoncrawl, audio) and seed the OpenAI audio budget switches. Idempotent.
alter table equity.document drop constraint if exists document_fetch_source_check;
alter table equity.document add constraint document_fetch_source_check check
    (fetch_source is null or fetch_source in ('original','original_retry','wayback','commoncrawl','company_site','audio','nse','bse'));
insert into equity.control (key, value, reason) values
    ('openai_audio_spend_usd', '0', 'estimated spend at $0.006 per audio minute'),
    ('openai_audio_budget_usd', '25', 'stop transcribing audio once the estimated spend reaches this')
on conflict (key) do nothing;
