import typer

from tilgangsinnsikt import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Tilgangsinnsikt: access governance analytics."""


@app.command()
def version() -> None:
    typer.echo(__version__)
