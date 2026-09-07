# Saved-evidence decision evaluation

Date: 2026-09-06

## Scope and limits

This was a bounded, model-assisted offline judgement exercise, not an independent
ground-truth study and not a performance or accuracy measurement. It used the live
Toronto SQLite database in read-only mode, the active Toronto profile, the normal
source parsers, and `web.query.load_detail`. It made no provider/API requests, did
not scrape or refresh source pages, and did not write to the database.

Exactly 20 latest saved listings were selected deterministically: 10 Craigslist and
10 Kijiji, spanning 5 current matches, 6 unverified listings, and 9 misses. Within
each source/status stratum, listings were ordered by SHA-256 of the listing ID. The
fixed allocation was Craigslist 3/3/4 and Kijiji 2/3/5 for
match/unverified/miss. Listing IDs and the redacted per-listing judgements are kept
only in the private companion artifact at
`~/.local/share/nostos/agent-evaluation/20260906/decision-evidence-sample.json`.

All 20 sampled rows had a saved description. Fourteen had a saved detail record
marked complete; six were not marked complete. The broader latest-record inventory
was 107 Craigslist listings (104 descriptions, 96 complete detail records) and 146
Kijiji listings (146 descriptions, 46 complete detail records).

## What the saved evidence supports

A careful reading could recover 14 decision-relevant facts that the current loaded
listing omitted across 10 sampled listings:

| Recoverable fact | Observed omissions | Typical saved evidence |
|---|---:|---|
| Parking | 8 | “1 parking … included”, “Indoor Parking: $150/month”, saved `parking: false` |
| Area | 4 | Craigslist source attributes such as “800 ft2” or “900 ft2” |
| Building laundry | 1 | “On-Site Laundry Facility” |
| Bachelor bedroom count | 1 | “BACHELOR APARTMENT” |

These are parser/extraction misses, not retrieval failures: the necessary text or
structured source field was already saved.

The review also found 12 unsupported, over-precise, conflict-suppressing, or
wrong-scope claims across 10 listings:

- Six area claims treated “under 900” or an implausible source value of `0 sqft` as
  an exact measured fact; a seventh applied a property-wide area to a room rental.
- Four bedroom claims collapsed meaningful semantics or conflicts: `1+1`, an
  office/flex room, contradictory `1BR`/“two bedroom” evidence, or property-wide
  bedroom count on a private-room ad.
- One private-room ad was represented as a full unit despite “Shared Kitchen &
  Bathroom”.
- Three listings contained material conflicts that should be preserved for review,
  including bedroom-count conflicts and “BSMT” versus “3RD Floor”.

Short saved-evidence examples include “1-Bedroom + Second Room/Home Office”,
“Private Rooms for Rent - $700 Each”, “in-suite washer and dryer”, and “two tandem
parking spots”. Contact details, listing URLs, names, and addresses were omitted from
the evaluation artifact.

## Decision effect in this sample

Careful saved-evidence interpretation materially changed four current statuses:

- one unverified listing became a match after explicit included parking was recovered;
- one miss became unverified because contradictory bedroom evidence should not have
  been collapsed to a definite failing value;
- one match became a miss because the evidence stated one bedroom plus an office/flex
  room, not two bedrooms;
- one unverified listing became a miss or out-of-scope because it advertised private
  rooms with shared facilities, not a full rental unit.

Other recovered facts improved the reasons without changing the final status. For
example, several basement or one-bedroom listings remained misses after their area
or parking evidence was corrected. Several plausible candidates remained genuinely
unverified because in-suite laundry was not stated.

## Build direction

Build **saved-evidence semantic extraction and conflict resolution first**. Include
the small deterministic corrections in the same narrow slice: treat zero area as
unknown, consume explicit structured parking values, preserve contradictory claims,
and distinguish bedroom, office/den, bachelor, room rental, and full-unit scope.

The smallest valuable user job is: from a listing detail page, a renter asks Nostos
to interpret already-saved evidence and sees quoted proposed facts, conflicts, and
the resulting status change before accepting or rejecting them. The acceptance gate
should cover parking recovery, zero-area normalization, `1 bedroom + office`, room
versus full-unit scope, conflicting evidence, unchanged user corrections, and the
normal detail-to-review-to-apply path.

Build the decision brief immediately after that resolved-evidence path exists. A
brief built first would make some current wrong or overconfident fields more
persuasive without fixing them. Source retrieval should follow as a targeted fallback
only for decision-blocking facts that remain absent after saved-text interpretation;
the sampled bottleneck was not a lack of saved descriptions.
