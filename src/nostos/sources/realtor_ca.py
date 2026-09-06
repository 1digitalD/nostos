from __future__ import annotations

import atexit
import hashlib
import importlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from protego import Protego
from selectolax.parser import HTMLParser, Node

from nostos.context import SearchContext
from nostos.model import (
    Absence,
    Area,
    Identity,
    LatLng,
    Listing,
    Money,
    Observed,
    Origin,
    Photo,
    Place,
    SourceRecord,
)
from nostos.sources.base import Capabilities, Liveness
from nostos.sources.http import RobotsDisallowedError

FetchText = Callable[[str], str]
NowProvider = Callable[[], datetime]

_BASE_URL = "https://www.realtor.ca"
_ID_RE = re.compile(r"/real-estate/(\d+)/")
_MONEY_RE = re.compile(r"\$([\d,]+)")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_POINT_RE = re.compile(r"_(-?\d+\.\d+)_(-?\d+\.\d+)(?:_|$)")


class PlaywrightRealtorFetcher:
    """Render Realtor.ca with an installed Chrome browser and a reusable context."""

    def __init__(self, *, timeout_ms: int = 30_000) -> None:
        self._timeout_ms = timeout_ms
        self._manager: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._robots: Protego | None = None
        self._last_navigation_at: float | None = None
        atexit.register(self.close)

    def __call__(self, url: str) -> str:
        self._assert_robots_allowed(url)
        self._wait_for_navigation_slot()
        page = self._get_page()
        if "/map#" in url:
            try:
                with page.expect_response(
                    lambda response: "AsyncPropertySearch_Post" in response.url,
                    timeout=self._timeout_ms,
                ) as response_info:
                    page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                return json.dumps(response_info.value.json())
            except Exception as exc:
                raise RuntimeError(
                    "Realtor.ca did not return its browser search response; "
                    "the site may be temporarily blocking automated access"
                ) from exc
        detail_page = self._context.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            detail_page.locator("#listingAddress").wait_for(
                state="visible", timeout=self._timeout_ms
            )
            return str(detail_page.content())
        finally:
            detail_page.close()

    def _wait_for_navigation_slot(self) -> None:
        now = time.monotonic()
        if self._last_navigation_at is not None:
            delay = 5.0 - (now - self._last_navigation_at)
            if delay > 0:
                time.sleep(delay)
        self._last_navigation_at = time.monotonic()

    def close(self) -> None:
        for resource_name in ("_context", "_browser", "_manager"):
            resource = getattr(self, resource_name)
            if resource is not None:
                try:
                    resource.close() if resource_name != "_manager" else resource.stop()
                except Exception:  # noqa: BLE001 - best-effort process cleanup
                    pass
                setattr(self, resource_name, None)
        self._page = None

    def _get_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            sync_api = importlib.import_module("playwright.sync_api")
        except ImportError as exc:
            raise RuntimeError(
                "Realtor.ca needs the browser extra: pip install 'nostos-cli[browser]'"
            ) from exc
        self._manager = sync_api.sync_playwright().start()
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        profile_dir = cache_root / "nostos" / "realtor-ca-chrome"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._context = self._manager.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", headless=False, locale="en-CA"
            )
        except Exception as exc:
            self.close()
            raise RuntimeError(
                "Realtor.ca needs Google Chrome installed and available to Playwright"
            ) from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _assert_robots_allowed(self, url: str) -> None:
        if self._robots is None:
            response = httpx.get(f"{_BASE_URL}/robots.txt", timeout=15, follow_redirects=True)
            response.raise_for_status()
            self._robots = Protego.parse(response.text)
        if not self._robots.can_fetch(url, "nostos"):
            raise RobotsDisallowedError(url, "disallowed for user-agent 'nostos'")


