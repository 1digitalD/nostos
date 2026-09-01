"""CLI entrypoint for Nostos."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NamedTuple, NoReturn

import typer

from nostos.config.citypack import Citypack, load_citypack
from nostos.config.profile import ScaledWeight, WeightValue
from nostos.config.wizard import (
    PetsPreference,
    PreferenceLevel,
    WizardAnswers,
    build_profile_payload,
    dump_profile_yaml,
    missing_required_values,
)
from nostos.context import load_search_context
from nostos.enrich.text import TextRuleEnricher
from nostos.model import SourceRecord
from nostos.rank.engine import NormalizationWindow, RankEngine, RuleContribution, ScoreResult
from nostos.rank.explain import render_score_explanation
from nostos.rank.profile_scoring import (
    listing_title,
    prepare_listing_for_profile,
    rent_display,
    score_listing_for_profile,
)
from nostos.rank.rules import Signal
from nostos.sources import (
    CraigslistSource,
    KijijiSource,
    Source,
    enabled_sources,
    resolve_source_registry,
)
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ScoreRepo
from nostos.watch.notify import NullNotifier
from nostos.watch.runner import run_watch
from nostos.web.query import ListFilter

DEFAULT_CITYPACK_FILE = "vancouver.yaml"
DEFAULT_PROFILE_FILE = "profile.yaml"
DEFAULT_DB_FILE = "nostos.db"
DEFAULT_SCHEDULE = "0 */6 * * *"

SOURCE_FACTORIES: dict[str, type[Source] | Any] = {
    "craigslist": CraigslistSource,
    "kijiji": KijijiSource,
}

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Nostos: self-hosted rental-market watch ranked by your rubric.\n\n"
        "Examples:\n"
        "  nostos init\n"
        "  nostos watch --yes\n"
        "  nostos list --limit 20\n"
        "  nostos explain craigslist:abc123"
    ),
)


@app.callback()
def main() -> None:
    """Run the Nostos CLI."""


@app.command("init")
def init_command(
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile output path. Defaults to XDG config location.",
        ),
    ] = None,
    citypack: Annotated[
        Path | None,
        typer.Option(
            "--citypack",
            help="Citypack YAML/JSON path. Defaults to packaged citypacks/vancouver.yaml.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing profile file.",
        ),
    ] = False,
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            help="Disable prompts and require all required values as flags.",
        ),
    ] = False,
    city: Annotated[
        str | None,
        typer.Option("--city", help="City name that must match citypack.name."),
    ] = None,
    max_rent: Annotated[
        float | None,
        typer.Option("--max-rent", min=1.0, help="Maximum monthly rent."),
    ] = None,
    beds: Annotated[
        float | None,
        typer.Option("--beds", min=0.0, help="Exact bedroom count."),
    ] = None,
    baths_min: Annotated[
        float | None,
        typer.Option("--baths-min", min=0.0, help="Minimum bathrooms."),
    ] = None,
    baths_max: Annotated[
        float | None,
        typer.Option("--baths-max", min=0.0, help="Maximum bathrooms."),
    ] = None,
    min_area: Annotated[
        float | None,
        typer.Option("--min-area", min=0.0, help="Minimum floor area."),
    ] = None,
    laundry: Annotated[
        PreferenceLevel | None,
        typer.Option(
            "--laundry",
            help="In-suite laundry preference: deal-breaker, nice-to-have, or dont-care.",
        ),
    ] = None,
    must_have_laundry: Annotated[
        bool | None,
        typer.Option(
            "--must-have-laundry",
            help="Shortcut for --laundry deal-breaker.",
        ),
    ] = None,
    parking: Annotated[
        PreferenceLevel | None,
        typer.Option(
            "--parking",
            help="Parking preference: deal-breaker, nice-to-have, or dont-care.",
        ),
    ] = None,
    pets: Annotated[
        PetsPreference | None,
        typer.Option(
            "--pets",
            help="Pet-friendly preference: prefer, avoid, or dont-care.",
        ),
    ] = None,
    avoid_basement: Annotated[
        bool,
        typer.Option(
            "--avoid-basement/--allow-basement",
            help="Exclude basement listings by default.",
        ),
    ] = True,
    require_unfurnished: Annotated[
        bool,
        typer.Option(
            "--require-unfurnished/--allow-furnished",
            help="Exclude furnished-only listings by default.",
        ),
    ] = True,
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Repeatable source name (for example --source craigslist --source kijiji).",
        ),
    ] = None,
    notify_url: Annotated[
        list[str] | None,
        typer.Option(
            "--notify-url",
            help="Repeatable notification URL (for example ntfy://host/topic).",
        ),
    ] = None,
    schedule: Annotated[
        str,
        typer.Option(
            "--schedule",
            help="Cron expression for watch schedule metadata.",
        ),
    ] = DEFAULT_SCHEDULE,
) -> None:
    """Create a user profile from plain-language preferences.

    Examples:
      nostos init
      nostos init --non-interactive --city vancouver --max-rent 3400 --beds 2 \
