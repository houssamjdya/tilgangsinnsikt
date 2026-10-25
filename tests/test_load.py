import shutil
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from tilgangsinnsikt import db
from tilgangsinnsikt.cli import app


def _csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def _counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: conn.execute(f"select count(*) from {t}").fetchone()[0] for t in db.MODEL_TABLES}


def test_load_replaces_model_data_and_keeps_findings(
    conn: psycopg.Connection, small_dataset: Path
) -> None:
    db.migrate(conn)
    counts = db.load_csv_dir(conn, small_dataset)
    assert counts == {t: _csv_rows(small_dataset / f"{t}.csv") for t in db.MODEL_TABLES}
    assert _counts(conn) == counts

    conn.execute(
        "insert into finding (analysis, rule, severity, title_nb, explanation_nb, evidence, run_id)"
        " values ('lifecycle', 'orphan_account', 'high', 'Test', 'Test.', '{}', 'r1')"
    )
    conn.commit()
    assert db.load_csv_dir(conn, small_dataset) == counts
    assert _counts(conn) == counts
    assert conn.execute("select min(event_id) from access_event").fetchone()[0] == 1
    assert conn.execute("select count(*) from finding").fetchone()[0] == 1


def test_missing_file_leaves_database_untouched(
    conn: psycopg.Connection, small_dataset: Path, tmp_path: Path
) -> None:
    db.migrate(conn)
    before = db.load_csv_dir(conn, small_dataset)
    broken = Path(shutil.copytree(small_dataset, tmp_path / "broken"))
    (broken / "account.csv").unlink()
    with pytest.raises(FileNotFoundError, match="account.csv"):
        db.load_csv_dir(conn, broken)
    assert _counts(conn) == before


def test_failed_copy_rolls_back(
    conn: psycopg.Connection, small_dataset: Path, tmp_path: Path
) -> None:
    db.migrate(conn)
    before = db.load_csv_dir(conn, small_dataset)
    broken = Path(shutil.copytree(small_dataset, tmp_path / "broken"))
    with (broken / "assignment.csv").open("a", encoding="utf-8") as f:
        f.write("ytelse:p999999,ytelse:veileder,2026-01-01,\n")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.load_csv_dir(conn, broken)
    assert _counts(conn) == before


def test_load_command_stops_before_connecting_when_files_are_missing(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["load", "--data", str(tmp_path)])
    assert result.exit_code == 1
    assert "org_unit.csv" in result.output
    assert "generate" in result.output
