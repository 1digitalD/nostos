from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlparse

import extruct
from selectolax.parser import HTMLParser

from nostos.context import SearchContext
from nostos.enrich.location import point_from_html
from nostos.enrich.text import (
    furnishing_from_text,
    infer_area_key_from_neighborhood_text,
    neighborhood_haystack,
    parking_from_text,
)
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
from nostos.sources.http import RobotsDisallowedError, build_fetch_text

FetchText = Callable[[str], str]
NowProvider = Callable[[], datetime]

_KIJIJI_ID_RE = re.compile(r"/(\d{9,10})(?:$|\?|#)")
_ROOM_ONLY_RE = re.compile(
    r"\b(room for rent|shared (?:home|house|apartment|unit)|roommate)\b",
    flags=re.IGNORECASE,
)
_REQUEST_HEADERS = {"User-Agent": "nostos/0.1", "Accept-Language": "en-CA,en;q=0.9"}
DETAIL_TEXT_MAX_BYTES = 100_000
DETAIL_PHOTO_MAX = 50


class KijijiSource:
    name = "kijiji"
    capabilities = Capabilities(
        requires_credentials=False,
        supports_detail_fetch=True,
        requires_browser=False,
        rate_limit_per_minute=45.0,
    )

    def __init__(
        self,
        *,
        fetcher: FetchText | None = None,
        now_provider: NowProvider | None = None,
    ) -> None:
        self._fetcher = fetcher or build_fetch_text(
            self.capabilities,
            user_agent=_REQUEST_HEADERS["User-Agent"],
            headers=_REQUEST_HEADERS,
            timeout=20.0,
        )
        self._now_provider = now_provider or _utc_now

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        discovered: dict[str, SourceRecord] = {}
        for search_url in _discover_urls(ctx):
            try:
                html = self._fetcher(search_url)
            except RobotsDisallowedError:
                continue
            fetched_at = self._now_provider()
            item_list = _extract_item_list(html=html, base_url=search_url)
            for record in _records_from_item_list(
                item_list=item_list,
                fetched_at=fetched_at,
            ):
                discovered[record.source_id] = record
        return iter(discovered.values())

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        fetched_at = self._now_provider()
        existing_payload = _mapping_payload(rec.payload)
        merged_payload = dict(existing_payload)

        try:
            html = self._fetcher(rec.url)
        except RobotsDisallowedError:
            merged_payload["detail_status"] = "blocked"
            merged_payload["detail_error"] = "source robots policy blocked the detail request"
        except Exception as exc:
            merged_payload["detail_status"] = "failed"
            merged_payload["detail_error"] = _sanitized_fetch_error(exc)
        else:
            page_outcome = _detail_page_outcome(html)
            if page_outcome is not None:
                status, error = page_outcome
                merged_payload["detail_status"] = status
                merged_payload["detail_error"] = error
            else:
                try:
                    detail_payload = _detail_payload(html=html, base_url=rec.url)
                except Exception as exc:
                    merged_payload["detail_status"] = "failed"
                    merged_payload["detail_error"] = _sanitized_parse_error(exc)
                else:
                    canonical_url = _as_text(detail_payload.get("canonical_url"))
                    if not _detail_identity_matches(rec, canonical_url):
                        merged_payload["detail_status"] = "failed"
                        merged_payload["detail_error"] = (
                            "detail page identity did not match the requested listing"
                        )
                    elif not _has_listing_detail(detail_payload):
                        merged_payload["detail_status"] = "failed"
                        merged_payload["detail_error"] = (
                            "detail page did not contain recognizable listing content"
                        )
                    else:
                        point = point_from_html(html)
                        if point:
                            detail_payload["point"] = point
                        merged_payload.update(
                            {
                                key: value
                                for key, value in detail_payload.items()
                                if _has_detail_value(value)
                            }
                        )
                        partial = bool(
                            not canonical_url
                            or merged_payload.get("description_truncated")
                            or merged_payload.get("photos_truncated")
                        )
                        merged_payload["detail_status"] = (
                            "partial" if partial else "complete"
                        )
                        if not canonical_url:
                            merged_payload["detail_error"] = (
                                "canonical URL unavailable; source identity was not "
                                "independently verified"
                            )
                        elif partial:
                            merged_payload["detail_error"] = (
                                "detail content was truncated at storage limits"
                            )
                        else:
                            merged_payload.pop("detail_error", None)

        return SourceRecord(
            source=rec.source,
            source_id=rec.source_id,
            url=rec.url,
            content_hash=_content_hash(rec.source_id, rec.url, merged_payload),
            fetched_at=fetched_at,
            payload=merged_payload,
        )

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        payload = _mapping_payload(rec.payload)
        if bool(payload.get("removed")):
            return Liveness.FAILED
        detail_status = _as_text(payload.get("detail_status"))
        if detail_status == "removed":
            return Liveness.FAILED
        if detail_status in {"blocked", "failed"}:
            return Liveness.DEGRADED
        if _as_text(payload.get("title")):
            return Liveness.OK
        return Liveness.DEGRADED

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        payload = _mapping_payload(rec.payload)
        observed_at = rec.fetched_at
        title = _clean_listing_title(_as_text(payload.get("title")) or "")
        description = _as_text(payload.get("description"))
        address = _as_text(payload.get("address"))
        price = _as_int(payload.get("price"))
        area_key = _infer_area_key(payload, ctx)
        parking_fact = parking_from_text(
            " ".join(part for part in (title, description) if part)
        )
        furnishing_fact = furnishing_from_text(
            " ".join(part for part in (title, description) if part)
        )

        place = Place.model_validate(
            {
                "raw_address": address,
                "structured": None,
                "point": payload.get("point"),
                "area_key": area_key,
            },
            context={"area_vocabulary": ctx.area_vocabulary},
        )
        attributes: dict[str, Observed[str] | Absence] = {}
        if title:
            attributes["title"] = Observed[str](
                value=title,
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="kijiji title",
                observed_at=observed_at,
            )
        if description:
            attributes["description"] = Observed[str](
                value=description,
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="kijiji description",
                observed_at=observed_at,
            )
        structured_location = _as_text(payload.get("structuredLocation")) or _as_text(
            payload.get("structured_location")
        )
        if structured_location:
            attributes["structuredLocation"] = Observed[str](
                value=structured_location,
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="kijiji structured location",
                observed_at=observed_at,
            )
        photos = _photo_list(payload.get("photos"))

        return Listing(
            identity=Identity(
                listing_id=f"{self.name}:{rec.source_id}",
                source=self.name,
                source_id=rec.source_id,
                url=rec.url,
                signature=Identity.compute_signature(title=title, price=price, address=address),
            ),
            place=place,
            rent=_money_field(
                amount=price,
                currency=ctx.citypack.locale.currency,
                observed_at=observed_at,
            ),
            beds=_float_field(
                value=payload.get("beds"),
                observed_at=observed_at,
                evidence="kijiji beds",
            ),
            baths=_float_field(
                value=payload.get("baths"),
                observed_at=observed_at,
                evidence="kijiji baths",
            ),
            area=_area_field(
                value=payload.get("area_sqft"),
                unit=ctx.citypack.locale.area_unit,
                observed_at=observed_at,
            ),
            floor=Absence.NOT_STATED,
            parking=_parking_field(parking_fact, observed_at),
            furnishing=_furnishing_field(furnishing_fact, observed_at),
            photos=photos,
            attributes=attributes,
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def _discover_urls(ctx: SearchContext) -> tuple[str, ...]:
    cfg = ctx.citypack.sources.get("kijiji")
    if cfg is None:
        return ()
    raw_regions = (cfg.model_extra or {}).get("regions")
    if not isinstance(raw_regions, Sequence):
        return ()

    urls: list[str] = []
    for raw_region in raw_regions:
        region = _mapping_or_none(raw_region)
        if region is None:
            continue
        region_path = _as_text(region.get("path"))
        region_id = _as_text(region.get("id"))
        if not region_path or not region_id:
            continue

        raw_keywords = region.get("keywords")
        keywords = _keyword_list(raw_keywords)
        if not keywords:
            keywords = ("apartments",)
        for keyword in keywords:
            urls.append(
                _search_url(
                    region_path=region_path,
                    region_id=region_id,
                    keyword=keyword,
                )
            )
    return tuple(urls)


def _search_url(*, region_path: str, region_id: str, keyword: str) -> str:
    keyword_slug = keyword.strip().lower().replace(" ", "-")
    region_suffix = region_id[4:] if region_id.startswith("c37l") else region_id
    return f"https://www.kijiji.ca/b-apartments-condos/{region_path}/{keyword_slug}/k0c37l{region_suffix}"


def _extract_item_list(*, html: str, base_url: str) -> Mapping[str, object]:
    extracted = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
    raw_jsonld = extracted.get("json-ld")
    if not isinstance(raw_jsonld, Sequence):
        raise ValueError("Kijiji discovery payload did not contain json-ld data")

    for candidate in _iter_jsonld_nodes(raw_jsonld):
        if _is_type(candidate.get("@type"), "ItemList"):
            return candidate
    raise ValueError("Kijiji ItemList JSON-LD missing")


def _records_from_item_list(
    *,
    item_list: Mapping[str, object],
    fetched_at: datetime,
) -> Iterator[SourceRecord]:
    raw_elements = item_list.get("itemListElement")
    if not isinstance(raw_elements, Sequence):
        return iter(())

    records: list[SourceRecord] = []
    for raw_element in raw_elements:
        element = _mapping_or_none(raw_element)
        if element is None:
            continue
        item = _mapping_or_none(element.get("item"))
        if item is None:
            continue
        url = _as_text(item.get("url"))
        if not url:
            continue
        match = _KIJIJI_ID_RE.search(url)
        if match is None:
            continue
        source_id = match.group(1)
        payload = _item_payload(item)
        records.append(
            SourceRecord(
                source="kijiji",
                source_id=source_id,
                url=url,
                content_hash=_content_hash(source_id, url, payload),
                fetched_at=fetched_at,
                payload=payload,
            )
        )
    return iter(records)


def _item_payload(item: Mapping[str, object]) -> dict[str, object]:
    title = _to_plain_text(item.get("name"))
    raw_description = _to_plain_text(item.get("description"))
    description, description_truncated = _bounded_utf8_text(raw_description)
    address = _address_text(item.get("address"))
    haystack = " ".join(token for token in (title, description, address) if token)
    offers = _mapping_or_none(item.get("offers"))
    floor_size = _mapping_or_none(item.get("floorSize"))
    all_images = _image_urls(item.get("image"))
    images = all_images[:DETAIL_PHOTO_MAX]
    parking_fact = parking_from_text(haystack)

    furnishing_fact = furnishing_from_text(haystack)
    furnishing = furnishing_fact[0].lower() if furnishing_fact is not None else None

    return {
        "title": title,
        "description": description,
        "description_truncated": description_truncated,
        "address": address,
        "price": _as_int(offers.get("price") if offers else None),
        "beds": _as_float(item.get("numberOfBedrooms")),
        "baths": _as_float(item.get("numberOfBathroomsTotal")),
        "area_sqft": _as_int(floor_size.get("value") if floor_size else None),
        "photos": images,
        "photos_truncated": len(all_images) > DETAIL_PHOTO_MAX,
        "parking": parking_fact[0] if parking_fact is not None else None,
        "furnishing": furnishing,
        "full_unit": not bool(_ROOM_ONLY_RE.search(haystack)) if haystack else None,
    }


def _detail_payload(*, html: str, base_url: str) -> dict[str, object]:
    extracted = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
    raw_jsonld = extracted.get("json-ld")
    nodes = raw_jsonld if isinstance(raw_jsonld, Sequence) else ()
    listing_node = _select_detail_node(nodes)

    detail_payload: dict[str, object] = {}
    if listing_node is not None:
        detail_payload = _item_payload(listing_node)

    parser = HTMLParser(html)
    canonical_url = _link_href(parser, rel="canonical")
    og_title = _meta_content(parser, property_name="og:title")
    og_image = _meta_content(parser, property_name="og:image")
    og_url = _meta_content(parser, property_name="og:url")

    if og_title:
        detail_payload["title"] = og_title
    if og_image and not _image_urls(detail_payload.get("photos")):
        detail_payload["photos"] = [og_image]
    listing_url = _as_text(listing_node.get("url")) if listing_node is not None else None
    resolved_url = canonical_url or og_url or listing_url
    if resolved_url:
        detail_payload["canonical_url"] = resolved_url
    return detail_payload


def _select_detail_node(nodes: Sequence[object]) -> Mapping[str, object] | None:
    for node in _iter_jsonld_nodes(nodes):
        if _is_type(node.get("@type"), "ItemList"):
            continue
        if _as_text(node.get("offers")) is not None:
            return node
        if _as_text(node.get("numberOfBedrooms")) is not None:
            return node
        if _mapping_or_none(node.get("address")) is not None:
            return node
    return None


def _iter_jsonld_nodes(values: Sequence[object]) -> Iterator[Mapping[str, object]]:
    for value in values:
        mapping = _mapping_or_none(value)
        if mapping is None:
            continue
        yield mapping
        graph = mapping.get("@graph")
        if isinstance(graph, Sequence):
            for graph_node in graph:
                graph_mapping = _mapping_or_none(graph_node)
                if graph_mapping is not None:
                    yield graph_mapping


def _is_type(raw_type: object, expected: str) -> bool:
    if isinstance(raw_type, str):
        return raw_type == expected
    if isinstance(raw_type, Sequence):
        return expected in raw_type
    return False


def _meta_content(parser: HTMLParser, *, property_name: str) -> str | None:
    selector = f'meta[property="{property_name}"]'
    node = parser.css_first(selector)
    if node is None:
        return None
    value = node.attributes.get("content")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _link_href(parser: HTMLParser, *, rel: str) -> str | None:
    node = parser.css_first(f'link[rel="{rel}"]')
    if node is None:
        return None
    value = node.attributes.get("href")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _address_text(raw_value: object) -> str | None:
    mapping = _mapping_or_none(raw_value)
    if mapping is not None:
        parts = (
            _as_text(mapping.get("streetAddress")),
            _as_text(mapping.get("addressLocality")),
            _as_text(mapping.get("addressRegion")),
        )
        joined = " ".join(part for part in parts if part)
        return joined or None
    return _as_text(raw_value)


def _image_urls(raw_value: object) -> list[str]:
    candidates: list[str] = []
    if isinstance(raw_value, str):
        candidates.append(raw_value)
    elif isinstance(raw_value, Mapping):
        preferred = _as_text(raw_value.get("contentUrl")) or _as_text(raw_value.get("url"))
        if preferred:
            candidates.append(preferred)
    elif isinstance(raw_value, Sequence):
        for item in raw_value:
            candidates.extend(_image_urls(item))

    deduped: dict[str, None] = {}
    for candidate in candidates:
        normalized = candidate.strip()
        if normalized and _is_http_url(normalized):
            deduped.setdefault(normalized, None)
    return list(deduped)


def _bounded_utf8_text(text: str | None) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    encoded = text.encode("utf-8")
    if len(encoded) <= DETAIL_TEXT_MAX_BYTES:
        return text, False
    bounded = encoded[:DETAIL_TEXT_MAX_BYTES].decode("utf-8", errors="ignore")
    return bounded, True


def _detail_identity_matches(rec: SourceRecord, canonical_url: str | None) -> bool:
    requested_match = _KIJIJI_ID_RE.search(rec.url)
    if rec.source != "kijiji" or not requested_match or requested_match.group(1) != rec.source_id:
        return False
    if not canonical_url:
        return True
    hostname = urlparse(canonical_url).hostname
    if not hostname or not hostname.endswith("kijiji.ca"):
        return False
    match = _KIJIJI_ID_RE.search(canonical_url)
    return bool(match and match.group(1) == rec.source_id)


def _detail_page_outcome(html: str) -> tuple[str, str] | None:
    normalized = _visible_page_text(html)
    if any(
        marker in normalized
        for marker in (
            "this ad is no longer available",
            "the ad you are looking for is no longer available",
            "this listing is no longer available",
        )
    ):
        return "removed", "listing is no longer available"
    if any(
        marker in normalized
        for marker in (
            "verify you are human",
            "pardon our interruption",
            "access denied",
            "captcha",
        )
    ):
        return "blocked", "source challenge blocked the detail request"
    return None


def _visible_page_text(html: str) -> str:
    parser = HTMLParser(html)
    for hidden in parser.css("script, style, noscript"):
        hidden.decompose()
    body = parser.body
    if body is None:
        return ""
    return " ".join(body.text(separator=" ", strip=True).lower().split())


def _has_detail_value(value: object) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True


def _has_listing_detail(detail: Mapping[str, object]) -> bool:
    return any(
        _has_detail_value(detail.get(key))
        for key in ("description", "photos", "price", "address", "beds", "baths", "area_sqft")
    )


def _sanitized_fetch_error(exc: Exception) -> str:
    return f"detail fetch failed ({type(exc).__name__})"


def _sanitized_parse_error(exc: Exception) -> str:
    return f"detail parse failed ({type(exc).__name__})"


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_listing_title(title: str) -> str:
    """Remove Kijiji page-title chrome from a listing's actual title."""

    cleaned = title.strip()
    for marker in (
        " | Long Term Rentals |",
        " | Apartments & Condos for Rent |",
        " | Real Estate |",
    ):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0].strip()
            break
    if cleaned.endswith(" - Kijiji"):
        cleaned = cleaned.removesuffix(" - Kijiji").strip()
    return cleaned


