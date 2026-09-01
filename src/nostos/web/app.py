"""FastAPI app for the local Nostos web UI."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nostos.context import load_search_context
from nostos.sources import (
    CraigslistSource,
    KijijiSource,
    Source,
    enabled_sources,
    resolve_source_registry,
)
from nostos.store.actions import ActionKind, ActionRepo
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ScoreRepo
from nostos.web.query import (
    ListFilter,
    ListRow,
    known_areas,
    known_sources,
    load_detail,
    query_list,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"



def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["relativetime"] = _render_relative_time
    templates.env.filters["score_badge"] = _render_score_badge
    templates.env.filters["money_short"] = _render_money_short
    return templates


def _render_relative_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC)
    now = datetime.now(tz=UTC)
    delta = now - aware
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _render_score_badge(value: float | None) -> str:
    if value is None:
        return '<span class="score score-none">—</span>'
    if value >= 75:
        cls = "score score-good"
    elif value >= 50:
        cls = "score score-mid"
    else:
        cls = "score score-low"
    return f'<span class="{cls}">{value:.1f}</span>'


def _render_money_short(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"${value / 1000:.2f}k"
    return f"${value:.0f}"


class AppState:
    """Holds resolved context, sources, and templates for the app lifetime."""

    def __init__(
        self,
        *,
        db_path: Path,
        profile_path: Path,
        citypack_path: Path,
        templates: Jinja2Templates,
    ) -> None:
        self.db_path = db_path
        self.profile_path = profile_path
        self.citypack_path = citypack_path
        self.templates = templates

        context = load_search_context(
            citypack_path=citypack_path,
            profile_path=profile_path,
        )
        self.context = context
        self.profile_id = profile_path.stem

        sources = (
            CraigslistSource(),
            KijijiSource(),
        )
        resolutions = resolve_source_registry(
            context=context,
            sources=sources,
            credentials_present={source.name: True for source in sources},
        )
        active = enabled_sources(resolutions)
        self.sources: dict[str, Source] = {item.name: item for item in active}

    def connect(self) -> Any:
        conn = connect(self.db_path)
        apply_migrations(conn)
        return conn





def get_state(request: Request) -> AppState:
    stored = getattr(request.app.state, "nostos", None)
    if not isinstance(stored, AppState):
        msg = "AppState is not initialized"
        raise RuntimeError(msg)
    return stored


StateDep = Annotated[AppState, Depends(get_state)]


def create_app(*, db_path: Path, profile_path: Path, citypack_path: Path) -> FastAPI:
    """Build the FastAPI app bound to a specific db/profile/citypack triple."""

    templates = _build_templates()
    state = AppState(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
        templates=templates,
    )
    app = FastAPI(
        title="Nostos local web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.nostos = state
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        state: StateDep,
        rent_min: float | None = Query(default=None, ge=0),
        rent_max: float | None = Query(default=None, ge=0),
        beds: float | None = Query(default=None, ge=0),
        baths_min: float | None = Query(default=None, ge=0),
        area_min: float | None = Query(default=None, ge=0),
        score_min: float | None = Query(default=None, ge=0, le=100),
        source: str | None = Query(default=None),
        area_name: str | None = Query(default=None),
        sort: str = Query(default="score"),
    ) -> HTMLResponse:
        filters = ListFilter(
            rent_min=rent_min,
            rent_max=rent_max,
            beds=beds,
            baths_min=baths_min,
            area_min=area_min,
            score_min=score_min,
            source=source or None,
            area_name=area_name or None,
            sort=sort,
        )
        with state.connect() as conn:
            rows = query_list(
                conn,
                context=state.context,
                profile_id=state.profile_id,
                sources=state.sources,
                filters=filters,
            )

        return state.templates.TemplateResponse(
            request=request,
            name="list.html",
            context={
                "rows": rows,
                "filters": filters,
                "active_filters": _active_filters(filters),
                "areas": known_areas(state.context),
                "sources": known_sources(state.sources.values()),
                "profile_id": state.profile_id,
            },
        )

    @app.get("/listings/{listing_id}", response_class=HTMLResponse)
    def detail(
        listing_id: str,
        request: Request,
        state: StateDep,
    ) -> HTMLResponse:
        with state.connect() as conn:
            row = load_detail(
                conn,
                listing_id=listing_id,
                context=state.context,
                profile_id=state.profile_id,
                sources=state.sources,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Listing not found")
            actions = ActionRepo(conn).get_actions(listing_id=listing_id)
            breakdown = _score_breakdown(conn, listing_id=listing_id, profile_id=state.profile_id)

        return state.templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                "row": row,
                "listing_id": listing_id,
                "actions": actions,
                "breakdown": breakdown,
                "profile_id": state.profile_id,
            },
        )

    @app.post("/listings/{listing_id}/star")
    def star_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "star")
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/dismiss")
    def dismiss_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "dismiss")
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/contacted")
    def contacted_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "contacted")
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/note")
    def note_action(
        listing_id: str,
        state: StateDep,
        note: Annotated[str, Form(...)],
    ) -> RedirectResponse:
        cleaned = note.strip()
        if cleaned:
            _record_action(state, listing_id, "note", note=cleaned)
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.get("/listings/{listing_id}/explain.json", response_class=JSONResponse)
    def explain_json(listing_id: str, state: StateDep) -> JSONResponse:
        with state.connect() as conn:
            breakdown = _score_breakdown(conn, listing_id=listing_id, profile_id=state.profile_id)
        if breakdown is None:
            raise HTTPException(status_code=404, detail="No score breakdown stored")
        return JSONResponse(content=breakdown)

    return app


def _record_action(
    state: AppState,
    listing_id: str,
    kind: ActionKind,
    *,
    note: str | None = None,
) -> None:
    with state.connect() as conn:
        ActionRepo(conn).record_action(listing_id=listing_id, kind=kind, note=note)


def _active_filters(filters: ListFilter) -> dict[str, object]:
    pairs: dict[str, object] = {}
    for name in (
        "rent_min",
        "rent_max",
        "beds",
        "baths_min",
        "area_min",
        "score_min",
        "source",
        "area_name",
    ):
        value = getattr(filters, name)
        if value is not None:
            pairs[name] = value
    return pairs


def _score_breakdown(
    conn: Any, *, listing_id: str, profile_id: str
) -> Mapping[str, object] | None:
    """Render the stored score breakdown as a serializable mapping.

    Uses the same `ScoreRepo.get_score` path the CLI's `nostos explain` uses.
    The score JSON is stored as-is so the template can render it.
    """

    score_row = ScoreRepo(conn).get_score(listing_id, profile_id)
    if score_row is None:
        return None
    return {
        "listing_id": score_row.listing_id,
        "profile_id": score_row.profile_id,
        "score": score_row.score,
        "computed_at": score_row.computed_at.astimezone(UTC).isoformat(),
        "breakdown_json": score_row.breakdown_json,
    }


__all__ = [
    "AppState",
    "ListRow",
    "create_app",
]
