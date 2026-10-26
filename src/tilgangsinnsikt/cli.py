import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import httpx
import psycopg
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from tilgangsinnsikt import __version__, db, evaluation, runs
from tilgangsinnsikt.config import EntraSettings
from tilgangsinnsikt.connectors import entra
from tilgangsinnsikt.report import SEVERITY_ORDER, Report, markdown, terminal
from tilgangsinnsikt.synth import generator

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

DATA_DIR = Path("data/synthetic")
# Outside docs/ and ignored by git: a report from real data must never be committed.
REPORT_PATH = Path("reports/report.md")

AsOf = Annotated[
    datetime | None,
    typer.Option(formats=["%Y-%m-%d"], help="Reference date (YYYY-MM-DD). Defaults to today."),
]
DataDir = Annotated[Path, typer.Option(help="Directory with one CSV file per table.")]
RunId = Annotated[str | None, typer.Option(help="Run to use. Defaults to the latest run.")]


@contextmanager
def database() -> Iterator[psycopg.Connection]:
    try:
        with db.connect() as conn:
            db.migrate(conn)
            yield conn
    except psycopg.OperationalError:
        console.print("[red]Could not connect to PostgreSQL.[/red]")
        console.print("Start it with docker compose up -d db and check POSTGRES_* in .env.")
        raise typer.Exit(1) from None


def _date(value: datetime | None, default: date) -> date:
    return value.date() if value else default


@app.callback()
def main() -> None:
    """Tilgangsinnsikt: access governance analytics."""


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command()
def generate(
    seed: Annotated[int, typer.Option(help="Random seed.")] = 42,
    small: Annotated[bool, typer.Option(help="2 000 identities and 100 000 events.")] = False,
    identities: Annotated[
        int | None, typer.Option(help="Override the number of identities.")
    ] = None,
    events: Annotated[
        int | None, typer.Option(help="Override the number of access events.")
    ] = None,
    as_of: AsOf = None,
    out: Annotated[Path, typer.Option(help="Output directory.")] = DATA_DIR,
) -> None:
    """Generate synthetic data with planted findings and a ground-truth file."""
    size = generator.SMALL if small else generator.FULL
    size = replace(size, identities=identities or size.identities, events=events or size.events)
    reference = _date(as_of, date.today())
    with console.status("Generating synthetic data"):
        dataset = generator.generate(seed, size, reference, out)

    rows = Table(title="Generated data")
    rows.add_column("Table")
    rows.add_column("Rows", justify="right")
    for name, df in dataset.tables.items():
        rows.add_row(name, f"{len(df):,}")
    planted = Table(title="Planted findings")
    planted.add_column("Rule")
    planted.add_column("Count", justify="right")
    for key, ids in dataset.planted.items():
        planted.add_row(key, str(len(ids)))
    console.print(rows, planted)
    console.print(f"Seed {seed}, as of {reference.isoformat()}.")
    console.print(f"Wrote CSV files and ground_truth.json to {out}")


@app.command()
def load(data: DataDir = DATA_DIR) -> None:
    """Load CSV files into PostgreSQL, replacing the current model data."""
    if missing := db.missing_csv_files(data):
        console.print(f"[red]Missing in {data}:[/red] {', '.join(missing)}")
        console.print("Run tilgangsinnsikt generate first, or point --data at the right directory.")
        raise typer.Exit(1)
    started = time.perf_counter()
    with database() as conn:
        counts = db.load_csv_dir(conn, data)

    rows = Table(title="Loaded data")
    rows.add_column("Table")
    rows.add_column("Rows", justify="right")
    for name, count in counts.items():
        rows.add_row(name, f"{count:,}")
    console.print(rows)
    console.print(f"Loaded from {data} in {time.perf_counter() - started:.1f} s")


