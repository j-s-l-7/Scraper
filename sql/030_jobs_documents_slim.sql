-- Job queue + self-healing control plane, document catalog, and storage slimming (free-tier 500 MB budget).
-- Idempotent.

-- ───────────── Job queue (any number of workers on any machine pull via SKIP LOCKED) ─────────────
create table if not exists equity.job (
    job_id        bigserial primary key,
    batch         text     not null,                -- e.g. '2026-11-01' (one full refresh)
    kind          text     not null check (kind in ('screener','yf','docs_ar','docs_concall')),
    company_id    int      not null references equity.company on delete cascade,
    priority      smallint not null default 100,    -- lower runs first (universe rank)
    status        text     not null default 'pending'
                  check (status in ('pending','running','retry','done','dead','paused')),
    attempts      smallint not null default 0,
    max_attempts  smallint not null default 6,
    next_run_at   timestamptz not null default now(),
    route         text     not null default 'any' check (route in ('any','local','gh')),  -- rule engine can pin a job
    locked_by     text,                              -- worker id: '<host>:<pid>:<slot>' or 'gh:<run>:<shard>'
    heartbeat_at  timestamptz,
    error_class   text,
    last_error    text,
    history       jsonb    not null default '[]',    -- [{at, worker, error_class, action}] audit of every failure + rule applied
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (batch, kind, company_id)
);
create index if not exists job_claim_idx on equity.job (kind, status, next_run_at, priority);

-- Global switches the rule engine flips (e.g. pause everything on DB disk pressure).
create table if not exists equity.control (
    key        text primary key,          -- 'paused', 'screener_min_interval', 'storage_limit_mb', ...
    value      jsonb not null,
    reason     text,
    updated_at timestamptz not null default now()
);
insert into equity.control (key, value, reason) values
    ('paused', 'false', 'initial'),
    ('storage_limit_mb', '470', 'Supabase free tier 500 MB; keep 30 MB headroom')
on conflict (key) do nothing;

-- ───────────── Document catalog (text lives in local compressed files until DB is upgraded) ─────────────
create table if not exists equity.document (
    company_id    int  not null references equity.company on delete cascade,
    doc_type      text not null check (doc_type in ('annual_report','concall_transcript','concall_ppt')),
    url           text not null,
    period_label  text,                   -- 'Annual Report 2026' / 'Oct 2026'
    fiscal_year   smallint,               -- FY end year (FY2026 = Apr 2025–Mar 2026) when derivable
    doc_date      date,
    source        text,                   -- bse | nse | other
    status        text not null default 'pending'
                  check (status in ('pending','ok','no_text','missing','failed')),
    page_count    int,
    char_count    int,
    pdf_bytes     bigint,
    pdf_sha256    text,
    text_path     text,                   -- relative: data/documents/<SYMBOL>/<doc_type>/<file>.jsonl.gz (1 JSON line per page)
    text_location text not null default 'local' check (text_location in ('local','db')),
    extractor     text,                   -- 'pymupdf-1.x' / 'pypdf-x'
    error         text,
    extracted_at  timestamptz,
    primary key (company_id, doc_type, url)
);
create index if not exists document_status_idx on equity.document (status);

-- Page-level text table, created now but left empty on the free tier; `load-documents` fills it after an upgrade.
create table if not exists equity.document_page (
    company_id  int  not null,
    doc_type    text not null,
    url         text not null,
    page_no     int  not null,
    text        text not null,
    primary key (company_id, doc_type, url, page_no),
    foreign key (company_id, doc_type, url) references equity.document on delete cascade
);

-- ───────────── Slimming (measured: ~3.5 MB/company before) ─────────────
-- Wide valuation history replaces long valuation_series (series name was repeated in every row + PK).
create table if not exists equity.valuation_history (
    company_id          int     not null references equity.company on delete cascade,
    basis               char(1) not null check (basis in ('C','S')),
    obs_date            date    not null,
    pe                  real, median_pe          real, eps        real,
    pbv                 real, median_pbv         real, book_value real,
    ev_multiple         real, median_ev_multiple real, ebitda     real,
    mcap_sales          real, median_mcap_sales  real, sales      real,
    gpm                 real, opm                real, npm        real,
    quarter_sales       real,
    extra               jsonb,              -- any series screener adds later
    primary key (company_id, basis, obs_date)
);
-- Redundant indexes: PK (symbol,date) already serves per-symbol scans in both directions;
-- cross-sectional fact queries are fast enough as seq scans at this size.
drop index if exists equity.idx_historical_prices_symbol_date;
drop index if exists equity.financial_fact_metric_period_idx;

do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'equity' loop
    execute format('alter table equity.%I enable row level security', t);
  end loop;
end $$;
