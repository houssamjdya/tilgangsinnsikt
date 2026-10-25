"""Synthetic DYO dataset with planted access weaknesses and a ground-truth file.

The organisation, assignments and logins are built here; planting and access events live
in plant.py and events.py.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from tilgangsinnsikt.synth import applications as apps
from tilgangsinnsikt.synth import events, org, plant
from tilgangsinnsikt.synth.seeds import streams

PLANTED_KEYS = [
    "leaver_active_account",
    "leaver_with_entitlements",
    "orphan_account",
    "dormant_privileged",
    "over_provisioned",
    "sod_violation",
    "lookup_out_of_region",
    "lookup_colleague",
    "lookup_volume_spike",
]


@dataclass(frozen=True)
class Size:
    name: str
    identities: int
    events: int
    planted: dict[str, int]


FULL_COUNTS = [40, 60, 25, 50, 80, 25, 30, 15, 10]
SMALL_COUNTS = [8, 10, 5, 8, 12, 5, 6, 3, 2]
FULL = Size("full", 22_000, 2_000_000, dict(zip(PLANTED_KEYS, FULL_COUNTS, strict=True)))
SMALL = Size("small", 2_000, 100_000, dict(zip(PLANTED_KEYS, SMALL_COUNTS, strict=True)))

TABLE_COLUMNS = {
    "org_unit": org.UNIT_COLUMNS,
    "application": ["app_id", "name", "domain"],
    "identity": [
        "identity_id",
        "display_name",
        "unit_id",
        "job_title",
        "manager_id",
        "employment_start",
        "employment_end",
        "status",
    ],
    "entitlement": ["entitlement_id", "app_id", "name", "description", "risk_level"],
    "account": [
        "account_id",
        "identity_id",
        "app_id",
        "username",
        "enabled",
        "last_login",
        "source",
    ],
    "assignment": ["account_id", "entitlement_id", "granted_at", "granted_by"],
    "access_event": [
        "account_id",
        "app_id",
        "occurred_at",
        "subject_ref",
        "subject_region",
        "subject_domain",
        "subject_is_employee",
        "action",
    ],
}


BASELINE_DAYS = 60
OUT_OF_REGION_SHARE = 0.25
SPIKE_FACTOR = 5
SPIKE_MIN = 50
IDENTITIES_PER_LOCAL_OFFICE = 88


@dataclass
class Dataset:
    seed: int
    size: Size
    as_of: date
    tables: dict[str, pd.DataFrame]
    planted: dict[str, list[str]]
    repaired: dict[str, int] = field(default_factory=dict)

    def ground_truth(self) -> dict:
        return {
            "seed": self.seed,
            "size": self.size.name,
            "as_of": self.as_of.isoformat(),
            "planted": {key: self.planted[key] for key in PLANTED_KEYS},
        }


class Builder:
    def __init__(self, seed: int, size: Size, as_of: date) -> None:
        self.seed, self.size, self.as_of = seed, size, as_of
        self.rng = streams(seed)
        self.rules = plant.load_sod_rules()
        self.used: set[str] = set()
        self.planted: dict[str, list[str]] = {}
        self.extra: set[tuple[str, str]] = set()
        self.repaired: dict[str, int] = {}
        self.account_apps: dict[str, list[str]] = {}
        self.enabled_leaver_accounts: set[str] = set()
        self.dormant: dict[str, datetime | None] = {}

    def build(self) -> Dataset:
        local_offices = round(self.size.identities / IDENTITIES_PER_LOCAL_OFFICE)
        self.units = org.build_units(max(len(org.REGIONS), local_offices))
        self.people = org.build_people(
            self.size.identities, self.units, self.as_of, self.rng["people"]
        )
        self.person = self.people.set_index("identity_id").to_dict("index")
        self.ids = sorted(self.people.identity_id)
        self.active = [i for i in self.ids if self.person[i]["status"] == "active"]
        self.group = {
            i: (self.person[i]["job_title"], self.person[i]["unit_type"]) for i in self.ids
        }

        self._assign_baseline()
        plant.sod(self, self.size.planted["sod_violation"])
        plant.over_provisioned(self, self.size.planted["over_provisioned"])
        plant.leavers(
            self,
            self.size.planted["leaver_active_account"],
            self.size.planted["leaver_with_entitlements"],
        )
        self._primary_apps()
        plant.dormant(self, self.size.planted["dormant_privileged"])
        access = events.generate(self)
        accounts = self._accounts(access)
        orphans, orphan_assignments = plant.orphans(
            self, self.size.planted["orphan_account"], TABLE_COLUMNS
        )
        tables = {
            "org_unit": self.units,
            "application": pd.DataFrame(apps.APPLICATIONS, columns=TABLE_COLUMNS["application"]),
            "identity": self.people[TABLE_COLUMNS["identity"]].sort_values("identity_id"),
            "entitlement": self._entitlements(),
            "account": pd.concat([accounts, orphans], ignore_index=True),
            "assignment": pd.concat([self._assignments(), orphan_assignments], ignore_index=True),
            "access_event": access[TABLE_COLUMNS["access_event"]],
        }
        tables = {name: df.reset_index(drop=True) for name, df in tables.items()}
        tables["account"]["last_login"] = pd.to_datetime(tables["account"].last_login, utc=True)
        planted = {key: sorted(self.planted[key]) for key in PLANTED_KEYS}
        return Dataset(self.seed, self.size, self.as_of, tables, planted, self.repaired)

    def prevalence(self) -> tuple[Counter, Counter]:
        sizes: Counter = Counter(self.group[i] for i in self.active)
        holders: Counter = Counter()
        for i in self.active:
            for e in self.held[i]:
                holders[(self.group[i], e)] += 1
        return sizes, holders

    def _assign_baseline(self) -> None:
        rng = self.rng["assignments"]
        self.held: dict[str, set[str]] = {i: set() for i in self.ids}
        for (title, unit_type), group in self.people.groupby(["job_title", "unit_type"]):
            grants = apps.COMMON + apps.ROLE_MATRIX.get((title, unit_type), [])
            active = sorted(group.identity_id[group["status"] == "active"])
            leavers = sorted(group.identity_id[group["status"] == "leaver"])
            for grant in grants:
                shares = {
                    e: float(rng.uniform(0.90, 0.98)) if s is None else s
                    for e, s in grant.shares.items()
                }
                pool = [
                    i for i in active if grant.requires is None or grant.requires in self.held[i]
                ]
                order = rng.permutation(len(pool))
                offset = 0
                for e, share in shares.items():
                    k = round(share * len(pool))
                    for j in order[offset : offset + k]:
                        self.held[pool[j]].add(e)
                    offset += k
                for i in leavers:
                    if grant.requires and grant.requires not in self.held[i]:
                        continue
                    draw, cumulative = rng.random(), 0.0
                    for e, share in shares.items():
                        cumulative += share
                        if draw < cumulative:
                            self.held[i].add(e)
                            break
        for i in self.ids:
            self.held[i].add(apps.UNIT_GROUP_PREFIX + self.person[i]["unit_id"])

    def _primary_apps(self) -> None:
        self.primary: dict[str, str] = {}
        for i in self.ids:
            for app in apps.LOOKUP_APPS.get(self.person[i]["job_title"], []):
                if app in self.account_apps[i]:
                    self.primary[i] = app
                    break

    def _accounts(self, events: pd.DataFrame) -> pd.DataFrame:
        rng = self.rng["logins"]
        last_event = events.groupby("account_id").occurred_at.max().to_dict()
        privileged = {
            i for i in self.active if any(apps.RISK.get(e) == "high" for e in self.held[i])
        }
        rows = []
        for i in self.ids:
            p = self.person[i]
            start, end = p["employment_start"], p["employment_end"]
            for app in self.account_apps[i]:
                account_id = f"{app}:{i.lower()}"
                if account_id in self.dormant:
                    login = self.dormant[account_id]
                elif p["status"] == "leaver":
                    latest = max(start, end - timedelta(days=int(rng.integers(0, 31))))
                    login = plant.login_on(latest, rng)
                else:
                    horizon = 45 if i in privileged else 14 if app == "entra" else 200
                    back = int(rng.integers(1, max(2, min(horizon, (self.as_of - start).days) + 1)))
                    login = plant.login_on(self.as_of - timedelta(days=back), rng)
                    if i not in privileged and app != "entra" and rng.random() < 0.05:
                        login = None
                if account_id in last_event:
                    seen = last_event[account_id].to_pydatetime()
                    login = seen if login is None else max(login, seen)
                rows.append(
                    (
                        account_id,
                        i,
                        app,
                        f"{i.lower()}@dyo.example" if app == "entra" else i.lower(),
                        p["status"] == "active" or account_id in self.enabled_leaver_accounts,
                        login,
                        "synthetic",
                    )
                )
        return pd.DataFrame(rows, columns=TABLE_COLUMNS["account"])

    def _assignments(self) -> pd.DataFrame:
        rng = self.rng["assignments"]
        rows = []
        for i in self.ids:
            p = self.person[i]
            start = p["employment_start"]
            until = p["employment_end"] or self.as_of - timedelta(days=1)
            for e in sorted(self.held[i]):
                account_id = f"{e.split(':')[0]}:{i.lower()}"
                if e.startswith(apps.UNIT_GROUP_PREFIX):
                    rows.append((account_id, e, start, None))
                    continue
                if (i, e) in self.extra:
                    back = int(rng.integers(plant.RECENT_DAYS, 366))
                    granted = max(start, self.as_of - timedelta(days=back))
                else:
                    granted = start + timedelta(days=int(rng.integers(0, (until - start).days + 1)))
                granted_by = p["manager_id"] if rng.random() < 0.9 else None
                rows.append((account_id, e, granted, granted_by))
        return pd.DataFrame(rows, columns=TABLE_COLUMNS["assignment"])

    def _entitlements(self) -> pd.DataFrame:
        rows = [
            (e, e.split(":")[0], e.split(":", 1)[1], description, risk)
            for e, risk, description in apps.ENTITLEMENTS
        ]
        rows += [
            (apps.UNIT_GROUP_PREFIX + u, "entra", f"enhet-{u}", apps.UNIT_GROUP_DESCRIPTION, "low")
            for u in self.units.unit_id
        ]
        return pd.DataFrame(rows, columns=TABLE_COLUMNS["entitlement"])


def build(seed: int, size: Size, as_of: date) -> Dataset:
    dataset = Builder(seed, size, as_of).build()
    mismatches = {
        key: (len(found), len(dataset.planted[key]))
        for key, found in rule_hits(dataset.tables, as_of).items()
        if found != set(dataset.planted[key])
    }
    if mismatches:
        raise RuntimeError(f"planted findings do not match the data (found, planted): {mismatches}")
    return dataset


def rule_hits(tables: dict[str, pd.DataFrame], as_of: date) -> dict[str, set[str]]:
    """Which identities or accounts meet each planted rule, computed from the tables alone."""
    identity, account = tables["identity"], tables["account"]
    entitlement, access = tables["entitlement"], tables["access_event"]
    held = tables["assignment"].merge(account[["account_id", "identity_id"]], on="account_id")
    leavers = set(identity.identity_id[identity.status == "leaver"])
    active = identity[identity.status == "active"]
    high = set(entitlement.entitlement_id[entitlement.risk_level == "high"])

    privileged = held[held.entitlement_id.isin(high) & held.identity_id.isin(active.identity_id)]
    privileged = privileged.merge(account[["account_id", "last_login"]], on="account_id")
    stale_before = plant.midnight(as_of - timedelta(days=plant.DORMANT_DAYS))
    stale = privileged[privileged.last_login.isna() | (privileged.last_login < stale_before)]

    rules = plant.load_sod_rules()
    sets = held[held.identity_id.isin(active.identity_id)].groupby("identity_id").entitlement_id
    sod = {i for i, s in sets.agg(set).items() if plant.sod_violations(s, rules)}

    peer = active[["identity_id", "job_title", "unit_id"]].merge(
        tables["org_unit"][["unit_id", "unit_type"]], on="unit_id"
    )
    peer_held = held[~held.entitlement_id.str.startswith(apps.UNIT_GROUP_PREFIX)].merge(
        peer, on="identity_id"
    )
    group_size = peer.groupby(["job_title", "unit_type"]).size().rename("group_size")
    prevalence = (
        peer_held.groupby(["job_title", "unit_type", "entitlement_id"])
        .identity_id.nunique()
        .rename("holders")
        .reset_index()
        .merge(group_size.reset_index(), on=["job_title", "unit_type"])
    )
    rare = prevalence[
        (prevalence.group_size >= 20) & (prevalence.holders < 0.10 * prevalence.group_size)
    ]
    over = set(peer_held.merge(rare, on=["job_title", "unit_type", "entitlement_id"]).identity_id)

    return {
        "leaver_active_account": set(
            account.identity_id[account.enabled & account.identity_id.isin(leavers)]
        ),
        "leaver_with_entitlements": set(held.identity_id[held.identity_id.isin(leavers)]),
        "orphan_account": set(account.account_id[account.identity_id.isna() & account.enabled]),
        "dormant_privileged": set(stale.identity_id),
        "over_provisioned": over,
        "sod_violation": sod,
        **_lookup_hits(identity, account, tables, access, held, as_of),
    }


def _lookup_hits(identity, account, tables, access, held, as_of) -> dict[str, set[str]]:
    people = identity.merge(tables["org_unit"][["unit_id", "unit_type", "region"]], on="unit_id")
    exempt = set(people.identity_id[people.unit_type.isin(events.EXEMPT_UNIT_TYPES)])
    exempt |= set(held.identity_id[held.entitlement_id == apps.EXEMPT_ALL_REGIONS])
    ev = access.merge(account[["account_id", "identity_id"]], on="account_id")
    ev = ev.merge(people[["identity_id", "region"]], on="identity_id")
    window_start = plant.midnight(as_of - timedelta(days=plant.WINDOW_DAYS))
    baseline_start = window_start - timedelta(days=BASELINE_DAYS)
    window = ev[(ev.occurred_at >= window_start) & (ev.occurred_at < plant.midnight(as_of))]

    per_user = window.assign(out=window.subject_region != window.region).groupby("identity_id")
    stats = per_user.agg(
        n=("out", "size"), out=("out", "sum"), colleague=("subject_is_employee", "sum")
    )
    out_of_region = stats[
        (stats.n >= events.MIN_LOOKUPS)
        & (stats.out >= OUT_OF_REGION_SHARE * stats.n)
        & ~stats.index.isin(exempt)
    ]

    day = ev.occurred_at.dt.floor("D")
    daily = ev.assign(day=day).groupby(["identity_id", "day"]).size()
    baseline_days = pd.date_range(baseline_start, window_start, freq="D", inclusive="left")
    baseline = daily[daily.index.get_level_values("day") >= baseline_start]
    baseline = baseline[baseline.index.get_level_values("day") < window_start].unstack(fill_value=0)
    baseline = baseline.reindex(columns=baseline_days, fill_value=0)
    median = baseline.median(axis=1).reindex(stats.index, fill_value=0)
    in_window = (
        daily[daily.index.get_level_values("day") >= window_start].groupby("identity_id").max()
    )
    spikes = in_window[
        (in_window >= SPIKE_MIN)
        & (in_window >= SPIKE_FACTOR * median.reindex(in_window.index, fill_value=0))
    ]

    return {
        "lookup_out_of_region": set(out_of_region.index),
        "lookup_colleague": set(stats.index[stats.colleague >= events.COLLEAGUE_MIN]),
        "lookup_volume_spike": set(spikes.index),
    }


def _csv_ready(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column in out.columns:
        values = out[column]
        if isinstance(values.dtype, pd.DatetimeTZDtype):
            stamps = values.dt.tz_convert(None).to_numpy().astype("datetime64[s]")
            text = np.datetime_as_string(stamps, unit="s")
            out[column] = np.where(np.isnat(stamps), "", np.char.add(text, "Z"))
        elif values.dtype == bool:
            out[column] = np.where(values, "true", "false")
    return out


def write(dataset: Dataset, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in dataset.tables.items():
        _csv_ready(df).to_csv(out_dir / f"{name}.csv", index=False, lineterminator="\n")
    (out_dir / "ground_truth.json").write_text(
        json.dumps(dataset.ground_truth(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def generate(seed: int, size: Size, as_of: date, out_dir: Path) -> Dataset:
    dataset = build(seed, size, as_of)
    write(dataset, out_dir)
    return dataset
