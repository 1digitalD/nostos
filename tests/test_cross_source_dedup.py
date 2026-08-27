from __future__ import annotations

from typing import Any

from nostos.normalize.dedupe import dedupe_and_filter, make_sig


def test_cl_full_address_matches_rew_street_only() -> None:
    cl = make_sig(
        title="Vancouver West Coal Harbour 2 bed +den unfurnished apartment for rent",
        price=3300,
        address="1328 W Pender St, Vancouver, BC V6E 4T1",
    )
    rew = make_sig(
        title="2 bedrooms for $3,300/month at 1328 West Pender Street, Vancouver, BC for Rent",
        price=3300,
        address="1328 West Pender Street",
    )
    assert cl == rew
    assert cl == "1328 west pender|3300"


def test_cl_partial_postal_matches() -> None:
    cl = make_sig(
        title="x",
        price=3300,
        address="1328 W Pender St V6E 4T1",
    )
    rew = make_sig(
        title="x",
        price=3300,
        address="1328 West Pender Street",
    )
    assert cl == rew


def test_cl_no_postal_matches_and_price_bucket_boundary_is_distinct() -> None:
    cl = make_sig(
        title="x",
        price=3300,
        address="1328 W Pender St",
    )
    rew = make_sig(
        title="x",
        price=3300,
        address="1328 West Pender Street",
    )
    assert cl == rew

    # A price that straddles a bucket midpoint should not collapse.
    lower = make_sig(title="x", price=3312, address="1328 West Pender Street")
    upper = make_sig(title="x", price=3313, address="1328 West Pender Street")
    assert lower != upper


def test_cl_added_after_rew_does_not_create_new_row() -> None:
    state: dict[str, Any] = {
        "seen": {
            "7470189": {
                "url": "https://www.rew.ca/rentals/7470189",
                "title": "2 bedrooms for $3,300/month at 1328 West Pender Street",
                "price": 3300,
                "first_seen": "2026-08-12T00:00:00Z",
                "last_seen": "2026-08-12T00:00:00Z",
                "sig": "1328 west pender|3300",
                "source": "rew",
            },
        },
        "last_scans": {},
    }
    cl_cand: dict[str, Any] = {
        "id": "uXQh5yHMArQ3J9AfA8W8yV",
        "source": "craigslist",
        "url": "https://www.craigslist.org/view/d/uXQh5yHMArQ3J9AfA8W8yV",
        "title": "Vancouver West Coal Harbour 2 bed +den unfurnished apartment for rent",
        "price": 3300,
        "beds": 2,
        "baths": 2,
        "sqft": 1189,
        "address": "1328 W Pender St, Vancouver, BC V6E 4T1",
        "nb": "downtown_van",
        "photo": "https://example.com/cl.jpg",
    }
    accepted, _excluded, refreshes = dedupe_and_filter([cl_cand], state)

    assert len(accepted) == 0
    assert len(refreshes) == 0
    assert len(state["seen"]) == 1


def test_cl_already_seen_lands_in_refreshes() -> None:
    state: dict[str, Any] = {
        "seen": {
            "7470189": {
                "url": "https://www.rew.ca/rentals/7470189",
                "title": "2 bedrooms for $3,300/month at 1328 West Pender Street",
                "price": 3300,
                "first_seen": "2026-08-12T00:00:00Z",
                "last_seen": "2026-08-12T00:00:00Z",
                "sig": "1328 west pender|3300",
                "source": "rew",
            },
            "uXQh5yHMArQ3J9AfA8W8yV": {
                "url": "https://www.craigslist.org/view/d/uXQh5yHMArQ3J9AfA8W8yV",
                "title": "Vancouver West Coal Harbour 2 bed +den unfurnished apartment for rent",
                "price": 3300,
                "first_seen": "2026-08-12T00:00:00Z",
                "last_seen": "2026-08-12T00:00:00Z",
                "sig": "vancouver west coal harbour 2 bed|3300",
                "source": "craigslist",
            },
        },
        "last_scans": {},
    }
    cl_cand: dict[str, Any] = {
        "id": "uXQh5yHMArQ3J9AfA8W8yV",
        "source": "craigslist",
        "url": "https://www.craigslist.org/view/d/uXQh5yHMArQ3J9AfA8W8yV",
        "title": "Vancouver West Coal Harbour 2 bed +den unfurnished apartment for rent",
        "price": 3300,
        "beds": 2,
        "baths": 2,
        "sqft": 1189,
        "address": "1328 W Pender St, Vancouver, BC V6E 4T1",
        "nb": "downtown_van",
        "photo": "https://example.com/cl.jpg",
    }
    accepted, _excluded, refreshes = dedupe_and_filter([cl_cand], state)

    assert len(accepted) == 0
    assert len(refreshes) == 1
    assert refreshes[0]["id"] == "uXQh5yHMArQ3J9AfA8W8yV"
    assert refreshes[0]["sig"] == "1328 west pender|3300"
