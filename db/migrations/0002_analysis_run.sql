-- One row per analyse invocation, so a run with no findings is still recorded and
-- evaluate can tell an analysis that found nothing from one that never ran.
create table analysis_run (
    run_id      text primary key,
    as_of       date not null,
    analyses    text[] not null,
    started_at  timestamptz not null default now(),
    finished_at timestamptz not null default clock_timestamp()
);

create index analysis_run_as_of_idx on analysis_run (as_of, started_at);
