-- Lake-only bookkeeping (not applied to Supabase; created by `lake-up`). Idempotent.
create table if not exists public.lake_load_log (
    run_id     bigint not null,          -- GitHub Actions run id
    artifact   text   not null,
    files      int,
    rows_loaded bigint,
    status     text,
    loaded_at  timestamptz not null default now(),
    primary key (run_id, artifact)
);
create table if not exists public.lake_window (
    window_id     serial primary key,
    from_rank     int not null,
    to_rank       int not null,
    ranks         text,                  -- optional explicit rank list (retry windows)
    attempt       smallint not null default 1,
    run_id        bigint,
    status        text not null default 'dispatched'
                  check (status in ('dispatched','done','loaded','failed','retried')),
    dispatched_at timestamptz not null default now(),
    loaded_at     timestamptz,
    note          text
);
create table if not exists public.lake_state (
    key text primary key, value text, updated_at timestamptz not null default now()
);
