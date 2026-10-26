-- Provenance on every row a connector writes, so connect-entra can replace its own rows
-- without touching data from other sources.
alter table entitlement
    add column source text not null default 'synthetic'
        check (source in ('synthetic', 'entra', 'csv'));

alter table assignment
    add column source text not null default 'synthetic'
        check (source in ('synthetic', 'entra', 'csv'));

-- Graph does not say when a user was added to a group; unknown is more honest than the
-- date of the sync.
alter table assignment alter column granted_at drop not null;

create index entitlement_source_idx on entitlement (source);
create index assignment_source_idx on assignment (source);
create index account_source_idx on account (source);
