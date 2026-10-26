from pathlib import Path

import psycopg
import pytest
import yaml
from conftest import AS_OF

from tilgangsinnsikt.analyses import sod
from tilgangsinnsikt.evaluation import score

VALID = {
    "id": "a-og-b",
    "severity": "high",
    "description_nb": "Test.",
    "left": ["ytelse:utbetaling-registrere"],
    "right": ["ytelse:utbetaling-godkjenne"],
}


def write_rules(tmp_path: Path, *rules: dict) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"rules": list(rules)}, allow_unicode=True), encoding="utf-8")
    return path


def test_sod_matches_ground_truth(
    loaded: psycopg.Connection, ground_truth: dict[str, list[str]]
) -> None:
    findings = sod.run(loaded, "r1", AS_OF)
    result = score("sod_violation", findings, ground_truth["sod_violation"])
    print(result)
    assert result.precision == result.recall == 1.0, result


def test_findings_carry_rule_severity_and_both_sides(loaded: psycopg.Connection) -> None:
    rules = {rule.id: rule for rule in sod.load_rules()}
    findings = sod.run(loaded, "r1", AS_OF)
    assert {f.evidence["rule"] for f in findings} == set(rules)
    for f in findings:
        rule = rules[f.evidence["rule"]]
        assert f.severity == rule.severity
        assert set(f.evidence["left"]) <= set(rule.left)
        assert set(f.evidence["right"]) <= set(rule.right)
        assert rule.description_nb in f.explanation_nb
    stored = loaded.execute("select count(*) from finding where analysis = 'sod'").fetchone()[0]
    assert stored == len(findings)


def test_custom_rules_file(loaded: psycopg.Connection, tmp_path: Path) -> None:
    rule = VALID | {"left": ["finnes:ikke"]}
    assert sod.run(loaded, "r1", AS_OF, write_rules(tmp_path, rule)) == []


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ([VALID | {"severity": "severe"}], "unknown severity"),
        ([VALID | {"right": VALID["left"]}], "on both sides"),
        ([{k: v for k, v in VALID.items() if k != "description_nb"}], "missing description_nb"),
        ([VALID, VALID], "duplicate"),
    ],
)
def test_invalid_rules_are_rejected(tmp_path: Path, rules: list[dict], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        sod.load_rules(write_rules(tmp_path, *rules))
