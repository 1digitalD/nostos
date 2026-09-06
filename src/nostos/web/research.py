"""Address-centred research helpers for the listing research workspace."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, cast

import httpx

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_RADIUS_METRES = 1500


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlng = math.radians(lng2 - lng1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1, math.sqrt(value)))


def _category(tags: dict[str, Any]) -> str | None:
    leisure = str(tags.get("leisure", ""))
    shop = str(tags.get("shop", ""))
    amenity = str(tags.get("amenity", ""))
    public_transport = str(tags.get("public_transport", ""))
    railway = str(tags.get("railway", ""))
    if leisure == "fitness_centre" or amenity == "gym":
        return "gyms"
    if shop in {"supermarket", "greengrocer"}:
        return "groceries"
    if leisure == "park":
        return "parks"
    if shop in {"mall", "department_store"}:
        return "shopping"
    if amenity in {"pharmacy", "clinic", "hospital", "doctors"}:
        return "services"
    if public_transport in {"station", "stop_position", "platform"} or railway in {
        "station",
        "subway_entrance",
        "tram_stop",
    }:
        return "transit"
    return None


@lru_cache(maxsize=256)
def nearby_places(lat: float, lng: float) -> dict[str, Any]:
    """Return named OpenStreetMap places within a walkable radius.

    Results are cached for the lifetime of the web process so revisiting a
    listing does not repeatedly call the public Overpass service.
    """

    query = f"""
    [out:json][timeout:12];
    (
      nwr(around:{_RADIUS_METRES},{lat},{lng})[leisure~"^(fitness_centre|park)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[shop~"^(supermarket|greengrocer|mall|department_store)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[amenity~"^(gym|pharmacy|clinic|hospital|doctors)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[public_transport~"^(station|stop_position|platform)$"];
      nwr(around:{_RADIUS_METRES},{lat},{lng})[railway~"^(station|subway_entrance|tram_stop)$"];
    );
    out center tags;
    """
    response = httpx.post(
        _OVERPASS_URL,
        content=query,
        timeout=15,
        headers={"User-Agent": "Nostos/0.4 personal rental research"},
    )
    response.raise_for_status()
    payload = response.json()
    groups: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("gyms", "groceries", "parks", "shopping", "services", "transit")
    }
    seen: set[tuple[str, str]] = set()
    for element in payload.get("elements", []):
        if not isinstance(element, dict):
            continue
        element_data = cast(dict[str, Any], element)
        tags = element_data.get("tags")
        if not isinstance(tags, dict):
            continue
        category = _category(tags)
        name = str(tags.get("name") or tags.get("brand") or tags.get("operator") or "").strip()
        raw_center = element_data.get("center")
        center = cast(dict[str, Any], raw_center) if isinstance(raw_center, dict) else element_data
        try:
            place_lat, place_lng = float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if category is None or not name or (category, name.casefold()) in seen:
            continue
        seen.add((category, name.casefold()))
        element_type = str(element_data.get("type", "node"))
        element_id = element_data.get("id")
        groups[category].append(
            {
                "name": name,
                "distance_km": round(_distance_km(lat, lng, place_lat, place_lng), 2),
                "url": f"https://www.openstreetmap.org/{element_type}/{element_id}",
            }
        )
    for places in groups.values():
        places.sort(key=lambda item: float(item["distance_km"]))
        del places[5:]
    return {
        "status": "ok",
        "radius_m": _RADIUS_METRES,
        "source": "OpenStreetMap contributors",
        "checked_at": datetime.now(UTC).isoformat(),
        "groups": groups,
    }