--laundry nice-to-have --pets avoid --source craigslist --source kijiji
      nostos init --profile ~/.config/nostos/profile.yaml --force
    """

    profile_path = _resolve_profile_path(profile)
    citypack_path = _resolve_citypack_path(citypack)
    _require_file(citypack_path, "--citypack")
    loaded_citypack = load_citypack(citypack_path)

    if profile_path.exists() and not force:
        _fail(
            f"Profile already exists at {profile_path}. Re-run with --force to overwrite."
        )

    explicit_laundry = _resolve_laundry_pref(laundry=laundry, must_have_laundry=must_have_laundry)
    interactive = sys.stdin.isatty() and not non_interactive

    selected_city = city or loaded_citypack.name
    selected_max_rent = max_rent
    selected_beds = beds
    selected_laundry = explicit_laundry
    selected_parking = parking
    selected_pets = pets
    selected_sources = list(source or ())
    selected_notify = list(notify_url or ())

    missing = missing_required_values(
        max_rent=selected_max_rent,
        beds=selected_beds,
        laundry=selected_laundry,
    )
    if missing and not interactive:
        missing_list = ", ".join(missing)
        _fail(
            "Missing required values for non-interactive init: "
            f"{missing_list}\n"
            "Example:\n"
            "  nostos init --non-interactive --city vancouver --max-rent 3400 --beds 2\n"
            "      --laundry nice-to-have --pets avoid --source craigslist --source kijiji"
        )

    if interactive:
        selected_city = _prompt_text(
            "City",
            default=selected_city,
        )
        if selected_max_rent is None:
            selected_max_rent = _prompt_float("Max monthly rent", default=3400.0, minimum=1.0)
        if selected_beds is None:
            selected_beds = _prompt_float("Bedrooms (exact)", default=2.0, minimum=0.0)
        if selected_laundry is None:
            selected_laundry = _prompt_preference(
                "In-suite laundry: deal-breaker, nice-to-have, or dont-care?",
                default=PreferenceLevel.NICE_TO_HAVE,
            )
        if selected_parking is None:
            selected_parking = _prompt_preference(
                "Parking: deal-breaker, nice-to-have, or dont-care?",
                default=PreferenceLevel.NICE_TO_HAVE,
            )
        if selected_pets is None:
            selected_pets = _prompt_pets_preference(
                "Pet-friendly listings: prefer, avoid, or dont-care?",
                default=PetsPreference.DONT_CARE,
            )
        if not selected_sources:
            enabled_by_default = [
                name
                for name, source_cfg in loaded_citypack.sources.items()
                if source_cfg.enabled
            ]
            default_text = ",".join(enabled_by_default) if enabled_by_default else ""
            source_text = _prompt_text(
                "Sources (comma-separated names from citypack.sources)",
                default=default_text,
            )
            selected_sources = _split_csv(source_text)
        if not selected_notify:
            notify_text = _prompt_text(
                "Notification URLs (comma-separated, optional)",
                default="",
            )
            selected_notify = _split_csv(notify_text)
    if selected_city != loaded_citypack.name:
        _fail(
            "profile.city must match citypack.name "
            f"(got profile.city={selected_city!r}, citypack.name={loaded_citypack.name!r})"
        )

    validated_sources = _validated_source_list(
        selected=selected_sources,
        citypack=loaded_citypack,
    )
    if not validated_sources:
        _fail("At least one --source must be selected for init.")

    assert selected_max_rent is not None
    assert selected_beds is not None
    assert selected_laundry is not None

    answers = WizardAnswers(
        city=selected_city,
        max_rent=selected_max_rent,
        beds=selected_beds,
        baths_min=baths_min,
        baths_max=baths_max,
        min_area=min_area,
        laundry=selected_laundry,
        parking=selected_parking,
        pets=selected_pets,
        source_names=tuple(validated_sources),
        notify_urls=tuple(selected_notify),
        avoid_basement=avoid_basement,
        require_unfurnished=require_unfurnished,
        schedule=schedule,
    )
    payload = build_profile_payload(answers=answers, citypack=loaded_citypack)

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(dump_profile_yaml(payload), encoding="utf-8")
    try:
        load_search_context(citypack_path=citypack_path, profile_path=profile_path)
    except ValueError as exc:
        _fail(str(exc))

    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"profile_id={_profile_id(profile_path)}")
    typer.echo(f"citypack_path={citypack_path}")


@app.command("watch")
def watch_command(
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile file path. Defaults to XDG config location.",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite DB path. Defaults to XDG data location."),
    ] = None,
    citypack: Annotated[
        Path | None,
        typer.Option(
            "--citypack",
            help="Citypack YAML/JSON path. Defaults to packaged citypacks/vancouver.yaml.",
        ),
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Limit run to these source names."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Dry run: no persist, no notify; still scrapes enabled sources "
                "against an in-memory DB."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation prompts."),
    ] = False,
) -> None:
    """Run the watch pipeline and score new listings.

    Examples:
      nostos watch --yes
      nostos watch --source craigslist --source kijiji --db ~/.local/share/nostos/nostos.db --yes
      nostos watch --dry-run --source craigslist
    """

    profile_path = _resolve_profile_path(profile)
    db_path = _resolve_db_path(db)
    citypack_path = _resolve_citypack_path(citypack)
    _require_file(profile_path, "--profile")
    _require_file(citypack_path, "--citypack")

    context = load_search_context(citypack_path=citypack_path, profile_path=profile_path)
    chosen_sources = _instantiate_sources(source_names=source)
    resolutions = resolve_source_registry(
        context=context,
        sources=chosen_sources,
        credentials_present={name: True for name in SOURCE_FACTORIES},
    )
    active_sources = enabled_sources(resolutions)
    if not active_sources:
        detail = "\n".join(
            f"  - {item.name}: {item.reason} ({item.detail})"
            for item in resolutions
        )
        _fail(f"No enabled sources resolved for watch.\n{detail}")

    if not dry_run:
        _confirm_if_needed(
            yes=yes,
            message=(
                f"Run watch with profile {profile_path} against {db_path}? "
                "This writes to the database and may send notifications."
            ),
        )

    profile_id = _profile_id(profile_path)
    watch_db_target = Path(":memory:") if dry_run else db_path
    notifier = NullNotifier() if dry_run else None
    with connect(watch_db_target) as conn:
        apply_migrations(conn)
        report = run_watch(
            conn=conn,
            context=context,
            sources=active_sources,
            profile_id=profile_id,
            notifier=notifier,
        )

    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"db_path={watch_db_target}")
    typer.echo(f"profile_id={profile_id}")
    typer.echo(f"run_id={report.run_id}")
    typer.echo(f"dry_run={str(dry_run).lower()}")
    for name, source_report in sorted(report.source_reports.items()):
        typer.echo(
            f"source={name}\tstatus={source_report.status}\tcount={source_report.count}"
        )
    for alert in report.alerts:
        typer.echo(f"alert={alert}")


@app.command("rank")
def rank_command(
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile file path. Defaults to XDG config location.",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite DB path. Defaults to XDG data location."),
    ] = None,
    citypack: Annotated[
        Path | None,
        typer.Option(
            "--citypack",
            help="Citypack YAML/JSON path. Defaults to packaged citypacks/vancouver.yaml.",
        ),
    ] = None,
) -> None:
    """Re-score stored listings using the active profile.

    Examples:
      nostos rank
      nostos rank --profile ~/.config/nostos/profile-work.yaml --db ~/.local/share/nostos/nostos.db
      nostos rank --citypack ./src/nostos/citypacks/vancouver.yaml
    """

    profile_path = _resolve_profile_path(profile)
    db_path = _resolve_db_path(db)
    citypack_path = _resolve_citypack_path(citypack)
    _require_file(profile_path, "--profile")
    _require_file(citypack_path, "--citypack")
    _require_file(db_path, "--db")

    context = load_search_context(citypack_path=citypack_path, profile_path=profile_path)
    profile_id = _profile_id(profile_path)
    rank_engine = RankEngine(context.profile)
    enrichers = (TextRuleEnricher(),)
    source_objects = {item.name: item for item in _instantiate_sources(source_names=None)}

    scored_rows: list[tuple[str, float]] = []
    with connect(db_path) as conn:
        apply_migrations(conn)
        rows = _latest_source_records(conn)
        score_repo = ScoreRepo(conn)
        with conn:
            conn.execute("DELETE FROM score WHERE profile_id = ?", (profile_id,))
            for record_row in rows:
                source_obj = source_objects.get(record_row.record.source)
                if source_obj is None:
                    continue
                listing = source_obj.to_listing(record_row.record, context)
                # `to_listing` is pure and only knows the raw record's own
                # source/source_id, so it always derives a per-source identity.
                # `record_row.listing_id` is the canonical id this record was
                # actually stored under (post cross-source dedupe) — key the
                # score on that, not on the freshly recomputed identity.
                if listing.identity.listing_id != record_row.listing_id:
                    listing = listing.model_copy(
                        update={
                            "identity": listing.identity.model_copy(
                                update={"listing_id": record_row.listing_id}
                            )
                        }
                    )
                scored_listing = score_listing_for_profile(
                    listing,
                    context=context,
                    enrichers=enrichers,
                    rank_engine=rank_engine,
                )
                if scored_listing is None:
                    continue
                result = scored_listing.result
                score_repo.upsert_score(
                    listing_id=scored_listing.listing.identity.listing_id,
                    profile_id=profile_id,
                    score=result.score,
                    breakdown_json=_score_result_to_json(result),
                    computed_at=record_row.record.fetched_at,
                )
                scored_rows.append((scored_listing.listing.identity.listing_id, result.score))

    scored_rows.sort(key=lambda item: item[1], reverse=True)
    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"db_path={db_path}")
    typer.echo(f"profile_id={profile_id}")
    typer.echo(f"ranked_count={len(scored_rows)}")
    for listing_id, score_value in scored_rows:
        typer.echo(f"listing_id={listing_id}\tscore={score_value:.3f}")


@app.command("list")
def list_command(
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile file path. Defaults to XDG config location.",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite DB path. Defaults to XDG data location."),
    ] = None,
    citypack: Annotated[
        Path | None,
        typer.Option(
            "--citypack",
            help="Citypack YAML/JSON path. Defaults to packaged citypacks/vancouver.yaml.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, help="Maximum number of ranked listings."),
    ] = 20,
) -> None:
    """List ranked listings for the active profile.

    Examples:
      nostos list
      nostos list --limit 10
      nostos list --profile ~/.config/nostos/profile.yaml --db ~/.local/share/nostos/nostos.db
    """

    profile_path = _resolve_profile_path(profile)
    db_path = _resolve_db_path(db)
    citypack_path = _resolve_citypack_path(citypack)
    _require_file(profile_path, "--profile")
    _require_file(db_path, "--db")
    _require_file(citypack_path, "--citypack")

    profile_id = _profile_id(profile_path)
    context = load_search_context(citypack_path=citypack_path, profile_path=profile_path)
    source_objects = {item.name: item for item in _instantiate_sources(source_names=None)}
    enrichers = (TextRuleEnricher(),)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT listing_id, score
            FROM score
            WHERE profile_id = ?
            ORDER BY score DESC, listing_id ASC
            """,
            (profile_id,),
        ).fetchall()
        listing_ids = tuple(str(row["listing_id"]) for row in rows)
        latest_records = _latest_source_records_by_listing_ids(conn, listing_ids=listing_ids)

    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"db_path={db_path}")
    typer.echo(f"citypack_path={citypack_path}")
    typer.echo(f"profile_id={profile_id}")
    rendered = 0
    for row in rows:
        if rendered >= limit:
            break
        listing_id = str(row["listing_id"])
        record_row = latest_records.get(listing_id)
        if record_row is None:
            continue
        source_obj = source_objects.get(record_row.record.source)
        if source_obj is None:
            continue
        listing = source_obj.to_listing(record_row.record, context)
        prepared = prepare_listing_for_profile(
            listing,
            context=context,
            enrichers=enrichers,
        )
        if prepared is None:
            continue
        score_value = float(row["score"])
        title = _tab_safe(listing_title(prepared))
        url = _tab_safe(prepared.identity.url)
        rent = _tab_safe(rent_display(prepared))
        typer.echo(
            f"listing_id={listing_id}\tscore={score_value:.3f}\ttitle={title}\turl={url}\trent={rent}"
        )
        rendered += 1
    typer.echo(f"listed_count={rendered}")


