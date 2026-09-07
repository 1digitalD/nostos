"""The normal detail -> correction -> reset journey crosses the real store."""

from pathlib import Path

from tests.web.test_extraction_review_workflow import (
    LISTING_ID,
    _apply,
    _client_with_listing,
    _review,
)


def _brief(body: str) -> str:
    return body.split('aria-labelledby="decision-title">', 1)[1].split("</section>", 1)[0]


def test_decision_updates_after_correction_and_reset(tmp_path: Path) -> None:
    client, _, _, _ = _client_with_listing(
        tmp_path,
        description="Bright two bedroom apartment.",
        hard={"rent": {"max": 3200, "currency": "CAD"}, "beds": {"eq": 2}, "exclude": []},
    )
    with client:
        _, token = _review(client)
        _apply(client, token)
        initial = client.get(f"/listings/{LISTING_ID}")
        assert initial.status_code == 200
        assert "What to confirm" in _brief(initial.text)
        assert 'href="#correct-facts"' in _brief(initial.text)
        corrected = client.post(
            f"/listings/{LISTING_ID}/correct", data={"field": "rent", "value": "5000"}
        )
        assert corrected.status_code == 200
        assert "meet your criteria" in _brief(corrected.text)
        assert "$5,000" in _brief(corrected.text)
        reloaded = client.get(f"/listings/{LISTING_ID}")
        assert "meet your criteria" in _brief(reloaded.text)
        reset = client.post(f"/listings/{LISTING_ID}/corrections/reset", data={"field": "rent"})
        assert reset.status_code == 200
        assert "$5,000" not in _brief(reset.text)
        invalid = client.post(
            f"/listings/{LISTING_ID}/correct", data={"field": "rent", "value": "wrong"}
        )
        assert invalid.status_code in {200, 400}
        assert "$5,000" not in _brief(client.get(f"/listings/{LISTING_ID}").text)
        assert client.get("/listings/missing").status_code == 404


def test_exclusion_facts_can_be_corrected_and_reset(tmp_path: Path) -> None:
    client, _, _, _ = _client_with_listing(
        tmp_path,
        description="Bright apartment.",
        hard={"exclude": ["basement", "furnished_only"]},
    )
    with client:
        _, token = _review(client)
        _apply(client, token)
        for field, value in [("basement", "no"), ("furnishing", "optional")]:
            response = client.post(
                f"/listings/{LISTING_ID}/correct", data={"field": field, "value": value}
            )
            assert response.status_code == 200
            assert "Correction applied" in response.text
        response = client.post(
            f"/listings/{LISTING_ID}/correct", data={"field": "basement", "value": "yes"}
        )
        assert "basement unit" in _brief(response.text).lower()
        response = client.post(
            f"/listings/{LISTING_ID}/corrections/reset", data={"field": "attributes.basement"}
        )
        assert response.status_code == 200
        assert "Outside your requirements" not in _brief(response.text)


def test_conflict_correction_survives_reload_and_reextract_then_reset(tmp_path: Path) -> None:
    client, _, _, _ = _client_with_listing(
        tmp_path,
        description="1-Bedroom + Second Room/Home Office",
        hard={"beds": {"eq": 2}, "exclude": []},
    )
    with client:
        _, token = _review(client)
        _apply(client, token)
        body = client.get(f"/listings/{LISTING_ID}").text
        assert "Bedroom claims differ" in _brief(body)
        assert "1-Bedroom" in _brief(body)
        assert 'class="status-badge status-unverified"' in body
        corrected = client.post(
            f"/listings/{LISTING_ID}/correct", data={"field": "beds", "value": "2"}
        )
        assert corrected.status_code == 200
        _, token = _review(client)
        _apply(client, token)
        assert "Bedroom claims differ" not in _brief(client.get(f"/listings/{LISTING_ID}").text)
        reset = client.post(f"/listings/{LISTING_ID}/corrections/reset", data={"field": "beds"})
        assert "Bedroom claims differ" in _brief(reset.text)


def test_furnishing_correction_changes_fact_through_reextract_and_reset(tmp_path: Path) -> None:
    client, _, _, _ = _client_with_listing(
        tmp_path,
        description="Furnished apartment.",
        hard={"exclude": ["furnished_only"]},
    )
    with client:
        _, token = _review(client)
        _apply(client, token)
        assert "furnished only" in _brief(client.get(f"/listings/{LISTING_ID}").text)
        corrected = client.post(
            f"/listings/{LISTING_ID}/correct", data={"field": "furnishing", "value": "unfurnished"}
        )
        assert "furnished only" not in _brief(corrected.text)
        _, token = _review(client)
        _apply(client, token)
        assert "furnished only" not in _brief(client.get(f"/listings/{LISTING_ID}").text)
        reset = client.post(
            f"/listings/{LISTING_ID}/corrections/reset", data={"field": "furnishing"}
        )
        assert "furnished only" in _brief(reset.text)
