# Implementation review and Toronto slice — 2026-09-04

Scope: current checkout, not a historical diff. Findings below are based on code,
HTTP probes, tests, and bounded live source probes. Personal profile values are not
copied into the repository. The deployed Toronto profile remains private configuration.

## Implementation status — 2026-09-05

Recommendations 1–6, 8, and 9 below are now implemented. Hard amenity requirements
use the shared explained evaluator; saved research remains browseable; profile changes
support preview, revision-guarded apply, history, and undo through web, CLI, and MCP;
known listings refresh after 24 hours; detail actions are reversible; and proximity plus
the unverified penalty affect ranking. Manual entry, hunt stages, viewing-time capture,
ICS export, source coordinates, Rogers Centre distance, map links, and cached walking
routes are also present. Move-in date, minimum lease, and stated total monthly cost are
enforced and editable in the profile UI. User fact corrections now override extracted
values with reset support, and each detail page has a research workspace that surfaces
strongly related stored ads plus targeted searches across other rental sites.

Finding 7 remains the main backend optimization: build and benchmark an indexed current
listing projection before result volume makes replay and in-process sorting noticeable.
Visual browser validation also remains outstanding because no browser provider was
available during this review; HTTP workflow coverage and the source detector were used.

## Findings at review start, in priority order

1. **P1: “deal-breaker” is not enforced.** `config/wizard.py` maps laundry and parking
   deal-breakers to weights. `rank/profile_scoring.py:passes_hard_filters` does not
   enforce either amenity. A high-scoring listing can lack the selected requirement.
   Add explicit hard amenity requirements after agreeing on unknown-data handling.
2. **P1: two filter evaluators disagree about missing facts.**
   `rank/profile_scoring.py:passes_hard_filters` rejects unstated rent/beds/baths/size;
   `web/query.py:classify_match_status` calls them unverified. Rescore removes their
   score, while the UI queries only scored records. The “unverified” view therefore
   cannot reliably recover those listings after rescore. Share one evaluator returning
   verdict, reasons and missing facts; store/display rejected and unverified results
   independently of whether they earn a ranking score.
3. **P1: source toggles hide saved research.** The profile editor says toggles control
   watch, but `web/app.py:AppState._resolve` only provides enabled parsers to replay and
   details. `rank/rescore.py` deletes scores when the parser is absent. Existing detail
   pages can become 404; the save banner mislabels these as hard-filter failures.
   Separate fetch enablement from availability of parsers for stored records.
4. **P1: repeated edits can lose work.** `web/templates/profile.html` has separate Save
   and Re-score forms. Re-score acts on the saved file and redirects, discarding dirty
   controls. Use one clear “Save and update rankings” action, preserve drafts, and make
   a preview explicitly read-only. Save currently writes YAML before scoring succeeds;
   a scoring failure can leave a new profile with old score rows. Validate executable
   rules before saving, track the active revision, and surface recovery coherently.
5. **P1: discovery optimization can preserve stale facts indefinitely.**
   `watch/runner.py:_should_skip_detail_fetch` skips known records without a posted
   value. Without a bounded refresh policy, price changes and removal are not revisited.
   Add a refresh age and bounded refresh queue; distinguish last fetched, last seen and
   availability instead of treating skipped fetching as current verification.
6. **P2: detail actions lack correction.** `web/templates/detail.html` disables selected
   shortlist/dismiss/contacted controls, including after success; undo is inconsistent
   with the list. Failures restore the control without explaining the error. Use
   reversible toggles, visible failure feedback, and announce updated states.
7. **P2: browsing cost grows with the full result set.** `web/query.py:query_list` reads
   all score rows, joins records/actions/breakdowns, parses and enriches every listing,
   then filters/sorts/limits in Python. Add a derived current-listing projection with
   parser version/content hash, indexed common filters and server-side pagination.
   Keep raw records canonical for replay. Benchmark representative counts before/after;
   do not increase scrape concurrency to compensate for repeated local parsing.
8. **P2: MCP is a CLI test-runner wrapper, not a criteria editing interface.**
   `mcp/server.py` invokes Typer's CliRunner and returns text. It has init/watch/rank/
   list/explain, but no read/update/preview/undo for a profile. Move reusable operations
   into a library service with structured results shared by CLI, web and MCP. Avoid
   regenerating a profile through init to change one preference.