class RealtorCaSource:
    name = "realtor_ca"
    capabilities = Capabilities(
        requires_credentials=False,
        supports_detail_fetch=True,
        requires_browser=True,
        rate_limit_per_minute=12.0,
    )

    def __init__(
        self,
        *,
        fetcher: FetchText | None = None,
        now_provider: NowProvider | None = None,
    ) -> None:
        self._fetcher = fetcher or PlaywrightRealtorFetcher()
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        search_url = _search_url(ctx)
        html = self._fetcher(search_url)
        fetched_at = self._now_provider()
        records: list[SourceRecord] = []
        api_results = _api_results(html)
        candidates = (
            api_results if api_results is not None else HTMLParser(html).css(".listingCard")
        )
        for candidate in candidates:
            record = (
                _record_from_api_result(candidate, fetched_at=fetched_at)
                if isinstance(candidate, Mapping)
                else _record_from_card(candidate, fetched_at=fetched_at)
            )
            if record is not None:
                records.append(record)
        return iter(records)

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        try:
            detail = _detail_payload(self._fetcher(rec.url))
        except RobotsDisallowedError:
            return rec
        except Exception:  # noqa: BLE001 - preserve discovery record on detail failure
            return rec
        if not detail:
            return rec
        existing = _payload(rec.payload)
        merged = {**existing, **detail}
        return SourceRecord(
            source=rec.source,
            source_id=rec.source_id,
            url=rec.url,
            content_hash=_content_hash(merged),
            fetched_at=self._now_provider(),
            payload=merged,
        )

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        payload = _payload(rec.payload)
        return Liveness.OK if payload.get("address") and payload.get("price") else Liveness.DEGRADED

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        payload = _payload(rec.payload)
        observed_at = rec.fetched_at
        address = _text(payload.get("address"))
        description = _text(payload.get("description"))
        management = _text(payload.get("management"))
        attributes: dict[str, Observed[Any] | Absence] = {}
        for key, value, evidence in (
            ("title", address, "Realtor.ca listing address"),
            ("description", description, "Realtor.ca listing description and building features"),
            ("management_company", management, "Realtor.ca maintenance management company"),
        ):
            if value:
                attributes[key] = _observed(value, observed_at, evidence)
        point_value = payload.get("point")
        point = LatLng.model_validate(point_value) if isinstance(point_value, Mapping) else None
        price = _float(payload.get("price"))
        parking_spaces = _float(payload.get("parking_spaces"))
        photos = [Photo(url=url) for url in _string_list(payload.get("photos"))]
        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=Identity.compute_signature(
                    title=address, price=int(price) if price is not None else None, address=address
                ),
            ),
            place=Place(
                raw_address=address,
                structured=None,
                point=point,
                area_key=_infer_area(address, ctx),
            ),
            rent=_money(price, ctx.citypack.locale.currency, observed_at),
            beds=_number(payload.get("beds"), observed_at, "Realtor.ca bedrooms"),
            baths=_number(payload.get("baths"), observed_at, "Realtor.ca bathrooms"),
            area=_area(payload.get("area_sqft"), ctx.citypack.locale.area_unit, observed_at),
            floor=Absence.NOT_STATED,
            parking=(
                _observed(
                    "Included" if parking_spaces > 0 else "Unavailable",
                    observed_at,
                    "Realtor.ca total parking spaces",
                )
                if parking_spaces is not None
                else Absence.NOT_STATED
            ),
            furnishing=Absence.NOT_STATED,
            photos=photos,
            attributes=attributes,
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def _search_url(ctx: SearchContext) -> str:
    if not ctx.citypack.areas:
        raise ValueError("Realtor.ca requires at least one configured city area")
    boxes = [area.bbox for area in ctx.citypack.areas]
    params: dict[str, str] = {
        "LatitudeMax": str(max(box[2] for box in boxes)),
        "LongitudeMax": str(max(box[3] for box in boxes)),
        "LatitudeMin": str(min(box[0] for box in boxes)),
        "LongitudeMin": str(min(box[1] for box in boxes)),
        "view": "list",
        "Sort": "6-D",
        "PropertyTypeGroupID": "1",
        "TransactionTypeId": "3",
        "PropertySearchTypeId": "1",
        "Currency": ctx.citypack.locale.currency,
    }
    beds = ctx.profile.hard.beds
    if beds is not None and beds.eq is not None:
        params["BedRange"] = f"{int(beds.eq)}-{int(beds.eq)}"
    return f"{_BASE_URL}/map#{urlencode(params)}"