@app.command("explain")
def explain_command(
    listing_id: Annotated[str, typer.Argument(help="Listing ID to explain.")],
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile file path. Defaults to XDG config location.",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite DB path. Defaults to XDG data location."),
    ] = None,
) -> None:
    """Explain why one listing scored the way it did.

    Examples:
      nostos explain craigslist:abc123
      nostos explain kijiji:1234567890 --profile ~/.config/nostos/profile.yaml
      nostos explain stub:listing-1 --db /tmp/nostos.db
    """

    profile_path = _resolve_profile_path(profile)
    db_path = _resolve_db_path(db)
    _require_file(profile_path, "--profile")
    _require_file(db_path, "--db")

    profile_id = _profile_id(profile_path)
    with connect(db_path) as conn:
        score_row = ScoreRepo(conn).get_score(listing_id, profile_id)

    if score_row is None:
        _fail(
            f"No score found for listing {listing_id!r} under profile_id {profile_id!r}. "
            "Run `nostos rank` first."
        )

    score_result = _score_result_from_json(score_row.breakdown_json, fallback_score=score_row.score)
    explanation = render_score_explanation(score_result)
    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"db_path={db_path}")
    typer.echo(f"profile_id={profile_id}")
    typer.echo(f"listing_id={listing_id}")
    typer.echo(f"score={score_row.score:.3f}")
    typer.echo("")
    typer.echo(explanation)


