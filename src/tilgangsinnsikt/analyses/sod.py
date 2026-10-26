"""A3: segregation of duties (arbeidsdeling) checked against rules/sod_rules.yaml."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg
import yaml

from tilgangsinnsikt.analyses.base import (
    ACTIVE_IDENTITY,
    SEVERITIES,
    Finding,
    nb_date,
    nb_list,
    register,
    rows,
    save,
    who,
)

ANALYSIS = "sod"
RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "sod_rules.yaml"
RULE_KEYS = ("id", "severity", "description_nb", "left", "right")


@dataclass(frozen=True)
class SodRule:
    id: str
    severity: str
    description_nb: str
    left: tuple[str, ...]
    right: tuple[str, ...]


def load_rules(path: Path = RULES_PATH) -> list[SodRule]:
    rules = []
    for n, entry in enumerate(yaml.safe_load(path.read_text(encoding="utf-8"))["rules"], 1):
        if missing := [key for key in RULE_KEYS if not entry.get(key)]:
            raise ValueError(f"SoD rule {n} in {path.name} is missing {', '.join(missing)}")
        rule = SodRule(
            str(entry["id"]),
            entry["severity"],
            entry["description_nb"],
            tuple(entry["left"]),
            tuple(entry["right"]),
        )
        if rule.severity not in SEVERITIES:
            raise ValueError(f"SoD rule {rule.id} has unknown severity {rule.severity!r}")
        if overlap := set(rule.left) & set(rule.right):
            raise ValueError(f"SoD rule {rule.id} has {', '.join(sorted(overlap))} on both sides")
        rules.append(rule)
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError(f"duplicate SoD rule ids in {path.name}")
    return rules


@register(ANALYSIS)
def run(
    conn: psycopg.Connection, run_id: str, as_of: date, rules_path: Path = RULES_PATH
) -> list[Finding]:
    rules = {rule.id: rule for rule in load_rules(rules_path)}
    sides = [
        (rule.id, side, e)
        for rule in rules.values()
        for side, entitlements in (("left", rule.left), ("right", rule.right))
        for e in entitlements
    ]
    query = f"""
        with rule_side (rule_id, side, entitlement_id) as (
            select * from unnest(%(rule_ids)s::text[], %(sides)s::text[], %(entitlements)s::text[])
        ),
        held as (
            select i.identity_id, i.display_name, r.rule_id, r.side, s.entitlement_id,
                   s.account_id, s.granted_at, e.name || ' (' || p.name || ')' as label
            from rule_side r
            join assignment s using (entitlement_id)
            join entitlement e using (entitlement_id)
            join application p on p.app_id = e.app_id
            join account a using (account_id)
            join identity i using (identity_id)
            where a.enabled and {ACTIVE_IDENTITY}
        )
        select identity_id, display_name, rule_id,
               array_agg(distinct label) filter (where side = 'left') as left_labels,
               array_agg(distinct label) filter (where side = 'right') as right_labels,
               array_agg(distinct entitlement_id) filter (where side = 'left') as left_held,
               array_agg(distinct entitlement_id) filter (where side = 'right') as right_held,
               array_agg(distinct account_id) as accounts,
               greatest(
                   min(granted_at) filter (where side = 'left'),
                   min(granted_at) filter (where side = 'right')
               ) as since
        from held
        group by identity_id, display_name, rule_id
        having bool_or(side = 'left') and bool_or(side = 'right')
        order by identity_id, rule_id
    """
    params = {
        "as_of": as_of,
        "rule_ids": [s[0] for s in sides],
        "sides": [s[1] for s in sides],
        "entitlements": [s[2] for s in sides],
    }
    with conn.transaction():
        findings = []
        for row in rows(conn, query, params):
            rule = rules[row["rule_id"]]
            findings.append(
                Finding(
                    ANALYSIS,
                    "sod_violation",
                    rule.severity,
                    "Brudd på arbeidsdeling",
                    f"{who(row['display_name'], row['identity_id'])} har både "
                    f"{nb_list(row['left_labels'])} og {nb_list(row['right_labels'])}, og har "
                    f"hatt kombinasjonen siden {nb_date(row['since'])}. {rule.description_nb} "
                    "Fjern en av rettighetene, eller dokumenter et kompenserende tiltak hvis "
                    "kombinasjonen er nødvendig.",
                    {
                        "rule": rule.id,
                        "left": row["left_held"],
                        "right": row["right_held"],
                        "accounts": row["accounts"],
                        "since": row["since"].isoformat(),
                    },
                    identity_id=row["identity_id"],
                )
            )
        save(conn, run_id, findings)
    return findings
