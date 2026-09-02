"""FastAPI app for the local Nostos web UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nostos.config.profile import Profile, ScaledWeight
from nostos.config.wizard import dump_profile_yaml
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

# Maximum length for a note saved through the web UI. Notes are free-form text
# but unbounded length is a foot-gun (large DB rows, slow renders). The cap is
# generous so legitimate notes still fit comfortably.
_MAX_NOTE_LEN = 4000

# Acceptable sort keys. Anything else falls back to "score" rather than 422 —
# invalid input is silently ignored so a stale bookmark or hand-crafted link
# cannot break the page.
_VALID_SORT_KEYS: frozenset[str] = frozenset({"score", "rent", "posted", "address"})



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
        normalized_sort = sort if sort in _VALID_SORT_KEYS else "score"
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
        )
        with state.connect() as conn:
            rows = query_list(
                conn,
                context=state.context,
                profile_id=state.profile_id,
                sources=state.sources,
                filters=filters,
            )
            row_actions = _row_action_states(
                conn, listing_ids=tuple(row.listing_id for row in rows)
            )
            contributors_by_id = _contributors_by_listing(
                conn,
                listing_ids=tuple(row.listing_id for row in rows),
                profile_id=state.profile_id,
                n=2,
            )
            filter_chips = _filter_chips(filters, known_areas(state.context))
            profile_summary = _profile_summary(state.context.profile)

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
            action_repo = ActionRepo(conn)
            actions = action_repo.get_actions(listing_id=listing_id)
            action_state = {
                "starred": action_repo.has_action(listing_id=listing_id, kind="star"),
                "dismissed": action_repo.has_action(listing_id=listing_id, kind="dismiss"),
                "excluded": action_repo.has_action(listing_id=listing_id, kind="excluded"),
                "contacted": action_repo.has_action(listing_id=listing_id, kind="contacted"),
            }
            breakdown = _score_breakdown(conn, listing_id=listing_id, profile_id=state.profile_id)
            breakdown_json = breakdown.get("breakdown_json") if breakdown else None
            breakdown_contributors = (
                _top_contributors(breakdown_json, n=8) if breakdown_json else []
            )

        return state.templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                "row": row,
                "listing_id": listing_id,
                "actions": actions,
                "action_state": action_state,
                "breakdown": breakdown,
                "breakdown_contributors": breakdown_contributors,
                "category_scores": row.category_scores,
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

    @app.get("/profile", response_class=HTMLResponse)
    def profile_view(
        request: Request,
        state: StateDep,
        saved: bool = False,
        error: str | None = None,
    ) -> HTMLResponse:
        """Render the editable ranking-factors page from the active profile."""

        profile = state.context.profile
        form = _build_profile_form_data(profile)
        return state.templates.TemplateResponse(
            request=request,
            name="profile.html",
            context={
                "form": form,
                "profile_path": str(state.profile_path),
                "saved": saved,
                "error": error,
                "profile_id": state.profile_id,
            },
        )

    @app.post("/profile", response_class=HTMLResponse)
    async def profile_save(
        request: Request,
        state: StateDep,
    ) -> HTMLResponse:
        """Apply form edits to the current profile and save back to disk."""

        form_data = await request.form()
        try:
            new_profile = _apply_profile_form(state.context.profile, form_data)
            _save_profile_yaml(state.profile_path, new_profile)
        except (ValueError, KeyError, TypeError) as exc:
            return profile_view(
                request=request,
                state=state,
                saved=False,
                error=str(exc),
            )
        return profile_view(
            request=request,
            state=state,
            saved=True,
            error=None,
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
        ("sort", "sort", lambda f: f"sort: {f.sort}"),
    ]

    chips: list[dict[str, str]] = []
    for param, _key, label_fn in chip_specs:
        value = getattr(filters, _key)
        if value is None:
            continue
        if param == "sort" and value == "score":
            continue
        label = label_fn(filters)
        remove_url = _url_without_param(filters, param)
        chips.append({"label": label, "param": param, "remove_url": remove_url})
    return chips

def _profile_summary(profile: Profile) -> list[str]:
    """Render the active profile's hard filters as a row of inline chips.

    Surfaces WHAT the ranking uses (rent.max, beds, baths, area.min,
    enabled sources) — the user should see these at a glance and have a
    single click to edit them.
    """

    chips: list[str] = []
    hard = profile.hard
    if hard.rent is not None and hard.rent.max is not None:
        chips.append(f"rent ≤ ${int(hard.rent.max):,}")
    if hard.beds is not None:
        if hard.beds.eq is not None:
            chips.append(f"{int(hard.beds.eq)} bd exactly")
        elif hard.beds.min is not None:
            chips.append(f"≥ {int(hard.beds.min)} bd")
    if hard.baths is not None:
        if hard.baths.eq is not None:
            chips.append(f"{hard.baths.eq} ba exactly")
        else:
            bits: list[str] = []
            if hard.baths.min is not None:
                bits.append(f"≥ {hard.baths.min}")
            if hard.baths.max is not None:
                bits.append(f"≤ {hard.baths.max}")
            if bits:
                chips.append("ba " + " ".join(bits))
    if hard.area is not None and hard.area.min is not None:
        chips.append(f"≥ {int(hard.area.min)} {hard.area.unit}")
    enabled_sources = sorted(k for k, v in profile.sources.items() if v)
    if enabled_sources:
        chips.append("sources: " + " + ".join(enabled_sources))
    return chips


def _url_without_param(filters: ListFilter, param: str) -> str:
    """Build a query string with the given param dropped (others preserved)."""

    pairs: dict[str, object] = {
        "rent_min": filters.rent_min,
        "rent_max": filters.rent_max,
        "beds": filters.beds,
        "baths_min": filters.baths_min,
        "area_min": filters.area_min,
        "score_min": filters.score_min,
        "source": filters.source,
        "area_name": filters.area_name,
        "sort": filters.sort,
    }
    pairs.pop(param, None)
    if pairs.get("sort") == "score":
        pairs.pop("sort", None)
    pairs = {k: v for k, v in pairs.items() if v is not None}
    if not pairs:
        return "/"
    return "/?" + urlencode(pairs)


def _contributors_by_listing(
    conn: Any, *, listing_ids: tuple[str, ...], profile_id: str, n: int = 2
) -> dict[str, list[dict[str, float | str]]]:
    """Pre-compute top-n contributors for many listings in one pass."""

    result: dict[str, list[dict[str, float | str]]] = {}
    for listing_id in listing_ids:
        bd = _score_breakdown(conn, listing_id=listing_id, profile_id=profile_id)
        if bd is None:
            continue
        breakdown = bd.get("breakdown_json")
        contributors = _top_contributors(breakdown, n=n)
        if contributors:
            result[str(listing_id)] = contributors
    return result



def _build_profile_form_data(profile: Profile) -> dict[str, object]:
    """Shape the active profile into a form-friendly dict for the profile page."""

    hard = profile.hard
    beds_eq = hard.beds.eq if hard.beds else None
    beds_min = hard.beds.min if hard.beds else None
    baths_min = hard.baths.min if hard.baths else None
    baths_max = hard.baths.max if hard.baths else None

    weights: list[dict[str, object]] = []
    for rule_key, value in profile.weights.items():
        scalar = int(value.cap) if isinstance(value, ScaledWeight) else int(value)
        weights.append({"rule_key": rule_key, "label": rule_key, "value": scalar})

    sources: list[dict[str, object]] = []
    for source_key in sorted(profile.sources.keys()):
        sources.append({
            "key": source_key,
            "label": source_key,
            "enabled": bool(profile.sources[source_key]),
        })

    return {
        "hard": {
            "rent_max": hard.rent.max if hard.rent else None,
            "beds_eq": beds_eq,
            "beds_min": beds_min,
            "baths_min": baths_min,
            "baths_max": baths_max,
            "area_min": hard.area.min if hard.area else None,
        },
        "weights": weights,
        "sources": sources,
    }


def _apply_profile_form(profile: Profile, form_data: Any) -> Profile:
    """Return a new Profile with form fields applied; preserve everything else."""

    payload = profile.model_dump(mode="json")

    hard = dict(payload.get("hard") or {})
    rent_max_raw = form_data.get("rent_max")
    hard["rent"] = (
        {"max": _coerce_float(rent_max_raw), "currency": "CAD"} if rent_max_raw else None
    )
    beds_eq = _coerce_float(form_data.get("beds_eq"))
    beds_min = _coerce_float(form_data.get("beds_min"))
    if beds_eq is not None or beds_min is not None:
        hard["beds"] = {"eq": beds_eq, "min": beds_min, "max": None}
    else:
        hard["beds"] = None
    baths_min = _coerce_float(form_data.get("baths_min"))
    baths_max = _coerce_float(form_data.get("baths_max"))
    if baths_min is not None or baths_max is not None:
        hard["baths"] = {"min": baths_min, "max": baths_max}
    else:
        hard["baths"] = None
    area_min = _coerce_float(form_data.get("area_min"))
    hard["area"] = (
        {"min": area_min, "unit": "sqft"} if area_min is not None else None
    )
    payload["hard"] = hard

    weights = dict(payload.get("weights") or {})
    for rule_key in list(weights.keys()):
        form_key = f"weight_{rule_key}"
        if form_key not in form_data:
            continue
        new_value = _coerce_float(form_data.get(form_key))
        if new_value is None:
            continue
        existing = weights[rule_key]
        if isinstance(existing, dict):
            weights[rule_key] = {**existing, "cap": int(new_value)}
        else:
            weights[rule_key] = int(new_value)
    payload["weights"] = weights

    sources = dict(payload.get("sources") or {})
    for source_key in list(sources.keys()):
        sources[source_key] = form_data.get(f"src_{source_key}") is not None
    payload["sources"] = sources

    return Profile.model_validate(payload)


def _save_profile_yaml(path: Path, profile: Profile) -> None:
    """Atomically write the profile to disk (tmp + rename)."""

    text_yaml = dump_profile_yaml(profile.model_dump(mode="json"))
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text_yaml, encoding="utf-8")
    tmp_path.replace(path)


def _coerce_float(value: Any) -> float | None:
    """Convert a form field to float, returning None for empty/malformed."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

__all__ = [
    "AppState",
    "ListRow",
    "create_app",
]