9. **P2: some exposed configuration has no ranking effect.** `Profile` accepts proximity,
   avoid_areas and confidence.unverified_penalty, but the ranking engine consumes weights
   and area_key_weights only. Do not offer those fields as working controls until wired
   to supported observations; flag inactive settings explicitly.

Fixed in this slice: normal list-form submission sent empty numeric values and returned
422 JSON. Empty values now mean unset while invalid, negative and out-of-range values
still fail validation. Regression covers applying and clearing a realistic GET form.
Malformed hand-edited URLs still return API validation JSON; human-readable recovery is
an adjacent improvement, not part of this fix.

## UI/UX evidence and priorities

Two independent assessments: source/workflow review and detector/HTTP evidence. The
browser provider reported no available browsers, so no visual or browser-JavaScript
validation was possible. Synthetic local HTTP checks covered list, detail, profile and
mutation behaviour; the temporary server and fixtures were removed.

The mechanical detector returned zero findings but ran a **degraded regex fallback**
without its parser dependencies. This is not a clean accessibility audit. Source colour
calculations show the 11px unverified badge at about 3.56:1 contrast and selected amber
chips at about 3.69:1, below 4.5:1 for small text (`web/static/style.css`). Confirm final
computed colours in a real browser before changing the palette.

Keep the useful separation of hard filters and ranking weights, evidence-backed score
explanations, and validation that preserves submitted values. Improve the decision loop:
list → inspect → edit → preview → apply → same list context → undo. Show active criteria
first and hide inactive advanced rules behind disclosure. Distinguish “no data fetched”
from “criteria excluded everything,” with counts and an edit-criteria recovery action.

## Criteria revision workflow and remaining user decisions

No personal criteria have been changed. Consult on:

- Monthly rent versus total recurring cost (utilities, parking and other stated fees).
- Minimum bedrooms versus exactly N; whether a den is a substitute.
- Whether size is a hard minimum or a preference when listings omit it.
- Confirmed amenity requirements versus weighted preferences; unknown is a separate fact.
- Exact move-in deadline, minimum lease term, and desired travel mode/time threshold.
- Pet wording: an occupant's lack of pets is not the same preference as avoiding all
  pet-friendly properties. Current negative pets.allowed weight does the latter.
- Toronto neighbourhood preferences independently of Vancouver weights. No transfer of
  Vancouver area keys. Advertised walkability phrases are weaker evidence than a route.

Recommended repeatable agent interaction:

1. Read the current profile and its revision, including supported/inactive criteria.
2. Propose a small typed patch in ordinary language; show exact changes.
3. Preview on stored records without fetching or writing: matched/unverified/rejected
   counts, entrants/exits, and representative before/after order with reasons.
4. Apply the reviewed patch only against the revision previewed. Preserve unrelated
   fields, write revision history, and atomically activate matching scores.
5. Undo to a prior revision and restore the same inspection context.

Shipped tools: `profile_get`, `profile_preview`, `profile_apply(expected_revision)`,
`profile_history`, `profile_undo`, with matching hyphenated CLI commands. Agent-authored
changes go through deterministic validation; scheduled ranking continues without a
model call. Acceptance coverage changes a requirement through the shared workflow,
rejects stale edits, and restores a prior revision without losing listing notes.

## Toronto slice delivered

Smallest valuable job: a renter selects Toronto, fetches listings, and opens a ranked
list with reasons so they can start evaluating housing for a move.

Normal entry: `nostos init --city toronto --profile ...`, then watch and web using that
profile. Packaged Toronto city facts select Craigslist City of Toronto (`tor`) and
Kijiji City of Toronto (`1700273`). Citypack selection follows the profile automatically;
Toronto gets a separate default DB. Vancouver defaults remain compatible. Explicit DB
paths are operator overrides and MUST NOT combine cities: the underlying store has no
city partition. A multi-city combined dashboard is deferred.

Validation:

- Automated synthetic-response E2E crosses CLI init → actual Craigslist discover/detail
  parser → watch → SQLite → rank → list → web rendering. Tighten rent, replay to empty,
  relax and replay to restore results, without further fetches. Invalid city fails before
  creating a profile. Kijiji city URL and empty discovery are covered separately.
