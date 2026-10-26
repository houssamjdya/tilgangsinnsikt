# Architecture

Tilgangsinnsikt is a command-line tool around one PostgreSQL schema. Sources are loaded into
an IGA-style model, analyses read only from that model, and reports read only the stored
findings. Each part can be replaced without touching the others.

```mermaid
flowchart TB
    subgraph Sources
        synth[synth/ generator] --> csv[(CSV files and<br/>ground_truth.json)]
        graph[Microsoft Graph]
    end
    csv --> load[db.load_csv_dir]
    graph --> entra[connectors/entra.py]
    load --> model[(model tables)]
    entra --> model
    model --> runner[runs.run_analyses]
    runner --> lifecycle & peer & sod & logs
    lifecycle[A1 lifecycle] --> findings[(finding, analysis_run)]
    peer[A2 peer_group] --> findings
    sod[A3 sod] --> findings
    logs[A4 log_anomalies] --> findings
    findings --> report[report/ terminal and Markdown]
    findings --> evaluate[evaluation.py vs ground truth]
```

## Layout

| Path | Role |
|---|---|
| `db/migrations/` | Plain SQL migrations, applied in file order by `db.migrate` and recorded in `schema_migration` |
| `src/tilgangsinnsikt/db.py` | Connection, migration runner, bulk `COPY` helpers, CSV loading |
| `src/tilgangsinnsikt/config.py` | Settings from environment variables and `.env` (pydantic-settings) |
| `src/tilgangsinnsikt/synth/` | Deterministic synthetic organisation, planted findings and ground truth |
| `src/tilgangsinnsikt/connectors/entra.py` | Read-only Microsoft Graph connector |
| `src/tilgangsinnsikt/analyses/` | The four analyses, the `Finding` type and the registry |
| `src/tilgangsinnsikt/runs.py` | Runs analyses as one recorded run and reads runs back |
| `src/tilgangsinnsikt/evaluation.py` | Precision and recall per rule against the ground truth |
| `src/tilgangsinnsikt/report/` | Report content and its terminal and Markdown renderers |
| `src/tilgangsinnsikt/cli.py` | The `typer` commands |
| `rules/` | SoD rules and Entra group risk patterns, editable without code changes |

## Data model and ownership

The model tables are `org_unit`, `identity`, `application`, `account`, `entitlement`,
`assignment` and `access_event`. `account`, `entitlement` and `assignment` carry a `source`
column (`synthetic`, `entra` or `csv`), which decides who may change a row:

- `load` owns the synthetic data. It truncates and reloads every model table in one
  transaction, so a failed load leaves the previous data in place.
- `connect-entra` deletes and reinserts only rows with `source = 'entra'`, in one
  transaction. It runs after `load` and never touches rows from other sources.
- `finding` and `analysis_run` are never truncated: findings are snapshots of earlier runs and
  survive a reload of the model.

`access_event` and `finding` have no foreign keys on purpose. Logs can refer to accounts that
no longer exist, and findings must outlive the data they were computed from.

## Analyses and runs

Each analysis module registers a function `run(conn, run_id, as_of) -> list[Finding]` in
`analyses.base.REGISTRY`. The function does its aggregation in SQL, shapes the rows into
`Finding` objects with a Norwegian title and explanation and a JSON `evidence` dict, writes them
with `COPY` and returns them.

`runs.run_analyses` inserts an `analysis_run` row, calls each selected analysis and records the
finish time, all in one transaction. A run is therefore stored with all its findings or not at
all, and `evaluate` can tell an analysis that found nothing from one that was never run.

Every time window is computed from the run's `as_of` date, never from `now()`. Generation,
analysis and evaluation can all be pinned to one date, which makes the tests and the published
numbers reproducible.

Some results describe the organisation rather than a weakness. RBAC coverage from A2 is stored
as a finding with rule `rbac_coverage` so every run keeps it, but `METRIC_RULES` keeps it out of
finding counts, priorities and evaluation; the report shows it as a key metric instead.

## Synthetic data and ground truth

The generator builds a fictional directorate with 12 regions, about 250 local offices, regional
offices, a directorate and three contact centres. Assignments come from a role matrix keyed by
job title and unit type, so peer groups and candidate roles are meaningful. Each stage draws
from its own random stream (`synth/seeds.py`), so changing one stage does not shift the others,
and the same seed and date give byte-identical files.

Planted weaknesses are drawn from disjoint candidate pools and written to `ground_truth.json`.
Before writing, the generator recomputes every rule from its own output and fails if anything
other than the planted items meets a rule. Chance threshold crossings in the access log are
repaired. The dataset is therefore noise-free by design: it tests that the rules are
implemented correctly, not how they perform on real data.

## Entra connector

The connector authenticates with MSAL client credentials and calls Graph with GET requests only.
It follows `@odata.nextLink` paging, refuses next links outside `graph.microsoft.com` so the
token is never sent elsewhere, and retries throttled requests using `Retry-After`. Without an
Entra ID P1 licence or `AuditLog.Read.All`, Graph refuses `signInActivity`; the connector then
reads users without it and warns.

Users become accounts in the `entra` application, linked to identities by `employeeId` or a
mapping file. Groups become entitlements keyed by object ID, with a risk level from name
patterns. Direct user memberships become assignments with an unknown grant date. SoD rules
written with names such as `entra:Utviklere` are matched to real groups at analysis time through
the configured display names in `ENTRA_SOD_GROUPS`.

Output is counts only. Error messages are reduced to HTTP status and error codes, and the
loggers of the HTTP and authentication libraries are limited to warnings, because their messages
can contain tenant or object IDs.

## Testing

Tests run against PostgreSQL in a throwaway schema per test, so they never touch loaded demo
data. A small synthetic dataset is generated once per session with a fixed date. Lifecycle and
SoD tests require precision and recall of 1.0 against the ground truth; peer-group and log
tests require the statistical targets and print the actual numbers. The Entra tests use a fake
Graph server on `httpx.MockTransport`, patch out the real sign-in and run from an empty
directory so no `.env` is read. CI runs ruff, gitleaks and the test suite with a PostgreSQL
service container.

## Adding a source or an analysis

A new connector writes to the model tables with its own `source` value and replaces only its own
rows. A new analysis is a module with a registered `run` function and an entry in
`runs.ANALYSIS_ORDER`; if it has planted findings, the generator gets a new ground-truth key and
`evaluation.RULE_ANALYSIS` maps the rule to the analysis.
