"""Markdown report in Norwegian, for sharing with managers and identity administrators."""

from tilgangsinnsikt.analyses.base import nb_date, nb_percent
from tilgangsinnsikt.report import (
    ANALYSIS_NB,
    SEVERITY_NB,
    SEVERITY_ORDER,
    TOP_PER_ANALYSIS,
    Report,
    subject,
)


def render(report: Report) -> str:
    run = report.run
    analyses = ", ".join(ANALYSIS_NB[a].lower() for a in run.analyses)
    lines = [
        "# Tilgangsinnsikt: tilgangsrapport",
        "",
        f"Referansedato {nb_date(run.as_of)}, kjøring `{run.run_id}`. Analyser: {analyses}.",
        "",
        "## Nøkkeltall",
        "",
        "| Nøkkeltall | Verdi |",
        "|---|---|",
        f"| Rolledekning (RBAC) | {_coverage(report)} |",
        f"| Funn totalt | {len(report.findings)} |",
    ]
    counts = report.by_severity()
    lines += [f"| {SEVERITY_NB[s]} | {counts[s]} |" for s in SEVERITY_ORDER]
    if report.coverage:
        lines += ["", report.coverage.explanation_nb]

    lines += ["", "## Hva bør gjøres først", ""]
    priorities = report.priorities()
    lines += [
        f"{n}. **{SEVERITY_NB[p.severity]}**: {p.title_nb} ({p.count} funn, "
        f"{ANALYSIS_NB[p.analysis].lower()})"
        for n, p in enumerate(priorities, 1)
    ] or ["Ingen funn."]

    lines += [
        "",
        "## Oppsummering",
        "",
        "| Analyse | Regel | " + " | ".join(SEVERITY_NB[s] for s in SEVERITY_ORDER) + " | Totalt |",
        "|---|---|" + "---:|" * (len(SEVERITY_ORDER) + 1),
    ]
    for analysis, rule, title, by_severity in report.summary():
        cells = " | ".join(str(by_severity[s]) for s in SEVERITY_ORDER)
        total = sum(by_severity.values())
        lines.append(f"| {ANALYSIS_NB[analysis]} | {title} (`{rule}`) | {cells} | {total} |")

    lines += ["", "## Funn per analyse"]
    for analysis, total, top in report.top():
        lines += ["", f"### {ANALYSIS_NB[analysis]}", ""]
        if not top:
            lines.append("Ingen funn.")
            continue
        if total > TOP_PER_ANALYSIS:
            lines += [f"De {len(top)} viktigste av {total} funn.", ""]
        for f in top:
            lines += [
                f"**{SEVERITY_NB[f.severity]}: {f.title_nb}** ({subject(f)})",
                "",
                f.explanation_nb,
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def _coverage(report: Report) -> str:
    if report.coverage is None:
        return "ikke beregnet (avvik fra kolleger ble ikke kjørt)"
    evidence = report.coverage.evidence
    return (
        f"**{nb_percent(evidence['coverage'])}** ({evidence['explained']} av "
        f"{evidence['assignments']} tildelinger)"
    )
