import json
from datetime import timedelta
from pathlib import Path

import pandas as pd
import psycopg
import pytest
import yaml
from conftest import AS_OF

from tilgangsinnsikt import db
from tilgangsinnsikt.synth import generator, plant

LOAD_ORDER = [
    "org_unit",
    "application",
    "identity",
    "entitlement",
    "account",
    "assignment",
    "access_event",
]
WINDOW_START = pd.Timestamp(AS_OF, tz="UTC") - timedelta(days=30)


def read(out: Path, name: str) -> pd.DataFrame:
    df = pd.read_csv(out / f"{name}.csv", keep_default_na=False, na_values=[""])
    for column in ("last_login", "occurred_at"):
        if column in df:
            df[column] = pd.to_datetime(df[column], utc=True)
    return df


def planted(out: Path) -> dict[str, set[str]]:
    truth = json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))
    return {key: set(ids) for key, ids in truth["planted"].items()}


@pytest.fixture(scope="module")
def tables(small_dataset: Path) -> dict[str, pd.DataFrame]:
    return {name: read(small_dataset, name) for name in LOAD_ORDER}


@pytest.fixture(scope="module")
def held(tables) -> pd.DataFrame:
    accounts = tables["account"][["account_id", "identity_id"]]
    return tables["assignment"].merge(accounts, on="account_id")


@pytest.fixture(scope="module")
def people(tables) -> pd.DataFrame:
    units = tables["org_unit"][["unit_id", "unit_type", "region"]]
    return tables["identity"].merge(units, on="unit_id")


def test_same_seed_gives_identical_files(small_dataset: Path, tmp_path: Path) -> None:
    generator.generate(42, generator.SMALL, AS_OF, tmp_path / "same")
    generator.generate(43, generator.SMALL, AS_OF, tmp_path / "other")
    for path in small_dataset.iterdir():
        assert (tmp_path / "same" / path.name).read_bytes() == path.read_bytes(), path.name
    other = (tmp_path / "other" / "access_event.csv").read_bytes()
    assert other != (small_dataset / "access_event.csv").read_bytes()


def test_ground_truth_shape(small_dataset: Path) -> None:
    truth = json.loads((small_dataset / "ground_truth.json").read_text(encoding="utf-8"))
    assert truth["seed"] == 42
    assert truth["size"] == "small"
    assert truth["as_of"] == AS_OF.isoformat()
    assert {key: len(ids) for key, ids in truth["planted"].items()} == generator.SMALL.planted


def test_sizes_and_lifecycle_shares(tables, people) -> None:
    assert len(tables["identity"]) == 2_000
    assert len(tables["access_event"]) == 100_000
    assert (people.status == "leaver").mean() == pytest.approx(0.05, abs=0.005)
    joiners = pd.to_datetime(people.employment_start) > pd.Timestamp(AS_OF - timedelta(days=90))
    assert joiners.mean() == pytest.approx(0.03, abs=0.005)


def test_organisation(tables, people) -> None:
    units = tables["org_unit"]
    assert units.region.nunique() == 12
    assert set(units.unit_type) == {"direktorat", "regionkontor", "lokalkontor", "kontaktsenter"}
    leaders = people[(people.job_title == "leder") & (people.status == "active")]
    assert set(units.unit_id) <= set(leaders.unit_id)

    staff = people[people.job_title != "leder"].merge(
        people[["identity_id", "unit_id", "job_title"]],
        left_on="manager_id",
        right_on="identity_id",
        suffixes=("", "_manager"),
    )
    assert len(staff) == (people.job_title != "leder").sum()
    assert (staff.job_title_manager == "leder").all()
    assert (staff.unit_id_manager == staff.unit_id).all()

    entra = tables["account"][tables["account"].app_id == "entra"]
    assert entra.username.str.fullmatch(r"[a-z0-9-]+@dyo\.example").all()


