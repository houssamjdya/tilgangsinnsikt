"""Planting of the access weaknesses listed in the ground truth."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import yaml

from tilgangsinnsikt.synth import applications as apps

if TYPE_CHECKING:
    from tilgangsinnsikt.synth.generator import Builder

RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "sod_rules.yaml"

LOOKBACK_DAYS = 180
WINDOW_DAYS = 30
DORMANT_DAYS = 90
# Two weeks of slack keeps planted items on the same side of every time threshold when
# the analyses run a few days after generation.
RECENT_DAYS = 14
MIN_OPTIONAL_SHARE = 0.12
OVER_PROVISION_MIN_GROUP = 40
ORPHAN_PREFIXES = ["konsulent", "tmp", "test", "ekstern", "svc"]

KIND_NORMAL, KIND_OUT, KIND_IN, KIND_COLLEAGUE = 0, 1, 2, 3


def load_sod_rules(path: Path = RULES_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]


def sod_violations(entitlements: set[str], rules: list[dict]) -> list[str]:
    return [
        rule["id"]
        for rule in rules
        if entitlements & set(rule["left"]) and entitlements & set(rule["right"])
    ]


def midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def login_on(day: date, rng) -> datetime:
    return midnight(day) + timedelta(seconds=int(rng.integers(6 * 3600, 17 * 3600)))


def take(b: Builder, pool: list[str], count: int, key: str, rng) -> list[str]:
    if len(pool) < count:
        raise ValueError(f"only {len(pool)} candidates for {key}, need {count}")
    chosen = [pool[j] for j in rng.permutation(len(pool))[:count]]
    b.used.update(chosen)
    return chosen


def sod(b: Builder, count: int) -> None:
    rng = b.rng["plant"]
    sizes, holders = b.prevalence()
    per_rule = [count // len(b.rules)] * len(b.rules)
    for r in range(count % len(b.rules)):
        per_rule[r] += 1
    planted = []
    for rule, k in zip(b.rules, per_rule, strict=True):
        candidates: dict[str, str] = {}
        for i in b.active:
            if i in b.used or i in candidates:
                continue
            held = b.held[i]
            for side, other in ((rule["left"], rule["right"]), (rule["right"], rule["left"])):
                if not held & set(side) or held & set(other):
                    continue
                for e in other:
                    common = holders[(b.group[i], e)] / sizes[b.group[i]]
                    if common >= MIN_OPTIONAL_SHARE and sod_violations(held | {e}, b.rules) == [
                        rule["id"]
                    ]:
                        candidates[i] = e
                        break
                if i in candidates:
                    break
        for i in take(b, sorted(candidates), k, f"sod rule {rule['id']}", rng):
            b.held[i].add(candidates[i])
            b.extra.add((i, candidates[i]))
            planted.append(i)
    b.planted["sod_violation"] = planted


def over_provisioned(b: Builder, count: int) -> None:
    rng = b.rng["plant"]
    sizes, holders = b.prevalence()
    sod_side = {e for rule in b.rules for e in rule["left"] + rule["right"]}
    options = sorted(e for e in apps.RISK if e not in sod_side and e != apps.EXEMPT_ALL_REGIONS)
    eligible = [
        i for i in b.active if i not in b.used and sizes[b.group[i]] >= OVER_PROVISION_MIN_GROUP
    ]
    planted_holders: Counter = Counter()
    planted = []
    for j in rng.permutation(len(eligible)):
        if len(planted) == count:
            break
        i = eligible[j]
        g = b.group[i]
        cap = max(1, int(0.05 * sizes[g]))
        free = [e for e in options if holders[(g, e)] == 0 and planted_holders[(g, e)] < cap]
        k = int(rng.integers(2, 5))
        if len(free) < k:
            continue
        for e in rng.choice(free, k, replace=False):
            e = str(e)
            b.held[i].add(e)
            b.extra.add((i, e))
            planted_holders[(g, e)] += 1
        planted.append(i)
    if len(planted) < count:
        raise ValueError(f"only {len(planted)} candidates for over_provisioned, need {count}")
    b.used.update(planted)
    b.planted["over_provisioned"] = planted


def leavers(b: Builder, n_active: int, n_with: int) -> None:
    rng = b.rng["plant"]
    leaver_ids = [i for i in b.ids if b.person[i]["status"] == "leaver"]
    overlap = round(0.25 * min(n_active, n_with))
    chosen = take(b, leaver_ids, n_active + n_with - overlap, "leavers", rng)
    both, active_only, with_only = chosen[:overlap], chosen[overlap:n_active], chosen[n_active:]

    b.account_apps = {
        i: sorted({e.split(":")[0] for e in b.held[i]} | {"entra", "hrportal"}) for i in b.ids
    }
    b.enabled_leaver_accounts = set()
    for i in both + active_only:
        b.enabled_leaver_accounts.add(f"entra:{i.lower()}")
        others = [a for a in b.account_apps[i] if a != "entra"]
        if others and rng.random() < 0.3:
            b.enabled_leaver_accounts.add(f"{others[int(rng.integers(len(others)))]}:{i.lower()}")
    keep = set(both + with_only)
    for i in leaver_ids:
        if i not in keep:
            b.held[i] = set()
    b.planted["leaver_active_account"] = both + active_only
    b.planted["leaver_with_entitlements"] = both + with_only


def dormant(b: Builder, count: int) -> None:
    rng = b.rng["plant"]
    cutoff = b.as_of - timedelta(days=400)
    candidates: dict[str, str] = {}
    for i in b.active:
        if i in b.used or b.person[i]["employment_start"] > cutoff:
            continue
        for e in sorted(b.held[i]):
            app = e.split(":")[0]
            if apps.RISK.get(e) == "high" and app not in ("entra", b.primary.get(i)):
                candidates[i] = f"{app}:{i.lower()}"
                break
    b.dormant = {}
    chosen = take(b, sorted(candidates), count, "dormant_privileged", rng)
    for i in chosen:
        if rng.random() < 0.3:
            b.dormant[candidates[i]] = None
        else:
            day = b.as_of - timedelta(days=int(rng.integers(DORMANT_DAYS + RECENT_DAYS, 401)))
            b.dormant[candidates[i]] = midnight(day) + timedelta(
                seconds=int(rng.integers(7 * 3600, 17 * 3600))
            )
    b.planted["dormant_privileged"] = chosen


def lookups(
    b: Builder,
    regular: list[str],
    user_pos: dict[str, int],
    expected: np.ndarray,
    weekday: np.ndarray,
    eligible: np.ndarray,
) -> tuple[list[int], list[int], list[int]]:
    """Pick the lookup users and their planted events; blanks their baseline where replaced."""
    rng = b.rng["plant"]
    out_users = take(
        b, regular, b.size.planted["lookup_out_of_region"], "lookup_out_of_region", rng
    )
    regular = [i for i in regular if i not in b.used]
    colleague_users = take(b, regular, b.size.planted["lookup_colleague"], "lookup_colleague", rng)
    quiet = [i for i in regular if i not in b.used and expected[user_pos[i]] <= 2.0]
    spike_users = take(b, quiet, b.size.planted["lookup_volume_spike"], "lookup_volume_spike", rng)
    b.planted["lookup_out_of_region"] = out_users
    b.planted["lookup_colleague"] = colleague_users
    b.planted["lookup_volume_spike"] = spike_users

    window_days = np.flatnonzero(
        weekday & (np.arange(LOOKBACK_DAYS) >= LOOKBACK_DAYS - WINDOW_DAYS)
    )
    recent_days = np.flatnonzero(
        weekday & (np.arange(LOOKBACK_DAYS) >= LOOKBACK_DAYS - RECENT_DAYS)
    )
    planted_u, planted_d, planted_kind = [], [], []
    for i in out_users:
        n = int(rng.integers(60, 101))
        n_out = round(float(rng.uniform(0.40, 0.70)) * n)
        eligible[user_pos[i], LOOKBACK_DAYS - WINDOW_DAYS :] = False
        planted_u += [user_pos[i]] * n
        planted_d += list(rng.choice(window_days, n))
        planted_kind += [KIND_OUT] * n_out + [KIND_IN] * (n - n_out)
    for i in colleague_users:
        n = int(rng.integers(8, 21))
        planted_u += [user_pos[i]] * n
        planted_d += list(rng.choice(recent_days, n))
        planted_kind += [KIND_COLLEAGUE] * n
    for i in spike_users:
        day = int(rng.choice(recent_days))
        n = int(rng.integers(60, 101))
        eligible[user_pos[i], day] = False
        planted_u += [user_pos[i]] * n
        planted_d += [day] * n
        planted_kind += [KIND_NORMAL] * n
    return planted_u, planted_d, planted_kind


def orphans(
    b: Builder, n_enabled: int, columns: dict[str, list[str]]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = b.rng["plant"]
    n_disabled = max(5, round(0.005 * b.size.identities))
    app_ids = [a for a, _, _ in apps.APPLICATIONS]
    by_app: dict[str, list[str]] = {}
    for e in sorted(apps.RISK):
        by_app.setdefault(e.split(":")[0], []).append(e)
    accounts, assignments, enabled = [], [], []
    for n in range(n_enabled + n_disabled):
        app = app_ids[int(rng.integers(len(app_ids)))]
        username = f"{ORPHAN_PREFIXES[int(rng.integers(len(ORPHAN_PREFIXES)))]}-{n + 1:04d}"
        if app == "entra":
            username += "@dyo.example"
        account_id = f"{app}:{username}"
        back = int(rng.integers(1, 301))
        login = None if rng.random() < 0.2 else login_on(b.as_of - timedelta(days=back), rng)
        accounts.append((account_id, None, app, username, n < n_enabled, login, "synthetic"))
        if n < n_enabled:
            enabled.append(account_id)
        if app != "entra":
            e = by_app[app][int(rng.integers(len(by_app[app])))]
            granted = b.as_of - timedelta(days=back + int(rng.integers(30, 700)))
            assignments.append((account_id, e, granted, None))
    b.planted["orphan_account"] = enabled
    return (
        pd.DataFrame(accounts, columns=columns["account"]),
        pd.DataFrame(assignments, columns=columns["assignment"]),
    )
