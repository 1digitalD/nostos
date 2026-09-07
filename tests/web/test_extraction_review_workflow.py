"""End-to-end coverage for reviewing extracted listing details.

The tests deliberately start with a saved source record and use only the web
workflow to request a preview and apply it.  SQLite assertions are limited to
the durable result of that public operation (facts, score, and user state),
while rendered assertions exercise the same detail page a renter sees.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nostos.model.source_record import SourceRecord
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo, ScoreRepo
from nostos.web import create_app
from tests.web.test_profile_page import _seed_profile_and_citypack

LISTING_ID = "craigslist:extraction-review-1"


def _workspace(
    tmp_path: Path,
    *,
    hard: dict[str, object] | None = None,
) -> tuple[TestClient, Path, Path, str]:
    db_path, profile_path, citypack_path = _seed_profile_and_citypack(tmp_path)
    if hard is not None:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["hard"] = hard
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with connect(db_path) as conn:
        apply_migrations(conn)
    app = create_app(
        db_path=db_path,
        profile_path=profile_path,
        citypack_path=citypack_path,
    )
    return TestClient(app), db_path, profile_path, profile_path.stem


def _save_listing(
    db_path: Path,
    *,
    listing_id: str = LISTING_ID,
    description: str,
    content_hash: str = "source-v1",
    fetched_at: datetime | None = None,
    price: int = 2800,
) -> None:
    fetched_at = fetched_at or datetime(2026, 9, 1, tzinfo=UTC)
    with connect(db_path) as conn:
        ListingRepo(conn).ensure_listing(listing_id, seen_at=fetched_at)
        ListingRepo(conn).add_source_record(
            listing_id=listing_id,
            record=SourceRecord(
                source="craigslist",
                source_id=listing_id.split(":", 1)[1],
                url=f"https://example.test/{listing_id}",
                payload={
                    "title": "Sunny 2BR in Kitsilano",
                    "address": "1234 West 4th Ave, Kitsilano",
                    "price": price,
                    "beds": 2,
                    "baths": 1,
                    "sqft": 850,
                    "posted": fetched_at.isoformat(),
                    "description": description,
                },
                content_hash=content_hash,
                fetched_at=fetched_at,
            ),
        )
        conn.commit()


def _client_with_listing(
    tmp_path: Path,
    *,
    description: str,
    hard: dict[str, object] | None = None,
    price: int = 2800,
) -> tuple[TestClient, Path, Path, str]:
    client, db_path, profile_path, profile_id = _workspace(tmp_path, hard=hard)
    _save_listing(db_path, description=description, price=price)
    return client, db_path, profile_path, profile_id


def _preview_token(body: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', body)
    assert match, "review page must expose an opaque apply token"
    return match.group(1)


def _review(client: TestClient, listing_id: str = LISTING_ID) -> tuple[str, str]:
    response = client.get(f"/listings/{listing_id}/extraction-review")
    assert response.status_code == 200, response.text
    assert "Review extracted details" in response.text
    return response.text, _preview_token(response.text)


def _apply(client: TestClient, token: str, listing_id: str = LISTING_ID) -> Any:
    response = client.post(
        f"/listings/{listing_id}/extraction-review",
        data={"token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(f"/listings/{listing_id}")
    return response


def _observations(
    db_path: Path, listing_id: str = LISTING_ID
) -> dict[str, list[dict[str, object]]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT field, value_json, origin, evidence
            FROM observation
            WHERE listing_id=?
            ORDER BY id
            """,
            (listing_id,),
        ).fetchall()
    result: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        result.setdefault(str(row["field"]), []).append(
            {
                "value": json.loads(str(row["value_json"])),
                "origin": str(row["origin"]),
                "evidence": row["evidence"],
            }
        )
    return result


def test_saved_source_review_applies_facts_to_sqlite_and_detail_page(tmp_path: Path) -> None:
    client, db_path, _profile_path, profile_id = _client_with_listing(
        tmp_path,
        description=(
            "Entire apartment. 3rd floor, 850 sqft. Parking available. "
            "Washer/dryer in suite."
        ),
    )

    preview, token = _review(client)
    assert "Washer/dryer in suite" in preview
    assert "Full Unit" in preview
    applied = _apply(client, token)

    facts = _observations(db_path)
    assert facts["parking"][-1]["value"] == "Available"
    assert facts["attributes.in_suite_laundry"][-1]["value"] is True
    assert facts["floor"][-1]["value"] == 3
    with connect(db_path) as conn:
        assert ScoreRepo(conn).get_score(LISTING_ID, profile_id) is not None

    detail = client.get(applied.headers["location"])
    assert detail.status_code == 200
    assert "Extracted details applied" in detail.text
    assert re.search(r"<dt>Floor</dt><dd>3</dd>", detail.text)
    assert "Available" in detail.text
    assert "In-suite laundry" in detail.text


