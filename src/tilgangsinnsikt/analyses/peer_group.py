"""A2: entitlements that are rare among peers, candidate roles and RBAC coverage."""

from collections.abc import Sequence
from datetime import date

import psycopg

from tilgangsinnsikt.analyses.base import (
    ACTIVE_IDENTITY,
    DYNAMIC_ENTITLEMENT_PATTERN,
    Finding,
    nb_count,
    nb_list,
    nb_percent,
    register,
    rows,
    save,
    who,
)

ANALYSIS = "peer_group"
PEER_KEYS = ("job_title", "unit_type")
KEY_COLUMNS = {
    "job_title": "i.job_title",
    "unit_type": "u.unit_type",
    "unit_id": "i.unit_id",
    "region": "u.region",
}
KEY_NAMES_NB = {
    "job_title": "stilling",
    "unit_type": "enhetstype",
    "unit_id": "enhet",
    "region": "region",
}
RARE_PREVALENCE = 0.10
ROLE_PREVALENCE = 0.80
MIN_PEER_GROUP = 20


def _prevalence_sql(keys: Sequence[str]) -> str:
    columns = ", ".join(f"{KEY_COLUMNS[k]} as {k}" for k in keys)
    group = ", ".join(keys)
    return f"""
        with member as (
            select i.identity_id, i.display_name, {columns}
            from identity i
            join org_unit u using (unit_id)
            where {ACTIVE_IDENTITY}
        ),
        held as (
            select a.identity_id, s.entitlement_id,
                   min(s.granted_at) as granted_at, min(s.granted_by) as granted_by
            from assignment s
            join account a using (account_id)
            where a.enabled and s.entitlement_id not like %(exclude)s
            group by a.identity_id, s.entitlement_id
        ),
        peer_group as (
            select {group}, count(*) as size
            from member
            group by {group}
            having count(*) >= %(min_group)s
        ),
        prevalence as (
            select {group}, g.size, h.entitlement_id, count(*) as holders
            from member m
            join peer_group g using ({group})
            join held h using (identity_id)
            group by {group}, g.size, h.entitlement_id
        ),
        candidate_role as (
            select {group}, array_agg(entitlement_id order by entitlement_id) as entitlements
            from prevalence
            where holders >= %(role)s * size
            group by {group}
        )
    """


@register(ANALYSIS)
def run(
    conn: psycopg.Connection,
    run_id: str,
    as_of: date,
    peer_keys: Sequence[str] = PEER_KEYS,
    exclude: str = DYNAMIC_ENTITLEMENT_PATTERN,
) -> list[Finding]:
    if unknown := [k for k in peer_keys if k not in KEY_COLUMNS]:
        raise ValueError(f"unknown peer group keys: {', '.join(unknown)}")
    keys = list(peer_keys)
    group = ", ".join(keys)
    params = {
        "as_of": as_of,
        "exclude": exclude,
        "min_group": MIN_PEER_GROUP,
        "rare": RARE_PREVALENCE,
        "role": ROLE_PREVALENCE,
    }
    base = _prevalence_sql(keys)
    rare_query = f"""{base}
        select m.identity_id, m.display_name, {", ".join(f"m.{k}" for k in keys)}, r.size,
               coalesce(c.entitlements, '{{}}') as candidate_role,
               bool_or(e.risk_level = 'high') as any_high,
               array_agg(e.name || ' (' || p.name || ')' order by e.entitlement_id) as labels,
               jsonb_agg(jsonb_build_object(
                   'entitlement_id', e.entitlement_id,
                   'risk_level', e.risk_level,
                   'holders', r.holders,
                   'prevalence', round(r.holders::numeric / r.size, 3),
                   'granted_at', h.granted_at,
                   'granted_by', h.granted_by
               ) order by e.entitlement_id) as rare
        from member m
        join held h using (identity_id)
        join prevalence r using ({group}, entitlement_id)
        join entitlement e using (entitlement_id)
        join application p on p.app_id = e.app_id
        left join candidate_role c using ({group})
        where r.holders < %(rare)s * r.size
        group by m.identity_id, m.display_name, {", ".join(f"m.{k}" for k in keys)}, r.size,
                 c.entitlements
        order by m.identity_id
    """
    summary_query = f"""{base}
        select
            (select count(*) from member join held using (identity_id)) as assignments,
            (select count(*)
             from member m
             join held h using (identity_id)
             join candidate_role c using ({group})
             where h.entitlement_id = any(c.entitlements)) as explained,
            (select coalesce(jsonb_agg(jsonb_build_object(
                        {", ".join(f"'{k}', g.{k}" for k in keys)},
                        'size', g.size,
                        'candidate_role', coalesce(c.entitlements, '{{}}')
                    ) order by {", ".join(f"g.{k}" for k in keys)}), '[]')
             from peer_group g
             left join candidate_role c using ({group})) as peer_groups
    """
    with conn.transaction():
        findings = [_rare_finding(row, keys) for row in rows(conn, rare_query, params)]
        findings.append(_coverage_finding(rows(conn, summary_query, params)[0], keys))
        save(conn, run_id, findings)
    return findings


def _rare_finding(row: dict, keys: list[str]) -> Finding:
    rare = row["rare"]
    group = ", ".join(str(row[k]) for k in keys)
    return Finding(
        ANALYSIS,
        "over_provisioned",
        "high" if row["any_high"] else "medium",
        "Rettigheter som nesten ingen i samme rolle har",
        f"{who(row['display_name'], row['identity_id'])} har "
        f"{nb_count(len(rare), 'rettighet', 'rettigheter')} som færre enn "
        f"{nb_percent(RARE_PREVALENCE)} av de {row['size']} i samme gruppe ({group}) har: "
        f"{nb_list(row['labels'])}. Slike tildelinger er ofte rester etter tidligere oppgaver "
        "eller gitt ved en feil. Be lederen bekrefte behovet, eller fjern rettighetene.",
        {
            "peer_group": {k: row[k] for k in keys} | {"size": row["size"]},
            "rare": rare,
            "candidate_role": row["candidate_role"],
        },
        identity_id=row["identity_id"],
        entitlement_id=rare[0]["entitlement_id"] if len(rare) == 1 else None,
    )


def _coverage_finding(row: dict, keys: list[str]) -> Finding:
    total, explained = row["assignments"], row["explained"]
    coverage = explained / total if total else 0.0
    grouping = " og ".join(KEY_NAMES_NB[k] for k in keys)
    return Finding(
        ANALYSIS,
        "rbac_coverage",
        "low",
        "Rolledekning",
        f"{nb_percent(coverage)} av tildelingene til aktive identiteter ({explained} av "
        f"{total}) dekkes av kandidatroller, altså rettigheter som minst "
        f"{nb_percent(ROLE_PREVALENCE)} i samme gruppe ({grouping}) har. Resten er "
        "tildelt enkeltvis og må vurderes én for én ved attestering. Kandidatrollene per "
        "gruppe er et utgangspunkt for rollemodellering.",
        {
            "coverage": round(coverage, 4),
            "assignments": total,
            "explained": explained,
            "peer_keys": keys,
            "peer_groups": row["peer_groups"],
        },
    )
