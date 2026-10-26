"""A4: lookup patterns in the access log that need a manager's review."""

from datetime import date, timedelta

import psycopg

from tilgangsinnsikt.analyses.base import (
    ACTIVE_IDENTITY,
    DYNAMIC_ENTITLEMENT_PATTERN,
    Finding,
    nb_count,
    nb_date,
    nb_list,
    nb_percent,
    register,
    rows,
    save,
    utc_midnight,
    who,
)

ANALYSIS = "log_anomaly"
WINDOW_DAYS = 30
BASELINE_DAYS = 60
MIN_LOOKUPS = 20
OUT_OF_REGION_SHARE = 0.25
OUT_OF_DOMAIN_SHARE = 0.20
COLLEAGUE_MIN = 5
SPIKE_FACTOR = 5
# Without a floor, a user whose median is 0 or 1 lookups a day would be flagged for any
# ordinary busy day.
SPIKE_MIN = 50

# Exemptions from lookup_out_of_region, and why:
# - direktorat: directorate staff work on national caseloads (analysis, appeals, quality
#   control), so their lookups are spread over all regions by design.
# - kontaktsenter: contact centres take calls from the whole country and look up whoever
#   is calling, regardless of region.
# - ytelse:les-alle-regioner: holders were explicitly granted national read access in an
#   approved request; flagging them would repeat a decision that access review already
#   made, and bury the findings that matter.
# Exemption from lookup_out_of_domain, and why:
# - leavers: their assignments are removed at exit, so their current entitlements no
#   longer show which domains they worked in, and every lookup they made before leaving
#   would count as out of domain.
# No other exemptions: a colleague lookup or a sudden spike needs an explanation whatever
# the unit, and out-of-domain needs no unit exemption because it is measured against the
# user's own entitlements, which already reflect any national role.
EXEMPT_UNIT_TYPES = ["direktorat", "kontaktsenter"]
EXEMPT_ENTITLEMENTS = ["ytelse:les-alle-regioner"]

# Events are attributed to identities through their accounts. Events from orphan accounts
# have no identity and are left out here; lifecycle already reports those accounts.
WINDOW = f"""
    person as (
        select i.identity_id, i.display_name, u.region, u.unit_type, {ACTIVE_IDENTITY} as active
        from identity i
        join org_unit u using (unit_id)
    ),
    window_event as (
        select a.identity_id, e.subject_ref, e.subject_region, e.subject_domain,
               e.subject_is_employee, e.occurred_at
        from access_event e
        join account a using (account_id)
        where a.identity_id is not null
          and e.occurred_at >= %(window_start)s
          and e.occurred_at < %(window_end)s
    )
"""


@register(ANALYSIS)
def run(conn: psycopg.Connection, run_id: str, as_of: date) -> list[Finding]:
    window_start = as_of - timedelta(days=WINDOW_DAYS)
    params = {
        "as_of": as_of,
        "window_start": utc_midnight(window_start),
        "window_end": utc_midnight(as_of),
        "baseline_start": utc_midnight(window_start - timedelta(days=BASELINE_DAYS)),
        "baseline_first_day": window_start - timedelta(days=BASELINE_DAYS),
        "baseline_last_day": window_start - timedelta(days=1),
        "window_first_day": window_start,
        "min_lookups": MIN_LOOKUPS,
        "region_share": OUT_OF_REGION_SHARE,
        "domain_share": OUT_OF_DOMAIN_SHARE,
        "colleague_min": COLLEAGUE_MIN,
        "spike_factor": SPIKE_FACTOR,
        "spike_min": SPIKE_MIN,
        "exempt_unit_types": EXEMPT_UNIT_TYPES,
        "exempt_entitlements": EXEMPT_ENTITLEMENTS,
        "dynamic": DYNAMIC_ENTITLEMENT_PATTERN,
    }
    with conn.transaction():
        findings = [
            *lookup_out_of_region(conn, params),
            *lookup_colleague(conn, params),
            *lookup_volume_spike(conn, params),
            *lookup_out_of_domain(conn, params),
        ]
        save(conn, run_id, findings)
    return findings