@app.command("web")
def web_command(
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile",
            help="Profile file path. Defaults to XDG config location.",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite DB path. Defaults to XDG data location."),
    ] = None,
    citypack: Annotated[
        Path | None,
        typer.Option(
            "--citypack",
            help="Citypack YAML/JSON path. Defaults to packaged citypacks/vancouver.yaml.",
        ),
    ] = None,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="Port for the local web server (ignored in --export mode).",
        ),
    ] = 8421,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Bind address. Defaults to 127.0.0.1; any non-loopback host logs a loud warning.",
        ),
    ] = "127.0.0.1",
    export: Annotated[
        Path | None,
        typer.Option(
            "--export",
            help="Write a single self-contained HTML file to this path and exit.",
        ),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser",
            help="Skip auto-opening the browser when serving (serve mode only).",
        ),
    ] = False,
    filter_rent_max: Annotated[
        float | None,
        typer.Option(
            "--filter-rent-max",
            min=0,
            help="Export filter: max rent (export only).",
        ),
    ] = None,
    filter_score_min: Annotated[
        float | None,
        typer.Option(
            "--filter-score-min",
            min=0,
            max=100,
            help="Export filter: minimum score (export only).",
        ),
    ] = None,
    filter_source: Annotated[
        str | None,
        typer.Option(
            "--filter-source",
            help="Export filter: source name (export only).",
        ),
    ] = None,
) -> None:
    """Browse and act on ranked listings through a local web UI.

    Two modes:

    - serve (default): starts a FastAPI app bound to 127.0.0.1 on --port.
    - export: writes a single self-contained HTML file to --export path so it
      can be opened in any browser without a server (read-only — use the
      serve mode to record star/dismiss/contacted/note actions).

    Examples:
      nostos web
      nostos web --port 9000
      nostos web --export ~/Desktop/listings.html
      nostos web --export ~/Desktop/top.html --filter-rent-max 2800 --filter-score-min 70
      nostos web --host 0.0.0.0 --port 8421
    """

    profile_path = _resolve_profile_path(profile)
    db_path = _resolve_db_path(db)
    citypack_path = _resolve_citypack_path(citypack)
    _require_file(profile_path, "--profile")
    _require_file(citypack_path, "--citypack")
    _require_file(db_path, "--db")

    context = load_search_context(citypack_path=citypack_path, profile_path=profile_path)
    profile_id = _profile_id(profile_path)
    source_objects = {item.name: item for item in _instantiate_sources(source_names=None)}

    if export is not None:
        from nostos.web.query import query_list

        export_filters = ListFilter(
            rent_max=filter_rent_max,
            score_min=filter_score_min,
            source=filter_source,
        )
        with connect(db_path) as conn:
            rows = query_list(
                conn,
                context=context,
                profile_id=profile_id,
                sources=source_objects,
                filters=export_filters,
            )
        from nostos.web.static_export import write_static_export

        written = write_static_export(
            rows,
            output_path=export,
            profile_id=profile_id,
        )
        typer.echo(f"profile_path={profile_path}")
        typer.echo(f"db_path={db_path}")
        typer.echo(f"citypack_path={citypack_path}")
        typer.echo(f"profile_id={profile_id}")
        typer.echo(f"export_path={written}")
        typer.echo(f"listings_written={len(rows)}")
        return

    if host != "127.0.0.1" and not host.startswith("127."):
        typer.secho(
            f"WARNING: binding {host!r} exposes the web UI on your network. "
            "It has no auth — anyone who can reach it can record actions on your data.",
            fg=typer.colors.RED,
            err=True,
        )

    import webbrowser

    import uvicorn

    from nostos.web import create_app

    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    typer.echo(f"profile_path={profile_path}")
    typer.echo(f"db_path={db_path}")
    typer.echo(f"citypack_path={citypack_path}")
    typer.echo(f"profile_id={profile_id}")
    typer.echo(f"url=http://{host}:{port}/")
    typer.echo("Press Ctrl+C to stop the server.")
    url = f"http://{host}:{port}/"
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            typer.secho(
                f"Could not auto-open browser ({exc!s}); open {url} manually.",
                fg=typer.colors.YELLOW,
                err=True,
            )
    uvicorn.run(app, host=host, port=port, log_level="info")


