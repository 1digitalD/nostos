from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nostos.enrich.floorplan import analyze_gallery
from nostos.store.db import connect
from nostos.web.app import create_app
from tests.web.test_profile_page import _seed_listing, _seed_profile_and_citypack

PHOTO = "https://images.craigslist.org/test-plan.jpg"


def test_floorplan_scan_confirm_correction_and_stale_gallery(tmp_path: Path) -> None:
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (600, 600), "white").save(image, format="PNG")

    def analyzer(urls: list[str], selected_url: str | None) -> dict[str, Any]:
        return analyze_gallery(
            urls, selected_url=selected_url,
            fetcher=lambda url: image.getvalue(),
            ocr=lambda image_bytes: {"engine": "fixture", "lines": [
                {"text": "FLOOR PLAN", "confidence": 0.99},
                {"text": "BEDROOM 10 ft x 12 ft", "confidence": 0.98},
                {"text": "KITCHEN 8 ft x 9 ft", "confidence": 0.98},
                {"text": "Total area 800 sq ft", "confidence": 0.98},
            ]},
        )

    db, profile, pack = _seed_profile_and_citypack(tmp_path)
    client = TestClient(create_app(db_path=db, profile_path=profile, citypack_path=pack,
                                   floorplan_analyzer=analyzer))
    _seed_listing(db, profile.stem, "craigslist:test")
    with connect(db) as conn:
        source = conn.execute("SELECT id,payload FROM source_record LIMIT 1").fetchone()
        payload = json.loads(source["payload"])
        payload["photos"] = [PHOTO]
        conn.execute("UPDATE source_record SET payload=? WHERE id=?",
                     (json.dumps(payload), source["id"]))
        conn.commit()
        before = [tuple(r) for r in conn.execute("SELECT fields_json FROM listing")]
    url = "/listings/craigslist:test"
    assert "Scan photos for a floor plan" in client.get(url).text
    result = client.post(url + "/floor-plan")
    assert result.status_code == 200, result.text
    page = client.get(url)
    assert "Possible floor plan" in page.text
    assert "10 ft x 12 ft" in page.text
    from nostos.enrich.floorplan import get_floorplan_state
    with connect(db) as conn:
        saved = get_floorplan_state(conn, listing_id="craigslist:test")
        analysis = saved["analysis"]
        assert analysis is not None
    data = {"gallery_hash": analysis["gallery_hash"],
            "selected_source_hash": analysis["photos"][0]["source_hash"],
            "decision": "confirmed", "note": "Bedroom label corrected to 11 ft by 12 ft."}
    confirmed = client.post(url + "/floor-plan/decision", data=data)
    assert confirmed.status_code == 200, confirmed.text
    assert "You confirmed this image" in confirmed.text
    assert "Bedroom label corrected" in confirmed.text
    assert client.post(url + "/floor-plan").status_code == 200
    assert "Bedroom label corrected" in client.get(url).text
    with connect(db) as conn:
        assert [tuple(r) for r in conn.execute("SELECT fields_json FROM listing")] == before
        payload["photos"] = ["https://images.craigslist.org/changed-plan.jpg"]
        conn.execute("UPDATE source_record SET payload=? WHERE id=?",
                     (json.dumps(payload), source["id"]))
        conn.commit()
    assert "listing gallery changed" in client.get(url).text
    assert client.post(url + "/floor-plan/decision", data=data).status_code == 409
    invalid = client.post(url + "/floor-plan", data={"selected_url": "http://localhost/"})
    assert invalid.status_code == 422
