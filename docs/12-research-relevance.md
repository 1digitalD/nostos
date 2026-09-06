# Address research: requirements and implementation plan

## User job and entry point

A renter opens Research from a listing and sees cited evidence about that exact
building, so they can decide which questions to investigate before arranging a viewing.

## Requirements

1. Resolve a street number, street name/type/direction, and city. Remove unit numbers,
   listing facts and intersection commentary. Locality/postal-only inputs require
   address correction; never silently research a whole city as a building.
2. Begin with an address-only query, then bounded focused management/reviews and safety
   queries. Do not include rent or bedroom criteria. Keep provider integration optional
   and independent of agent hosts; no model call is needed for deterministic relevance.
3. Accept a search excerpt only with explicit matching street-address and city evidence.
   Match street abbreviations, but reject different numbers, directions and cities.
   Show the match reason and source excerpt; do not promote a snippet to a verified fact.
4. Keep recent dated evidence (two years); label absent evidence unknown. Never infer
   that a building is safe from no findings. Building aliases alone cannot prove identity.
5. Keep nearby map-pin results separate from building evidence. Existing duplicate
   matching and user correction remain available. Web snippets do not alter ranking.
6. Hide legacy reports until revalidated. Cache identity includes normalized address,
   city, algorithm version and a 24-hour TTL; corrected addresses invalidate reports.
7. Display partial provider failure, missing address and no accepted evidence honestly.

## Implementation sequence

1. Reproduce unrelated results with a deterministic provider fixture.
2. Add address normalization, focused bounded searches and a strict evidence gate.
3. Store relevance provenance and version; connect cache invalidation and explicit UI states.
4. Test provider -> HTTP endpoint -> SQLite -> rendered research, including wrong
   building/city, address correction, legacy cache, missing address and provider failure.
5. Dogfood a known Toronto address and a locality-only listing; deploy and probe both cities.

## Acceptance and scope

Go only when no known unrelated fixture passes, valid abbreviation variants pass,
the normal UI renders accepted citations and explains missing evidence, and existing
city data remains isolated. Live search quality remains provider-dependent.

Deferred: full-page corroboration, independently verified building aliases, multi-source
claim synthesis, neighbourhood web reports, and suggested fact corrections with evidence.
The current slice labels accepted items as matching source excerpts, not verified conclusions.

## Validation and delivery (2026-09-06)

- Automated suite: 325 tests passed; Ruff, strict mypy (91 source files), and
  whitespace checks passed. Workflow tests cross provider, HTTP, SQLite, and rendering.
- Live address-v2 search for 55 Bremner Boulevard, Toronto retained two matching
  sources and excluded 11 unrelated or ambiguous URLs. This validates matching,
  not independent truth of the source claims.
- Missing street-address input returns an actionable correction state without searching.
  Desktop/mobile browser checks covered that state and the populated report.
- Both city services use the revised runtime; city databases remain separate.
- Rollback: reinstall the previous Git revision into the runtime venv and restart
  both LaunchAgents. Migration 0006 is additive. Pre-migration SQLite backups are
  in /tmp/nostos-research-backup-20260906195348 (temporary recovery copies).
