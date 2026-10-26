"""Terminal report in Norwegian, with severity colours."""

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tilgangsinnsikt.analyses.base import nb_date, nb_percent
from tilgangsinnsikt.report import (
    ANALYSIS_NB,
    SEVERITY_NB,
    SEVERITY_ORDER,
    TOP_PER_ANALYSIS,
    Report,
    subject,
)

SEVERITY_STYLE = {"critical": "bold red", "high": "red", "medium": "yellow", "low": "cyan"}


def severity(value: str) -> Text:
    return Text(SEVERITY_NB[value], style=SEVERITY_STYLE[value])


def render(report: Report, console: Console) -> None:
    run = report.run
    console.rule("Tilgangsinnsikt: tilgangsrapport")
    analyses = ", ".join(ANALYSIS_NB[a].lower() for a in run.analyses)
    console.print(
        f"Referansedato {nb_date(run.as_of)}, kjøring {run.run_id}. Analyser: {analyses}.",
        highlight=False,
    )

    counts = report.by_severity()
    metrics = Table.grid(padding=(0, 2))
    metrics.add_column()
    metrics.add_column(justify="right")
    metrics.add_row("Rolledekning (RBAC)", _coverage(report))
    metrics.add_row("Funn totalt", str(len(report.findings)))
    for s in SEVERITY_ORDER:
        metrics.add_row(severity(s), str(counts[s]))
    body = [metrics]
    if report.coverage:
        body += [Text(), Text(report.coverage.explanation_nb, style="dim")]
    console.print(Panel(Group(*body), title="Nøkkeltall", title_align="left"))

    first = Table(title="Hva bør gjøres først", title_justify="left", show_lines=False)
    first.add_column("#", justify="right")
    first.add_column("Alvorlighet")
    first.add_column("Funn")
    first.add_column("Antall", justify="right")
    first.add_column("Analyse")
    for n, p in enumerate(report.priorities(), 1):
        first.add_row(
            str(n), severity(p.severity), p.title_nb, str(p.count), ANALYSIS_NB[p.analysis]
        )
    console.print(first)

    for analysis, total, top in report.top():
        title = ANALYSIS_NB[analysis]
        if total > TOP_PER_ANALYSIS:
            title += f": de {len(top)} viktigste av {total} funn"
        table = Table(title=title, title_justify="left", show_lines=True, expand=True)
        table.add_column("Alvorlighet", no_wrap=True)
        table.add_column("Gjelder", no_wrap=True)
        table.add_column("Forklaring", ratio=1)
        for f in top:
            text = Text.assemble((f.title_nb, "bold"), "\n", f.explanation_nb)
            table.add_row(severity(f.severity), subject(f), text)
        if not top:
            table.add_row("", "", "Ingen funn.")
        console.print(table)


def _coverage(report: Report) -> str:
    if report.coverage is None:
        return "ikke beregnet"
    evidence = report.coverage.evidence
    return (
        f"[bold]{nb_percent(evidence['coverage'])}[/bold] "
        f"({evidence['explained']} av {evidence['assignments']})"
    )
