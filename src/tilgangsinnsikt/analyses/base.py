"""Finding type, the analysis registry, finding storage and Norwegian text helpers."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tilgangsinnsikt import db

SEVERITIES = ["low", "medium", "high", "critical"]

# Stored as findings so every run keeps them, but they describe the organisation as a
# whole and are not weaknesses to act on.
METRIC_RULES = {"rbac_coverage"}

# Membership of these groups follows the HR unit automatically, so nobody decided to
# grant it and there is nothing to review or remove.
DYNAMIC_ENTITLEMENT_PATTERN = "entra:enhet-%"

ACTIVE_IDENTITY = (
    "(i.status = 'active' and (i.employment_end is null or i.employment_end >= %(as_of)s))"
)
LEAVER_IDENTITY = f"(not {ACTIVE_IDENTITY})"

FINDING_COLUMNS = [
    "analysis",
    "rule",
    "severity",
    "identity_id",
    "account_id",
    "entitlement_id",
    "title_nb",
    "explanation_nb",
    "evidence",
    "run_id",
]


@dataclass(frozen=True)
class Finding:
    analysis: str
    rule: str
    severity: str
    title_nb: str
    explanation_nb: str
    evidence: dict[str, Any] = field(default_factory=dict)
    identity_id: str | None = None
    account_id: str | None = None
    entitlement_id: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")


Analysis = Callable[[psycopg.Connection, str, date], list[Finding]]
REGISTRY: dict[str, Analysis] = {}


def register(name: str) -> Callable[[Analysis], Analysis]:
    def decorator(analysis: Analysis) -> Analysis:
        REGISTRY[name] = analysis
        return analysis

    return decorator


def rows(conn: psycopg.Connection, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def save(conn: psycopg.Connection, run_id: str, findings: Sequence[Finding]) -> int:
    return db.copy_rows(
        conn,
        "finding",
        FINDING_COLUMNS,
        (
            (
                f.analysis,
                f.rule,
                f.severity,
                f.identity_id,
                f.account_id,
                f.entitlement_id,
                f.title_nb,
                f.explanation_nb,
                json.dumps(f.evidence, ensure_ascii=False),
                run_id,
            )
            for f in findings
        ),
    )


def utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def nb_percent(share: float) -> str:
    return f"{round(100 * share)} %"


def nb_date(value: date | str) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return value.strftime("%d.%m.%Y")


def nb_count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def nb_list(items: Sequence[str], limit: int = 3) -> str:
    items = list(items)
    if len(items) > limit:
        return f"{', '.join(items[:limit])} og {len(items) - limit} til"
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} og {items[-1]}"


def who(display_name: str, identity_id: str) -> str:
    return f"{display_name} ({identity_id})"
