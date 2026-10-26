"""Report content shared by the terminal and Markdown renderers."""

from collections import Counter
from dataclasses import dataclass

from tilgangsinnsikt.analyses.base import METRIC_RULES, SEVERITIES, Finding
from tilgangsinnsikt.runs import ANALYSIS_ORDER, Run

TOP_PER_ANALYSIS = 5

SEVERITY_NB = {"critical": "Kritisk", "high": "Høy", "medium": "Middels", "low": "Lav"}
ANALYSIS_NB = {
    "lifecycle": "Livssyklus",
    "peer_group": "Avvik fra kolleger",
    "sod": "Arbeidsdeling",
    "log_anomaly": "Oppslag i logg",
}
SEVERITY_ORDER = list(reversed(SEVERITIES))


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity)


def subject(finding: Finding) -> str:
    return finding.identity_id or finding.account_id or ""


@dataclass(frozen=True)
class Priority:
    severity: str
    analysis: str
    rule: str
    title_nb: str
    count: int


@dataclass(frozen=True)
class Report:
    run: Run
    findings: list[Finding]
    coverage: Finding | None

    @classmethod
    def build(cls, run: Run, findings: list[Finding]) -> "Report":
        coverage = next((f for f in findings if f.rule == "rbac_coverage"), None)
        actionable = sorted(
            (f for f in findings if f.rule not in METRIC_RULES),
            key=lambda f: (
                severity_rank(f.severity),
                ANALYSIS_ORDER.index(f.analysis),
                f.rule,
                subject(f),
            ),
        )
        return cls(run, actionable, coverage)

    def by_severity(self) -> Counter:
        return Counter(f.severity for f in self.findings)

    def priorities(self) -> list[Priority]:
        groups = Counter((f.severity, f.analysis, f.rule, f.title_nb) for f in self.findings)
        return sorted(
            (Priority(*key, count) for key, count in groups.items()),
            key=lambda p: (severity_rank(p.severity), -p.count, p.rule),
        )

    def summary(self) -> list[tuple[str, str, str, Counter]]:
        rows: dict[tuple[str, str], tuple[str, Counter]] = {}
        for f in self.findings:
            title, counts = rows.setdefault((f.analysis, f.rule), (f.title_nb, Counter()))
            counts[f.severity] += 1
        return [
            (analysis, rule, title, counts)
            for (analysis, rule), (title, counts) in sorted(
                rows.items(), key=lambda item: (ANALYSIS_ORDER.index(item[0][0]), item[0][1])
            )
        ]

    def top(self) -> list[tuple[str, int, list[Finding]]]:
        out = []
        for analysis in ANALYSIS_ORDER:
            found = [f for f in self.findings if f.analysis == analysis]
            if analysis in self.run.analyses:
                out.append((analysis, len(found), found[:TOP_PER_ANALYSIS]))
        return out
