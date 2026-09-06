from __future__ import annotations

from typing import Any

import httpx

from nostos.web import research


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "elements": [
                {"type": "node", "id": 1, "lat": 43.65, "lon": -79.39,
                 "tags": {"name": "Neighbourhood Fitness", "leisure": "fitness_centre"}},
                {"type": "node", "id": 2, "lat": 43.651, "lon": -79.391,
                 "tags": {"name": "Fresh Market", "shop": "supermarket"}},
                {"type": "way", "id": 3, "center": {"lat": 43.652, "lon": -79.392},
                 "tags": {"name": "Local Park", "leisure": "park"}},
            ]
        }


def test_nearby_places_groups_and_distances_results(monkeypatch: Any) -> None:
    research.nearby_places.cache_clear()
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _Response())

    result = research.nearby_places(43.649, -79.389)

    assert result["status"] == "ok"
    groups = result["groups"]
    assert groups["gyms"][0]["name"] == "Neighbourhood Fitness"
    assert groups["groceries"][0]["name"] == "Fresh Market"
    assert groups["parks"][0]["url"] == "https://www.openstreetmap.org/way/3"
    assert groups["gyms"][0]["distance_km"] > 0
