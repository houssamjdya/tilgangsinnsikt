from pathlib import Path

import pandas as pd
import psycopg
import pytest
from conftest import AS_OF

from tilgangsinnsikt.analyses import peer_group
from tilgangsinnsikt.evaluation import score


def read(out: Path, name: str) -> pd.DataFrame:
    return pd.read_csv(out / f"{name}.csv")


def findings_by_rule(conn: psycopg.Connection, **kwargs) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in peer_group.run(conn, "r1", AS_OF, **kwargs):
        out.setdefault(f.rule, []).append(f)
    return out


def test_peer_group_matches_ground_truth(
    loaded: psycopg.Connection, ground_truth: dict[str, list[str]]
) -> None:
    findings = peer_group.run(loaded, "r1", AS_OF)
    result = score("over_provisioned", findings, ground_truth["over_provisioned"])
    print(result)
    assert result.recall >= 0.9 and result.precision >= 0.8, result


def test_severity_follows_the_riskiest_rare_entitlement(loaded: psycopg.Connection) -> None:
    for f in findings_by_rule(loaded)["over_provisioned"]:
        high = any(r["risk_level"] == "high" for r in f.evidence["rare"])
        assert f.severity == ("high" if high else "medium")
        assert all(r["prevalence"] < 0.10 for r in f.evidence["rare"])
        assert f.evidence["peer_group"]["size"] >= 20
        assert not any(r["entitlement_id"].startswith("entra:enhet-") for r in f.evidence["rare"])


def test_candidate_role_and_coverage(loaded: psycopg.Connection, small_dataset: Path) -> None:
    [summary] = findings_by_rule(loaded)["rbac_coverage"]
    groups = {(g["job_title"], g["unit_type"]): g for g in summary.evidence["peer_groups"]}
    assert set(groups[("veileder", "lokalkontor")]["candidate_role"]) == {
        "arkiv:les-egen-region",
        "hrportal:ansatt",
        "oppfolging:veileder",
        "personregister:les",
    }

    units = read(small_dataset, "org_unit")[["unit_id", "unit_type"]]
    people = read(small_dataset, "identity").merge(units, on="unit_id")
    people = people[people.status == "active"]
    accounts = read(small_dataset, "account")[["account_id", "identity_id"]]
    held = read(small_dataset, "assignment").merge(accounts)
    held = held[~held.entitlement_id.str.startswith("entra:enhet-")]
    held = held.merge(people[["identity_id", "job_title", "unit_type"]])
    held = held.drop_duplicates(["identity_id", "entitlement_id"])
    roles = {
        (g["job_title"], g["unit_type"], e)
        for g in summary.evidence["peer_groups"]
        for e in g["candidate_role"]
    }
    explained = sum(
        (t, u, e) in roles
        for t, u, e in zip(held.job_title, held.unit_type, held.entitlement_id, strict=True)
    )
    print(f"rbac_coverage: {explained} of {len(held)} = {explained / len(held):.3f}")
    assert summary.evidence["assignments"] == len(held)
    assert summary.evidence["explained"] == explained


def test_peer_keys_are_configurable(loaded: psycopg.Connection) -> None:
    findings = findings_by_rule(loaded, peer_keys=["job_title"])
    assert findings["over_provisioned"]
    assert all(
        set(f.evidence["peer_group"]) == {"job_title", "size"} for f in findings["over_provisioned"]
    )
    with pytest.raises(ValueError, match="unknown peer group keys"):
        peer_group.run(loaded, "r1", AS_OF, peer_keys=["job_title", "shoe_size"])


def test_dynamic_groups_are_excluded_only_through_the_pattern(loaded: psycopg.Connection) -> None:
    findings = findings_by_rule(loaded, exclude="no-match")["over_provisioned"]
    assert any(
        r["entitlement_id"].startswith("entra:enhet-") for f in findings for r in f.evidence["rare"]
    )
