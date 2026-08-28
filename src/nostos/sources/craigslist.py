from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from selectolax.parser import HTMLParser

from nostos.context import SearchContext
from nostos.model import (
    Absence,
    Area,
    Identity,
    Listing,
    Money,
    Observed,
    Origin,
    Photo,
    Place,
    SourceRecord,
)
from nostos.sources.base import Capabilities, Liveness

CL_SOURCE_NAME = "craigslist"
CL_BASE_DEFAULT = "https://vancouver.craigslist.org"
CL_AREAS_DEFAULT: tuple[str, ...] = ("van", "nvn", "bby")
CL_EPOCH = "1970-01-01T00:00:00Z"
CL_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
CL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
_ID_RE = re.compile(r"/([A-Za-z0-9]+)(?:[?#].*)?$")
_PRICE_RE = re.compile(r"\$([\d,]+)")
_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*br\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ba|bath|bathroom)s?\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d{3,5})\s*(?:ft2|sq\s*ft|sqft|square\s*feet)\b", re.IGNORECASE)
_UNIT_RE = re.compile(r"(?:#|unit|apt|suite)\s*(\d{1,4})", re.IGNORECASE)


class CraigslistSource:
    name = CL_SOURCE_NAME
    capabilities = Capabilities(
        requires_credentials=False,
        supports_detail_fetch=True,
        requires_browser=False,
        rate_limit_per_minute=60.0,
    )

    def __init__(
        self,
        *,
        fetch_text: Callable[[str], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch_text = fetch_text or _http_fetch_text
        self._now = now or _utc_now

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        base_url, areas = _source_scope(ctx)
        query = _discover_query(ctx)
        fetched_at = self._now()
        by_id: dict[str, SourceRecord] = {}

        for area in areas:
            for item in self._discover_area(base_url=base_url, area=area, query=query):
                source_id = _coerce_str(item.get("id"))
                if not source_id:
                    continue
                item_url = _coerce_str(item.get("url"))
                if not item_url:
                    continue

                payload: dict[str, Any] = {
                    "id": source_id,
                    "source": CL_SOURCE_NAME,
                    "area": area,
                    "url": item_url,
                    "posted": _coerce_str(item.get("posted")),
                    "title": _coerce_str(item.get("title")),
                    "price": _coerce_int(item.get("price")),
                }
                by_id[source_id] = SourceRecord(
                    source=self.name,
                    source_id=source_id,
                    url=item_url,
                    content_hash=_content_hash(payload),
                    fetched_at=fetched_at,
                    payload=payload,
                )

        ordered = sorted(by_id.values(), key=lambda rec: rec.source_id)
        return iter(ordered)

    def _discover_area(
        self,
        *,
        base_url: str,
        area: str,
        query: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        rss_url = f"{base_url}/search/{area}/apa?{urlencode(query)}"
        should_fallback_to_html = False
        rss_items: list[dict[str, Any]] = []

        try:
            rss_text = self._fetch_text(rss_url)
        except httpx.HTTPStatusError as exc:
            should_fallback_to_html = bool(
                exc.response is not None and exc.response.status_code == 403
            )
            if not should_fallback_to_html:
                raise
        else:
            rss_items = parse_cl_rss(rss_text, CL_EPOCH)
            should_fallback_to_html = len(rss_items) == 0

        if not should_fallback_to_html:
            return rss_items

        html_query = dict(query)
        html_query.pop("format", None)
        html_url = f"{base_url}/search/{area}/apa?{urlencode(html_query)}"
        html_text = self._fetch_text(html_url)
        return parse_cl_search_html(html_text, CL_EPOCH)

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        fetched_at = self._now()
        payload = _payload_mapping(rec.payload)
        payload_copy = dict(payload)
        payload_copy.setdefault("source", self.name)
        payload_copy.setdefault("id", rec.source_id)
        payload_copy.setdefault("url", rec.url)

        try:
            html = self._fetch_text(rec.url)
            detail = _parse_detail_html(html)
            payload_copy.update(
                {key: value for key, value in detail.items() if value not in (None, "")}
            )
            payload_copy["posted"] = cl_posted_iso(
                _coerce_str(payload_copy.get("posted")),
                _coerce_str(payload_copy.get("posted_label")),
                current=fetched_at,
            )
            payload_copy.pop("posted_label", None)
        except Exception as exc:
            payload_copy["detail_error"] = str(exc)

        return SourceRecord(
            source=rec.source,
            source_id=rec.source_id,
            url=rec.url,
            content_hash=_content_hash(payload_copy),
            fetched_at=fetched_at,
            payload=payload_copy,
        )

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        payload = _payload_mapping(rec.payload)
        marker = payload.get("liveness")
        if marker in {"ok", "degraded", "failed"}:
            return Liveness(_coerce_str(marker))
        if payload.get("detail_error"):
            return Liveness.DEGRADED
        if not _coerce_str(payload.get("title")) and not _coerce_int(payload.get("price")):
            return Liveness.DEGRADED
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        payload = _payload_mapping(rec.payload)
        observed_at = rec.fetched_at

        title = _coerce_str(payload.get("title"))
        raw_address = _coerce_str(payload.get("address")) or _coerce_str(payload.get("location"))
        price = _coerce_int(payload.get("price"))
        beds = _coerce_float(payload.get("beds"))
        baths = _coerce_float(payload.get("baths"))
        sqft = _coerce_float(payload.get("sqft"))
        floor = _coerce_int(payload.get("floor"))
        parking = _coerce_str(payload.get("parking"))
        furnishing = _coerce_str(payload.get("furnished"))

        area_key = _infer_area_key(
            payload=payload,
            raw_address=raw_address,
            title=title,
            ctx=ctx,
        )
        place = Place(
            raw_address=raw_address or None,
            structured=None,
            point=None,
            area_key=area_key,
        )

        origin = Origin.DETAIL_PAGE if _coerce_str(payload.get("address")) else Origin.SOURCE_FIELD
        attributes: dict[str, Observed[Any] | Absence] = {}
        posted = _coerce_str(payload.get("posted"))
        if posted:
            attributes["posted"] = _text_observed(
                posted,
                observed_at,
                Origin.SOURCE_FIELD,
                "craigslist posted",
            )
        description = _coerce_str(payload.get("description"))
        if description:
            attributes["description"] = _text_observed(
                description, observed_at, Origin.DETAIL_PAGE, "craigslist posting body"
            )
        if title:
            attributes["title"] = _text_observed(
                title,
                observed_at,
                Origin.SOURCE_FIELD,
                "craigslist title",
            )

        photos = _parse_photos(payload)

        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=Identity.compute_signature(
                    title=title or rec.source_id,
                    price=price,
                    address=raw_address,
                ),
            ),
            place=place,
            rent=_money_field(
                amount=price,
                observed_at=observed_at,
                currency=ctx.citypack.locale.currency,
            ),
            beds=_float_field(
                beds,
                observed_at=observed_at,
                origin=origin,
                evidence="craigslist bedrooms",
            ),
            baths=_float_field(
                baths,
                observed_at=observed_at,
                origin=origin,
                evidence="craigslist bathrooms",
            ),
            area=_area_field(
                sqft,
                observed_at=observed_at,
                unit=ctx.citypack.locale.area_unit,
                origin=origin,
            ),
            floor=_int_field(
                floor,
                observed_at=observed_at,
                origin=origin,
                evidence="craigslist floor",
            ),
            parking=_string_field(
                parking, observed_at=observed_at, origin=origin, evidence="craigslist parking"
            ),
            furnishing=_string_field(
                furnishing, observed_at=observed_at, origin=origin, evidence="craigslist furnishing"
            ),
            photos=photos,
            attributes=attributes,
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def parse_cl_rss(rss: str, last_scan_iso: str) -> list[dict[str, Any]]:
    """Ported parser from apartment-hunt adapter with base62 id handling."""
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(rss)
    except ET.ParseError:
        return items

    for item in root.findall(".//item"):
        link = _extract_link(item)
        if not link:
            continue
        match = _ID_RE.search(link)
        if not match:
            continue

        posted = _parse_feed_datetime(_extract_posted(item))
        if posted < last_scan_iso:
            continue

        title = _extract_item_text(item, "title")
        description = _extract_item_text(item, "description")
        price = _extract_price(description or title)
        items.append(
            {
                "id": match.group(1),
                "source": CL_SOURCE_NAME,
                "url": link,
                "posted": posted,
                "title": title,
                "price": price,
            }
        )

    return items


def parse_cl_search_html(html: str, last_scan_iso: str) -> list[dict[str, Any]]:
    del last_scan_iso
    node = HTMLParser(html)
    items: list[dict[str, Any]] = []

    for search_result in node.css("li.cl-static-search-result"):
        listing_link = search_result.css_first("a[href]")
        if listing_link is None:
            continue
        href_value = listing_link.attributes.get("href")
        if not isinstance(href_value, str):
            continue
        listing_url = href_value.strip()
        if not listing_url:
            continue

        match = _ID_RE.search(listing_url)
        if not match:
            continue

        title = _node_text(search_result.css_first(".title"))
        if not title:
            title_attr = search_result.attributes.get("title")
            title = title_attr.strip() if isinstance(title_attr, str) else ""

        items.append(
            {
                "id": match.group(1),
                "source": CL_SOURCE_NAME,
                "url": listing_url,
                "posted": "",
                "title": title,
                "price": _extract_price(_node_text(search_result.css_first(".price"))),
                "location": _node_text(search_result.css_first(".location")),
            }
        )

    return items


def cl_posted_iso(posted: str, label: str, current: datetime | None = None) -> str:
    """Port of apartment-hunt fallback date parser for Craigslist labels."""
    if posted:
        return posted.replace("+00:00", "Z")

    now = current or _utc_now()
    age = re.search(
        r"<?(\d+)\s*(min|minute|h|hr|hour|d|day|w|week|month|mo)s?\s+ago",
        label or "",
        re.IGNORECASE,
    )
    if age:
        amount = int(age.group(1))
        unit = age.group(2).lower()
        hours = (
            amount / 60
            if unit.startswith("min")
            else amount
            if unit in {"h", "hr", "hour"}
            else amount * 24
            if unit in {"d", "day"}
            else amount * 168
            if unit in {"w", "week"}
            else amount * 720
        )
        return (now - timedelta(hours=hours)).strftime(CL_ISO_FMT)

    month_day = re.match(r"^(\d{1,2})[-/](\d{1,2})$", (label or "").strip())
    if month_day:
        parsed = datetime(
            now.year,
            int(month_day.group(1)),
            int(month_day.group(2)),
            tzinfo=UTC,
        )
        if parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed.strftime(CL_ISO_FMT)

    return ""


def _extract_link(item: ET.Element) -> str:
    for child in item:
        if _local_name(child.tag) == "link":
            return (child.text or "").strip()
    return ""


def _extract_posted(item: ET.Element) -> str:
    for child in item:
        local = _local_name(child.tag)
        if local in {"date", "pubDate"}:
            return (child.text or "").strip()
    return ""


def _extract_item_text(item: ET.Element, target: str) -> str:
    for child in item:
        if _local_name(child.tag) == target:
            return (child.text or "").strip()
    return ""


def _local_name(tag: str) -> str:
    return tag.split("}", maxsplit=1)[-1]


def _parse_feed_datetime(value: str) -> str:
    if not value:
        return CL_EPOCH

    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None

    if parsed is None:
        return CL_EPOCH
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime(CL_ISO_FMT)


def _extract_price(text: str) -> int:
    match = _PRICE_RE.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return 0


def _parse_detail_html(html: str) -> dict[str, Any]:
    node = HTMLParser(html)
    result: dict[str, Any] = {}

    title = _meta_content(node, "og:title") or _first_text(node, "title")
    if title:
        result["title"] = title

    photo_url = _meta_content(node, "og:image")
    if photo_url:
        result["photo"] = photo_url

    posted = _attribute(node, "time[datetime]", "datetime")
    if posted:
        result["posted"] = posted

    posting_body = _node_text(node.css_first("#postingbody"))
    if posting_body:
        result["description"] = posting_body[:2000]

    text_blob = " ".join(filter(None, [title, posting_body, _node_text(node.body)]))
    beds = _coerce_float(_regex_group(_BEDS_RE, text_blob))
    if beds is not None:
        result["beds"] = beds
    baths = _coerce_float(_regex_group(_BATHS_RE, text_blob))
    if baths is not None:
        result["baths"] = baths
    sqft = _coerce_int(_regex_group(_SQFT_RE, text_blob))
    if sqft is not None:
        result["sqft"] = sqft

    price_text = _first_text(node, ".price")
    price = _extract_price(price_text or "")
    if price:
        result["price"] = price

    address = _node_text(node.css_first(".mapaddress"))
    if _is_google_map_placeholder(address):
        address = ""
    if not address:
        address = _address_from_posting_body(posting_body)
    if address:
        result["address"] = address

    floor = _infer_floor(title=title or "", address=address)
    if floor is not None:
        result["floor"] = floor

    lowered = text_blob.lower()
    if "unfurnished" in lowered:
        result["furnished"] = "Unfurnished"
    elif "furnished" in lowered:
        result["furnished"] = "Furnished"
    if any(token in lowered for token in ("parking", "garage", "stall")):
        result["parking"] = "Included"

    return result


def _meta_content(node: HTMLParser, prop_name: str) -> str:
    meta = node.css_first(f'meta[property="{prop_name}"]')
    if meta is None:
        return ""
    value = meta.attributes.get("content")
    return value.strip() if isinstance(value, str) else ""


def _first_text(node: HTMLParser, selector: str) -> str:
    match = node.css_first(selector)
    return _node_text(match)


def _attribute(node: HTMLParser, selector: str, attr: str) -> str:
    match = node.css_first(selector)
    if match is None:
        return ""
    value = match.attributes.get(attr)
    return value.strip() if isinstance(value, str) else ""


def _node_text(node: Any) -> str:
    if node is None:
        return ""
    text = node.text(separator=" ", strip=True)
    if isinstance(text, str):
        return text.strip()
    if text is None:
        return ""
    return str(text).strip()


def _regex_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if match is None:
        return ""
    return match.group(1)


def _address_from_posting_body(text: str) -> str:
    if not text:
        return ""
    addr_line = re.search(r"(^|\n)\s*(?:-\s*)?Address:\s*([^\n]+)", text, re.IGNORECASE)
    if addr_line:
        candidate = addr_line.group(2).strip()
        if _looks_like_address(candidate):
            return candidate
    for line in text.splitlines():
        candidate = line.strip()
        if _looks_like_address(candidate):
            return candidate
    return ""


def _looks_like_address(value: str) -> bool:
    if not value or _is_google_map_placeholder(value):
        return False
    if not re.match(r"^\d", value):
        return False
    return bool(
        re.search(
            r"\b(street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?|drive|dr\.?|lane|ln|court|ct|place|pl|way)\b",
            value,
            re.IGNORECASE,
        )
    )


def _is_google_map_placeholder(value: str) -> bool:
    return bool(re.match(r"^google map", value.strip(), re.IGNORECASE))


def _infer_floor(*, title: str, address: str) -> int | None:
    combined = f"{title} {address}"
    match = _UNIT_RE.search(combined)
    if match is None:
        return None
    unit = _coerce_int(match.group(1))
    if unit is None:
        return None
    if unit >= 100:
        return unit // 100
    return unit


def _parse_photos(payload: Mapping[str, Any]) -> list[Photo]:
    photos: list[Photo] = []
    photo = _coerce_str(payload.get("photo"))
    if photo:
        photos.append(Photo(url=photo))
    raw_photos = payload.get("photos")
    if isinstance(raw_photos, Sequence):
        for raw in raw_photos:
            if isinstance(raw, Mapping):
                url = _coerce_str(raw.get("url"))
                if url:
                    photos.append(Photo(url=url))
            elif isinstance(raw, str) and raw.strip():
                photos.append(Photo(url=raw.strip()))
    deduped: dict[str, Photo] = {item.url: item for item in photos}
    return list(deduped.values())


def _money_field(
    *,
    amount: int | None,
    observed_at: datetime,
    currency: str,
) -> Observed[Money] | Absence:
    if amount is None or amount <= 0:
        return Absence.NOT_STATED
    return Observed[Money](
        value=Money(amount=Decimal(str(amount)), currency=currency, period="month"),
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence="craigslist listed price",
        observed_at=observed_at,
    )


def _float_field(
    value: float | None,
    *,
    observed_at: datetime,
    origin: Origin,
    evidence: str,
) -> Observed[float] | Absence:
    if value is None:
        return Absence.NOT_STATED
    return Observed[float](
        value=float(value),
        origin=origin,
        confidence=1.0,
        evidence=evidence,
        observed_at=observed_at,
    )


def _area_field(
    value: float | None,
    *,
    observed_at: datetime,
    unit: str,
    origin: Origin,
) -> Observed[Area] | Absence:
    if value is None:
        return Absence.NOT_STATED
    return Observed[Area](
        value=Area(value=float(value), unit=unit),
        origin=origin,
        confidence=1.0,
        evidence="craigslist area",
        observed_at=observed_at,
    )


def _int_field(
    value: int | None,
    *,
    observed_at: datetime,
    origin: Origin,
    evidence: str,
) -> Observed[int] | Absence:
    if value is None:
        return Absence.NOT_STATED
    return Observed[int](
        value=value,
        origin=origin,
        confidence=0.8,
        evidence=evidence,
        observed_at=observed_at,
    )


def _string_field(
    value: str,
    *,
    observed_at: datetime,
    origin: Origin,
    evidence: str,
) -> Observed[str] | Absence:
    if not value:
        return Absence.NOT_STATED
    return _text_observed(value, observed_at, origin, evidence)


def _text_observed(
    value: str,
    observed_at: datetime,
    origin: Origin,
    evidence: str,
) -> Observed[str]:
    return Observed[str](
        value=value,
        origin=origin,
        confidence=1.0,
        evidence=evidence,
        observed_at=observed_at,
    )


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("CraigslistSource payload must be a mapping")
    return payload


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_scope(ctx: SearchContext) -> tuple[str, tuple[str, ...]]:
    source_cfg = ctx.citypack.sources.get(CL_SOURCE_NAME)
    extras = source_cfg.model_extra if source_cfg is not None and source_cfg.model_extra else {}

    base_url = _coerce_str(extras.get("base_url")) or CL_BASE_DEFAULT
    base_url = base_url.rstrip("/")

    configured_areas = extras.get("areas")
    areas: tuple[str, ...]
    if isinstance(configured_areas, Sequence) and not isinstance(configured_areas, (str, bytes)):
        normalized = tuple(_coerce_str(item) for item in configured_areas if _coerce_str(item))
        areas = normalized or CL_AREAS_DEFAULT
    else:
        areas = CL_AREAS_DEFAULT

    return base_url, areas


def _discover_query(ctx: SearchContext) -> dict[str, str]:
    query: dict[str, str] = {"format": "rss", "hasImage": "1"}

    beds_filter = ctx.profile.hard.beds
    if beds_filter is not None and beds_filter.eq is not None:
        bedrooms = int(beds_filter.eq)
        query["min_bedrooms"] = str(bedrooms)
        query["max_bedrooms"] = str(bedrooms)

    area_filter = ctx.profile.hard.area
    if area_filter is not None:
        query["minSqft"] = str(int(area_filter.min))

    rent_filter = ctx.profile.hard.rent
    if rent_filter is not None:
        query["max_price"] = str(int(rent_filter.max))

    return query


def _infer_area_key(
    *,
    payload: Mapping[str, Any],
    raw_address: str,
    title: str,
    ctx: SearchContext,
) -> str | None:
    haystack = " ".join(
        part
        for part in (
            title.lower(),
            _coerce_str(payload.get("location")).lower(),
            raw_address.lower(),
        )
        if part
    )
    if not haystack:
        return None
    for area in ctx.citypack.areas:
        if any(keyword.lower() in haystack for keyword in area.keywords):
            return area.key
    return None


def _content_hash(payload: Mapping[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _http_fetch_text(url: str) -> str:
    response = httpx.get(
        url,
        headers={"User-Agent": CL_USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"},
        timeout=30.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _utc_now() -> datetime:
    return datetime.now(UTC)

