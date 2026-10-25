from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tilgangsinnsikt import __version__
from tilgangsinnsikt.synth import generator

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


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
    as_of: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Reference date. Defaults to today."),
    ] = None,
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("data/synthetic"),
) -> None:
    """Generate synthetic data with planted findings and a ground-truth file."""
    size = generator.SMALL if small else generator.FULL
    size = replace(size, identities=identities or size.identities, events=events or size.events)
    reference = as_of.date() if as_of else date.today()
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
