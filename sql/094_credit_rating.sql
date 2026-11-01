-- Credit ratings parsed from rating-rationale pages/PDFs; the text goes to data/documents like other documents. Idempotent.
do $$
begin
    alter table equity.document drop constraint if exists document_doc_type_check;
    alter table equity.document add constraint document_doc_type_check
        check (doc_type in ('annual_report','concall_transcript','concall_ppt','credit_rating'));
exception when others then null;
end $$;

create table if not exists equity.credit_rating (
    company_id  int  not null,
    url         text not null,
    agency      text not null default '',
    instrument  text not null default '',
    rating      text not null,
    outlook     text,
    action      text,
    date        date,
    loaded_at   timestamptz not null default now(),
    primary key (company_id, url, agency, instrument, rating)
);
create index if not exists credit_rating_company_idx on equity.credit_rating (company_id, date);
alter table equity.credit_rating enable row level security;
