"""Thin MCP server that wraps Nostos CLI commands."""

from __future__ import annotations

from collections.abc import Sequence

from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from nostos.cli import app as cli_app

mcp = MCPServer("nostos")


def _invoke_cli(argv: Sequence[str]) -> str:
    runner = CliRunner()
    result = runner.invoke(cli_app, list(argv))
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    output = "".join(parts)
    if result.exit_code != 0:
        command = " ".join(argv)
        message = output.strip() or f"nostos {command} exited with code {result.exit_code}"
        raise RuntimeError(message)
    return output


def _append_flag(argv: list[str], flag: str, *, enabled: bool) -> None:
    if enabled:
        argv.append(flag)


def _append_option(argv: list[str], flag: str, value: str | float | int) -> None:
    argv.extend([flag, str(value)])


def _append_options(argv: list[str], flag: str, values: Sequence[str] | None) -> None:
    if values is None:
        return
    for value in values:
        if value:
            argv.extend([flag, value])


@mcp.tool()
def init(
    profile: str | None = None,
    citypack: str | None = None,
    force: bool = False,
    non_interactive: bool = False,
    city: str | None = None,
    max_rent: float | None = None,
    beds: float | None = None,
    baths_min: float | None = None,
    baths_max: float | None = None,
    min_area: float | None = None,
    laundry: str | None = None,
    must_have_laundry: bool | None = None,
    parking: str | None = None,
    pets: str | None = None,
    avoid_basement: bool = True,
    require_unfurnished: bool = True,
    source: list[str] | None = None,
    notify_url: list[str] | None = None,
    schedule: str = "0 */6 * * *",
) -> str:
    """Create a user profile. Wraps `nostos init`."""

    argv = ["init"]
    if profile is not None:
        _append_option(argv, "--profile", profile)
    if citypack is not None:
        _append_option(argv, "--citypack", citypack)
    _append_flag(argv, "--force", enabled=force)
    _append_flag(argv, "--non-interactive", enabled=non_interactive)
    if city is not None:
        _append_option(argv, "--city", city)
    if max_rent is not None:
        _append_option(argv, "--max-rent", max_rent)
    if beds is not None:
        _append_option(argv, "--beds", beds)
    if baths_min is not None:
        _append_option(argv, "--baths-min", baths_min)
    if baths_max is not None:
        _append_option(argv, "--baths-max", baths_max)
    if min_area is not None:
        _append_option(argv, "--min-area", min_area)
    if laundry is not None:
        _append_option(argv, "--laundry", laundry)
    if must_have_laundry is not None:
        _append_flag(argv, "--must-have-laundry", enabled=must_have_laundry)
    if parking is not None:
        _append_option(argv, "--parking", parking)
    if pets is not None:
        _append_option(argv, "--pets", pets)
    _append_flag(argv, "--avoid-basement" if avoid_basement else "--allow-basement", enabled=True)
    _append_flag(
        argv,
        "--require-unfurnished" if require_unfurnished else "--allow-furnished",
        enabled=True,
    )
    _append_options(argv, "--source", source)
    _append_options(argv, "--notify-url", notify_url)
    _append_option(argv, "--schedule", schedule)
    return _invoke_cli(argv)


@mcp.tool()
def watch(
    profile: str | None = None,
    db: str | None = None,
    citypack: str | None = None,
    source: list[str] | None = None,
    dry_run: bool = False,
    yes: bool = True,
) -> str:
    """Run the watch pipeline. Wraps `nostos watch`.

    Defaults `yes` to true because MCP runs are non-interactive.
    """

    argv = ["watch"]
    if profile is not None:
        _append_option(argv, "--profile", profile)
    if db is not None:
        _append_option(argv, "--db", db)
    if citypack is not None:
        _append_option(argv, "--citypack", citypack)
    _append_options(argv, "--source", source)
    _append_flag(argv, "--dry-run", enabled=dry_run)
    _append_flag(argv, "--yes", enabled=yes)
    return _invoke_cli(argv)


@mcp.tool()
def rank(
    profile: str | None = None,
    db: str | None = None,
    citypack: str | None = None,
) -> str:
    """Re-score stored listings. Wraps `nostos rank`."""

    argv = ["rank"]
    if profile is not None:
        _append_option(argv, "--profile", profile)
    if db is not None:
        _append_option(argv, "--db", db)
    if citypack is not None:
        _append_option(argv, "--citypack", citypack)
    return _invoke_cli(argv)


@mcp.tool(name="list")
def list_command(
    profile: str | None = None,
    db: str | None = None,
    citypack: str | None = None,
    limit: int = 20,
) -> str:
    """List ranked listings. Wraps `nostos list`."""

    argv = ["list"]
    if profile is not None:
        _append_option(argv, "--profile", profile)
    if db is not None:
        _append_option(argv, "--db", db)
    if citypack is not None:
        _append_option(argv, "--citypack", citypack)
    _append_option(argv, "--limit", limit)
    return _invoke_cli(argv)


@mcp.tool()
def explain(
    listing_id: str,
    profile: str | None = None,
    db: str | None = None,
) -> str:
    """Explain a listing score. Wraps `nostos explain`."""

    argv = ["explain", listing_id]
    if profile is not None:
        _append_option(argv, "--profile", profile)
    if db is not None:
        _append_option(argv, "--db", db)
    return _invoke_cli(argv)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
