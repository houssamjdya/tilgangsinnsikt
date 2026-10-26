import json
from pathlib import Path

import pytest
from conftest import AS_OF
from typer.testing import CliRunner

from tilgangsinnsikt import __version__
from tilgangsinnsikt.cli import app

AS_OF_ARG = ["--as-of", AS_OF.isoformat()]


def invoke(*args: str):
    return CliRunner().invoke(app, list(args), env={"COLUMNS": "200"})


@pytest.fixture
def analysed(cli_schema: str, small_dataset: Path) -> Path:
    assert invoke("load", "--data", str(small_dataset)).exit_code == 0
    result = invoke("analyse", *AS_OF_ARG)
    assert result.exit_code == 0, result.output
    return small_dataset


def test_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_analyse_prints_summary_and_coverage(cli_schema: str, small_dataset: Path) -> None:
    invoke("load", "--data", str(small_dataset))
    result = invoke("analyse", *AS_OF_ARG)
    assert result.exit_code == 0, result.output
    assert "leaver_active_account" in result.output
    assert "RBAC coverage" in result.output
    assert "rbac_coverage" not in result.output


def test_analyse_rejects_unknown_analyses(cli_schema: str) -> None:
    result = invoke("analyse", "--only", "lifecycle,roles")
    assert result.exit_code == 1
    assert "roles" in result.output


def test_analyse_needs_loaded_data(cli_schema: str) -> None:
    result = invoke("analyse", *AS_OF_ARG)
    assert result.exit_code == 1
    assert "load" in result.output


def test_evaluate_scores_only_ground_truth_rules(analysed: Path) -> None:
    result = invoke("evaluate", "--data", str(analysed))
    assert result.exit_code == 0, result.output
    planted = json.loads((analysed / "ground_truth.json").read_text())["planted"]
    for rule in planted:
        assert rule in result.output
    for rule in ("joiner_without_baseline", "lookup_out_of_domain", "rbac_coverage"):
        assert rule not in result.output
    assert "FAIL" not in result.output


def test_evaluate_marks_skipped_analyses_as_not_run(cli_schema: str, small_dataset: Path) -> None:
    invoke("load", "--data", str(small_dataset))
    invoke("analyse", "--only", "sod", *AS_OF_ARG)
    result = invoke("evaluate", "--data", str(small_dataset))
    assert result.exit_code == 0, result.output
    assert result.output.count("not run") == 8
    assert result.output.count("pass") == 1


def test_evaluate_needs_a_run_for_the_ground_truth_date(
    cli_schema: str, small_dataset: Path
) -> None:
    invoke("load", "--data", str(small_dataset))
    invoke("analyse", "--as-of", "2026-07-01")
    result = invoke("evaluate", "--data", str(small_dataset))
    assert result.exit_code == 1
    assert f"analyse --as-of {AS_OF.isoformat()}" in result.output


def test_markdown_report_puts_coverage_first(analysed: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    result = invoke("report", "--format", "md", "--out", str(out))
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Nøkkeltall",
        "## Hva bør gjøres først",
        "## Oppsummering",
        "## Funn per analyse",
    ]
    key_metrics = text.split("## Hva bør gjøres først")[0]
    assert "Rolledekning (RBAC)" in key_metrics
    assert "Rolledekning" not in text.split("## Hva bør gjøres først")[1]
    assert "rbac_coverage" not in text
    assert "1. **Kritisk**" in text


def test_terminal_report(analysed: Path) -> None:
    result = invoke("report")
    assert result.exit_code == 0, result.output
    output = result.output
    assert output.index("Rolledekning (RBAC)") < output.index("Hva bør gjøres først")
    assert "Sluttet, men har aktive kontoer" in output


def test_report_needs_a_run(cli_schema: str) -> None:
    result = invoke("report")
    assert result.exit_code == 1
    assert "analyse" in result.output


def test_demo_runs_every_step(cli_schema: str, tmp_path: Path) -> None:
    result = invoke("demo", *AS_OF_ARG, "--data", str(tmp_path / "synthetic"))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "synthetic" / "ground_truth.json").is_file()
    assert "Loaded from" in result.output
    assert "Hva bør gjøres først" in result.output
