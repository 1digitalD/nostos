"""Cross-source dedupe helpers ported from apartment-hunt."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from nostos.model.identity import Identity, Signature

SEEN_EVICTION_DAYS = 90

Candidate = dict[str, Any]
State = dict[str, Any]
CriteriaFn = Callable[[Candidate], tuple[bool, str]]
NowIsoFn = Callable[[], str]


def make_sig(title: str, price: int | str | float | None, address: str = "") -> Signature:
    """Cross-source dedupe signature from address/title and a price bucket."""
    return Identity.compute_signature(title=title or "", price=price, address=address or "")


def prune_state_seen(state: State, max_age_days: int = SEEN_EVICTION_DAYS) -> int:
    """Drop entries from state['seen'] that have not been observed in N days."""
    seen = state.get("seen")
    if not isinstance(seen, dict) or not seen:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    pruned = 0
    for listing_id in list(seen):
        entry = seen.get(listing_id)
        if not isinstance(entry, dict):
            continue
        timestamp = entry.get("last_seen")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed_at < cutoff:
            del seen[listing_id]
            pruned += 1
    return pruned


def dedupe_and_filter(
    new_candidates: list[Candidate],
    state: State,
    *,
    apply_criteria: CriteriaFn | None = None,
    now_iso: NowIsoFn | None = None,
) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    """Return (accepted, excluded, refreshes) and update state['seen'] in-place."""
    criteria = apply_criteria or _default_apply_criteria
    now_fn = now_iso or _now_iso
    seen = _ensure_seen_state(state)

    accepted: list[Candidate] = []
    excluded: list[Candidate] = []
    refreshes: list[Candidate] = []

    seen_sigs = {
        str(entry_sig): listing_id
        for listing_id, entry in seen.items()
        if isinstance(entry, dict) and (entry_sig := entry.get("sig"))
    }
    seen_ids = set(seen)
    run_sigs: set[str] = set()
    now = now_fn()

    for candidate in new_candidates:
        source = str(candidate.get("source", "unknown"))
        listing_id = str(candidate.get("id", ""))
        if not listing_id:
            continue

        url = str(candidate.get("url") or _url_placeholder(source, listing_id))
        sig = make_sig(
            title=str(candidate.get("title", "")),
            price=candidate.get("price"),
            address=str(candidate.get("address", "") or ""),
        )

        if listing_id in seen_ids:
            entry = seen.get(listing_id)
            if isinstance(entry, dict):
                entry["last_seen"] = now
            candidate["sig"] = sig
            candidate["url"] = url
            refreshes.append(candidate)
            continue

        if sig in seen_sigs:
            existing_id = seen_sigs[sig]
            entry = seen.get(existing_id)
            if isinstance(entry, dict):
                entry["last_seen"] = now
            candidate["sig"] = sig
            candidate["url"] = url
            continue

        if sig in run_sigs:
            continue

        is_allowed, status = criteria(candidate)
        candidate["status"] = status
        candidate["sig"] = sig
        candidate["url"] = url
        if "photoUnverified" not in candidate:
            candidate["photoUnverified"] = not bool(candidate.get("photo"))

        if not status or status == "excluded" or not is_allowed:
            excluded.append(candidate)
            continue

        seen[listing_id] = {
            "url": url,
            "title": str(candidate.get("title", "")),
            "price": candidate.get("price") or "",
            "first_seen": now,
            "last_seen": now,
            "sig": sig,
            "source": source,
        }
        accepted.append(candidate)
        run_sigs.add(sig)

    return accepted, excluded, refreshes


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_apply_criteria(candidate: Candidate) -> tuple[bool, str]:
    del candidate
    return True, "included"


def _ensure_seen_state(state: State) -> dict[str, dict[str, Any]]:
    seen = state.setdefault("seen", {})
    if not isinstance(seen, dict):
        msg = "state['seen'] must be a dictionary"
        raise TypeError(msg)
    return seen


def _url_placeholder(source: str, source_id: str) -> str:
    return f"https://{source}.invalid/{source_id}"
