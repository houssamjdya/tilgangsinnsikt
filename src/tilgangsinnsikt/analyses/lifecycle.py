"""A1: lifecycle weaknesses for leavers, orphan accounts, dormant privileges and joiners."""

from datetime import date, timedelta
from typing import Any

import psycopg

from tilgangsinnsikt.analyses.base import (
    ACTIVE_IDENTITY,
    DYNAMIC_ENTITLEMENT_PATTERN,
    LEAVER_IDENTITY,
    Finding,
    nb_count,
    nb_date,
    nb_list,
    register,
    rows,
    save,
    utc_midnight,
    who,
)

ANALYSIS = "lifecycle"
DORMANT_DAYS = 90
JOINER_DAYS = 90
JOINER_GRACE_DAYS = 14
# Above the share of any optional role, so a joiner is never told to request one side of
# a disjoint pair (for example both sides of an SoD rule).
BASELINE_PREVALENCE = 0.90
MIN_PEER_GROUP = 20

LABEL = "e.name || ' (' || p.name || ')'"
RISK_ORDER = (
    "e.entitlement_id like %(dynamic)s,"
    " array_position(array['high', 'medium', 'low'], e.risk_level)"
)


@register(ANALYSIS)
def run(conn: psycopg.Connection, run_id: str, as_of: date) -> list[Finding]:
    params = {
        "as_of": as_of,
        "stale_before": utc_midnight(as_of - timedelta(days=DORMANT_DAYS)),
        "joined_after": as_of - timedelta(days=JOINER_DAYS),
        "joined_before": as_of - timedelta(days=JOINER_GRACE_DAYS),
        "dynamic": DYNAMIC_ENTITLEMENT_PATTERN,
        "prevalence": BASELINE_PREVALENCE,
        "min_group": MIN_PEER_GROUP,
    }
    with conn.transaction():
        findings = [
            *leaver_active_account(conn, params),
            *leaver_with_entitlements(conn, params),
            *orphan_account(conn, params),
            *dormant_privileged(conn, params),
            *joiner_without_baseline(conn, params),
        ]
        save(conn, run_id, findings)
    return findings


def _left(row: dict[str, Any]) -> str:
    person = who(row["display_name"], row["identity_id"])
    if row["employment_end"] is None:
        return f"{person} er registrert som sluttet"
    return f"{person} sluttet {nb_date(row['employment_end'])}"


def _end(row: dict[str, Any]) -> str | None:
    return row["employment_end"] and row["employment_end"].isoformat()


