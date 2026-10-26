from datetime import timedelta

import psycopg
from conftest import AS_OF

from tilgangsinnsikt.analyses import log_anomalies
from tilgangsinnsikt.analyses.base import utc_midnight
from tilgangsinnsikt.evaluation import score

PLANTED_RULES = ["lookup_out_of_region", "lookup_colleague", "lookup_volume_spike"]
WINDOW = {
    "start": utc_midnight(AS_OF - timedelta(days=30)),
    "end": utc_midnight(AS_OF),
}


def test_log_anomalies_match_ground_truth(
    loaded: psycopg.Connection, ground_truth: dict[str, list[str]]
) -> None:
    findings = log_anomalies.run(loaded, "r1", AS_OF)
    for rule in PLANTED_RULES:
        result = score(rule, findings, ground_truth[rule])
        print(result)
        assert result.recall >= 0.9 and result.precision >= 0.8, result


def test_exempt_users_cross_the_threshold_but_are_not_flagged(loaded: psycopg.Connection) -> None:
    exempt_over_threshold = {
        row[0]
        for row in loaded.execute(
            """
            select a.identity_id
            from access_event e
            join account a using (account_id)
            join identity i using (identity_id)
            join org_unit u using (unit_id)
            where e.occurred_at >= %(start)s and e.occurred_at < %(end)s
              and (u.unit_type in ('direktorat', 'kontaktsenter')
                   or exists (select 1 from assignment s join account x using (account_id)
                              where x.identity_id = a.identity_id
                                and s.entitlement_id = 'ytelse:les-alle-regioner'))
            group by a.identity_id
            having count(*) >= 20
               and count(*) filter (where e.subject_region <> u.region) >= 0.25 * count(*)
            """,
            WINDOW,
        )
    }
    flagged = {
        f.identity_id
        for f in log_anomalies.run(loaded, "r1", AS_OF)
        if f.rule == "lookup_out_of_region"
    }
    print(f"exempt users over the out-of-region threshold: {len(exempt_over_threshold)}")
    assert exempt_over_threshold
    assert not exempt_over_threshold & flagged


def test_spike_evidence_meets_both_thresholds(loaded: psycopg.Connection) -> None:
    spikes = [f for f in log_anomalies.run(loaded, "r1", AS_OF) if f.rule == "lookup_volume_spike"]
    assert spikes
    for f in spikes:
        assert f.evidence["lookups"] >= 50
        assert f.evidence["lookups"] >= 5 * f.evidence["baseline_median"]
        assert f.evidence["spike_days"] >= 1


def test_out_of_domain_flags_active_users_only(loaded: psycopg.Connection) -> None:
    assert not [
        f for f in log_anomalies.run(loaded, "r1", AS_OF) if f.rule == "lookup_out_of_domain"
    ]
    target = loaded.execute(
        """
        select a.identity_id
        from access_event e
        join account a using (account_id)
        join identity i using (identity_id)
        where e.occurred_at >= %(start)s and e.occurred_at < %(end)s and i.status = 'active'
          and i.job_title = 'veileder'
        group by a.identity_id
        having count(*) >= 20
        order by a.identity_id
        limit 1
        """,
        WINDOW,
    ).fetchone()[0]
    loaded.execute(
        "update access_event e set subject_domain = 'okonomi' from account a"
        " where a.account_id = e.account_id and a.identity_id = %(id)s"
        " and e.occurred_at >= %(start)s and e.occurred_at < %(end)s",
        WINDOW | {"id": target},
    )
    loaded.commit()
    [finding] = [
        f for f in log_anomalies.run(loaded, "r2", AS_OF) if f.rule == "lookup_out_of_domain"
    ]
    assert finding.identity_id == target
    assert finding.evidence["share"] == 1.0
    assert finding.evidence["domains"] == ["okonomi"]


def test_window_follows_as_of(loaded: psycopg.Connection) -> None:
    assert log_anomalies.run(loaded, "r1", AS_OF + timedelta(days=200)) == []
