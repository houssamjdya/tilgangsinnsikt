"""Analysis runs: one recorded run per analyse invocation, with its findings."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

import psycopg
from psycopg.rows import class_row, dict_row

from tilgangsinnsikt.analyses import REGISTRY, Finding

ANALYSIS_ORDER = ["lifecycle", "peer_group", "sod", "log_anomaly"]


@dataclass(frozen=True)
class Run:
    run_id: str
    as_of: date
    analyses: list[str]
    started_at: datetime
    finished_at: datetime


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def run_analyses(
    conn: psycopg.Connection, as_of: date, names: Sequence[str] = ANALYSIS_ORDER
) -> tuple[Run, list[Finding]]:
    if unknown := [name for name in names if name not in REGISTRY]:
        raise ValueError(f"unknown analyses: {', '.join(unknown)}")
    run_id = new_run_id()
    findings: list[Finding] = []
    # One transaction: a run is recorded together with all its findings or not at all.
    with conn.transaction():
        conn.execute(
            "insert into analysis_run (run_id, as_of, analyses) values (%s, %s, %s)",
            (run_id, as_of, list(names)),
        )
        for name in names:
            findings += REGISTRY[name](conn, run_id, as_of)
        conn.execute(
            "update analysis_run set finished_at = clock_timestamp() where run_id = %s",
            (run_id,),
        )
    return get_run(conn, run_id), findings


def get_run(conn: psycopg.Connection, run_id: str) -> Run | None:
    with conn.cursor(row_factory=class_row(Run)) as cur:
        return cur.execute("select * from analysis_run where run_id = %s", (run_id,)).fetchone()


def latest_run(conn: psycopg.Connection, as_of: date | None = None) -> Run | None:
    with conn.cursor(row_factory=class_row(Run)) as cur:
        return cur.execute(
            "select * from analysis_run"
            " where %(as_of)s::date is null or as_of = %(as_of)s::date"
            " order by started_at desc, run_id desc limit 1",
            {"as_of": as_of},
        ).fetchone()


def load_findings(conn: psycopg.Connection, run_id: str) -> list[Finding]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "select analysis, rule, severity, title_nb, explanation_nb, evidence,"
            " identity_id, account_id, entitlement_id"
            " from finding where run_id = %s order by finding_id",
            (run_id,),
        ).fetchall()
    return [Finding(**row) for row in rows]