def leaver_active_account(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        select i.identity_id, i.display_name, i.employment_end,
               %(as_of)s::date - i.employment_end as days_since_end,
               array_agg(distinct p.name order by p.name) as apps,
               jsonb_agg(jsonb_build_object(
                   'account_id', a.account_id,
                   'app_id', a.app_id,
                   'last_login', (a.last_login at time zone 'UTC')::date
               ) order by a.account_id) as accounts
        from identity i
        join account a using (identity_id)
        join application p using (app_id)
        where a.enabled and {LEAVER_IDENTITY}
        group by i.identity_id
        order by i.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        n = len(row["accounts"])
        findings.append(
            Finding(
                ANALYSIS,
                "leaver_active_account",
                "critical",
                "Sluttet, men har aktive kontoer",
                f"{_left(row)}, men har fortsatt "
                f"{nb_count(n, 'aktiv konto', 'aktive kontoer')} i {nb_list(row['apps'])}. "
                "En aktiv konto etter sluttdato kan brukes av den som har sluttet, eller av "
                "andre som får tak i påloggingen. Deaktiver kontoene og avklar med lederen "
                "om noe må overføres.",
                {
                    "employment_end": _end(row),
                    "days_since_end": row["days_since_end"],
                    "accounts": row["accounts"],
                },
                identity_id=row["identity_id"],
                account_id=row["accounts"][0]["account_id"] if n == 1 else None,
            )
        )
    return findings


def leaver_with_entitlements(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        select i.identity_id, i.display_name, i.employment_end,
               %(as_of)s::date - i.employment_end as days_since_end,
               array_agg({LABEL} order by {RISK_ORDER}, e.entitlement_id) as labels,
               jsonb_agg(jsonb_build_object(
                   'account_id', s.account_id,
                   'entitlement_id', s.entitlement_id,
                   'risk_level', e.risk_level,
                   'granted_at', s.granted_at
               ) order by {RISK_ORDER}, e.entitlement_id) as assignments
        from identity i
        join account a using (identity_id)
        join assignment s using (account_id)
        join entitlement e using (entitlement_id)
        join application p on p.app_id = e.app_id
        where {LEAVER_IDENTITY}
        group by i.identity_id
        order by i.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        n = len(row["assignments"])
        findings.append(
            Finding(
                ANALYSIS,
                "leaver_with_entitlements",
                "high",
                "Sluttet, men har fortsatt tildelte rettigheter",
                f"{_left(row)}, men har fortsatt "
                f"{nb_count(n, 'tildelt rettighet', 'tildelte rettigheter')}: "
                f"{nb_list(row['labels'])}. Tildelinger som blir liggende etter sluttdato, "
                "kommer tilbake hvis kontoen aktiveres igjen. Fjern tildelingene som en del av "
                "avslutningsrutinen.",
                {
                    "employment_end": _end(row),
                    "days_since_end": row["days_since_end"],
                    "assignments": row["assignments"],
                },
                identity_id=row["identity_id"],
                entitlement_id=row["assignments"][0]["entitlement_id"] if n == 1 else None,
            )
        )
    return findings


def orphan_account(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        select a.account_id, a.app_id, p.name as app_name,
               (a.last_login at time zone 'UTC')::date as last_login,
               coalesce(
                   jsonb_agg(s.entitlement_id order by {RISK_ORDER}, s.entitlement_id)
                       filter (where s.entitlement_id is not null),
                   '[]'
               ) as entitlements
        from account a
        join application p using (app_id)
        left join assignment s using (account_id)
        left join entitlement e using (entitlement_id)
        where a.identity_id is null and a.enabled
        group by a.account_id, p.name
        order by a.account_id
    """
    findings = []
    for row in rows(conn, query, params):
        if row["last_login"] is None:
            used = "har aldri vært brukt"
        else:
            used = f"ble sist brukt {nb_date(row['last_login'])}"
        findings.append(
            Finding(
                ANALYSIS,
                "orphan_account",
                "high",
                "Konto uten eier i HR",
                f"Kontoen {row['account_id']} i {row['app_name']} er aktiv, men er ikke knyttet "
                f"til noen identitet i HR, og {used}. En konto uten eier blir ikke fulgt opp "
                "når noen slutter eller bytter jobb. Finn eieren og knytt kontoen til en "
                "identitet, eller deaktiver den.",
                {
                    "app_id": row["app_id"],
                    "last_login": row["last_login"] and row["last_login"].isoformat(),
                    "entitlements": row["entitlements"],
                },
                account_id=row["account_id"],
            )
        )
    return findings


def dormant_privileged(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        select i.identity_id, i.display_name,
               bool_or(a.last_login is null) as never_used,
               count(distinct a.account_id) as accounts,
               jsonb_agg(jsonb_build_object(
                   'account_id', a.account_id,
                   'entitlement_id', e.entitlement_id,
                   'label', {LABEL},
                   'last_login', (a.last_login at time zone 'UTC')::date
               ) order by e.entitlement_id) as privileges
        from identity i
        join account a using (identity_id)
        join assignment s using (account_id)
        join entitlement e using (entitlement_id)
        join application p on p.app_id = e.app_id
        where {ACTIVE_IDENTITY}
          and a.enabled
          and e.risk_level = 'high'
          and (a.last_login is null or a.last_login < %(stale_before)s)
        group by i.identity_id
        order by i.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        privileges = row["privileges"]
        items = [
            f"{p['label']}, aldri brukt"
            if p["last_login"] is None
            else f"{p['label']}, sist brukt {nb_date(p['last_login'])}"
            for p in privileges
        ]
        for p in privileges:
            del p["label"]
        findings.append(
            Finding(
                ANALYSIS,
                "dormant_privileged",
                # A high-risk privilege that has never been used was most likely granted
                # by mistake or for a need that never came.
                "critical" if row["never_used"] else "medium",
                "Høyrisikorettighet som ikke brukes",
                f"{who(row['display_name'], row['identity_id'])} har "
                f"{nb_count(len(privileges), 'høyrisikorettighet', 'høyrisikorettigheter')} på "
                f"{'en konto' if row['accounts'] == 1 else 'kontoer'} som ikke er brukt de "
                f"siste {DORMANT_DAYS} dagene: "
                f"{'; '.join(items)}. Ubrukte privilegier gir risiko uten nytte. Be lederen "
                "bekrefte behovet, eller fjern rettigheten.",
                {"dormant_days": DORMANT_DAYS, "privileges": privileges},
                identity_id=row["identity_id"],
                entitlement_id=privileges[0]["entitlement_id"] if len(privileges) == 1 else None,
            )
        )
    return findings


def joiner_without_baseline(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        with active as (
            select i.identity_id, i.display_name, i.job_title, i.employment_start, u.unit_type
            from identity i
            join org_unit u using (unit_id)
            where {ACTIVE_IDENTITY}
        ),
        held as (
            select distinct a.identity_id, s.entitlement_id
            from assignment s
            join account a using (account_id)
            where a.enabled and s.entitlement_id not like %(dynamic)s
        ),
        peer_group as (
            select job_title, unit_type, count(*) as size
            from active
            group by job_title, unit_type
            having count(*) >= %(min_group)s
        ),
        baseline as (
            select g.job_title, g.unit_type, h.entitlement_id
            from active m
            join peer_group g using (job_title, unit_type)
            join held h using (identity_id)
            group by g.job_title, g.unit_type, g.size, h.entitlement_id
            having count(*) >= %(prevalence)s * g.size
        )
        select j.identity_id, j.display_name, j.job_title, j.unit_type, j.employment_start,
               array_agg({LABEL} order by b.entitlement_id) as labels,
               array_agg(b.entitlement_id order by b.entitlement_id) as missing
        from active j
        join baseline b using (job_title, unit_type)
        join entitlement e on e.entitlement_id = b.entitlement_id
        join application p on p.app_id = e.app_id
        where j.employment_start > %(joined_after)s
          and j.employment_start < %(joined_before)s
          and not exists (
              select 1 from held h
              where h.identity_id = j.identity_id and h.entitlement_id = b.entitlement_id
          )
        group by j.identity_id, j.display_name, j.job_title, j.unit_type, j.employment_start
        order by j.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        n = len(row["missing"])
        findings.append(
            Finding(
                ANALYSIS,
                "joiner_without_baseline",
                "low",
                "Nyansatt mangler grunntilganger",
                f"{who(row['display_name'], row['identity_id'])} begynte "
                f"{nb_date(row['employment_start'])} som {row['job_title']} ({row['unit_type']}), "
                f"men mangler {nb_count(n, 'rettighet', 'rettigheter')} som de fleste i samme "
                f"rolle har: {nb_list(row['labels'])}. Det kan bety at bestillingen av "
                "tilganger ikke er fullført. Sjekk med lederen om tilgangene skal bestilles.",
                {
                    "employment_start": row["employment_start"].isoformat(),
                    "peer_group": {"job_title": row["job_title"], "unit_type": row["unit_type"]},
                    "missing": row["missing"],
                },
                identity_id=row["identity_id"],
            )
        )
    return findings
