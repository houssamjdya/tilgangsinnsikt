from datetime import date, timedelta

import psycopg
from conftest import AS_OF

from tilgangsinnsikt.analyses import REGISTRY, lifecycle
from tilgangsinnsikt.evaluation import score

EXACT_RULES = [
    "leaver_active_account",
    "leaver_with_entitlements",
    "orphan_account",
    "dormant_privileged",
]


def test_lifecycle_matches_ground_truth(
    loaded: psycopg.Connection, ground_truth: dict[str, list[str]]
) -> None:
    findings = lifecycle.run(loaded, "r1", AS_OF)
    for rule in EXACT_RULES:
        result = score(rule, findings, ground_truth[rule])
        print(result)
        assert result.precision == result.recall == 1.0, result


def test_findings_are_stored_with_run_id(loaded: psycopg.Connection) -> None:
    findings = REGISTRY["lifecycle"](loaded, "r1", AS_OF)
    stored = loaded.execute(
        "select count(*), count(distinct run_id), bool_and(analysis = 'lifecycle')"
        " from finding where run_id = 'r1'"
    ).fetchone()
    assert stored == (len(findings), 1, True)
    assert all(f.title_nb and f.explanation_nb for f in findings)


def test_dormant_is_critical_only_when_never_used(loaded: psycopg.Connection) -> None:
    dormant = [f for f in lifecycle.run(loaded, "r1", AS_OF) if f.rule == "dormant_privileged"]
    severities = {f.severity for f in dormant}
    assert severities == {"medium", "critical"}
    for f in dormant:
        never = any(p["last_login"] is None for p in f.evidence["privileges"])
        assert f.severity == ("critical" if never else "medium")


def test_time_windows_follow_as_of(
    loaded: psycopg.Connection, ground_truth: dict[str, list[str]]
) -> None:
    later = lifecycle.run(loaded, "r2", AS_OF + timedelta(days=120))
    dormant = {f.identity_id for f in later if f.rule == "dormant_privileged"}
    assert dormant > set(ground_truth["dormant_privileged"])


def test_joiners_miss_only_entitlements_they_lack(loaded: psycopg.Connection) -> None:
    joiners = [f for f in lifecycle.run(loaded, "r1", AS_OF) if f.rule == "joiner_without_baseline"]
    assert joiners
    for f in joiners:
        started = AS_OF - date.fromisoformat(f.evidence["employment_start"])
        assert timedelta(days=14) < started < timedelta(days=90)
        held = {
            row[0]
            for row in loaded.execute(
                "select s.entitlement_id from assignment s join account a using (account_id)"
                " where a.identity_id = %s",
                (f.identity_id,),
            )
        }
        assert f.evidence["missing"] and not held & set(f.evidence["missing"])