def _record_from_card(card: Node, *, fetched_at: datetime) -> SourceRecord | None:
    link = card.css_first("a.listingDetailsLink")
    href = link.attributes.get("href") if link is not None else None
    if not href or (match := _ID_RE.search(href)) is None:
        return None
    address = _node_text(card.css_first(".listingCardAddress"))
    if not re.search(r",\s*Toronto\b", address, re.IGNORECASE):
        return None
    url = urljoin(_BASE_URL, href)
    icon_values = [_node_text(node) for node in card.css(".listingCardIconNum")]
    payload: dict[str, Any] = {
        "address": address,
        "price": _first_number(_node_text(card.css_first(".listingCardPrice"))),
        "beds": _first_number(icon_values[0]) if len(icon_values) > 0 else None,
        "baths": _first_number(icon_values[1]) if len(icon_values) > 1 else None,
        "area_sqft": _first_number(icon_values[2]) if len(icon_values) > 2 else None,
        "brokerage": _node_text(card.css_first(".listingCardOfficeName")),
        "photos": _image_urls(card),
    }
    favourite = card.css_first("a.propertyCardDetailsFavouriteIcon")
    point_value = favourite.attributes.get("data-value") if favourite else None
    point_match = _POINT_RE.search(point_value) if point_value is not None else None
    if point_match is not None:
        payload["point"] = {"lat": float(point_match.group(1)), "lng": float(point_match.group(2))}
    source_id = match.group(1)
    return SourceRecord(
        source="realtor_ca",
        source_id=source_id,
        url=url,
        content_hash=_content_hash(payload),
        fetched_at=fetched_at,
        payload=payload,
    )


def _api_results(value: str) -> list[Mapping[str, Any]] | None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    results = payload.get("Results")
    if not isinstance(results, list):
        return None
    return [item for item in results if isinstance(item, Mapping)]


def _record_from_api_result(
    result: Mapping[str, Any], *, fetched_at: datetime
) -> SourceRecord | None:
    source_id = _text(result.get("Id"))
    relative_url = _text(result.get("RelativeDetailsURL") or result.get("RelativeURLEn"))
    building = _mapping(result.get("Building"))
    property_data = _mapping(result.get("Property"))
    address_data = _mapping(property_data.get("Address"))
    address = _text(address_data.get("AddressText")).replace("|", ", ")
    if not source_id or not relative_url or not re.search(r",\s*Toronto\b", address, re.IGNORECASE):
        return None

    floor_measurements = building.get("FloorAreaMeasurements")
    area_text = ""
    if isinstance(floor_measurements, list) and floor_measurements:
        area_text = _text(_mapping(floor_measurements[0]).get("AreaUnformatted"))
    description = " ".join(
        value
        for value in (
            _text(result.get("PublicRemarks")),
            _text(building.get("Ammenities")),
            _text(property_data.get("AmmenitiesNearBy")),
        )
        if value
    )
    individuals = result.get("Individual")
    organization: Mapping[str, Any] = {}
    if isinstance(individuals, list) and individuals:
        organization = _mapping(_mapping(individuals[0]).get("Organization"))
    payload: dict[str, Any] = {
        "address": address,
        "price": _first_number(
            _text(property_data.get("LeaseRentUnformattedValue") or property_data.get("LeaseRent"))
        ),
        "beds": _first_number(_text(building.get("Bedrooms"))),
        "baths": _first_number(_text(building.get("BathroomTotal"))),
        "area_sqft": _first_number(area_text),
        "brokerage": _text(organization.get("Name")),
        "description": description,
        "parking_spaces": _first_number(_text(property_data.get("ParkingSpaceTotal"))),
        "photos": _api_photo_urls(property_data.get("Photo")),
    }
    latitude = _float(address_data.get("Latitude"))
    longitude = _float(address_data.get("Longitude"))
    if latitude is not None and longitude is not None:
        payload["point"] = {"lat": latitude, "lng": longitude}
    return SourceRecord(
        source="realtor_ca",
        source_id=source_id,
        url=urljoin(_BASE_URL, relative_url),
        content_hash=_content_hash(payload),
        fetched_at=fetched_at,
        payload=payload,
    )