def _photo_list(raw_value: object) -> list[Photo]:
    return [Photo(url=url) for url in _image_urls(raw_value)]


def _infer_area_key(payload: Mapping[str, object], ctx: SearchContext) -> str | None:
    search_blob = neighborhood_haystack(
        _as_text(payload.get("address")),
        _as_text(payload.get("title")),
        _as_text(payload.get("structuredLocation")),
        _as_text(payload.get("structured_location")),
    )
    if not search_blob:
        return None
    return infer_area_key_from_neighborhood_text(search_blob, ctx)


def _money_field(
    *,
    amount: int | None,
    currency: str,
    observed_at: datetime,
) -> Observed[Money] | Absence:
    if amount is None:
        return Absence.NOT_STATED
    return Observed[Money](
        value=Money(amount=Decimal(str(amount)), currency=currency, period="month"),
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence="kijiji price",
        observed_at=observed_at,
    )


def _float_field(
    *,
    value: object,
    observed_at: datetime,
    evidence: str,
) -> Observed[float] | Absence:
    numeric = _as_float(value)
    if numeric is None:
        return Absence.NOT_STATED
    return Observed[float](
        value=numeric,
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence=evidence,
        observed_at=observed_at,
    )


def _area_field(*, value: object, unit: str, observed_at: datetime) -> Observed[Area] | Absence:
    numeric = _as_float(value)
    if numeric is None:
        return Absence.NOT_STATED
    return Observed[Area](
        value=Area(value=numeric, unit=unit),
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence="kijiji floor size",
        observed_at=observed_at,
    )


