"""FastAPI app for the local Nostos web UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from nostos.config.profile import Profile, ScaledWeight
from nostos.config.wizard import dump_profile_yaml
from nostos.context import SearchContext, load_search_context
from nostos.rank import rules as rules_module
from nostos.rank.rescore import RescoreReport, rescore_profile
from nostos.rank.rules import DEFAULT_REGISTRY
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

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Maximum length for a note saved through the web UI. Notes are free-form text
# but unbounded length is a foot-gun (large DB rows, slow renders). The cap is
# generous so legitimate notes still fit comfortably.
_MAX_NOTE_LEN = 4000

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
        )
        resolutions = resolve_source_registry(
            context=context,
            sources=sources,
            credentials_present={source.name: True for source in sources},
        )
        active = enabled_sources(resolutions)
        return context, {item.name: item for item in active}

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
        status: str | None = Query(default=None),
        starred: bool = Query(default=False),
        hide_dismissed: bool = Query(default=False),
        show_excluded: bool = Query(default=False),
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
                "breakdown": breakdown,
                "breakdown_contributors": breakdown_contributors,
                "category_scores": row.category_scores,
                "rule_rows_fired": [r for r in rule_rows if r.contribution != 0.0],
                "rule_rows_idle": [r for r in rule_rows if r.contribution == 0.0],
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
        _save_profile_yaml(state.profile_path, new_profile)
        state.reload()
        params: dict[str, object] = {"saved": 1}
        try:
            report = state.rescore()
        except ValueError as exc:
            params["error"] = f"Saved, but re-scoring failed: {_describe_error(exc)}"
        else:
            params["rescored"] = report.scored_count
            params["skipped"] = report.skipped
        return RedirectResponse(url="/profile?" + urlencode(params), status_code=303)

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
    """Return the non-default filter values keyed by URL param (sort excluded)."""

    pairs = _filter_query_pairs(filters)
    pairs.pop("sort", None)
    return pairs


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
        ("starred", "★ Shortlisted only", "Show only listings you shortlisted", filters.starred),
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
        ("match", "✓ Match", "Only listings that meet every hard criterion"),
        ("unverified", "? Unverified", "Only listings missing data for a criterion"),
        ("miss", "✕ Miss", "Only listings that fail a hard criterion"),
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