def _api_photo_urls(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    urls = [_text(_mapping(item).get("HighResPath")) for item in value]
    return list(dict.fromkeys(url for url in urls if url.startswith("https://")))


def _detail_payload(html: str) -> dict[str, Any]:
    parser = HTMLParser(html)
    if parser.css_first("#listingAddress") is None:
        return {}
    description = _node_text(parser.css_first("#PropertyDescription"))
    description = re.sub(r"^Listing Description\s*", "", description, flags=re.IGNORECASE)
    features = _node_text(parser.css_first("#propertyDetailsBuilding_BuildingFeatures"))
    management = _label_value(
        parser.css_first("#propertyDetailsSectionVal_MaintenanceManagementCompany")
    )
    address = _node_text(parser.css_first("#listingAddress"), separator=", ")
    payload: dict[str, Any] = {
        "address": address,
        "price": _first_number(_node_text(parser.css_first("#listingPriceValue"))),
        "beds": _icon_number(parser.css_first("#BedroomIcon")),
        "baths": _icon_number(parser.css_first("#BathroomIcon")),
        "area_sqft": _icon_number(parser.css_first("#SquareFootageIcon")),
        "description": " ".join(part for part in (description, features) if part),
        "management": management,
        "parking_spaces": _first_number(
            _label_value(parser.css_first("#propertyDetailsSectionVal_TotalParkingSpaces"))
        ),
        "photos": _image_urls(parser),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _icon_number(node: Node | None) -> float | None:
    return _first_number(_node_text(node.css_first(".listingCardIconNum") if node else None))


def _label_value(node: Node | None) -> str:
    if node is None:
        return ""
    texts = [
        text.strip() for text in node.text(separator="\n", strip=True).splitlines() if text.strip()
    ]
    return texts[-1] if texts else ""


def _image_urls(node: Node | HTMLParser) -> list[str]:
    urls: list[str] = []
    for image in node.css("img"):
        value = (image.attributes.get("src") or "").strip()
        if value.startswith("https://cdn.realtor.ca/listings/"):
            urls.append(value)
    return list(dict.fromkeys(urls))


def _node_text(node: Node | None, *, separator: str = " ") -> str:
    return node.text(separator=separator, strip=True) if node is not None else ""


def _first_number(value: str) -> float | None:
    money = _MONEY_RE.search(value)
    if money is not None:
        return float(money.group(1).replace(",", ""))
    number = _NUMBER_RE.search(value.replace(",", ""))
    return float(number.group(0)) if number is not None else None


def _payload(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("RealtorCaSource payload must be a mapping")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value or "").strip()


def _float(value: object) -> float | None:
    try:
        return float(str(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item).startswith("https://")]


def _observed(value: Any, observed_at: datetime, evidence: str) -> Observed[Any]:
    return Observed[Any](
        value=value,
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence=evidence,
        observed_at=observed_at,
    )


def _number(value: object, observed_at: datetime, evidence: str) -> Observed[float] | Absence:
    number = _float(value)
    return _observed(number, observed_at, evidence) if number is not None else Absence.NOT_STATED


def _money(value: float | None, currency: str, observed_at: datetime) -> Observed[Money] | Absence:
    if value is None:
        return Absence.NOT_STATED
    return _observed(
        Money(amount=Decimal(str(value)), currency=currency, period="month"),
        observed_at,
        "Realtor.ca listed rent",
    )


def _area(value: object, unit: str, observed_at: datetime) -> Observed[Area] | Absence:
    number = _float(value)
    if number is None:
        return Absence.NOT_STATED
    return _observed(Area(value=number, unit=unit), observed_at, "Realtor.ca square footage")


def _infer_area(address: str, ctx: SearchContext) -> str | None:
    lowered = address.casefold()
    for area in ctx.citypack.areas:
        if any(keyword.casefold() in lowered for keyword in area.keywords):
            return area.key
    return None


def _content_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
