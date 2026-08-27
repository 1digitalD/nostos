"""CLI entrypoint for Nostos."""

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Nostos: self-hosted rental-market watch ranked by your rubric.",
)


@app.callback()
def main() -> None:
    """Run the Nostos CLI."""


if __name__ == "__main__":
    app()
