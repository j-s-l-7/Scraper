-- GDELT DOC 2.0 news per company. tone is null for ArtList (GDELT only exposes tone in other modes). Idempotent.
create table if not exists equity.news_article (
    company_id int  not null,
    url        text not null,
    title      text,
    seendate   timestamptz,
    domain     text,
    tone       numeric,
    lang       text,
    loaded_at  timestamptz not null default now(),
    primary key (company_id, url)
);
create index if not exists news_article_seen_idx on equity.news_article (company_id, seendate);
alter table equity.news_article enable row level security;