def test_preview_is_stale_after_correction_profile_and_source_changes(tmp_path: Path) -> None:
    client, db_path, profile_path, _profile_id = _client_with_listing(
        tmp_path,
        description="Entire apartment. Parking available.",
    )

    # A correction made after preview invalidates that preview.
    _page, token = _review(client)
    correction = client.post(
        f"/listings/{LISTING_ID}/correct",
        data={"field": "rent", "value": "2500"},
        follow_redirects=False,
    )
    assert correction.status_code == 303
    stale = client.post(
        f"/listings/{LISTING_ID}/extraction-review",
        data={"token": token},
        follow_redirects=False,
    )
    assert stale.status_code in {400, 409}
    assert "no longer current" in stale.text.lower()

    # A profile update likewise invalidates a token generated under the old profile.
    _page, token = _review(client)
    saved = json.loads(profile_path.read_text(encoding="utf-8"))
    saved["hard"]["rent"]["max"] = 3199
    profile_path.write_text(json.dumps(saved), encoding="utf-8")
    stale = client.post(
        f"/listings/{LISTING_ID}/extraction-review",
        data={"token": token},
        follow_redirects=False,
    )
    assert stale.status_code in {400, 409}
    assert "no longer current" in stale.text.lower()

    # A newer saved source record invalidates the preview even when the listing ID is stable.
    _page, token = _review(client)
    _save_listing(
        db_path,
        description="Entire apartment. No parking available.",
        content_hash="source-v2",
        fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    stale = client.post(
        f"/listings/{LISTING_ID}/extraction-review",
        data={"token": token},
        follow_redirects=False,
    )
    assert stale.status_code in {400, 409}
    assert "no longer current" in stale.text.lower()


def test_repeated_apply_is_safe_and_does_not_duplicate_facts(tmp_path: Path) -> None:
    client, db_path, _profile_path, _profile_id = _client_with_listing(
        tmp_path,
        description="Entire apartment. Parking available. Washer/dryer in suite.",
    )
    _page, token = _review(client)
    _apply(client, token)
    before = _observations(db_path)

    repeated = client.post(
        f"/listings/{LISTING_ID}/extraction-review",
        data={"token": token},
        follow_redirects=False,
    )
    assert repeated.status_code == 303, repeated.text
    after = _observations(db_path)
    for field in ("parking", "attributes.in_suite_laundry"):
        assert len(after[field]) == len(before[field])


def test_hard_filter_miss_still_persists_extracted_facts_and_score(tmp_path: Path) -> None:
    client, db_path, _profile_path, profile_id = _client_with_listing(
        tmp_path,
        description="Entire apartment. No parking available. Washer/dryer in suite.",
        hard={
            "rent": {"max": 2500, "currency": "CAD"},
            "beds": {"eq": 2},
            "require_parking": True,
            "exclude": [],
        },
    )
    _page, token = _review(client)
    _apply(client, token)

    facts = _observations(db_path)
    assert facts["parking"][-1]["value"] == "Unavailable"
    assert facts["attributes.in_suite_laundry"][-1]["value"] is True
    detail = client.get(f"/listings/{LISTING_ID}")
    assert "Miss" in detail.text
    assert "parking" in detail.text.lower()


def test_saved_source_description_is_escaped_in_rendered_html(tmp_path: Path) -> None:
    description = 'A <script>alert("xss")</script> & "quoted" listing.'
    client, _db_path, _profile_path, _profile_id = _client_with_listing(
        tmp_path,
        description=description,
    )
    detail = client.get(f"/listings/{LISTING_ID}")
    assert detail.status_code == 200
    escaped = (
        "A &lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt; &amp; "
        "&#34;quoted&#34; listing."
    )
    assert escaped in detail.text
    assert description not in detail.text


def test_corrected_availability_is_shown_on_detail_page(tmp_path: Path) -> None:
    client, _db_path, _profile_path, _profile_id = _client_with_listing(
        tmp_path,
        description="Entire apartment. Parking available.",
    )
    corrected = client.post(
        f"/listings/{LISTING_ID}/correct",
        data={"field": "available_date", "value": "2026-10-15"},
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    detail = client.get(corrected.headers["location"])
    assert detail.status_code == 200
    assert re.search(r"<dt>Available</dt><dd>2026-10-15</dd>", detail.text)
    assert "Unstated" not in detail.text.split("<dt>Available</dt>", 1)[1].split("</div>", 1)[0]
