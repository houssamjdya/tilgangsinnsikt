from datetime import UTC, date, datetime

import psycopg
import pytest

from tilgangsinnsikt import db
from tilgangsinnsikt.config import DatabaseSettings

MODEL_TABLES = {
    "identity",
    "org_unit",
    "application",
    "account",
    "entitlement",
    "assignment",
    "access_event",
    "finding",
}


def _tables(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "select table_name from information_schema.tables where table_schema = current_schema()"
    )
    return {row[0] for row in rows}


def test_migrate_creates_model_and_is_idempotent(conn: psycopg.Connection) -> None:
    assert db.migrate(conn) == ["0001_schema", "0002_analysis_run"]
    assert db.migrate(conn) == []
    assert _tables(conn) == MODEL_TABLES | {"analysis_run", "schema_migration"}


def test_copy_rows_and_csv(conn: psycopg.Connection, tmp_path) -> None:
    db.migrate(conn)
    units = [("U-0001-01", "Testkontor", "Region A", "lokalkontor", None)]
    count = db.copy_rows(
        conn, "org_unit", ["unit_id", "name", "region", "unit_type", "parent_id"], units
    )
    assert count == 1

    csv_path = tmp_path / "identity.csv"
    csv_path.write_text(
        "identity_id,display_name,unit_id,job_title,manager_id,employment_start,employment_end,status\n"
        "P000001,Testperson En,U-0001-01,leder,,2020-01-01,,active\n"
        'P000002,"Testperson, To",U-0001-01,veileder,P000001,2021-03-01,2025-06-30,leaver\n',
        encoding="utf-8",
    )
    assert db.copy_csv(conn, "identity", csv_path) == 2
    conn.commit()

    rows = conn.execute(
        "select identity_id, display_name, manager_id, employment_end from identity order by 1"
    ).fetchall()
    assert rows == [
        ("P000001", "Testperson En", None, None),
        ("P000002", "Testperson, To", "P000001", date(2025, 6, 30)),
    ]


def test_copy_access_event(conn: psycopg.Connection) -> None:
    db.migrate(conn)
    columns = [
        "account_id",
        "app_id",
        "occurred_at",
        "subject_ref",
        "subject_region",
        "subject_domain",
        "subject_is_employee",
        "action",
    ]
    event = (
        "A000001",
        "ytelse",
        datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
        "S00000001",
        "Region A",
        "oppfolging",
        True,
        "read",
    )
    assert db.copy_rows(conn, "access_event", columns, [event]) == 1
    conn.commit()

    row = conn.execute(f"select {', '.join(columns)} from access_event").fetchone()
    assert row == event


def test_schema_rejects_invalid_values(conn: psycopg.Connection) -> None:
    db.migrate(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.copy_rows(conn, "application", ["app_id", "name", "domain"], [("x", "X", "ukjent")])


def test_settings_ignore_empty_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "")
    monkeypatch.setenv("POSTGRES_PORT", "")
    settings = DatabaseSettings(_env_file=None)
    assert settings.host == "localhost"
    assert settings.port == 5432
