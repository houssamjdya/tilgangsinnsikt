"""Access events: baseline lookup behaviour plus the planted lookup anomalies."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from tilgangsinnsikt.synth import applications as apps
from tilgangsinnsikt.synth import org, plant
from tilgangsinnsikt.synth.plant import (
    KIND_COLLEAGUE,
    KIND_IN,
    KIND_NORMAL,
    KIND_OUT,
    LOOKBACK_DAYS,
    WINDOW_DAYS,
)

if TYPE_CHECKING:
    from tilgangsinnsikt.synth.generator import Builder

EXEMPT_UNIT_TYPES = ("direktorat", "kontaktsenter")
MIN_LOOKUPS = 20
COLLEAGUE_MIN = 5
WEEKEND_WEIGHT = 0.05
OWN_REGION_SHARE = 0.98
EXEMPT_OWN_REGION_SHARE = 0.55
OWN_DOMAIN_SHARE = 0.97
COLLEAGUE_RATE = 0.0003
LOOKUP_DOMAINS = ["ytelse", "oppfolging", "arkiv"]


def generate(b: Builder) -> pd.DataFrame:
    rng = b.rng["events"]
    users = [i for i in b.ids if i in b.primary]
    n_users = len(users)
    user_pos = {i: n for n, i in enumerate(users)}
    days = [b.as_of - timedelta(days=LOOKBACK_DAYS - d) for d in range(LOOKBACK_DAYS)]
    weekday = np.array([d.weekday() < 5 for d in days])
    day_weight = np.where(weekday, 1.0, WEEKEND_WEIGHT)
    day_ord = np.array([d.toordinal() for d in days])
    start = np.array([b.person[i]["employment_start"].toordinal() for i in users])
    end = np.array([(b.person[i]["employment_end"] or b.as_of).toordinal() for i in users])
    eligible = (day_ord[None, :] >= start[:, None]) & (day_ord[None, :] <= end[:, None])
    factor = np.array([apps.LOOKUP_RATE_FACTOR[b.person[i]["job_title"]] for i in users])
    rate = rng.lognormal(0.0, 0.6, n_users) * factor

    exempt = np.array([is_exempt(b, i) for i in users])
    expected = b.size.events * rate / (rate * (eligible * day_weight).sum(axis=1)).sum()
    history_ok = start <= (b.as_of - timedelta(days=120)).toordinal()
    regular = [
        i
        for n, i in enumerate(users)
        if not exempt[n] and history_ok[n] and b.person[i]["status"] == "active" and i not in b.used
    ]
    planted_u, planted_d, planted_kind = plant.lookups(
        b, regular, user_pos, expected, weekday, eligible
    )

    n_base = b.size.events - len(planted_u)
    if n_base < 0:
        raise ValueError("--events is too small for the planted lookups")
    weights = (rate[:, None] * day_weight[None, :] * eligible).ravel()
    cells = np.repeat(np.arange(weights.size), rng.multinomial(n_base, weights / weights.sum()))
    u = np.concatenate([cells // LOOKBACK_DAYS, np.array(planted_u, dtype=np.int64)])
    d = np.concatenate([cells % LOOKBACK_DAYS, np.array(planted_d, dtype=np.int64)])
    kind = np.concatenate([np.zeros(cells.size, np.int64), np.array(planted_kind, dtype=np.int64)])
    events = _attributes(b, users, u, d, kind, weekday, days, exempt, rng)
    events = events.sort_values(["occurred_at", "account_id"], kind="stable")
    events = events.reset_index(drop=True)
    _repair(b, events, users, exempt)
    return events


def is_exempt(b: Builder, i: str) -> bool:
    return b.person[i]["unit_type"] in EXEMPT_UNIT_TYPES or apps.EXEMPT_ALL_REGIONS in b.held[i]


def _attributes(b: Builder, users, u, d, kind, weekday, days, exempt, rng) -> pd.DataFrame:
    n = u.size
    region_names = np.array(org.REGIONS)
    region_index = {r: k for k, r in enumerate(org.REGIONS)}
    own_region = np.array([region_index[b.person[i]["region"]] for i in users])[u]
    roll = rng.random(n)
    other_region = (own_region + rng.integers(1, len(org.REGIONS), n)) % len(org.REGIONS)
    own_share = np.where(exempt[u], EXEMPT_OWN_REGION_SHARE, OWN_REGION_SHARE)
    region = np.where(roll < own_share, own_region, other_region)
    region = np.where(kind == KIND_OUT, other_region, region)
    region = np.where((kind == KIND_IN) | (kind == KIND_COLLEAGUE), own_region, region)

    primary = np.array([b.primary[i] for i in users])[u]
    own_domain = np.array([apps.APP_DOMAIN[b.primary[i]] for i in users])[u]
    domain_pick = np.array(LOOKUP_DOMAINS)[rng.integers(0, len(LOOKUP_DOMAINS), n)]
    other_domain = np.where(domain_pick == own_domain, "arkiv", domain_pick)
    domain = np.where(rng.random(n) < OWN_DOMAIN_SHARE, own_domain, other_domain)

    is_employee = (rng.random(n) < COLLEAGUE_RATE) & (kind == KIND_NORMAL)
    is_employee |= kind == KIND_COLLEAGUE
    employee_ref = 90_000_000 + rng.integers(1, b.size.identities + 1, n)
    subject = np.where(is_employee, employee_ref, rng.integers(0, 90_000_000, n))

    weekend = ~weekday[d]
    hour_values = np.arange(7, 19)
    weekday_hours = np.where((hour_values >= 8) & (hour_values < 16), 0.90 / 8, 0.08 / 4)
    hour = np.where(
        weekend,
        rng.integers(9, 16, n),
        rng.choice(hour_values, n, p=weekday_hours / weekday_hours.sum()),
    )
    day_start = np.array([np.datetime64(day, "s") for day in days])[d]
    local = day_start + (hour * 3600 + rng.integers(0, 3600, n)).astype("timedelta64[s]")
    occurred = pd.DatetimeIndex(local).tz_localize("Europe/Oslo").tz_convert("UTC")

    lower = np.array([i.lower() for i in users])[u]
    return pd.DataFrame(
        {
            "account_id": np.char.add(np.char.add(primary, ":"), lower),
            "app_id": primary,
            "occurred_at": occurred,
            "subject_ref": np.char.add("S", np.char.zfill(subject.astype(str), 8)),
            "subject_region": region_names[region],
            "subject_domain": domain,
            "subject_is_employee": is_employee,
            "action": np.where(rng.random(n) < 0.85, "read", "update"),
            "identity_id": np.array(users)[u],
        }
    )


def _repair(b: Builder, events: pd.DataFrame, users: list[str], exempt: np.ndarray) -> None:
    # Pure chance can push an unplanted user across a lookup threshold; pull those
    # users back well below it so the ground truth stays exact.
    window = events[events.occurred_at >= plant.midnight(b.as_of - timedelta(days=WINDOW_DAYS))]
    own = window.identity_id.map({i: p["region"] for i, p in b.person.items()})
    is_out = window.subject_region != own
    skip = set(b.planted["lookup_out_of_region"]) | {
        i for i, e in zip(users, exempt, strict=True) if e
    }
    stats = is_out.groupby(window.identity_id).agg(["size", "sum"])
    offenders = stats[(stats["size"] >= MIN_LOOKUPS) & (stats["sum"] >= 0.20 * stats["size"])]
    flipped_regions = 0
    for i, row in offenders.iterrows():
        if i in skip:
            continue
        out_rows = window.index[(window.identity_id == i) & is_out]
        excess = out_rows[: len(out_rows) - int(0.15 * row["size"])]
        events.loc[excess, "subject_region"] = own[excess]
        flipped_regions += len(excess)

    employee = window[window.subject_is_employee]
    counts = employee.groupby("identity_id").size()
    flipped_colleague = 0
    for i in counts.index[counts >= COLLEAGUE_MIN - 2]:
        if i in b.planted["lookup_colleague"]:
            continue
        rows = employee.index[employee.identity_id == i]
        events.loc[rows, "subject_is_employee"] = False
        events.loc[rows, "subject_ref"] = [f"S{r % 90_000_000:08d}" for r in rows]
        flipped_colleague += len(rows)
    b.repaired = {"subject_region": flipped_regions, "subject_is_employee": flipped_colleague}