@app.command()
def analyse(
    only: Annotated[
        str | None,
        typer.Option(help=f"Comma-separated analyses. Default: {','.join(runs.ANALYSIS_ORDER)}."),
    ] = None,
    as_of: AsOf = None,
) -> None:
    """Run the analyses, store the findings and print a summary."""
    names = [n.strip() for n in only.split(",") if n.strip()] if only else runs.ANALYSIS_ORDER
    if unknown := [n for n in names if n not in runs.ANALYSIS_ORDER]:
        console.print(f"[red]Unknown analyses:[/red] {', '.join(unknown)}")
        console.print(f"Choose from {', '.join(runs.ANALYSIS_ORDER)}.")
        raise typer.Exit(1)
    reference = _date(as_of, date.today())
    started = time.perf_counter()
    with database() as conn:
        if not conn.execute("select exists (select 1 from identity)").fetchone()[0]:
            console.print("[red]The model tables are empty.[/red] Run tilgangsinnsikt load first.")
            raise typer.Exit(1)
        run, findings = runs.run_analyses(conn, reference, names)

    report = Report.build(run, findings)
    table = Table(title="Findings", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Analysis", no_wrap=True)
    table.add_column("Rule", no_wrap=True)
    for s in SEVERITY_ORDER:
        table.add_column(Text(s, style=terminal.SEVERITY_STYLE[s]), justify="right", no_wrap=True)
    table.add_column("Total", justify="right")
    for analysis, rule, _, counts in report.summary():
        cells = [str(counts[s]) for s in SEVERITY_ORDER]
        table.add_row(analysis, rule, *cells, str(sum(counts.values())))
    console.print(table)
    if report.coverage:
        console.print(f"RBAC coverage: {report.coverage.evidence['coverage']:.1%}")
    console.print(
        f"Run {run.run_id}, as of {run.as_of.isoformat()}: {len(report.findings)} findings"
        f" in {time.perf_counter() - started:.1f} s"
    )


@app.command()
def evaluate(
    data: DataDir = DATA_DIR,
    as_of: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%d"],
            help="Evaluate the latest run with this reference date. Defaults to the date in"
            " ground_truth.json.",
        ),
    ] = None,
    run_id: RunId = None,
) -> None:
    """Compare a run with ground_truth.json and print precision and recall per planted rule."""
    truth_path = data / "ground_truth.json"
    if not truth_path.is_file():
        console.print(f"[red]Missing {truth_path}.[/red] Run tilgangsinnsikt generate first.")
        raise typer.Exit(1)
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    truth_date = date.fromisoformat(truth["as_of"])
    reference = _date(as_of, truth_date)
    if reference != truth_date:
        console.print(
            f"[yellow]The ground truth was planted for {truth_date.isoformat()}; time-based"
            f" rules will not match for {reference.isoformat()}.[/yellow]"
        )
    with database() as conn:
        run = runs.get_run(conn, run_id) if run_id else runs.latest_run(conn, reference)
        if run is None:
            console.print(
                f"[red]No analysis run found{f' with id {run_id}' if run_id else ''}"
                f" for {reference.isoformat()}.[/red]"
            )
            console.print(f"Run tilgangsinnsikt analyse --as-of {reference.isoformat()} first.")
            raise typer.Exit(1)
        findings = runs.load_findings(conn, run.run_id)
    if run.as_of != reference:
        console.print(
            f"[yellow]Run {run.run_id} used as-of {run.as_of.isoformat()}, not"
            f" {reference.isoformat()}.[/yellow]"
        )

    table = Table(title=f"Evaluation of run {run.run_id}", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Rule", no_wrap=True)
    for column in ("Planted", "TP", "FP", "FN", "Prec.", "Recall"):
        table.add_column(column, justify="right", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    failed = False
    for rule, planted in truth["planted"].items():
        analysis = evaluation.RULE_ANALYSIS.get(rule)
        if analysis not in run.analyses:
            table.add_row(rule, str(len(planted)), *[""] * 5, Text("not run", style="dim"))
            continue
        result = evaluation.score(rule, findings, planted)
        failed |= not result.passed
        table.add_row(
            rule,
            str(len(planted)),
            str(result.true_positives),
            str(result.false_positives),
            str(result.false_negatives),
            f"{result.precision:.2f}",
            f"{result.recall:.2f}",
            Text("pass", style="green") if result.passed else Text("FAIL", style="bold red"),
        )
    console.print(table)
    console.print(
        "Targets: precision and recall 1.0 for lifecycle and sod; precision 0.8 and recall"
        " 0.9 for peer_group and log_anomaly."
    )
    console.print(f"Ground truth: {truth_path}")
    if failed:
        raise typer.Exit(1)


@app.command()
def report(
    format: Annotated[str, typer.Option(help="terminal or md.")] = "terminal",
    out: Annotated[Path, typer.Option(help="Output file for --format md.")] = REPORT_PATH,
    run_id: RunId = None,
) -> None:
    """Show the findings of a run as a Norwegian report."""
    if format not in ("terminal", "md"):
        console.print("[red]--format must be terminal or md.[/red]")
        raise typer.Exit(1)
    with database() as conn:
        run = runs.get_run(conn, run_id) if run_id else runs.latest_run(conn)
        if run is None:
            console.print("[red]No analysis run found.[/red] Run tilgangsinnsikt analyse first.")
            raise typer.Exit(1)
        result = Report.build(run, runs.load_findings(conn, run.run_id))
    if format == "terminal":
        terminal.render(result, console)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown.render(result), encoding="utf-8")
    console.print(f"Wrote the report for run {run.run_id} to {out}")


@app.command()
def demo(
    seed: Annotated[int, typer.Option(help="Random seed.")] = 42,
    as_of: AsOf = None,
    data: Annotated[Path, typer.Option(help="Directory for the synthetic data.")] = DATA_DIR,
) -> None:
    """Generate a small synthetic dataset, load it, analyse it and print the report."""
    reference = _date(as_of, date.today())
    as_of_value = datetime(reference.year, reference.month, reference.day)
    generate(seed=seed, small=True, identities=None, events=None, as_of=as_of_value, out=data)
    load(data=data)
    analyse(only=None, as_of=as_of_value)
    report(format="terminal", out=REPORT_PATH, run_id=None)


@app.command("connect-entra")
def connect_entra(
    dry_run: Annotated[
        bool, typer.Option(help="Read from Graph and print counts without writing anything.")
    ] = False,
) -> None:
    """Read users, groups and memberships from Microsoft Entra ID (read-only Graph access)."""
    settings = EntraSettings()
    try:
        entra.require(settings)
        patterns = entra.load_risk_patterns(settings.privileged_groups)
        identity_map = (
            entra.load_identity_map(settings.identity_map) if settings.identity_map else {}
        )
        with console.status("Reading from Microsoft Graph"):
            snapshot = entra.fetch(entra.client(settings))
    except entra.EntraError as error:
        console.print(f"[red]Entra ID: {error}[/red]")
        raise typer.Exit(1) from None
    except OSError as error:
        console.print(f"[red]Could not read a settings file ({type(error).__name__}).[/red]")
        raise typer.Exit(1) from None
    except httpx.HTTPError as error:
        console.print(f"[red]Could not reach Microsoft Graph ({type(error).__name__}).[/red]")
        raise typer.Exit(1) from None

    if dry_run:
        identities = _identity_ids()
        if identities is None:
            console.print("[yellow]No database: links to identities were not checked.[/yellow]")
        rows = entra.map_snapshot(snapshot, identities or set(), identity_map, patterns)
    else:
        with database() as conn:
            identities = {r[0] for r in conn.execute("select identity_id from identity")}
            rows = entra.map_snapshot(snapshot, identities, identity_map, patterns)
            entra.write(conn, rows)

    table = Table(title="Entra ID (counts only)")
    table.add_column("Item")
    table.add_column("Count", justify="right")
    for item, count in sorted(rows.counts.items()):
        table.add_row(item, f"{count:,}")
    console.print(table)
    if not snapshot.sign_in_activity:
        console.print(
            "[yellow]signInActivity is not available (it needs Entra ID P1 and"
            " AuditLog.Read.All), so last_login is empty for every Entra account.[/yellow]"
        )
    if dry_run:
        console.print("Dry run: nothing was written.")
    else:
        console.print("Replaced all rows with source entra; rows from other sources are unchanged.")


def _identity_ids() -> set[str] | None:
    try:
        with db.connect() as conn:
            return {r[0] for r in conn.execute("select identity_id from identity")}
    except psycopg.Error:
        return None