def test_lifecycle_rules_match_ground_truth(small_dataset: Path, tables, held, people) -> None:
    accounts, entitlements = tables["account"], tables["entitlement"]
    leavers = set(people.identity_id[people.status == "leaver"])
    active = set(people.identity_id[people.status == "active"])
    high = set(entitlements.entitlement_id[entitlements.risk_level == "high"])

    privileged = held[held.entitlement_id.isin(high) & held.identity_id.isin(active)]
    privileged = privileged.merge(accounts[["account_id", "last_login"]], on="account_id")
    cutoff = pd.Timestamp(AS_OF, tz="UTC") - timedelta(days=90)
    dormant = privileged[privileged.last_login.isna() | (privileged.last_login < cutoff)]

    found = {
        "leaver_active_account": set(
            accounts.identity_id[accounts.enabled & accounts.identity_id.isin(leavers)]
        ),
        "leaver_with_entitlements": set(held.identity_id[held.identity_id.isin(leavers)]),
        "orphan_account": set(accounts.account_id[accounts.identity_id.isna() & accounts.enabled]),
        "dormant_privileged": set(dormant.identity_id),
    }
    truth = planted(small_dataset)
    for key, ids in found.items():
        print(f"{key}: found {len(ids)}, planted {len(truth[key])}")
        assert ids == truth[key], key


def test_sod_rule_matches_ground_truth(small_dataset: Path, held, people) -> None:
    rules = yaml.safe_load(plant.RULES_PATH.read_text(encoding="utf-8"))["rules"]
    active = held[held.identity_id.isin(people.identity_id[people.status == "active"])]
    sets = active.groupby("identity_id").entitlement_id.agg(set)
    violators = {
        identity_id
        for identity_id, held_set in sets.items()
        for rule in rules
        if held_set & set(rule["left"]) and held_set & set(rule["right"])
    }
    assert violators == planted(small_dataset)["sod_violation"]


def test_rare_assignments_are_exactly_the_planted_over_provisioning(
    small_dataset: Path, held, people
) -> None:
    active = people[people.status == "active"]
    peer = held[~held.entitlement_id.str.startswith("entra:enhet-")].merge(
        active[["identity_id", "job_title", "unit_type"]], on="identity_id"
    )
    sizes = active.groupby(["job_title", "unit_type"]).size().rename("size").reset_index()
    prevalence = (
        (peer.groupby(["job_title", "unit_type", "entitlement_id"]).size().rename("holders"))
        .reset_index()
        .merge(sizes)
    )
    rare = prevalence[(prevalence["size"] >= 20) & (prevalence.holders < 0.1 * prevalence["size"])]
    flagged = set(peer.merge(rare).identity_id)
    truth = planted(small_dataset)["over_provisioned"]
    print(f"over_provisioned: flagged {len(flagged)}, planted {len(truth)}")
    assert flagged == truth


def test_lookup_rules_match_ground_truth(small_dataset: Path, tables, held, people) -> None:
    events = tables["access_event"].merge(tables["account"][["account_id", "identity_id"]])
    events = events.merge(people[["identity_id", "region", "unit_type"]])
    exempt = set(people.identity_id[people.unit_type.isin(["direktorat", "kontaktsenter"])])
    exempt |= set(held.identity_id[held.entitlement_id == "ytelse:les-alle-regioner"])
    window = events[events.occurred_at >= WINDOW_START]

    stats = (
        window.assign(out=window.subject_region != window.region)
        .groupby("identity_id")
        .agg(n=("out", "size"), out=("out", "sum"), colleague=("subject_is_employee", "sum"))
    )
    out_of_region = stats[(stats.n >= 20) & (stats.out >= 0.25 * stats.n)].index
    daily = events.groupby(["identity_id", events.occurred_at.dt.floor("D")]).size()
    days = pd.date_range(WINDOW_START - timedelta(days=60), WINDOW_START, inclusive="left")
    baseline = daily.unstack(fill_value=0).reindex(columns=days, fill_value=0).median(axis=1)
    peak = daily[daily.index.get_level_values(1) >= WINDOW_START].groupby(level=0).max()
    spikes = peak[(peak >= 50) & (peak >= 5 * baseline.reindex(peak.index, fill_value=0))].index

    truth = planted(small_dataset)
    assert set(out_of_region) - exempt == truth["lookup_out_of_region"]
    assert set(stats.index[stats.colleague >= 5]) == truth["lookup_colleague"]
    assert set(spikes) == truth["lookup_volume_spike"]


def test_csv_files_load_into_schema(small_dataset: Path, conn: psycopg.Connection, tables) -> None:
    db.migrate(conn)
    for name in LOAD_ORDER:
        assert db.copy_csv(conn, name, small_dataset / f"{name}.csv") == len(tables[name])
    conn.commit()
    assert conn.execute("select count(*) from access_event").fetchone()[0] == 100_000
