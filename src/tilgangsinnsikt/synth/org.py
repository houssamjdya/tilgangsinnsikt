"""Fictional organisation for DYO: regions, units, job titles and people."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

REGIONS = [
    "Region Tind",
    "Region Vidda",
    "Region Kyst",
    "Region Skog",
    "Region Elv",
    "Region Dal",
    "Region Hei",
    "Region Myr",
    "Region Bølge",
    "Region Bre",
    "Region Li",
    "Region Eng",
]

DIRECTORATE_SECTIONS = [
    "Direktørens stab",
    "Ytelsesavdelingen",
    "Oppfølgingsavdelingen",
    "IT-avdelingen",
    "Økonomiavdelingen",
    "HR-avdelingen",
    "Analyseavdelingen",
    "Juridisk avdeling",
    "Kommunikasjonsavdelingen",
    "Dokumentasjonsavdelingen",
]

CONTACT_CENTRE_REGIONS = [1, 4, 8]

JOB_TITLES = {
    "veileder": 0.45,
    "saksbehandler": 0.30,
    "rådgiver": 0.08,
    "leder": 0.06,
    "it-konsulent": 0.05,
    "økonomikonsulent": 0.03,
    "hr-konsulent": 0.02,
    "kundeveileder": 0.01,
}

SECTION_FOR_TITLE = {
    "it-konsulent": "IT-avdelingen",
    "økonomikonsulent": "Økonomiavdelingen",
    "hr-konsulent": "HR-avdelingen",
}

LEAVER_SHARE = 0.05
JOINER_SHARE = 0.03

FIRST_NAMES = [
    "Anne", "Inger", "Kari", "Marit", "Ingrid", "Liv", "Eva", "Berit", "Astrid", "Hilde",
    "Nina", "Ida", "Silje", "Maria", "Hanne", "Ragnhild", "Tone", "Camilla", "Emma", "Nora",
    "Sara", "Ingeborg", "Thea", "Julie", "Mari", "Elin", "Kristin", "Sigrid", "Tuva", "Line",
    "Jan", "Per", "Bjørn", "Ole", "Lars", "Kjell", "Knut", "Arne", "Svein", "Hans",
    "Geir", "Morten", "Tor", "Erik", "Terje", "Odd", "Rune", "Trond", "Anders", "Martin",
    "Jonas", "Magnus", "Henrik", "Sindre", "Eirik", "Even", "Håkon", "Sander", "Emil", "Aksel",
]  # fmt: skip

SURNAME_STARTS = [
    "Brams", "Drøv", "Fjask", "Gjelm", "Hask", "Kvim", "Lystr", "Narv",
    "Plim", "Rusk", "Skjelv", "Tind", "Ulst", "Vrål", "Ylv",
]  # fmt: skip
SURNAME_ENDS = ["mo", "vik", "stad", "rud", "dal", "heim", "set", "bø", "nes", "li", "haug", "berg"]

UNIT_COLUMNS = ["unit_id", "name", "region", "unit_type", "parent_id"]


def build_units(local_offices: int) -> pd.DataFrame:
    rows = []
    root = "U-0000-01"
    for s, section in enumerate(DIRECTORATE_SECTIONS, 1):
        parent = None if s == 1 else root
        rows.append(
            (f"U-0000-{s:02d}", f"Direktoratet, {section}", REGIONS[0], "direktorat", parent)
        )
    per_region = [local_offices // len(REGIONS)] * len(REGIONS)
    for r in range(local_offices % len(REGIONS)):
        per_region[r] += 1
    for r, region in enumerate(REGIONS, 1):
        short = region.removeprefix("Region ")
        regional = f"U-{r:02d}00-01"
        rows.append((regional, f"Regionkontor {short}", region, "regionkontor", root))
        for n in range(1, per_region[r - 1] + 1):
            rows.append(
                (
                    f"U-{r:02d}{n:02d}-01",
                    f"Lokalkontor {short} {n}",
                    region,
                    "lokalkontor",
                    regional,
                )
            )
    for r in CONTACT_CENTRE_REGIONS:
        region = REGIONS[r]
        short = region.removeprefix("Region ")
        rows.append(
            (f"U-{r + 1:02d}90-01", f"Kontaktsenter {short}", region, "kontaktsenter", root)
        )
    return pd.DataFrame(rows, columns=UNIT_COLUMNS)


def _split(total: int, shares: dict[str, float]) -> dict[str, int]:
    exact = {k: total * v for k, v in shares.items()}
    counts = {k: int(v) for k, v in exact.items()}
    by_remainder = sorted(shares, key=lambda k: (counts[k] - exact[k], k))
    for k in by_remainder[: total - sum(counts.values())]:
        counts[k] += 1
    return counts


def _place(title: str, n: int, units: pd.DataFrame, local_weights: np.ndarray, rng) -> np.ndarray:
    local = units.loc[units.unit_type == "lokalkontor", "unit_id"].to_numpy()
    regional = units.loc[units.unit_type == "regionkontor", "unit_id"].to_numpy()
    sections = units.loc[units.unit_type == "direktorat"]
    if title in SECTION_FOR_TITLE:
        unit = units.loc[units.name == f"Direktoratet, {SECTION_FOR_TITLE[title]}", "unit_id"]
        return np.repeat(unit.to_numpy(), n)
    if title == "veileder":
        return rng.choice(local, size=n, p=local_weights)
    if title == "kundeveileder":
        return rng.choice(units.loc[units.unit_type == "kontaktsenter", "unit_id"].to_numpy(), n)
    if title == "saksbehandler":
        n_regional = round(n * 0.7)
        return np.concatenate(
            [rng.choice(regional, n_regional), rng.choice(local, n - n_regional, p=local_weights)]
        )
    if title == "rådgiver":
        other_sections = sections.loc[
            ~sections.name.isin([f"Direktoratet, {s}" for s in SECTION_FOR_TITLE.values()]),
            "unit_id",
        ].to_numpy()
        n_dir, n_reg = round(n * 0.5), round(n * 0.25)
        return np.concatenate(
            [
                rng.choice(other_sections, n_dir),
                rng.choice(regional, n_reg),
                rng.choice(local, n - n_dir - n_reg, p=local_weights),
            ]
        )
    raise ValueError(f"no placement rule for {title}")


def _names(n: int, rng) -> list[str]:
    first = rng.choice(FIRST_NAMES, n)
    start = rng.choice(SURNAME_STARTS, n)
    end = rng.choice(SURNAME_ENDS, n)
    return [f"{f} {s}{e}" for f, s, e in zip(first, start, end, strict=True)]


def build_people(n: int, units: pd.DataFrame, as_of: date, rng) -> pd.DataFrame:
    counts = _split(n, JOB_TITLES)
    local_count = int((units.unit_type == "lokalkontor").sum())
    local_weights = rng.lognormal(0.0, 0.4, local_count)
    local_weights /= local_weights.sum()

    staff_units, staff_titles = [], []
    for title, count in counts.items():
        if title == "leder":
            continue
        staff_units.append(_place(title, count, units, local_weights, rng))
        staff_titles += [title] * count
    staff_units = np.concatenate(staff_units)

    unit_ids = units.unit_id.to_numpy()
    leader_total = max(counts["leder"], len(unit_ids))
    sizes = pd.Series(staff_units).value_counts().reindex(unit_ids, fill_value=0).to_numpy()
    extra = rng.choice(unit_ids, leader_total - len(unit_ids), p=(sizes + 1) / (sizes + 1).sum())
    leader_units = np.concatenate([unit_ids, extra])
    is_head = np.r_[np.ones(len(unit_ids), bool), np.zeros(len(extra), bool)]

    people = pd.DataFrame(
        {
            "unit_id": np.concatenate([leader_units, staff_units]),
            "job_title": ["leder"] * leader_total + staff_titles,
            "is_head": np.r_[is_head, np.zeros(len(staff_units), bool)],
        }
    )
    people = people.iloc[rng.permutation(len(people))].reset_index(drop=True)
    people["identity_id"] = [f"P{i:06d}" for i in range(1, len(people) + 1)]
    people["display_name"] = _names(len(people), rng)

    candidates = rng.permutation(np.flatnonzero(~people.is_head.to_numpy()))
    n_leavers, n_joiners = round(len(people) * LEAVER_SHARE), round(len(people) * JOINER_SHARE)
    status = np.full(len(people), "active", dtype=object)
    status[candidates[:n_leavers]] = "leaver"
    joiner = np.zeros(len(people), bool)
    joiner[candidates[n_leavers : n_leavers + n_joiners]] = True

    start, end = [], []
    for i in range(len(people)):
        if status[i] == "leaver":
            e = as_of - timedelta(days=int(rng.integers(1, 731)))
            start.append(e - timedelta(days=int(rng.integers(60, 7301))))
            end.append(e)
        elif joiner[i]:
            start.append(as_of - timedelta(days=int(rng.integers(1, 90))))
            end.append(None)
        else:
            start.append(as_of - timedelta(days=int(rng.integers(120, 7301))))
            end.append(None)
    people["status"] = status
    people["is_joiner"] = joiner
    people["employment_start"] = start
    people["employment_end"] = end
    people["manager_id"] = _managers(people, units, rng)
    return people.merge(units[["unit_id", "unit_type", "region"]], on="unit_id", how="left")


def _managers(people: pd.DataFrame, units: pd.DataFrame, rng) -> list[str | None]:
    heads = dict(
        zip(people.unit_id[people.is_head], people.identity_id[people.is_head], strict=True)
    )
    parent = dict(zip(units.unit_id, units.parent_id, strict=True))
    active_leaders: dict[str, list[str]] = {}
    for unit_id, identity_id, title, status in zip(
        people.unit_id, people.identity_id, people.job_title, people.status, strict=True
    ):
        if title == "leder" and status == "active":
            active_leaders.setdefault(unit_id, []).append(identity_id)

    managers: list[str | None] = []
    for unit_id, title, head in zip(people.unit_id, people.job_title, people.is_head, strict=True):
        if head:
            parent_unit = parent[unit_id]
            managers.append(heads[parent_unit] if pd.notna(parent_unit) else None)
        elif title == "leder":
            managers.append(heads[unit_id])
        else:
            options = sorted(active_leaders[unit_id])
            managers.append(options[int(rng.integers(len(options)))])
    return managers
