"""Source coordinates and optional cached, keyless walking routes."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from selectolax.parser import HTMLParser

from nostos.config.profile import Landmark
from nostos.model import LatLng, Listing


def point_from_html(html: str) -> dict[str, float] | None:
    tree = HTMLParser(html)
    for script_node in tree.css('script[type="application/ld+json"]'):
        try:
            point = find_point(json.loads(script_node.text()))
        except (ValueError, TypeError):
            continue
        if point:
            return point
    meta_node = tree.css_first('meta[name="geo.position"]')
    if meta_node:
        content = meta_node.attributes.get("content") or ""
        raw = content.split(";")
        if len(raw) == 2:
            return find_point({"latitude": raw[0], "longitude": raw[1]})
    return None


def find_point(value: Any) -> dict[str, float] | None:
    if isinstance(value, dict):
        if "latitude" in value and "longitude" in value:
            try:
                point = LatLng(lat=float(value["latitude"]), lng=float(value["longitude"]))
                return point.model_dump()
            except (ValueError, TypeError):
                return None
        for child in value.values():
            found = find_point(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_point(child)
            if found:
                return found
    return None


def distance_km(listing: Listing, landmark: Landmark) -> float | None:
    point = listing.place.point
    if point is None:
        return None
    lat1, lat2 = math.radians(point.lat), math.radians(landmark.lat)
    dlat = lat2 - lat1
    dlon = math.radians(landmark.lng - point.lng)
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def directions_url(address: str, landmark: Landmark, mode: str = "walking") -> str:
    return "https://www.google.com/maps/dir/?" + urlencode({
        "api": "1", "origin": address, "destination": f"{landmark.lat},{landmark.lng}",
        "travelmode": mode,
    })


def walking_route(conn: sqlite3.Connection, listing: Listing, landmark: Landmark) -> dict[str, Any]:
    point = listing.place.point
    if point is None:
        raise ValueError(
            "No source coordinates. Use the walking/transit map link to verify the address."
        )
    coords = f"{point.lng},{point.lat};{landmark.lng},{landmark.lat}"
    key = hashlib.sha256(coords.encode()).hexdigest()
    row = conn.execute("SELECT payload FROM route_cache WHERE cache_key=?", (key,)).fetchone()
    if row:
        return dict(json.loads(row[0]))
    url = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/" + coords
    response = httpx.get(url, params={"overview": "false"}, timeout=15,
                         headers={"User-Agent": "Nostos/0.4 personal rental search"})
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("No walking route returned. Verify with the map link.")
    route = data["routes"][0]
    result = {"minutes": round(float(route["duration"]) / 60),
              "km": round(float(route["distance"]) / 1000, 2),
              "source": "OpenStreetMap / FOSSGIS walking router",
              "note": "Estimated route from source map pin; verify the exact entrance.",
              "computed_at": datetime.now(UTC).isoformat()}
    with conn:
        conn.execute("INSERT OR REPLACE INTO route_cache VALUES (?,?,?)",
                     (key, json.dumps(result), result["computed_at"]))
    return result
