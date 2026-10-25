create table org_unit (
    unit_id   text primary key,
    name      text not null,
    region    text not null,
    unit_type text not null
        check (unit_type in ('lokalkontor', 'regionkontor', 'direktorat', 'kontaktsenter')),
    parent_id text references org_unit (unit_id)
);

create table identity (
    identity_id      text primary key,
    display_name     text not null,
    unit_id          text not null references org_unit (unit_id),
    job_title        text not null,
    manager_id       text references identity (identity_id),
    employment_start date not null,
    employment_end   date,
    status           text not null check (status in ('active', 'leaver'))
);

create index identity_unit_id_idx on identity (unit_id);

create table application (
    app_id text primary key,
    name   text not null,
    domain text not null
        check (domain in ('ytelse', 'oppfolging', 'arkiv', 'okonomi', 'hr', 'plattform'))
);

create table account (
    account_id  text primary key,
    identity_id text references identity (identity_id),
    app_id      text not null references application (app_id),
    username    text not null,
    enabled     boolean not null,
    last_login  timestamptz,
    source      text not null check (source in ('synthetic', 'entra', 'csv'))
);

create index account_identity_id_idx on account (identity_id);

create table entitlement (
    entitlement_id text primary key,
    app_id         text not null references application (app_id),
    name           text not null,
    description    text,
    risk_level     text not null check (risk_level in ('low', 'medium', 'high'))
);

create table assignment (
    account_id     text not null references account (account_id),
    entitlement_id text not null references entitlement (entitlement_id),
    granted_at     date not null,
    granted_by     text references identity (identity_id),
    primary key (account_id, entitlement_id)
);

create index assignment_entitlement_id_idx on assignment (entitlement_id);

-- No foreign keys: logs can reference accounts that no longer exist in the model,
-- and those events are exactly the ones worth seeing.
create table access_event (
    event_id            bigserial primary key,
    account_id          text not null,
    app_id              text not null,
    occurred_at         timestamptz not null,
    subject_ref         text not null,
    subject_region      text not null,
    subject_domain      text not null,
    subject_is_employee boolean not null,
    action              text not null check (action in ('read', 'update'))
);

create index access_event_account_time_idx on access_event (account_id, occurred_at);

-- No foreign keys: findings are a snapshot of a run and must survive a data reload.
create table finding (
    finding_id     bigserial primary key,
    analysis       text not null
        check (analysis in ('lifecycle', 'peer_group', 'sod', 'log_anomaly')),
    rule           text not null,
    severity       text not null check (severity in ('low', 'medium', 'high', 'critical')),
    identity_id    text,
    account_id     text,
    entitlement_id text,
    title_nb       text not null,
    explanation_nb text not null,
    evidence       jsonb not null,
    run_id         text not null,
    created_at     timestamptz not null default now()
);

create index finding_run_id_idx on finding (run_id);