def _period(params: dict) -> str:
    return f"{WINDOW_DAYS} dagene fram til {nb_date(params['as_of'])}"


def lookup_out_of_region(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        with {WINDOW},
        exempt as (
            select identity_id from person where unit_type = any(%(exempt_unit_types)s)
            union
            select a.identity_id
            from assignment s
            join account a using (account_id)
            where s.entitlement_id = any(%(exempt_entitlements)s) and a.identity_id is not null
        ),
        outside as (
            select w.identity_id, w.subject_region, count(*) as lookups
            from window_event w
            join person p using (identity_id)
            where w.subject_region <> p.region
            group by w.identity_id, w.subject_region
        )
        select p.identity_id, p.display_name, p.region, count(*) as lookups,
               count(*) filter (where w.subject_region <> p.region) as outside,
               (select jsonb_agg(jsonb_build_object('region', o.subject_region,
                                                    'lookups', o.lookups)
                                 order by o.lookups desc, o.subject_region)
                from outside o where o.identity_id = p.identity_id) as by_region
        from window_event w
        join person p using (identity_id)
        where p.identity_id not in (select identity_id from exempt)
        group by p.identity_id, p.display_name, p.region
        having count(*) >= %(min_lookups)s
           and count(*) filter (where w.subject_region <> p.region) >= %(region_share)s * count(*)
        order by p.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        share = row["outside"] / row["lookups"]
        regions = [r["region"] for r in row["by_region"]]
        findings.append(
            Finding(
                ANALYSIS,
                "lookup_out_of_region",
                "high",
                "Mange oppslag utenfor egen region",
                f"{who(row['display_name'], row['identity_id'])} i {row['region']} gjorde "
                f"{row['lookups']} oppslag de siste {_period(params)}, og {row['outside']} av "
                f"dem ({nb_percent(share)}) gjaldt saker i andre regioner, mest "
                f"{nb_list(regions[:2])}. Oppslag uten tjenstlig behov kan være brudd på "
                "taushetsplikten. Be lederen gå gjennom oppslagene og bekrefte at de var "
                "tjenstlig begrunnet.",
                {
                    "region": row["region"],
                    "lookups": row["lookups"],
                    "outside_region": row["outside"],
                    "share": round(share, 3),
                    "by_region": row["by_region"],
                    "window_days": WINDOW_DAYS,
                },
                identity_id=row["identity_id"],
            )
        )
    return findings


def lookup_colleague(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        with {WINDOW}
        select p.identity_id, p.display_name, count(*) as lookups,
               count(distinct w.subject_ref) as subjects,
               min(w.occurred_at at time zone 'UTC')::date as first_day,
               max(w.occurred_at at time zone 'UTC')::date as last_day
        from window_event w
        join person p using (identity_id)
        where w.subject_is_employee
        group by p.identity_id, p.display_name
        having count(*) >= %(colleague_min)s
        order by p.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        findings.append(
            Finding(
                ANALYSIS,
                "lookup_colleague",
                "critical",
                "Oppslag på kolleger",
                f"{who(row['display_name'], row['identity_id'])} gjorde "
                f"{row['lookups']} oppslag på "
                f"{nb_count(row['subjects'], 'ansatt', 'ansatte')} i etaten mellom "
                f"{nb_date(row['first_day'])} og {nb_date(row['last_day'])}. Oppslag på "
                "kolleger krever alltid en tjenstlig grunn, ellers er det snoking og brudd "
                "på taushetsplikten. Lederen bør gå gjennom oppslagene med den ansatte snarest.",
                {
                    "lookups": row["lookups"],
                    "subjects": row["subjects"],
                    "first_day": row["first_day"].isoformat(),
                    "last_day": row["last_day"].isoformat(),
                    "window_days": WINDOW_DAYS,
                },
                identity_id=row["identity_id"],
            )
        )
    return findings


def lookup_volume_spike(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = """
        with user_day as (
            select a.identity_id, (e.occurred_at at time zone 'UTC')::date as day,
                   count(*) as lookups
            from access_event e
            join account a using (account_id)
            where a.identity_id is not null
              and e.occurred_at >= %(baseline_start)s
              and e.occurred_at < %(window_end)s
            group by a.identity_id, day
        ),
        peak as (
            select distinct on (identity_id) identity_id, day, lookups
            from user_day
            where day >= %(window_first_day)s
            order by identity_id, lookups desc, day
        ),
        baseline as (
            select k.identity_id,
                   percentile_cont(0.5) within group (order by coalesce(d.lookups, 0)) as median
            from peak k
            cross join generate_series(
                %(baseline_first_day)s::date, %(baseline_last_day)s::date, interval '1 day'
            ) as g (day)
            left join user_day d on d.identity_id = k.identity_id and d.day = g.day::date
            group by k.identity_id
        )
        select i.identity_id, i.display_name, k.day, k.lookups, b.median,
               (select count(*) from user_day d
                where d.identity_id = k.identity_id and d.day >= %(window_first_day)s
                  and d.lookups >= %(spike_min)s
                  and d.lookups >= %(spike_factor)s * b.median) as spike_days
        from peak k
        join baseline b using (identity_id)
        join identity i using (identity_id)
        where k.lookups >= %(spike_min)s and k.lookups >= %(spike_factor)s * b.median
        order by i.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        median = float(row["median"])
        findings.append(
            Finding(
                ANALYSIS,
                "lookup_volume_spike",
                "medium",
                "Uvanlig mange oppslag på én dag",
                f"{who(row['display_name'], row['identity_id'])} gjorde {row['lookups']} "
                f"oppslag {nb_date(row['day'])}, mot en median på {median:g} per dag de "
                f"{BASELINE_DAYS} dagene før. En slik topp kan skyldes en ekstraordinær "
                "arbeidsoppgave, men også masseoppslag eller at kontoen er misbrukt. Be "
                "lederen bekrefte hva oppslagene gjaldt.",
                {
                    "day": row["day"].isoformat(),
                    "lookups": row["lookups"],
                    "baseline_median": median,
                    "baseline_days": BASELINE_DAYS,
                    "spike_days": row["spike_days"],
                },
                identity_id=row["identity_id"],
            )
        )
    return findings


def lookup_out_of_domain(conn: psycopg.Connection, params: dict) -> list[Finding]:
    query = f"""
        with {WINDOW},
        own_domain as (
            select distinct a.identity_id, p.domain
            from assignment s
            join account a using (account_id)
            join entitlement e using (entitlement_id)
            join application p on p.app_id = e.app_id
            where a.enabled and a.identity_id is not null
              and s.entitlement_id not like %(dynamic)s
        ),
        marked as (
            select w.identity_id, w.subject_domain,
                   not exists (
                       select 1 from own_domain d
                       where d.identity_id = w.identity_id and d.domain = w.subject_domain
                   ) as outside
            from window_event w
        )
        select p.identity_id, p.display_name, count(*) as lookups,
               count(*) filter (where m.outside) as outside,
               array_agg(distinct m.subject_domain) filter (where m.outside) as domains
        from marked m
        join person p using (identity_id)
        where p.active
        group by p.identity_id, p.display_name
        having count(*) >= %(min_lookups)s
           and count(*) filter (where m.outside) >= %(domain_share)s * count(*)
        order by p.identity_id
    """
    findings = []
    for row in rows(conn, query, params):
        share = row["outside"] / row["lookups"]
        findings.append(
            Finding(
                ANALYSIS,
                "lookup_out_of_domain",
                "medium",
                "Oppslag utenfor egne fagområder",
                f"{who(row['display_name'], row['identity_id'])} gjorde {row['lookups']} "
                f"oppslag de siste {_period(params)}, og {row['outside']} av dem "
                f"({nb_percent(share)}) gjaldt fagområder uten tildelte rettigheter: "
                f"{nb_list(row['domains'])}. Det kan bety at rettighetene ikke stemmer med "
                "arbeidsoppgavene, eller at oppslagene mangler tjenstlig behov. Be lederen "
                "vurdere begge deler.",
                {
                    "lookups": row["lookups"],
                    "outside_domain": row["outside"],
                    "share": round(share, 3),
                    "domains": row["domains"],
                    "window_days": WINDOW_DAYS,
                },
                identity_id=row["identity_id"],
            )
        )
    return findings