def _parking_field(
    fact: tuple[str, str] | None,
    observed_at: datetime,
) -> Observed[str] | Absence:
    if fact is None:
        return Absence.NOT_STATED
    value, evidence = fact
    return Observed[str](
        value=value,
        origin=Origin.TEXT_RULE,
        confidence=0.8,
        evidence=evidence,
        observed_at=observed_at,
    )


def _furnishing_field(
    fact: tuple[str, str] | None,
    observed_at: datetime,
) -> Observed[str] | Absence:
    if fact is None:
        return Absence.NOT_STATED
    value, evidence = fact
    return Observed[str](
        value=value.lower(),
        origin=Origin.TEXT_RULE,
        confidence=0.8,
        evidence=evidence,
        observed_at=observed_at,
    )


def _keyword_list(raw_value: object) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        keyword = raw_value.strip()
        return (keyword,) if keyword else ()
    if isinstance(raw_value, Sequence):
        keywords: list[str] = []
        for entry in raw_value:
            if isinstance(entry, str) and entry.strip():
                keywords.append(entry.strip())
        return tuple(keywords)
    return ()


def _mapping_payload(payload: object) -> Mapping[str, object]:
    mapping = _mapping_or_none(payload)
    if mapping is None:
        raise ValueError("SourceRecord.payload must be a mapping for KijijiSource")
    return mapping


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, object], value)


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _to_plain_text(value: object) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    node = HTMLParser(f"<div>{text}</div>").css_first("div")
    if node is None:
        return text
    cleaned = node.text(separator=" ", strip=True)
    return cleaned if cleaned else None


def _as_int(value: object) -> int | None:
    numeric = _as_float(value)
    if numeric is None:
        return None
    return int(numeric)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = _as_text(value)
    if text is None:
        return None
    normalized = text.replace(",", "")
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", normalized)
    if len(numeric_tokens) != 1:
        return None
    try:
        return float(numeric_tokens[0])
    except ValueError:
        return None


def _content_hash(source_id: str, url: str, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"id": source_id, "url": url, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
