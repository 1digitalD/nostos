"""FastAPI app for the local Nostos web UI."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote_plus, urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator, ValidationError

from nostos.config.profile import Profile, ScaledWeight
from nostos.config.wizard import dump_profile_yaml
from nostos.context import SearchContext, load_search_context
from nostos.enrich.location import directions_url, distance_km, walking_route
from nostos.enrich.review import (
    ExtractionPreviewNotFound,
    StaleExtractionPreview,
    apply_extraction_revision,
    preview_extraction_revision,
)
from nostos.model import Observed, Origin
from nostos.rank import rules as rules_module
from nostos.rank.rescore import RescoreReport, rescore_profile
from nostos.rank.rules import DEFAULT_REGISTRY
from nostos.sources import (
    CraigslistSource,
    KijijiSource,
    RealtorCaSource,
    Source,
)
from nostos.sources.manual import ManualSource
from nostos.store.actions import ActionKind, ActionRepo
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ObservationRepo, ResearchRepo, ScoreRepo
from nostos.web.query import (
    SORT_OPTIONS,
    STATUS_FILTER_VALUES,
    ListFilter,
    ListRow,
    known_areas,
    known_sources,
    load_detail,
    normalize_sort,
    query_list,
    rule_rows_from_breakdown,
    sort_label,
)
from nostos.web.research import (
    ResearchProvider,
    compile_web_research,
    nearby_places,
    normalize_research_subject,
    research_cache_key,
)
from nostos.web.research_report import build_research_report
from nostos.workflows import (
    CorrectionField,
    ManualListing,
    add_manual,
    apply_profile,
    clear_listing_correction,
    correct_listing_fact,
    listing_corrections,
    preview_profile,
    profile_history,
    progress,
    research_candidates,
    revision,
    update_progress,
    viewing_ics,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Maximum length for a note saved through the web UI. Notes are free-form text
# but unbounded length is a foot-gun (large DB rows, slow renders). The cap is
# generous so legitimate notes still fit comfortably.
_MAX_NOTE_LEN = 4000

# Keep the initial DOM and response small enough to remain responsive over the
# tailnet. The query fetches one extra row to decide whether to show Next.
_LIST_PAGE_SIZE = 36

# Acceptable sort keys. Anything else falls back to "score" rather than 422 —
# invalid input is silently ignored so a stale bookmark or hand-crafted link
# cannot break the page.
_VALID_SORT_KEYS: frozenset[str] = frozenset(
    {key for key, _label in SORT_OPTIONS} | {"rent", "posted"}
)

# Query parameters that make up a list-view URL, in canonical order.
_LIST_PARAMS: tuple[str, ...] = (
    "rent_min",
    "rent_max",
    "beds",
    "baths_min",
    "area_min",
    "score_min",
    "source",
    "area_name",
    "status",
    "starred",
    "hide_dismissed",
    "show_excluded",
    "sort",
)

_CITY_PORTS: dict[str, int] = {"vancouver": 8421, "toronto": 8422}



def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["relativetime"] = _render_relative_time
    templates.env.filters["score_badge"] = _render_score_badge
    templates.env.filters["money_short"] = _render_money_short
    templates.env.filters["fact_value"] = _render_fact_value
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


def _google_search_url(subject: str, topic: str) -> str:
    query = quote_plus(f'"{subject[:160]}" {topic}'.strip())
    return f"https://www.google.com/search?q={query}&tbs=qdr:y"


def _maps_search_url(subject: str, topic: str) -> str:
    query = quote_plus(f"{topic} near {subject[:160]}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def _research_sections(subject: str, city: str) -> list[dict[str, object]]:
    official_domain = "toronto.ca" if city == "toronto" else "vancouver.ca"
    return [
        {
            "title": "Address and building",
            "description": (
                "Current references for the address, building operator, and resident experience."
            ),
            "links": [
                ("Current address results", _google_search_url(subject, "")),
                ("Building management", _google_search_url(subject, "property management")),
                ("Recent resident reviews", _google_search_url(subject, "building reviews")),
            ],
        },
        {
            "title": "Area and locality",
            "description": (
                "Neighbourhood context and recent local changes that may affect daily life."
            ),
            "links": [
                ("Neighbourhood overview", _google_search_url(subject, "neighbourhood")),
                ("Recent local news", _google_search_url(subject, "neighbourhood news")),
                (
                    "Official city information",
                    _google_search_url(subject, f"site:{official_domain}"),
                ),
            ],
        },
        {
            "title": "Safety and building concerns",
            "description": (
                "Recent incident and maintenance searches. Verify claims against official records."
            ),
            "links": [
                ("Recent incidents", _google_search_url(subject, "police OR fire OR incident")),
                (
                    "Maintenance concerns",
                    _google_search_url(subject, "maintenance OR elevator OR bedbugs"),
                ),
                (
                    "Official safety records",
                    _google_search_url(subject, f"site:{official_domain} safety"),
                ),
            ],
        },
        {
            "title": "Daily services and shopping",
            "description": (
                "Current map listings around the address; confirm walking routes and opening hours."
            ),
            "links": [
                ("Gyms", _maps_search_url(subject, "gym")),
                ("Grocery stores", _maps_search_url(subject, "grocery store")),
                ("Shopping", _maps_search_url(subject, "shopping")),
                ("Pharmacies and clinics", _maps_search_url(subject, "pharmacy clinic")),
            ],
        },
        {
            "title": "Parks and access",
            "description": "Nearby green space and transit access from the listed address.",
            "links": [
                ("Parks", _maps_search_url(subject, "park")),
                ("Transit stops", _maps_search_url(subject, "public transit")),
                ("Bike services", _maps_search_url(subject, "bike share")),
            ],
        },
    ]


def _looks_like_street_address(value: str) -> bool:
    return re.search(r"\b\d{1,6}\s+[A-Za-z]", value) is not None


_STREET_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z][A-Za-z0-9 .'-]{0,70}?\s+"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|way|lane|ln|"
    r"court|ct|crescent|cres|place|pl|terrace|trail|highway|hwy)\b",
    re.IGNORECASE,
)


def _research_subject(address: str | None, title: str | None, listing_id: str) -> str:
    if address and address.strip():
        return address.strip()
    title_value = (title or "").strip()
    title_address = _STREET_ADDRESS_RE.search(title_value)
    if title_address is not None:
        return title_address.group(0).strip()
    return title_value or listing_id


def _research_identity(row: ListRow, city: str) -> str | None:
    override = row.listing.attributes.get("research_address")
    if isinstance(override, Observed):
        return normalize_research_subject(str(override.value), city)
    return normalize_research_subject(row.address or "", city) or normalize_research_subject(
        row.title or "", city
    )


def _research_cache_current(run: dict[str, object] | None, subject: str | None, city: str) -> bool:
    if run is None or subject is None or run.get("cache_key") != research_cache_key(subject, city):
        return False
    try:
        fetched_at = datetime.fromisoformat(str(run["fetched_at"]))
        age = datetime.now(UTC) - fetched_at.astimezone(UTC)
        return timedelta(0) <= age < timedelta(hours=24)
    except (ValueError, KeyError):
        return False


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
        self.profile_id = profile_path.stem
        self.context, self.sources = self._resolve()

    def _resolve(self) -> tuple[SearchContext, dict[str, Source]]:
        """Load citypack + profile from disk and resolve the enabled sources."""

        context = load_search_context(
            citypack_path=self.citypack_path,
            profile_path=self.profile_path,
        )
        sources = (
            CraigslistSource(),
            KijijiSource(),
            RealtorCaSource(),
            ManualSource(),
        )
        return context, {item.name: item for item in sources}

    def reload(self) -> None:
        """Re-read the profile/citypack from disk so edits apply without a restart."""

        self.context, self.sources = self._resolve()

    def rescore(self) -> RescoreReport:
        """Recompute every stored score for the active profile (no network)."""

        with self.connect() as conn:
            return rescore_profile(
                conn,
                context=self.context,
                profile_id=self.profile_id,
                sources=self.sources,
            )

    def connect(self) -> Any:
        conn = connect(self.db_path)
        apply_migrations(conn)
        return conn


def _record_nearby_observations(
    state: AppState, listing_id: str, result: Mapping[str, Any]
) -> bool:
    groups = result.get("groups")
    if not isinstance(groups, Mapping):
        return False
    checked_at_raw = result.get("checked_at")
    try:
        checked_at = datetime.fromisoformat(str(checked_at_raw))
    except ValueError:
        checked_at = datetime.now(UTC)
    field_groups = {
        "attributes.nearest_gym_km": "gyms",
        "attributes.nearest_grocery_km": "groceries",
    }
    changed = False
    with state.connect() as conn, conn:
        repo = ObservationRepo(conn)
        for field_name, group_name in field_groups.items():
            places = groups.get(group_name)
            if not isinstance(places, list) or not places or not isinstance(places[0], Mapping):
                continue
            place = places[0]
            try:
                distance = float(place["distance_km"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(place.get("name") or group_name)
            previous = conn.execute(
                """
                SELECT value_json FROM observation
                WHERE listing_id=? AND field=? AND origin=?
                ORDER BY observed_at DESC,id DESC LIMIT 1
                """,
                (listing_id, field_name, Origin.GEO_PROVIDER.value),
            ).fetchone()
            if previous is not None and float(json.loads(str(previous["value_json"]))) == distance:
                continue
            repo.record_observation(
                listing_id=listing_id,
                field=field_name,
                value_json=distance,
                origin=Origin.GEO_PROVIDER,
                confidence=0.9,
                evidence=f"OpenStreetMap: {name}, {distance:.2f} km from source map pin",
                observed_at=checked_at,
            )
            changed = True
    return changed





def get_state(request: Request) -> AppState:
    stored = getattr(request.app.state, "nostos", None)
    if not isinstance(stored, AppState):
        msg = "AppState is not initialized"
        raise RuntimeError(msg)
    return stored


StateDep = Annotated[AppState, Depends(get_state)]


def _blank_filter_as_none(value: object) -> object:
    """HTML GET forms submit unset number inputs as empty strings."""
    return None if value == "" else value


OptionalFilterNumber = Annotated[
    float | None, Query(ge=0), BeforeValidator(_blank_filter_as_none)
]
OptionalScoreFilter = Annotated[
    float | None, Query(ge=0, le=100), BeforeValidator(_blank_filter_as_none)
]


def create_app(
    *,
    db_path: Path,
    profile_path: Path,
    citypack_path: Path,
    research_provider: ResearchProvider | None = None,
    enable_detail_worker: bool = False,
    floorplan_analyzer: Callable[[list[str], str | None], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the FastAPI app bound to a specific db/profile/citypack triple."""

    templates = _build_templates()
    state = AppState(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
        templates=templates,
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        worker: threading.Thread | None = None
        if enable_detail_worker:
            def work() -> None:
                from nostos.enrich.refresh import run_refresh_once
                while not stop.is_set():
                    try:
                        context = load_search_context(
                            citypack_path=state.citypack_path, profile_path=state.profile_path
                        )
                        ran = run_refresh_once(
                            state.db_path, context=context, profile_id=state.profile_id,
                            sources=state.sources,
                            context_loader=lambda: load_search_context(
                                citypack_path=state.citypack_path,
                                profile_path=state.profile_path,
                            ),
                        )
                    except Exception:
                        logging.getLogger(__name__).exception("Detail worker iteration failed")
                        ran = False
                    if not ran:
                        stop.wait(2)
            worker = threading.Thread(target=work, name="nostos-detail-worker", daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=1)
                # Any interrupted claim is recovered through its durable expiry.

    app = FastAPI(
        lifespan=lifespan,
        title="Nostos local web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.nostos = state
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    research_lock = threading.Lock()
    active_research: set[str] = set()
    floorplan_lock = threading.Lock()

    @app.get("/switch-city/{city}")
    def switch_city(city: str, request: Request) -> RedirectResponse:
        port = _CITY_PORTS.get(city.casefold())
        if port is None:
            raise HTTPException(status_code=404, detail="Unsupported city")
        hostname = request.url.hostname or "127.0.0.1"
        scheme = request.url.scheme
        return RedirectResponse(url=f"{scheme}://{hostname}:{port}/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        state: StateDep,
        rent_min: OptionalFilterNumber = None,
        rent_max: OptionalFilterNumber = None,
        beds: OptionalFilterNumber = None,
        baths_min: OptionalFilterNumber = None,
        area_min: OptionalFilterNumber = None,
        score_min: OptionalScoreFilter = None,
        source: str | None = Query(default=None),
        area_name: str | None = Query(default=None),
        sort: str = Query(default="score"),
        status: str | None = Query(default=None),
        starred: bool = Query(default=False),
        hide_dismissed: bool = Query(default=False),
        show_excluded: bool = Query(default=False),
        page: int = Query(default=1, ge=1),
    ) -> HTMLResponse:
        normalized_sort = normalize_sort(sort if sort in _VALID_SORT_KEYS else None)
        filters = ListFilter(
            rent_min=rent_min,
            rent_max=rent_max,
            beds=beds,
            baths_min=baths_min,
            area_min=area_min,
            score_min=score_min,
            source=source or None,
            area_name=area_name or None,
            sort=normalized_sort,
            status=status if status in STATUS_FILTER_VALUES else None,
            starred=starred,
            hide_dismissed=hide_dismissed,
            show_excluded=show_excluded,
        )
        areas = known_areas(state.context)
        with state.connect() as conn:
            rows = query_list(
                conn,
                context=state.context,
                profile_id=state.profile_id,
                sources=state.sources,
                filters=filters,
                limit=_LIST_PAGE_SIZE + 1,
                offset=(page - 1) * _LIST_PAGE_SIZE,
            )
            has_next = len(rows) > _LIST_PAGE_SIZE
            rows = rows[:_LIST_PAGE_SIZE]
            row_actions = _row_action_states(
                conn, listing_ids=tuple(row.listing_id for row in rows)
            )
            contributors_by_id = _contributors_by_listing(
                conn,
                listing_ids=tuple(row.listing_id for row in rows),
                profile_id=state.profile_id,
                n=2,
            )
            filter_chips = _filter_chips(filters, areas)
            profile_summary = _profile_summary(state.context.profile, areas)

        return state.templates.TemplateResponse(
            request=request,
            name="list.html",
            context={
                "rows": rows,
                "row_actions": row_actions,
                "contributors_by_id": contributors_by_id,
                "filters": filters,
                "active_filters": _active_filters(filters),
                "filter_chips": filter_chips,
                "profile_summary": profile_summary,
                "areas": areas,
                "area_chips": _area_chips(filters, areas),
                "quick_toggles": _quick_toggles(filters),
                "status_chips": _status_chips(filters),
                "sort_options": SORT_OPTIONS,
                "sort_label": sort_label(filters.sort),
                "sources": known_sources(state.sources.values()),
                "profile_id": state.profile_id,
                "page": page,
                "has_next": has_next,
                "previous_url": _url_with(filters, page=page - 1) if page > 1 else None,
                "next_url": _url_with(filters, page=page + 1) if has_next else None,
            },
        )

    @app.get("/listings/{listing_id}", response_class=HTMLResponse)
    def detail(
        listing_id: str,
        request: Request,
        state: StateDep,
        error: str | None = None,
        corrected: bool = False,
        extracted: bool = False,
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
            action_repo = ActionRepo(conn)
            actions = action_repo.get_actions(listing_id=listing_id)
            action_state = {
                "starred": action_repo.has_action(listing_id=listing_id, kind="star"),
                "dismissed": action_repo.has_action(listing_id=listing_id, kind="dismiss"),
                "excluded": action_repo.has_action(listing_id=listing_id, kind="excluded"),
                "contacted": action_repo.has_action(listing_id=listing_id, kind="contacted"),
            }
            hunt_progress = progress(conn, listing_id)
            corrections = listing_corrections(conn, listing_id)
            from nostos.enrich.floorplan import gallery_fingerprint, get_floorplan_state
            floorplan = get_floorplan_state(conn, listing_id=listing_id)
            photo_urls = [str(photo.url) for photo in row.photos]
            floorplan_gallery_hash = gallery_fingerprint(photo_urls)
            floorplan_stale = bool(
                floorplan["analysis"]
                and floorplan["analysis"]["gallery_hash"] != floorplan_gallery_hash
            )
            landmark = state.context.profile.landmark
            landmark_context = None
            if landmark is not None:
                landmark_context = {
                    "name": landmark.name,
                    "distance_km": distance_km(row.listing, landmark),
                    "walking_url": directions_url(row.address or "Toronto", landmark),
                    "transit_url": directions_url(row.address or "Toronto", landmark, "transit"),
                }
            breakdown = _score_breakdown(conn, listing_id=listing_id, profile_id=state.profile_id)
            breakdown_json = breakdown.get("breakdown_json") if breakdown else None
            breakdown_contributors = (
                _top_contributors(breakdown_json, n=8) if breakdown_json else []
            )
            rule_rows = rule_rows_from_breakdown(
                breakdown_json if isinstance(breakdown_json, Mapping) else None
            )

        return state.templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                "row": row,
                "listing_id": listing_id,
                "actions": actions,
                "action_state": action_state,
                "hunt_progress": hunt_progress,
                "corrections": corrections,
                "correction_error": error,
                "corrected": corrected,
                "extracted": extracted,
                "listing_evidence": _listing_evidence(row),
                "floorplan": floorplan,
                "floorplan_gallery_hash": floorplan_gallery_hash,
                "floorplan_stale": floorplan_stale,
                "landmark": landmark_context,
                "breakdown": breakdown,
                "breakdown_contributors": breakdown_contributors,
                "category_scores": row.category_scores,
                "rule_rows_fired": [r for r in rule_rows if r.contribution != 0.0],
                "rule_rows_idle": [r for r in rule_rows if r.contribution == 0.0],
                "profile_id": state.profile_id,
            },
        )

    @app.post("/listings/{listing_id}/floor-plan", response_class=JSONResponse)
    def analyze_floorplan(
        listing_id: str, state: StateDep,
        selected_url: Annotated[str, Form()] = "",
    ) -> JSONResponse:
        from nostos.enrich.floorplan import analyze_gallery, gallery_fingerprint, save_analysis

        with state.connect() as conn:
            row = load_detail(
                conn, listing_id=listing_id, context=state.context,
                profile_id=state.profile_id, sources=state.sources,
            )
        if row is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        urls = [str(photo.url) for photo in row.photos]
        if selected_url and selected_url not in urls:
            raise HTTPException(status_code=422, detail="Choose an image from this listing.")
        if not floorplan_lock.acquire(blocking=False):
            return JSONResponse(
                {"message": "Another image scan is running. Retry shortly."}, status_code=429
            )
        try:
            result = (
                floorplan_analyzer(urls, selected_url or None) if floorplan_analyzer
                else analyze_gallery(urls, selected_url=selected_url or None)
            )
            with state.connect() as conn:
                current = load_detail(
                    conn, listing_id=listing_id, context=state.context,
                    profile_id=state.profile_id, sources=state.sources,
                )
                if current is None or gallery_fingerprint(
                    [str(photo.url) for photo in current.photos]
                ) != result["gallery_hash"]:
                    return JSONResponse(
                        {"message": "Listing images changed. Reload and scan the current gallery."},
                        status_code=409,
                    )
                save_analysis(conn, listing_id=listing_id, result=result)
            return JSONResponse({"reload": True, "status": result["status"]})
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"message": str(exc)}, status_code=422)
        finally:
            floorplan_lock.release()

    @app.post("/listings/{listing_id}/floor-plan/decision")
    def floorplan_decision(
        listing_id: str, state: StateDep,
        gallery_hash: Annotated[str, Form(...)],
        selected_source_hash: Annotated[str, Form(...)],
        decision: Annotated[str, Form(...)],
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        from nostos.enrich.floorplan import gallery_fingerprint, set_floorplan_decision

        with state.connect() as conn:
            row = load_detail(
                conn, listing_id=listing_id, context=state.context,
                profile_id=state.profile_id, sources=state.sources,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Listing not found")
            if gallery_fingerprint([str(photo.url) for photo in row.photos]) != gallery_hash:
                raise HTTPException(
                    status_code=409, detail="Images changed. Reload and scan again."
                )
            try:
                set_floorplan_decision(
                    conn, listing_id=listing_id, gallery_hash=gallery_hash,
                    selected_source_hash=selected_source_hash, decision=decision, note=note,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/listings/{listing_id}#floor-plan", status_code=303)

    @app.post("/listings/{listing_id}/detail-refresh", response_class=JSONResponse)
    def detail_refresh(listing_id: str, state: StateDep) -> JSONResponse:
        from nostos.enrich.refresh import enqueue_refresh
        try:
            with state.connect() as conn:
                record = conn.execute(
                    "SELECT source FROM source_record WHERE listing_id=? ORDER BY id DESC LIMIT 1",
                    (listing_id,),
                ).fetchone()
                source = state.sources.get(str(record["source"])) if record else None
                if source is not None and not source.capabilities.supports_detail_fetch:
                    raise HTTPException(
                        status_code=422, detail="This source supports saved-content review only."
                    )
                job = enqueue_refresh(conn, listing_id=listing_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(job, status_code=202)

    @app.get("/listings/{listing_id}/detail-refresh", response_class=JSONResponse)
    def detail_refresh_status(listing_id: str, state: StateDep) -> JSONResponse:
        from nostos.enrich.refresh import get_refresh_job
        with state.connect() as conn:
            job = get_refresh_job(conn, listing_id=listing_id)
        return JSONResponse(job or {"state": "idle"})

    @app.get("/listings/{listing_id}/extraction-review", response_class=HTMLResponse)
    def extraction_review(listing_id: str, request: Request, state: StateDep) -> HTMLResponse:
        state.reload()
        try:
            with state.connect() as conn:
                preview = preview_extraction_revision(
                    conn, listing_id=listing_id, context=state.context,
                    profile_id=state.profile_id, sources=state.sources,
                )
        except (ExtractionPreviewNotFound, ValueError) as exc:
            raise HTTPException(
                status_code=404, detail="Saved listing evidence is unavailable"
            ) from exc
        return state.templates.TemplateResponse(
            request=request, name="extraction_review.html",
            context={"preview": preview, "profile_id": state.profile_id},
        )

    @app.post("/listings/{listing_id}/extraction-review", response_class=HTMLResponse)
    def extraction_apply(
        listing_id: str, request: Request, state: StateDep,
        token: Annotated[str, Form(...)],
    ) -> Response:
        state.reload()
        try:
            with state.connect() as conn:
                apply_extraction_revision(
                    conn, listing_id=listing_id, preview_token=token, context=state.context,
                )
        except (StaleExtractionPreview, ExtractionPreviewNotFound, ValueError):
            return state.templates.TemplateResponse(
                request=request, name="extraction_review.html", status_code=409,
                context={"preview": None, "listing_id": listing_id,
                         "profile_id": state.profile_id},
            )
        return RedirectResponse(url=f"/listings/{listing_id}?extracted=1", status_code=303)

    @app.post("/listings/{listing_id}/star")
    def star_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "star")
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/dismiss")
    def dismiss_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "dismiss")
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/excluded")
    def excluded_action(listing_id: str, state: StateDep) -> RedirectResponse:
        _record_action(state, listing_id, "excluded")
        # Excluded listings are filtered out of the index by query_list, so
        # redirecting home surfaces the remaining shortlist without the just-
        # hidden listing.
        return RedirectResponse(url="/", status_code=303)

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
            if len(cleaned) > _MAX_NOTE_LEN:
                msg = f"Note too long ({len(cleaned)} chars); cap is {_MAX_NOTE_LEN}."
                raise HTTPException(status_code=400, detail=msg)
            _record_action(state, listing_id, "note", note=cleaned)
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/correct")
    def correct_fact_action(
        listing_id: str,
        state: StateDep,
        field: Annotated[str, Form(...)],
        value: Annotated[str, Form(...)],
    ) -> RedirectResponse:
        try:
            with state.connect() as conn:
                correct_listing_fact(
                    conn,
                    listing_id=listing_id,
                    field=cast(CorrectionField, field),
                    value=value,
                    currency=state.context.citypack.locale.currency,
                    area_unit=state.context.citypack.locale.area_unit,
                )
            state.rescore()
        except ValueError as exc:
            return RedirectResponse(
                url=f"/listings/{listing_id}?" + urlencode({"error": str(exc)}),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/listings/{listing_id}?corrected=1", status_code=303
        )

    @app.post("/listings/{listing_id}/corrections/reset")
    def reset_fact_action(
        listing_id: str,
        state: StateDep,
        field: Annotated[str, Form(...)],
    ) -> RedirectResponse:
        try:
            with state.connect() as conn:
                clear_listing_correction(conn, listing_id=listing_id, field=field)
            state.rescore()
        except ValueError as exc:
            return RedirectResponse(
                url=f"/listings/{listing_id}?" + urlencode({"error": str(exc)}),
                status_code=303,
            )
        return RedirectResponse(url=f"/listings/{listing_id}?corrected=1", status_code=303)

    @app.get("/listings/{listing_id}/research", response_class=HTMLResponse)
    def research_listing(listing_id: str, request: Request, state: StateDep) -> HTMLResponse:
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
            related = research_candidates(conn, listing_id)
            checked_records = int(
                conn.execute("SELECT COUNT(DISTINCT listing_id) FROM source_record").fetchone()[0]
            )
            source_rows = conn.execute(
                "SELECT source, COUNT(DISTINCT listing_id) AS count "
                "FROM source_record GROUP BY source ORDER BY source"
            ).fetchall()
            web_research_run, web_research_results = ResearchRepo(conn).get(listing_id)
            excluded_sources = {
                (str(item["cache_key"]), str(item["url"]))
                for item in conn.execute(
                    "SELECT cache_key,url FROM research_source_feedback WHERE listing_id=?",
                    (listing_id,),
                )
            }
        city = state.context.profile.city
        subject = _research_identity(row, city)
        cache_current = _research_cache_current(web_research_run, subject, city)
        if not cache_current:
            web_research_run, web_research_results = None, []
        report = None
        excluded_urls: set[str] = set()
        if subject and web_research_run:
            key = research_cache_key(subject, city)
            excluded_urls = {url for cache_key, url in excluded_sources if cache_key == key}
            report = build_research_report(
                subject, city,
                [item for item in web_research_results if item["url"] not in excluded_urls],
                fetched_at=str(web_research_run["fetched_at"]),
                filtered_stale_count=int(str(web_research_run.get("filtered_stale_count", 0))),
                filtered_irrelevant_count=int(
                    str(web_research_run.get("filtered_irrelevant_count", 0))
                ),
                partial_errors=(
                    [str(web_research_run["error"])] if web_research_run.get("error") else []
                ),
            )
        return state.templates.TemplateResponse(
            request=request,
            name="research.html",
            context={
                "row": row,
                "listing_id": listing_id,
                "related": related,
                "research_subject": subject or "Exact street address needed",
                "research_subject_precise": subject is not None,
                "research_address_error": request.query_params.get("address_error"),
                "research_sections": _research_sections(subject, city) if subject else [],
                "checked_records": checked_records,
                "source_counts": [
                    {"source": str(item["source"]), "count": int(item["count"])}
                    for item in source_rows
                ],
                "missing_facts": _missing_research_facts(row),
                "web_research_run": web_research_run,
                "web_research_results": web_research_results,
                "compiled_report": report,
                "excluded_research_urls": excluded_urls,
                "web_research_auto_start": subject is not None and not cache_current,
                "profile_id": state.profile_id,
            },
        )

    @app.post("/listings/{listing_id}/research-source")
    def research_source_feedback(
        listing_id: str, state: StateDep, url: Annotated[str, Form(...)],
        cache_key: Annotated[str, Form(...)], excluded: Annotated[bool, Form(...)],
    ) -> RedirectResponse:
        with state.connect() as conn:
            row = load_detail(
                conn, listing_id=listing_id, context=state.context,
                profile_id=state.profile_id, sources=state.sources,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Listing not found")
            subject = _research_identity(row, state.context.profile.city)
            if not subject or cache_key != research_cache_key(subject, state.context.profile.city):
                raise HTTPException(status_code=409, detail="Address changed. Reload the report.")
            run, results = ResearchRepo(conn).get(listing_id)
            if not run or run.get("cache_key") != cache_key or not any(
                item["url"] == url for item in results
            ):
                raise HTTPException(status_code=409, detail="Sources changed. Reload the report.")
            if excluded:
                conn.execute(
                    "INSERT OR REPLACE INTO research_source_feedback VALUES (?,?,?,?)",
                    (listing_id, cache_key, url, datetime.now(UTC).isoformat()),
                )
            else:
                conn.execute(
                    "DELETE FROM research_source_feedback "
                    "WHERE listing_id=? AND cache_key=? AND url=?",
                    (listing_id, cache_key, url),
                )
            conn.commit()
        return RedirectResponse(url=f"/listings/{listing_id}/research", status_code=303)

    @app.post("/listings/{listing_id}/web-research.json", response_class=JSONResponse)
    def web_research_listing(
        listing_id: str,
        state: StateDep,
        force: bool = Query(default=False),
    ) -> JSONResponse:
        with state.connect() as conn:
            row = load_detail(
                conn,
                listing_id=listing_id,
                context=state.context,
                profile_id=state.profile_id,
                sources=state.sources,
            )
            cached_run, cached_results = ResearchRepo(conn).get(listing_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        city = state.context.profile.city
        subject = _research_identity(row, city)
        if subject is None:
            return JSONResponse(
                {"status": "needs_address",
                 "message": "Enter the exact street address to research this building.",
                 "reload": False}, status_code=422,
            )
        if not force and _research_cache_current(cached_run, subject, city):
            return JSONResponse(
                {"status": "cached", "count": len(cached_results), "reload": False}
            )
        with research_lock:
            if listing_id in active_research or len(active_research) >= 2:
                return JSONResponse(
                    {"status": "busy", "reload": False,
                     "message": "Research is already running. Retry shortly to read the report."},
                    status_code=429,
                )
            active_research.add(listing_id)
        try:
            compiled = compile_web_research(
                subject,
                state.context.profile.city,
                provider=research_provider,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                {"status": "error", "message": str(exc), "reload": False},
                status_code=502,
            )
        finally:
            with research_lock:
                active_research.discard(listing_id)
        results = compiled["results"]
        if not isinstance(results, list):
            results = []
        with state.connect() as conn:
            current_row = load_detail(
                conn, listing_id=listing_id, context=state.context,
                profile_id=state.profile_id, sources=state.sources,
            )
            if current_row is None or _research_identity(current_row, city) != subject:
                return JSONResponse(
                    {"status": "error",
                     "message": "The address changed during research. Refresh for the new address.",
                     "reload": False},
                    status_code=409,
                )
            ResearchRepo(conn).replace_results(
                listing_id=listing_id,
                subject=subject,
                provider=str(compiled["provider"]),
                status=("partial" if compiled.get("partial_errors")
                        else ("complete" if results else "empty")),
                error="; ".join(compiled.get("partial_errors", [])) or None,
                fetched_at=str(compiled["fetched_at"]),
                filtered_stale_count=int(compiled["filtered_stale_count"]),
                results=cast(list[dict[str, str]], results),
                cache_key=research_cache_key(subject, city),
                filtered_irrelevant_count=int(compiled.get("filtered_irrelevant_count", 0)),
            )
        return JSONResponse(
            {
                "status": "complete" if results else "empty",
                "count": len(results),
                "filtered_stale_count": int(compiled["filtered_stale_count"]),
                "reload": True,
            }
        )

    @app.post("/listings/{listing_id}/research-address")
    def research_address(
        listing_id: str, state: StateDep, address: Annotated[str, Form(...)],
    ) -> RedirectResponse:
        normalized = normalize_research_subject(address, state.context.profile.city)
        if normalized is None:
            return RedirectResponse(
                url=f"/listings/{listing_id}/research?" + urlencode(
                    {"address_error": "Enter a street number and street name in this city."}
                ), status_code=303,
            )
        with state.connect() as conn:
            correct_listing_fact(
                conn, listing_id=listing_id, field="research_address", value=normalized,
                currency=state.context.citypack.locale.currency,
                area_unit=state.context.citypack.locale.area_unit,
            )
        return RedirectResponse(url=f"/listings/{listing_id}/research", status_code=303)

    @app.post("/listings/{listing_id}/nearby.json", response_class=JSONResponse)
    def nearby_listing(listing_id: str, state: StateDep) -> JSONResponse:
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
        point = row.listing.place.point
        if point is None:
            return JSONResponse(
                {"status": "unavailable", "message": "No source map pin is available."}
            )
        try:
            result = nearby_places(round(point.lat, 5), round(point.lng, 5))
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
            return JSONResponse(
                {
                    "status": "unavailable",
                    "message": (
                        "Nearby place data is temporarily unavailable. "
                        "Use the map searches below."
                    ),
                }
            )
        changed = _record_nearby_observations(state, listing_id, result)
        if changed:
            state.rescore()
        return JSONResponse({**result, "ranking_updated": changed})

    @app.get("/profile", response_class=HTMLResponse)
    def profile_view(
        request: Request,
        state: StateDep,
        saved: bool = False,
        rescored: int | None = Query(default=None, ge=0),
        skipped: int | None = Query(default=None, ge=0),
        error: str | None = None,
    ) -> HTMLResponse:
        """Render the editable ranking-factors page from the active profile."""

        submission = _submission_from_profile(state.context.profile)
        return _render_profile_page(
            request,
            state,
            submission=submission,
            saved=saved,
            rescored=rescored,
            skipped=skipped,
            error=error,
            status_code=200,
        )

    @app.post("/profile")
    async def profile_save(
        request: Request,
        state: StateDep,
    ) -> Response:
        """Apply form edits to the profile, save, reload, re-score, then redirect.

        Redirect-after-POST keeps a browser refresh from re-submitting. On a
        validation error the form is re-rendered (HTTP 400) with the submitted
        values preserved so nothing the user typed is lost.
        """

        form_data = await request.form()
        submission = _submission_from_form(form_data)
        try:
            new_profile = _apply_profile_form(state.context.profile, submission, state.context)
        except (ValueError, KeyError, TypeError) as exc:
            return _render_profile_page(
                request,
                state,
                submission=submission,
                saved=False,
                rescored=None,
                skipped=None,
                error=_describe_error(exc),
                status_code=400,
            )
        expected = str(form_data.get("expected_revision") or revision(state.context.profile))
        params: dict[str, object] = {"saved": 1}
        try:
            with state.connect() as conn:
                result = apply_profile(
                    conn,
                    path=state.profile_path,
                    context=state.context,
                    patch=new_profile.model_dump(mode="json"),
                    expected_revision=expected,
                    sources=state.sources,
                    replace=True,
                )
        except ValueError as exc:
            return _render_profile_page(
                request,
                state,
                submission=submission,
                saved=False,
                rescored=None,
                skipped=None,
                error=_describe_error(exc),
                status_code=409,
            )
        else:
            state.reload()
            params["rescored"] = result["scored"]
            params["skipped"] = result["skipped"]
        return RedirectResponse(url="/profile?" + urlencode(params), status_code=303)

    @app.post("/profile/preview", response_class=HTMLResponse)
    async def profile_preview(request: Request, state: StateDep) -> HTMLResponse:
        form_data = await request.form()
        submission = _submission_from_form(form_data)
        try:
            proposed = _apply_profile_form(state.context.profile, submission, state.context)
            with state.connect() as conn:
                preview = preview_profile(
                    conn,
                    context=state.context,
                    proposed=proposed,
                    sources=state.sources,
                )
        except (ValueError, KeyError, TypeError) as exc:
            return _render_profile_page(
                request,
                state,
                submission=submission,
                saved=False,
                rescored=None,
                skipped=None,
                error=_describe_error(exc),
                status_code=400,
            )
        return state.templates.TemplateResponse(
            request=request,
            name="profile_preview.html",
            context={
                "preview": preview,
                "profile_id": state.profile_id,
                "proposed_json": proposed.model_dump_json(),
            },
        )

    @app.post("/profile/apply-preview")
    async def profile_apply_preview(request: Request, state: StateDep) -> RedirectResponse:
        form = await request.form()
        proposed = Profile.model_validate_json(str(form.get("proposed_json") or ""))
        expected = str(form.get("expected_revision") or "")
        try:
            with state.connect() as conn:
                result = apply_profile(
                    conn,
                    path=state.profile_path,
                    context=state.context,
                    patch=proposed.model_dump(mode="json"),
                    expected_revision=expected,
                    sources=state.sources,
                    replace=True,
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_describe_error(exc)) from exc
        state.reload()
        return RedirectResponse(
            url="/profile?" + urlencode(
                {"saved": 1, "rescored": result["scored"], "skipped": result["skipped"]}
            ),
            status_code=303,
        )

    @app.post("/profile/rescore")
    def profile_rescore(state: StateDep) -> RedirectResponse:
        """Re-score every stored listing against the active profile."""

        params: dict[str, object] = {}
        try:
            report = state.rescore()
        except ValueError as exc:
            params["error"] = f"Re-scoring failed: {_describe_error(exc)}"
        else:
            params["rescored"] = report.scored_count
            params["skipped"] = report.skipped
        return RedirectResponse(url="/profile?" + urlencode(params), status_code=303)

    @app.post("/profile/undo")
    def profile_undo(state: StateDep) -> RedirectResponse:
        with state.connect() as conn:
            history = profile_history(conn, state.profile_path)
            target = next(
                (item for item in history if item["revision"] != revision(state.context.profile)),
                None,
            )
            if target is None:
                return RedirectResponse(
                    url="/profile?error=No+earlier+criteria+revision",
                    status_code=303,
                )
            result = apply_profile(
                conn,
                path=state.profile_path,
                context=state.context,
                patch=Profile.model_validate_json(target["payload"]).model_dump(mode="json"),
                expected_revision=revision(state.context.profile),
                sources=state.sources,
                replace=True,
            )
        state.reload()
        return RedirectResponse(
            url="/profile?" + urlencode({"saved": 1, "rescored": result["scored"]}),
            status_code=303,
        )

    @app.post("/manual")
    async def manual_add(request: Request, state: StateDep) -> RedirectResponse:
        form = await request.form()
        payload = {key: value for key, value in form.items() if isinstance(value, str)}
        for key in ("rent", "beds", "baths", "area", "floor", "lease_months", "total_monthly"):
            if not payload.get(key):
                payload.pop(key, None)
        listing = ManualListing.model_validate(payload)
        with state.connect() as conn:
            listing_id = add_manual(conn, listing)
        state.rescore()
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.post("/listings/{listing_id}/progress")
    async def progress_update(
        listing_id: str, request: Request, state: StateDep
    ) -> RedirectResponse:
        form = await request.form()
        with state.connect() as conn:
            update_progress(
                conn,
                listing_id,
                str(form.get("stage") or "spotted"),  # type: ignore[arg-type]
                str(form.get("viewing_at") or ""),
                state.context.citypack.locale.timezone,
            )
        return RedirectResponse(url=f"/listings/{listing_id}", status_code=303)

    @app.get("/listings/{listing_id}/viewing.ics")
    def calendar_export(listing_id: str, state: StateDep) -> Response:
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
            body = viewing_ics(conn, listing_id, row.title, row.address or "")
        return Response(
            body,
            media_type="text/calendar",
            headers={"Content-Disposition": 'attachment; filename="nostos-viewing.ics"'},
        )

    @app.get("/listings/{listing_id}/walking-route", response_class=HTMLResponse)
    def landmark_route(listing_id: str, state: StateDep) -> HTMLResponse:
        landmark = state.context.profile.landmark
        if landmark is None:
            raise HTTPException(status_code=404, detail="No landmark configured")
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
            try:
                route = walking_route(conn, row.listing, landmark)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return HTMLResponse(
            f"<h1>{route['minutes']} min walk · {route['km']} km</h1>"
            f"<p>{route['source']}. {route['note']}</p>"
            f"<p><a href='/listings/{listing_id}'>Back to listing</a></p>"
        )

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
        repo = ActionRepo(conn)
        if kind == "note":
            repo.record_action(listing_id=listing_id, kind=kind, note=note)
        else:
            repo.toggle_flag(listing_id=listing_id, kind=kind)
        conn.commit()


def _active_filters(filters: ListFilter) -> dict[str, object]:
    """Return the non-default filter values keyed by URL param (sort excluded)."""

    pairs = _filter_query_pairs(filters)
    pairs.pop("sort", None)
    return pairs


def _render_fact_value(value: Any) -> str:
    if value is None or value == "not_stated":
        return "Unstated"
    if value == "contradictory":
        return "Conflicting evidence"
    if value == "not_applicable":
        return "Not applicable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item).replace("_", " ") for item in value)
    if isinstance(value, dict):
        if "amount" in value:
            return f"{value['amount']} {value.get('currency', '')} / {value.get('period', 'month')}"
        if "value" in value and "unit" in value:
            return f"{value['value']} {value['unit']}"
    return str(value)


def _listing_evidence(row: ListRow) -> dict[str, Any]:
    attributes = row.listing.attributes
    description = attributes.get("description")
    source_attributes = attributes.get("source_attributes")
    facts: list[dict[str, Any]] = []
    for key, label in (
        ("in_suite_laundry", "In-suite laundry"),
        ("building_laundry", "Shared building laundry"),
        ("lease_months", "Lease length (months)"),
        ("total_monthly", "Total monthly cost"),
        ("utilities_included", "Utilities included"),
        ("pet_policy", "Pets"),
    ):
        item = attributes.get(key)
        if isinstance(item, Observed):
            value = _render_fact_value(item.value)
            facts.append({"label": label, "value": value, "evidence": item.evidence,
                          "origin": item.origin.value})
        else:
            facts.append({"label": label, "value": "Unstated", "evidence": None,
                          "origin": None})
    return {
        "description": description.value if isinstance(description, Observed) else "",
        "source_attributes": (
            source_attributes.value if isinstance(source_attributes, Observed) else ""
        ),
        "facts": facts,
    }


def _missing_research_facts(row: ListRow) -> list[str]:
    """Name decision facts that the selected listing still does not establish."""

    attributes = row.listing.attributes

    def has_attribute(name: str) -> bool:
        value = attributes.get(name)
        return isinstance(value, Observed) and value.value not in (None, "")

    facts = (
        ("Exact street address and unit number", bool(row.address)),
        ("Usable area", row.area_value is not None),
        ("Floor level", row.floor_text is not None),
        ("Parking availability", row.parking is not None),
        ("In-suite laundry", has_attribute("in_suite_laundry")),
        ("Exact availability date", has_attribute("available_date")),
        ("Lease length", has_attribute("lease_months")),
        ("Total monthly cost and utilities", has_attribute("total_monthly")),
        ("Floor plan or room dimensions", has_attribute("floor_plan")),
    )
    return [label for label, present in facts if not present]


def _filter_query_pairs(filters: ListFilter) -> dict[str, object]:
    """Serialize a ListFilter to URL query pairs, omitting defaults."""

    pairs: dict[str, object] = {}
    for name in _LIST_PARAMS:
        value = getattr(filters, name)
        if value is None or value is False:
            continue
        if name == "sort" and value == "score":
            continue
        if value is True:
            pairs[name] = 1
        elif isinstance(value, float) and value.is_integer():
            pairs[name] = int(value)
        else:
            pairs[name] = value
    return pairs


def _url_with(filters: ListFilter, **overrides: object) -> str:
    """Build a list URL from ``filters`` with some params overridden.

    Pass ``None`` (or ``False``) for a param to drop it from the URL.
    """

    pairs = _filter_query_pairs(filters)
    for key, value in overrides.items():
        if value is None or value is False:
            pairs.pop(key, None)
        else:
            pairs[key] = 1 if value is True else value
    if not pairs:
        return "/"
    return "/?" + urlencode(pairs)


def _area_chips(
    filters: ListFilter, areas: tuple[tuple[str, str], ...]
) -> list[dict[str, object]]:
    """Single-select area chips: "All" plus one per citypack area."""

    chips: list[dict[str, object]] = [
        {
            "key": "",
            "label": "All areas",
            "url": _url_with(filters, area_name=None),
            "active": filters.area_name is None,
        }
    ]
    for key, label in areas:
        active = filters.area_name == key
        chips.append(
            {
                "key": key,
                "label": label,
                # Clicking the active chip clears the selection.
                "url": _url_with(filters, area_name=None if active else key),
                "active": active,
            }
        )
    return chips


def _quick_toggles(filters: ListFilter) -> list[dict[str, object]]:
    """Boolean URL toggles rendered as chips (click flips the param)."""

    specs: tuple[tuple[str, str, str, bool], ...] = (
        ("starred", "Shortlisted only", "Show only listings you shortlisted", filters.starred),
        ("hide_dismissed", "Hide dismissed", "Drop listings you dismissed", filters.hide_dismissed),
        ("show_excluded", "Show excluded", "Include listings you excluded", filters.show_excluded),
    )
    return [
        {
            "param": param,
            "label": label,
            "title": title,
            "active": active,
            "url": _url_with(filters, **{param: not active}),
        }
        for param, label, title, active in specs
    ]


def _status_chips(filters: ListFilter) -> list[dict[str, object]]:
    """Match-status filter chips (single-select, click again to clear)."""

    specs: tuple[tuple[str, str, str], ...] = (
        ("match", "Matches", "Only listings that meet every hard criterion"),
        ("unverified", "Needs verification", "Only listings missing data for a criterion"),
        ("miss", "Misses", "Only listings that fail a hard criterion"),
    )
    chips: list[dict[str, object]] = []
    for value, label, title in specs:
        active = filters.status == value
        chips.append(
            {
                "value": value,
                "label": label,
                "title": title,
                "active": active,
                "url": _url_with(filters, status=None if active else value),
            }
        )
    return chips


def _row_action_states(
    conn: Any, *, listing_ids: tuple[str, ...]
) -> dict[str, dict[str, bool]]:
    """Return {listing_id: {starred: bool, dismissed: bool, contacted: bool}}."""

    return ActionRepo(conn).action_states_for(listing_ids=listing_ids)


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


def _top_contributors(breakdown: Any, *, n: int = 8) -> list[dict[str, float | str]]:
    """Return the top-n individual score contributors from a stored breakdown.

    Each entry: ``{"label": str, "contribution": float, "rule_key": str}``.
    Sorted by absolute contribution (impact) desc. Zero contributions are
    filtered out so a card never wastes a row on a rule that fired with
    zero weight.
    """

    if not isinstance(breakdown, Mapping):
        return []
    contributions = breakdown.get("contributions")
    if not isinstance(contributions, list):
        return []

    valid: list[dict[str, float | str]] = []
    for entry in contributions:
        if not isinstance(entry, Mapping):
            continue
        contribution = entry.get("contribution")
        if not isinstance(contribution, (int, float)) or contribution == 0:
            continue
        contribution_value = float(contribution)
        label = entry.get("label") or entry.get("rule_key") or ""
        rule_key = entry.get("rule_key") or ""
        valid.append(
            {
                "label": str(label),
                "contribution": contribution_value,
                "rule_key": str(rule_key),
            }
        )
    valid.sort(key=lambda item: abs(float(item["contribution"])), reverse=True)
    return valid[:n]




def _filter_chips(
    filters: ListFilter, areas: tuple[tuple[str, str], ...]
) -> list[dict[str, str]]:
    """Render active filters as removable chips with a remove-URL each."""

    area_labels = dict(areas)
    chip_specs: list[tuple[str, str, Callable[[ListFilter], str]]] = [
        ("rent_min", "rent_min", lambda f: f"rent ≥ ${int(f.rent_min or 0):,}"),
        ("rent_max", "rent_max", lambda f: f"rent ≤ ${int(f.rent_max or 0):,}"),
        ("beds", "beds", lambda f: f"{int(f.beds or 0)}+ beds"),
        ("baths_min", "baths_min", lambda f: f"≥ {f.baths_min} baths"),
        ("area_min", "area_min", lambda f: f"≥ {int(f.area_min or 0)} sqft"),
        ("score_min", "score_min", lambda f: f"score ≥ {int(f.score_min or 0)}"),
        ("source", "source", lambda f: f"source: {f.source or ''}"),
        (
            "area_name",
            "area_name",
            lambda f: f"area: {area_labels.get(f.area_name or '', f.area_name)}",
        ),
        ("status", "status", lambda f: f"status: {f.status or ''}"),
        ("starred", "starred", lambda f: "shortlisted only"),
        ("hide_dismissed", "hide_dismissed", lambda f: "dismissed hidden"),
        ("show_excluded", "show_excluded", lambda f: "excluded shown"),
        ("sort", "sort", lambda f: f"sort: {sort_label(f.sort)}"),
    ]

    chips: list[dict[str, str]] = []
    for param, _key, label_fn in chip_specs:
        value = getattr(filters, _key)
        if value is None or value is False:
            continue
        if param == "sort" and value == "score":
            continue
        label = label_fn(filters)
        remove_url = _url_without_param(filters, param)
        chips.append({"label": label, "param": param, "remove_url": remove_url})
    return chips

def _numeric_filter_chip(unit: str, eq: float | None, lo: float | None, hi: float | None) -> str:
    if eq is not None:
        return f"{eq:g} {unit}"
    bits: list[str] = []
    if lo is not None:
        bits.append(f"≥ {lo:g}")
    if hi is not None:
        bits.append(f"≤ {hi:g}")
    return f"{unit} " + " ".join(bits) if bits else unit


def _signed(value: float) -> str:
    """Format a weight with a proper minus sign (−) rather than a hyphen."""

    text = f"{abs(value):g}"
    return f"+{text}" if value >= 0 else f"−{text}"


def _profile_summary(
    profile: Profile, areas: tuple[tuple[str, str], ...] = ()
) -> list[dict[str, str]]:
    """Render the active profile as inline chips: hard filters, then top weights.

    Each chip is ``{"label": str, "kind": "hard" | "weight"}``. Surfaces WHAT
    the ranking uses so the user sees it at a glance and has one click to
    edit it. Weight labels come from the rule registry (fallback: the key);
    area-key weights use the citypack area label.
    """

    chips: list[dict[str, str]] = []
    hard = profile.hard
    if hard.rent is not None:
        if hard.rent.min is not None:
            chips.append({"label": f"rent ${int(hard.rent.min):,}–${int(hard.rent.max):,}",
                          "kind": "hard"})
        else:
            chips.append({"label": f"rent ≤ ${int(hard.rent.max):,}", "kind": "hard"})
    if hard.beds is not None:
        chips.append({
            "label": _numeric_filter_chip("bd", hard.beds.eq, hard.beds.min, hard.beds.max),
            "kind": "hard",
        })
    if hard.baths is not None:
        chips.append({
            "label": _numeric_filter_chip("ba", hard.baths.eq, hard.baths.min, hard.baths.max),
            "kind": "hard",
        })
    if hard.area is not None:
        chips.append({"label": f"≥ {int(hard.area.min)} {hard.area.unit}", "kind": "hard"})
    if hard.floor is not None:
        chips.append({
            "label": _numeric_filter_chip("floor", hard.floor.eq, hard.floor.min, hard.floor.max),
            "kind": "hard",
        })
    if hard.areas:
        count = len(hard.areas)
        chips.append({"label": f"{count} area{'' if count == 1 else 's'}", "kind": "hard"})
    for token in hard.exclude:
        chips.append({"label": f"no {token.replace('_', ' ')}", "kind": "hard"})

    area_labels = dict(areas)
    weighted: list[tuple[str, float]] = []
    for key, value in profile.weights.items():
        scalar = float(value.cap) if isinstance(value, ScaledWeight) else float(value)
        if scalar == 0:
            continue
        registered = DEFAULT_REGISTRY.get(key)
        weighted.append((registered.label if registered is not None else key, scalar))
    for area_key, value in profile.area_key_weights.items():
        if value == 0:
            continue
        weighted.append((area_labels.get(area_key, area_key), float(value)))
    weighted.sort(key=lambda item: abs(item[1]), reverse=True)
    for label, value in weighted[:3]:
        chips.append({"label": f"{label} {_signed(value)}", "kind": "weight"})

    enabled_sources = sorted(k for k, v in profile.sources.items() if v)
    if enabled_sources:
        chips.append({"label": "sources: " + " + ".join(enabled_sources), "kind": "hard"})
    return chips


def _url_without_param(filters: ListFilter, param: str) -> str:
    """Build a query string with the given param dropped (others preserved)."""

    return _url_with(filters, **{param: None})


def _contributors_by_listing(
    conn: Any, *, listing_ids: tuple[str, ...], profile_id: str, n: int = 2
) -> dict[str, list[dict[str, float | str]]]:
    """Pre-compute top-n contributors for many listings in one pass."""

    if not listing_ids:
        return {}
    placeholders = ",".join("?" for _ in listing_ids)
    rows = conn.execute(
        f"""
        SELECT listing_id, breakdown_json FROM score
        WHERE profile_id = ? AND listing_id IN ({placeholders})
        """,
        (profile_id, *listing_ids),
    ).fetchall()
    result: dict[str, list[dict[str, float | str]]] = {}
    for row in rows:
        try:
            breakdown = json.loads(str(row["breakdown_json"]))
        except (TypeError, ValueError):
            continue
        contributors = _top_contributors(breakdown, n=n)
        if contributors:
            result[str(row["listing_id"])] = contributors
    return result



# ---------------------------------------------------------------------------
# Profile editor: form model, parsing, rendering, persistence
# ---------------------------------------------------------------------------

# Category headings for the weights section. The registry's own mapping wins
# when present; this fallback keeps the page rendering on older registries.
_CATEGORY_LABEL_FALLBACK: dict[str, str] = {
    "amenities": "Amenities",
    "space": "Space & layout",
    "cost": "Cost",
    "proximity": "Location & proximity",
}
_CATEGORY_ORDER: tuple[str, ...] = ("amenities", "space", "cost", "proximity")
_OTHER_CATEGORY = "other"

# Rules whose weight is a ``ScaledWeight`` (rate per 100 units + cap) rather
# than a flat number, keyed to the rate field the engine expects.
_SCALED_RULE_RATE_KEYS: dict[str, str] = {
    "area.over_minimum": "per_100_sqft",
    "rent.headroom": "per_100",
}

# Short explanations shown under each rule. Used only when the registered
# ``Rule`` carries no ``description`` of its own.
_RULE_DESCRIPTION_FALLBACK: dict[str, str] = {
    "laundry.in_suite": "Washer/dryer inside the unit.",
    "laundry.building": "Shared or coin-op laundry in the building.",
    "parking.available": "A parking stall is included or available.",
    "pets.allowed": "Pets welcome (full points), considered (half), or refused (none).",
    "floor.low": "Lower floors score higher; fades out above the 4th floor.",
    "space.den_or_solarium": "Listing mentions a den or solarium.",
    "area.over_minimum": "Extra space beyond your area minimum.",
    "rent.headroom": "How far the rent sits below your rent maximum.",
    "walk.score": "Stated Walk Score, 0-100.",
    "density.walkable": "Phrases like 'steps from', 'walking distance', 'heart of'.",
    "density.sparse": "Phrases like 'suburban', 'quiet neighborhood', 'tree-lined'.",
    "photo.present": "The listing has at least one photo.",
}

# Hard-filter exclude tokens the form renders as checkboxes. Unknown tokens
# already in the profile are preserved untouched.
_EXCLUDE_TOKENS: tuple[tuple[str, str, str], ...] = (
    ("basement", "Exclude basement / below-grade units", "exclude_basement"),
    ("furnished_only", "Exclude furnished-only listings", "exclude_furnished_only"),
)

_WEIGHT_MIN = -15
_WEIGHT_MAX = 15


@dataclass(frozen=True, slots=True)
class ProfileSubmission:
    """Flat, string-valued view of the profile form.

    Built either from the active ``Profile`` (GET) or from the raw POST body,
    so a failed save can re-render exactly what the user typed. Checkbox
    fields are present in ``fields`` when checked and absent otherwise.
    """

    fields: Mapping[str, str] = field(default_factory=dict)
    areas: tuple[str, ...] = ()

    def get(self, name: str) -> str:
        return self.fields.get(name, "")

    def checked(self, name: str) -> bool:
        return name in self.fields


def _submission_from_form(form_data: Any) -> ProfileSubmission:
    fields: dict[str, str] = {}
    for key, value in form_data.multi_items():
        if isinstance(value, str) and key != "areas":
            fields[str(key)] = value
    areas = tuple(str(v) for v in form_data.getlist("areas") if isinstance(v, str) and v)
    return ProfileSubmission(fields=fields, areas=areas)


def _submission_from_profile(profile: Profile) -> ProfileSubmission:
    hard = profile.hard
    fields: dict[str, str] = {}

    def put(name: str, value: float | None) -> None:
        if value is not None:
            fields[name] = _format_number(value)

    if hard.rent is not None:
        put("rent_max", hard.rent.max)
        put("rent_min", hard.rent.min)
    if hard.beds is not None:
        put("beds_eq", hard.beds.eq)
        put("beds_min", hard.beds.min)
        put("beds_max", hard.beds.max)
    if hard.baths is not None:
        put("baths_eq", hard.baths.eq)
        put("baths_min", hard.baths.min)
        put("baths_max", hard.baths.max)
    if hard.floor is not None:
        put("floor_max", hard.floor.max)
    if hard.area is not None:
        put("area_min", hard.area.min)
    if hard.available_by is not None:
        fields["available_by"] = hard.available_by.isoformat()
    put("lease_months_min", hard.lease_months_min)
    put("total_monthly_max", hard.total_monthly_max)
    if hard.require_laundry:
        fields["require_laundry"] = "on"
    if hard.require_parking:
        fields["require_parking"] = "on"
    excludes = {token.strip().lower() for token in hard.exclude}
    for token, _label, field_name in _EXCLUDE_TOKENS:
        if token in excludes:
            fields[field_name] = "on"

    for rule_key, value in profile.weights.items():
        if isinstance(value, ScaledWeight):
            rate = value.per_100_sqft if value.per_100_sqft is not None else value.per_100
            put(f"weight_{rule_key}_rate", rate)
            put(f"weight_{rule_key}_cap", value.cap)
        else:
            put(f"weight_{rule_key}", value)
    for area_key, value in profile.area_key_weights.items():
        put(f"area_weight_{area_key}", value)
    put("unverified_penalty", profile.confidence.unverified_penalty)
    for source_key, enabled in profile.sources.items():
        if enabled:
            fields[f"src_{source_key}"] = "on"

    return ProfileSubmission(fields=fields, areas=tuple(hard.areas))


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _rule_groups(profile: Profile) -> list[dict[str, object]]:
    """Every registered rule, grouped by category, plus any unregistered keys
    the profile still carries (so they can be zeroed out from the UI)."""

    labels: Mapping[str, str] = getattr(
        rules_module, "CATEGORY_LABELS", _CATEGORY_LABEL_FALLBACK
    )
    registry = rules_module.DEFAULT_REGISTRY
    by_category: dict[str, list[dict[str, object]]] = {}
    seen: set[str] = set()
    for rule in registry.all():
        seen.add(rule.key)
        description = str(getattr(rule, "description", "") or "")
        by_category.setdefault(rule.category, []).append(
            _rule_row(
                key=rule.key,
                label=rule.label,
                description=description or _RULE_DESCRIPTION_FALLBACK.get(rule.key, ""),
            )
        )
    for rule_key, value in profile.weights.items():
        if rule_key in seen:
            continue
        row = _rule_row(
            key=rule_key,
            label=rule_key,
            description="Not a registered rule — set to 0 to drop it from the profile.",
        )
        if isinstance(value, ScaledWeight):
            row["scaled"] = True
            row["rate_key"] = "per_100_sqft" if value.per_100_sqft is not None else "per_100"
            row["rate_label"] = _rate_label(str(row["rate_key"]))
        by_category.setdefault(_OTHER_CATEGORY, []).append(row)

    ordered = [c for c in _CATEGORY_ORDER if c in by_category]
    ordered += sorted(c for c in by_category if c not in _CATEGORY_ORDER and c != _OTHER_CATEGORY)
    if _OTHER_CATEGORY in by_category:
        ordered.append(_OTHER_CATEGORY)
    groups: list[dict[str, object]] = []
    for category in ordered:
        fallback = _CATEGORY_LABEL_FALLBACK.get(category, category.replace("_", " ").title())
        if category == _OTHER_CATEGORY:
            fallback = "Other (not registered)"
        groups.append(
            {
                "key": category,
                "label": labels.get(category, fallback),
                "rules": by_category[category],
            }
        )
    return groups


def _rule_row(*, key: str, label: str, description: str) -> dict[str, object]:
    rate_key = _SCALED_RULE_RATE_KEYS.get(key)
    return {
        "key": key,
        "field": f"weight_{key}",
        "label": label,
        "description": description,
        "scaled": rate_key is not None,
        "rate_key": rate_key,
        "rate_label": _rate_label(rate_key) if rate_key else "",
    }


def _rate_label(rate_key: str) -> str:
    return "points per 100 sqft" if rate_key == "per_100_sqft" else "points per $100"


def _render_profile_page(
    request: Request,
    state: AppState,
    *,
    submission: ProfileSubmission,
    saved: bool,
    rescored: int | None,
    skipped: int | None,
    error: str | None,
    status_code: int,
) -> HTMLResponse:
    context = state.context
    profile = context.profile
    citypack = context.citypack
    source_keys = sorted(set(citypack.sources.keys()) | set(profile.sources.keys()))
    with state.connect() as conn:
        history = profile_history(conn, state.profile_path)
    return state.templates.TemplateResponse(
        request=request,
        name="profile.html",
        status_code=status_code,
        context={
            "sub": submission,
            "rule_groups": _rule_groups(profile),
            "areas": [{"key": area.key, "label": area.label} for area in citypack.areas],
            "source_keys": source_keys,
            "exclude_tokens": [
                {"token": token, "label": label, "field": field_name}
                for token, label, field_name in _EXCLUDE_TOKENS
            ],
            "currency": citypack.locale.currency,
            "area_unit": citypack.locale.area_unit,
            "weight_min": _WEIGHT_MIN,
            "weight_max": _WEIGHT_MAX,
            "profile_path": str(state.profile_path),
            "saved": saved,
            "rescored": rescored,
            "skipped": skipped,
            "error": error,
            "profile_id": state.profile_id,
            "profile_revision": revision(profile),
            "has_history": any(item["revision"] != revision(profile) for item in history),
        },
    )


def _describe_error(exc: BaseException) -> str:
    """Turn a Pydantic/ValueError into one readable sentence for the banner."""

    if isinstance(exc, ValidationError):
        parts: list[str] = []
        for item in exc.errors():
            loc = ".".join(str(token) for token in item.get("loc", ())) or "profile"
            message = str(item.get("msg", "invalid value"))
            message = message.removeprefix("Value error, ")
            parts.append(f"{loc}: {message}")
        return "; ".join(parts)
    return str(exc) or exc.__class__.__name__


def _apply_profile_form(
    profile: Profile,
    submission: ProfileSubmission,
    context: SearchContext,
) -> Profile:
    """Return a new Profile with the form applied; preserve everything else.

    Keys the form does not render (notify, schedule, proximity, avoid_areas,
    city, unknown exclude tokens, area weights for areas no longer in the
    citypack) are carried over from the current profile untouched.
    """

    payload = profile.model_dump(mode="json")
    citypack = context.citypack
    hard = dict(payload.get("hard") or {})

    rent_max = _coerce_float(submission.get("rent_max"), field_name="Rent max", minimum=0)
    rent_min = _coerce_float(submission.get("rent_min"), field_name="Rent min", minimum=0)
    if rent_max is None and rent_min is not None:
        raise ValueError("Rent min needs a rent max to go with it.")
    if rent_min is not None and rent_max is not None and rent_min > rent_max:
        raise ValueError("Rent min must be less than or equal to rent max.")
    existing_rent = hard.get("rent") if isinstance(hard.get("rent"), dict) else None
    if rent_max is None:
        hard["rent"] = None
    else:
        currency = (
            str(existing_rent.get("currency"))
            if existing_rent and existing_rent.get("currency")
            else citypack.locale.currency
        )
        hard["rent"] = {"max": rent_max, "min": rent_min, "currency": currency}

    hard["beds"] = _numeric_filter_from_form(submission, prefix="beds", label="Beds")
    hard["baths"] = _numeric_filter_from_form(submission, prefix="baths", label="Baths")

    floor_max = _coerce_float(submission.get("floor_max"), field_name="Floor max", minimum=0)
    existing_floor = hard.get("floor") if isinstance(hard.get("floor"), dict) else None
    floor_filter: dict[str, object] = {
        "eq": existing_floor.get("eq") if existing_floor else None,
        "min": existing_floor.get("min") if existing_floor else None,
        "max": floor_max,
    }
    if floor_filter["eq"] is not None and floor_max is not None:
        # An exact floor from the file cannot combine with a max from the form.
        floor_filter["eq"] = None
    hard["floor"] = floor_filter if any(v is not None for v in floor_filter.values()) else None

    area_min = _coerce_float(submission.get("area_min"), field_name="Area min", minimum=0)
    existing_area = hard.get("area") if isinstance(hard.get("area"), dict) else None
    if area_min is None:
        hard["area"] = None
    else:
        unit = (
            str(existing_area.get("unit"))
            if existing_area and existing_area.get("unit")
            else citypack.locale.area_unit
        )
        hard["area"] = {"min": area_min, "unit": unit}

    available_by = submission.get("available_by").strip()
    if available_by:
        try:
            hard["available_by"] = date.fromisoformat(available_by).isoformat()
        except ValueError:
            raise ValueError("Available by must be a valid date.") from None
    else:
        hard["available_by"] = None
    hard["lease_months_min"] = _coerce_float(
        submission.get("lease_months_min"), field_name="Minimum lease", minimum=0
    )
    hard["total_monthly_max"] = _coerce_float(
        submission.get("total_monthly_max"), field_name="Total monthly cost max", minimum=0
    )
    hard["require_laundry"] = submission.checked("require_laundry")
    hard["require_parking"] = submission.checked("require_parking")

    known_area_keys = {area.key for area in citypack.areas}
    unknown_areas = sorted(set(submission.areas) - known_area_keys)
    if unknown_areas:
        raise ValueError(f"Unknown area key(s): {', '.join(unknown_areas)}")
    hard["areas"] = [area.key for area in citypack.areas if area.key in set(submission.areas)]

    rendered_tokens = {token for token, _label, _field in _EXCLUDE_TOKENS}
    existing_exclude = hard.get("exclude")
    kept = [
        str(token)
        for token in (existing_exclude if isinstance(existing_exclude, list) else [])
        if str(token).strip().lower() not in rendered_tokens
    ]
    chosen = [
        token for token, _label, field_name in _EXCLUDE_TOKENS if submission.checked(field_name)
    ]
    hard["exclude"] = chosen + kept
    payload["hard"] = hard

    payload["weights"] = _weights_from_form(profile, submission)
    payload["area_key_weights"] = _area_weights_from_form(profile, submission, context)

    confidence = dict(payload.get("confidence") or {})
    penalty = _coerce_float(submission.get("unverified_penalty"), field_name="Unverified penalty")
    confidence["unverified_penalty"] = penalty if penalty is not None else 0.0
    payload["confidence"] = confidence

    sources = dict(payload.get("sources") or {})
    for source_key in sorted(set(citypack.sources.keys()) | set(sources.keys())):
        sources[source_key] = submission.checked(f"src_{source_key}")
    payload["sources"] = sources

    return Profile.model_validate(payload)


def _numeric_filter_from_form(
    submission: ProfileSubmission, *, prefix: str, label: str
) -> dict[str, object] | None:
    """Build a ``NumericHardFilter`` payload; an exact value wins over a range."""

    eq = _coerce_float(submission.get(f"{prefix}_eq"), field_name=f"{label} exactly", minimum=0)
    minimum = _coerce_float(submission.get(f"{prefix}_min"), field_name=f"{label} min", minimum=0)
    maximum = _coerce_float(submission.get(f"{prefix}_max"), field_name=f"{label} max", minimum=0)
    if eq is not None:
        return {"eq": eq, "min": None, "max": None}
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{label} min must be less than or equal to {label.lower()} max.")
    return {"eq": None, "min": minimum, "max": maximum}


def _weights_from_form(profile: Profile, submission: ProfileSubmission) -> dict[str, object]:
    """Weights for every rendered rule; zero/blank means "off" and is dropped."""

    existing = profile.model_dump(mode="json").get("weights") or {}
    weights: dict[str, object] = {}
    rendered: set[str] = set()
    for group in _rule_groups(profile):
        for row in group["rules"] if isinstance(group["rules"], list) else []:
            key = str(row["key"])
            rendered.add(key)
            if row["scaled"]:
                rate_key = str(row["rate_key"])
                rate = _coerce_float(
                    submission.get(f"weight_{key}_rate"), field_name=f"{row['label']} rate"
                )
                cap = _coerce_float(
                    submission.get(f"weight_{key}_cap"), field_name=f"{row['label']} cap"
                )
                if not rate or not cap:
                    continue
                weights[key] = {rate_key: rate, "cap": cap}
            else:
                value = _coerce_float(submission.get(f"weight_{key}"), field_name=str(row["label"]))
                if not value:
                    continue
                weights[key] = value
    # Anything not rendered (should not happen — unregistered keys are shown
    # too) is preserved verbatim rather than silently dropped.
    for key, value in existing.items():
        if key not in rendered:
            weights[key] = value
    return weights


def _area_weights_from_form(
    profile: Profile, submission: ProfileSubmission, context: SearchContext
) -> dict[str, float]:
    weights: dict[str, float] = {}
    known: set[str] = set()
    for area in context.citypack.areas:
        known.add(area.key)
        value = _coerce_float(
            submission.get(f"area_weight_{area.key}"), field_name=f"{area.label} weight"
        )
        if value:
            weights[area.key] = value
    for key, value in profile.area_key_weights.items():
        if key not in known and value:
            weights[key] = value
    return weights


def _save_profile_yaml(path: Path, profile: Profile) -> None:
    """Atomically write the profile to disk (tmp + rename)."""

    payload = _tidy_payload(profile.model_dump(mode="json"))
    text_yaml = dump_profile_yaml(payload)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text_yaml, encoding="utf-8")
    tmp_path.replace(path)


def _tidy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` leaves and empty optional blocks so the YAML stays readable.

    Round-trips cleanly: every omitted key has the same default when loaded.
    Top-level keys are kept even when empty so the file shape stays familiar.
    """

    hard_raw = payload.get("hard")
    hard: dict[str, Any] = dict(hard_raw) if isinstance(hard_raw, dict) else {}
    tidy_hard: dict[str, Any] = {}
    for key, value in hard.items():
        if value is None:
            continue
        if isinstance(value, dict):
            tidy_hard[key] = {k: v for k, v in value.items() if v is not None}
        else:
            tidy_hard[key] = value
    payload["hard"] = tidy_hard
    weights_raw = payload.get("weights")
    if isinstance(weights_raw, dict):
        payload["weights"] = {
            key: ({k: v for k, v in value.items() if v is not None}
                  if isinstance(value, dict) else value)
            for key, value in weights_raw.items()
        }
    compacted = _compact_numbers(payload)
    return compacted if isinstance(compacted, dict) else payload


def _compact_numbers(value: Any) -> Any:
    """Write integral floats as ints (``2500.0`` -> ``2500``); loads identically."""

    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _compact_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_compact_numbers(v) for v in value]
    return value


def _coerce_float(
    value: Any, *, field_name: str = "Value", minimum: float | None = None
) -> float | None:
    """Convert a form field to float. Empty -> None; malformed/negative -> error."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    else:
        text = value
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number (got {str(value).strip()!r}).") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be a finite number.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} cannot be negative.")
    return number


__all__ = [
    "AppState",
    "ListRow",
    "create_app",
]