- Final full suite: 289 tests passed, with Ruff and strict mypy clean.
- Bounded live discovery: Craigslist 356 records; Kijiji 43. One detail per source fetched
  and parsed with liveness `ok`. Craigslist sample had rent/beds/baths/size; Kijiji studio
  sample had rent/baths/size but beds unstated. Both lacked a recognized area key. Counts
  are observations on this date, not expected totals or guarantees of unique units.
- Manual operator probe confirms discovery and parser execution; visual dogfood remains
  unverified because no browser is available. Personal Toronto profile awaits consultation.

The deployed Toronto profile carries Vancouver's hard criteria and non-neighbourhood
weights, uses Rogers Centre as its landmark, and keeps Toronto area weights empty.

Go: Toronto discovery/replay/browse and repeatable criteria-editing slice. No-go: calling
it a proven personal shortlist until the remaining criteria choices and representative
results are reviewed. Deferred: GTA coverage, full address verification, transit-time
routing, fee normalization beyond stated total cost, and browser-validated responsive/
accessibility behaviour. Unknown listing facts stay unknown.


## Original roadmap gap check

Compared `docs/06-roadmap.md` and `docs/09-r1-tasks.md` with the current source tree.
This is capability verification, not a claim that the original operator ship gates
have been completed.

| Release | Present | Missing or partial |
| --- | --- | --- |
| R1 watch/rank | Two adapters, deterministic ranking, explanations, CLI/MCP, notification delivery library, run health | `schedule` is metadata; no scheduler installation/management in this repo. External scheduling was not audited. First-install notification ship gate not established. |
| R2 triage/track | Browse/filter/sort, reversible actions, notes, manual adds, hunt stages, viewing calendar/ICS, saved-research access | Field-correction UI for parser facts and automated communication follow-up. |
| R3 vicinity | Advertised Walk Score, city area weights, source coordinates, Rogers Centre straight-line scoring, map links, cached OSM walking routes | Robust address geocoding, transit-time routing, and named grocery/transit distance facts. |
| R4 anywhere | Citypack schema, source registry, fixture conformance tests, two packaged cities | User-facing coverage probe, citypack generator, adapter scaffolding, keychain credential integration. One-off developer probes are not the planned coverage product. |
| R5 photos | Photos displayed, photo-presence score, enrichment/budget infrastructure | Floor-plan OCR, condition/render assessment, vision provider and visible confidence/correction workflow. Deliberately deferred in the original roadmap. |
| R6 two rubrics | Scores keyed by profile ID | Comparison UI, disagreement sorting and per-person alert/deal-breaker handling. Deliberately deferred. |

Recommended order for the move:

1. Correct the existing requirement/unknown-data semantics and preserve research.
2. Add a confirmed Toronto landmark with verifiable location and route provenance.
   Begin with visible distance/travel-time facts; agree a threshold and weight before
   changing ranking. Unknown addresses must stay unknown, not be assigned a city-centre
   distance. Preview the effect on a representative shortlist.
3. Ship agent/web profile preview, apply and undo with revision checks.
4. Add manual URL entry, followed by a viewing tracker and calendar export.
5. Add freshness/price-change/removed-listing tracking and an operator health view.

Additional Toronto-useful criteria beyond the original roadmap: move-in availability,
minimum lease term, stated all-in monthly costs, and elevator/accessibility requirements
where relevant. Each needs sourced facts, unknown handling, and an explanation of its
filter/ranking effect. Floor-plan detection is the next evidence slice; dual-person
rubric comparison remains later work.

## Next adjoining slice: floor-plan detection and analysis

Actor/job: from a shortlisted listing, the renter opens a detected floor plan and sees
which image was classified as a plan, readable room dimensions, layout observations,
and uncertainty so they can judge usable space before booking a viewing.

Normal entry: the listing detail and research pages. The first slice should classify
stored listing images, preserve the original image and model/version provenance, allow
“not a floor plan” correction, and extract dimensions only when legible. Its end-to-end
gate is one realistic listing flowing from stored photos through classification to a
visible, correctable result; ordinary listing photos and unreadable plans must produce
clear empty/uncertain states. Room-layout scoring should follow only after this evidence
workflow is trustworthy.
