# Changelog

All notable changes to Nostos are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-09-02

Configurable filters and ranking criteria in the web UI. Everything the old
`apartment-hunt` rubric hardcoded is now a profile setting you can edit in the
browser, and edits apply immediately.

### Added

- **Profile editor at `/profile`** covers every hard filter (rent min/max, beds,
  baths, floor, area, allowed neighbourhoods, basement / furnished-only
  excludes), every registered ranking rule grouped by category with a
  plain-language description and slider, per-neighbourhood preferences,
  the unverified-data penalty, and source toggles. Saving re-scores all stored
  listings from their latest source record without a network fetch; a
  "Re-score now" button does the same on demand.
- **Hard filters** `rent.min`, `floor` (e.g. `{max: 12}`) and `areas` (allowed
  citypack area keys). An unstated floor or unknown area still passes and is
  shown as unverified; a stated value outside the bound fails.
- **`photo.present` rule** (amenities): rewards listings with at least one
  photo. Shipped profiles and the wizard carry it at +2.
- **Rule descriptions** and `CATEGORY_LABELS` on the rule registry, used by the
  editor and documented in `docs/04-config.md`.
- **`nostos.rank.rescore`** module shared by `nostos rank` and the web UI.
- **List page filters**: neighbourhood chips, shortlisted-only, hide-dismissed,
  show-excluded, match-status filter, more sort orders (rent desc, area desc,
  oldest first), collapsible numeric filters, and a profile summary strip that
  shows the top weights next to the hard filters.
- **Match reasons**: the status badge explains why a listing is unverified or a
  miss; the detail page shows a per-rule breakdown with evidence.

### Fixed

- Saving the profile no longer leaves the running web app on the old profile
  until restart, and scores are recomputed instead of going stale.
- Match-status classification never flagged a bedroom-count miss and could
  crash when the profile had no bedroom filter.

## [0.2.2] - 2026-09-02

- Status badge per card (Match / Unverified / Miss / Excluded), facts row,
  per-category breakdown bars, action buttons in the card body, new Exclude
  action (migration 0003), inline ranking-profile summary, first `/profile`
  editor (hard filters, existing weights, sources).

## [0.2.0] - 2026-09-01

- Local web UI (`nostos web`): photo-first card grid, one-click star / dismiss /
  contacted / note actions persisted in `listing_action`, static `--export`.

## [0.1.0] - 2026-08-31

First tagged release. The pipeline has been run end-to-end against the live
Vancouver sources (Craigslist + Kijiji) per `docs/10-live-smoke.md`; that run
captured real fixtures and surfaced one parser drift, which is fixed below.

### Added

- Self-hosted Vancouver apartment watch (`nostos watch`, `list`, `explain`)
  backed by an opinionated Vancouver citypack and example profile.
- `nostos init` non-interactive setup for repeatable city provisioning.
- MCP server (`nostos-mcp`) — a thin CLI wrapper for chat integrations.
- `scripts/capture_fixtures.py` — live fixture capture with mechanical
  redaction (phones, reply links, JSON-LD contacts). Manual review of every
  captured fixture is still required; the script does not catch personal
  names or phone numbers embedded in URL query strings.
- `docs/10-live-smoke.md` — the runbook for verifying the pipeline against
  live hosts before tagging a release.
- `CONTRIBUTING.md` — covers citypack authoring and source-adapter contribution.

### Changed

- **Cross-source dedupe is wired into the watch pipeline.** A unit listed on
  Craigslist and Kijiji now collapses to one record, ranked once. The
  signature is intentionally coarse (address + price bucket) so it matches
  cross-source postings without false-merging different units in the same
  building.
- **Per-source rate limits** are enforced (60 rpm default, configurable per
  source). Combined with `robots.txt` checks, the pipeline no longer risks
  getting IP-banned by Craigslist or Kijiji.
- **Kijiji area vocabulary** expanded; basement hard-filter detection tightened.
- **Ranking:** live detectors, location weighting, and list-limit semantics
  were reworked; the example Vancouver profile weights were re-aligned to
  match.
- The duplicate repo-root Vancouver citypack was removed; the canonical one
  now lives under `citypacks/vancouver/`.

### Fixed

- **Craigslist RSS discovery fails closed.** Previously a fully robots-blocked
  source was reported as `ok` with zero listings — which looked identical to a
  quiet market. Discovery now surfaces the block.
- **Craigslist HTML fallback** when RSS is blocked, so a robots.txt change on
  the RSS path does not black out the source.
- **Quiet-run false alert:** the `load-bearing source returned zero listings`
  alert no longer fires on a re-watch where the market simply has nothing new.
- **Parser drift in Craigslist `_SQFT_RE`:** real Craigslist uses `590 sq. ft.`
  (with periods); the old `sq\s*ft` regex did not match, so `area` came back
  as `not_stated` on live listings. Now `sq\.?\s*ft`. Surfaced by the live
  smoke run; this is the kind of bug hand-written fixtures could not catch.
- Watch runner no longer clobbers already-detailed listings when a seen
  empty-posted record arrives from the same source scan.
- T14/T15 ship-gate isolation, packaging, and CLI scoring gaps.

### Notes for self-hosters

- **PII in captured fixtures:** the mechanical redaction in
  `scripts/capture_fixtures.py` does **not** catch personal names. The
  R1 live-smoke capture had to manually redact two names ("Dea" and
  "Emily") that the script left in the rendered HTML. Read every captured
  fixture before committing. The script's phone-pattern pass also
  partially corrupted some JSON-LD longitudes (e.g. `-123.139595433273`
  → `-123.555-555-5555`); the parser does not use longitude, so there
  is no functional impact, but it is worth tightening in a future
  redaction pass.
- **Watch runtime** on the example Vancouver profile (rent ≤ $3200, 2BR,
  ≥700 sqft) returns ~349 filtered listings per area across 3 sub-areas.
  Cycle 1 takes ~10–25 min at the 60 rpm per-source rate limit; cycle 2
  (re-watch, suppression check) takes ~1 min. This is the real workload,
  not a parser bug.

[Unreleased]: https://github.com/1digitalD/nostos/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/1digitalD/nostos/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/1digitalD/nostos/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/1digitalD/nostos/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/1digitalD/nostos/releases/tag/v0.1.0