class _SourceRecordRow(NamedTuple):
    listing_id: str
    record: SourceRecord


def _resolve_laundry_pref(
    *,
    laundry: PreferenceLevel | None,
    must_have_laundry: bool | None,
) -> PreferenceLevel | None:
    if must_have_laundry is None:
        return laundry
    if must_have_laundry:
        if laundry is not None and laundry != PreferenceLevel.DEAL_BREAKER:
            _fail(
                "--must-have-laundry conflicts with --laundry. Use either --must-have-laundry "
                "or --laundry deal-breaker."
            )
        return PreferenceLevel.DEAL_BREAKER
    if laundry is not None:
        return laundry
    return PreferenceLevel.DONT_CARE


def _validated_source_list(*, selected: Iterable[str], citypack: Citypack) -> list[str]:
    known = set(citypack.sources)
    names = [item.strip() for item in selected if item.strip()]
    if not names:
        names = [
            source_name
            for source_name, source_cfg in citypack.sources.items()
            if source_cfg.enabled
        ]
    invalid = sorted(name for name in names if name not in known)
    if invalid:
        available = ", ".join(sorted(known))
        unknown = ", ".join(invalid)
        _fail(f"Unknown source(s): {unknown}. Available from citypack: {available}.")
    deduped: list[str] = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    return deduped


def _instantiate_sources(*, source_names: Iterable[str] | None) -> tuple[Source, ...]:
    requested = list(source_names) if source_names is not None else list(SOURCE_FACTORIES)
    if not requested:
        _fail("No sources configured. Add at least one source.")

    instances: list[Source] = []
    unknown = sorted(name for name in requested if name not in SOURCE_FACTORIES)
    if unknown:
        known = ", ".join(sorted(SOURCE_FACTORIES))
        _fail(f"Unknown --source value(s): {', '.join(unknown)}. Known sources: {known}.")

    for name in requested:
        factory = SOURCE_FACTORIES[name]
        instance = factory()
        instances.append(instance)
    return tuple(instances)


def _resolve_profile_path(path: Path | None) -> Path:
    if path is not None:
        return path.expanduser()
    return _default_config_dir() / DEFAULT_PROFILE_FILE


def _resolve_db_path(path: Path | None) -> Path:
    if path is not None:
        return path.expanduser()
    return _default_data_dir() / DEFAULT_DB_FILE


def _resolve_citypack_path(path: Path | None) -> Path:
    if path is not None:
        return path.expanduser()
    for candidate in _default_citypack_candidates():
        if candidate.exists():
            return candidate
    return _default_citypack_candidates()[0]


def _default_citypack_candidates() -> list[Path]:
    package_citypack = Path(__file__).resolve().parent / "citypacks" / DEFAULT_CITYPACK_FILE
    return [
        Path.cwd() / "citypacks" / DEFAULT_CITYPACK_FILE,
        package_citypack,
    ]


def _default_config_dir() -> Path:
    nostos_home = os.environ.get("NOSTOS_HOME")
    if nostos_home:
        return Path(nostos_home).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "nostos"
    return Path.home() / ".config" / "nostos"


def _default_data_dir() -> Path:
    nostos_home = os.environ.get("NOSTOS_HOME")
    if nostos_home:
        return Path(nostos_home).expanduser()
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "nostos"
    return Path.home() / ".local" / "share" / "nostos"


def _profile_id(profile_path: Path) -> str:
    return profile_path.stem


def _require_file(path: Path, option_name: str) -> None:
    if path.exists():
        return
    _fail(
        f"File not found: {path}\n"
        f"Provide {option_name} explicitly or run `nostos init` first."
    )


def _confirm_if_needed(*, yes: bool, message: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        _fail(f"{message}\nNon-interactive run requires --yes.")
    should_continue = typer.confirm(message, default=False)
    if not should_continue:
        raise typer.Exit(code=1)


def _prompt_text(prompt: str, *, default: str) -> str:
    value = str(typer.prompt(prompt, default=default, show_default=True)).strip()
    if value:
        return value
    _fail(f"{prompt} cannot be empty.")


def _prompt_float(prompt: str, *, default: float, minimum: float) -> float:
    value = typer.prompt(prompt, default=default, show_default=True)
    if value < minimum:
        _fail(f"{prompt} must be >= {minimum}.")
    return float(value)


def _prompt_preference(prompt: str, *, default: PreferenceLevel) -> PreferenceLevel:
    raw = typer.prompt(prompt, default=default.value, show_default=True).strip().lower()
    try:
        return PreferenceLevel(raw)
    except ValueError as exc:
        _fail("Expected one of: deal-breaker, nice-to-have, dont-care.")
        raise AssertionError from exc


def _prompt_pets_preference(prompt: str, *, default: PetsPreference) -> PetsPreference:
    raw = typer.prompt(prompt, default=default.value, show_default=True).strip().lower()
    try:
        return PetsPreference(raw)
    except ValueError as exc:
        _fail("Expected one of: prefer, avoid, dont-care.")
        raise AssertionError from exc


def _split_csv(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _latest_source_records(conn: sqlite3.Connection) -> tuple[_SourceRecordRow, ...]:
    rows = conn.execute(
        """
        SELECT
            sr.listing_id AS listing_id,
            sr.source AS source,
            sr.source_id AS source_id,
            sr.url AS url,
            sr.payload AS payload,
            sr.content_hash AS content_hash,
            sr.fetched_at AS fetched_at
        FROM source_record sr
        INNER JOIN (
            SELECT listing_id, MAX(id) AS latest_id
            FROM source_record
            GROUP BY listing_id
        ) latest
        ON latest.latest_id = sr.id
        ORDER BY sr.listing_id ASC
        """,
    ).fetchall()

    records: list[_SourceRecordRow] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, Mapping):
            continue
        fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
        source_record = SourceRecord(
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            url=str(row["url"]),
            payload=dict(payload),
            content_hash=str(row["content_hash"]),
            fetched_at=fetched_at,
        )
        records.append(
            _SourceRecordRow(
                listing_id=str(row["listing_id"]),
                record=source_record,
            )
        )
    return tuple(records)


def _latest_source_records_by_listing_ids(
    conn: sqlite3.Connection, *, listing_ids: tuple[str, ...]
) -> dict[str, _SourceRecordRow]:
    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT
            sr.listing_id AS listing_id,
            sr.source AS source,
            sr.source_id AS source_id,
            sr.url AS url,
            sr.payload AS payload,
            sr.content_hash AS content_hash,
            sr.fetched_at AS fetched_at
        FROM source_record sr
        INNER JOIN (
            SELECT listing_id, MAX(id) AS latest_id
            FROM source_record
            WHERE listing_id IN ({placeholders})
            GROUP BY listing_id
        ) latest
        ON latest.latest_id = sr.id
        ORDER BY sr.listing_id ASC
        """,
        listing_ids,
    ).fetchall()

    records: dict[str, _SourceRecordRow] = {}
    for row in rows:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, Mapping):
            continue
        fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
        source_record = SourceRecord(
            source=str(row["source"]),
            source_id=str(row["source_id"]),
            url=str(row["url"]),
            payload=dict(payload),
            content_hash=str(row["content_hash"]),
            fetched_at=fetched_at,
        )
        listing_id = str(row["listing_id"])
        records[listing_id] = _SourceRecordRow(listing_id=listing_id, record=source_record)
    return records


def _tab_safe(value: str) -> str:
    return value.replace("\t", " ").replace("\n", " ")


def _score_result_to_json(result: ScoreResult) -> dict[str, Any]:
    contributions: list[dict[str, Any]] = []
    for contribution in result.contributions:
        if isinstance(contribution.weight, ScaledWeight):
            weight_json: object = contribution.weight.model_dump(mode="json")
        else:
            weight_json = float(contribution.weight)
        signal_json: dict[str, Any] | None = None
        if contribution.signal is not None:
            signal_json = {
                "fired": contribution.signal.fired,
                "magnitude": contribution.signal.magnitude,
                "confidence": contribution.signal.confidence,
                "evidence": contribution.signal.evidence,
            }
        contributions.append(
            {
                "rule_key": contribution.rule_key,
                "category": contribution.category,
                "label": contribution.label,
                "weight": weight_json,
                "signal": signal_json,
                "shaped_magnitude": contribution.shaped_magnitude,
                "confidence_factor": contribution.confidence_factor,
                "min_possible": contribution.min_possible,
                "max_possible": contribution.max_possible,
                "contribution": contribution.contribution,
            }
        )
    return {
        "score": result.score,
        "total_contribution": result.total_contribution,
        "normalization": {
            "min_possible": result.normalization.min_possible,
            "max_possible": result.normalization.max_possible,
        },
        "contributions": contributions,
    }


def _score_result_from_json(
    payload: Mapping[str, object],
    *,
    fallback_score: float,
) -> ScoreResult:
    normalization_payload = _as_mapping(payload.get("normalization"))
    min_possible = _as_float(normalization_payload.get("min_possible"), default=0.0)
    max_possible = _as_float(normalization_payload.get("max_possible"), default=0.0)
    normalization = NormalizationWindow(min_possible=min_possible, max_possible=max_possible)

    raw_contributions = payload.get("contributions")
    parsed_contributions: list[RuleContribution] = []
    if isinstance(raw_contributions, list):
        for raw_item in raw_contributions:
            parsed_item = _as_mapping(raw_item)
            weight_payload = parsed_item.get("weight")
            weight_value: WeightValue
            if isinstance(weight_payload, Mapping):
                weight_value = ScaledWeight.model_validate(weight_payload)
            else:
                weight_value = _as_float(weight_payload, default=0.0)
            signal_value = _signal_from_json(parsed_item.get("signal"))
            parsed_contributions.append(
                RuleContribution(
                    rule_key=str(parsed_item.get("rule_key", "")),
                    category=str(parsed_item.get("category", "")),
                    label=str(parsed_item.get("label", "")),
                    weight=weight_value,
                    signal=signal_value,
                    shaped_magnitude=_as_float(parsed_item.get("shaped_magnitude"), default=0.0),
                    confidence_factor=_as_float(parsed_item.get("confidence_factor"), default=0.0),
                    min_possible=_as_float(parsed_item.get("min_possible"), default=0.0),
                    max_possible=_as_float(parsed_item.get("max_possible"), default=0.0),
                    contribution=_as_float(parsed_item.get("contribution"), default=0.0),
                )
            )

    score = _as_float(payload.get("score"), default=fallback_score)
    total = _as_float(payload.get("total_contribution"), default=0.0)
    return ScoreResult(
        score=score,
        total_contribution=total,
        normalization=normalization,
        contributions=tuple(parsed_contributions),
    )


def _signal_from_json(value: object) -> Signal | None:
    if not isinstance(value, Mapping):
        return None
    return Signal(
        fired=bool(value.get("fired", False)),
        magnitude=_as_float(value.get("magnitude"), default=0.0),
        confidence=_as_float(value.get("confidence"), default=0.0),
        evidence=_as_optional_string(value.get("evidence")),
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return default


def _as_optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
