# App assessment and listing enrichment requirements

Assessment date: 2026-09-06. Source revision: df0e0fa.

## Assessment

The app has a useful rental-hunt shell: city isolation and switching, criteria preview
and undo, ranking explanations, shortlist/notes/viewing workflows, corrections, and
address research. Its main weakness is evidence quality underneath those workflows.
Missing inputs and incorrect positive claims make the ranking less trustworthy than
the polished presentation suggests. Prioritize reliable listing detail enrichment
before adding sources or further visual polish.

This assessment used current source code, read-only queries of both live city stores,
and deterministic parser probes. It did not re-fetch every source listing, verify
image URLs, or perform a new exhaustive browser/accessibility review. Counts include
all stored listings, not only active or currently visible matches. Zero observations
does not prove the source omitted a fact.

## Measured coverage

Latest source record per canonical listing, with observed fields from fields_json:

| City/source | Listings | No photos | Exactly one photo | No description | Description exactly 2,000 characters |
|---|---:|---:|---:|---:|---:|
| Toronto Craigslist | 70 | 27 | 43 | 27 | 6 |
| Toronto Kijiji | 105 | 0 | 105 | 0 | 2 |
| Vancouver Craigslist | 1,552 | 499 | 1,053 | 460 | 168 |
| Vancouver Kijiji | 90 | 0 | 90 | 0 | 0 |

- All 27 Toronto Craigslist records missing descriptions/photos contain a detail
  error: `[Errno 2] No such file or directory`. The responsible missing dependency
  or path still needs a fresh fetch trace; do not call these source omissions.
- None of the 1,817 stored listings has an observed `available_date`, `lease_months`,
  or `total_monthly` field in the projection.
- Toronto has only 16 observed in-suite laundry fields across 175 listings.
- Only three Toronto listings have stored nearest-gym/grocery distance observations;
  Vancouver has none. Text-based proximity rules can still fire without these fields.
- No Realtor.ca listings are stored. The latest stored Realtor.ca run failed waiting
  for a browser response; installed adapter support is not working market coverage.

## Existing pipeline and defects

The detail step already exists: discovery -> fetch_detail -> adapter conversion ->
canonicalization/persistence -> text enrichment/scoring. The web layer reconstructs
listings from the latest source record and runs text rules again. There is no durable,
independently retryable detail-enrichment job exposed through the listing UI.

### Priority 0: prevent incorrect eligibility claims

1. Craigslist `_parse_detail_html` treats any mention of parking, garage or stall as
   `Included`. Probes return Included for both “No parking available” and “Parking
   available for $150/month”. Kijiji `_item_payload` returns parking=True for “No
   parking”, and False for a description saying nothing about parking. These are
   semantic errors, not just incomplete extraction.
2. `TextRuleEnricher` interprets “TOP-FLOOR 2-BEDROOM” as floor=2. The stored Toronto
   listing `craigslist:1Cwp9kaR8RGC24Zx1XqRdA` contains this exact false observation.
3. `_in_suite_laundry('No shared laundry')` returns positive in-suite evidence.
   Absence of shared laundry does not establish private laundry. Generic washer/dryer
   mentions also need unit-versus-building context.
4. Source adapters assign high-precedence origins to heuristic facts. The text
   enricher primarily fills missing values, so later careful rules cannot reliably
   repair these earlier claims. Reprocessing must retire superseded machine
   observations while preserving user corrections.

### Priority 1: stop losing source detail

5. Craigslist saves only the first 2,000 characters of postingbody and extracts only
   og:image in its detail parser. Later amenities/terms are lost to enrichment and
   the gallery is reduced to a cover image. Preserve full bounded source text and
   extract all listing-specific gallery assets.
6. Kijiji relies on JSON-LD plus Open Graph title/image, without a general visible
   description/amenities/gallery fallback. It merges empty detail fields over discovery
   fields. Realtor.ca also performs a broad detail merge. Merge valid evidence by
   field; do not erase a useful discovery value with an empty detail value.
7. Kijiji and Realtor.ca return the original record on detail-fetch exceptions. Their
   liveness checks can then report OK. Detail success, source discovery success,
   and actual listing availability need separate states.
8. Recent records can be skipped for 24 hours based on fetch time, without an
   explicit detail-completion/version state. Failed enrichment needs its own retry
   schedule; parser upgrades need replay independent of rediscovery.

### Priority 1: make stored information useful

9. Automatic text enrichment has no available-date, lease-length, or total-cost
   extraction. These fields exist in corrections and criteria but are largely
   disconnected from automatic collection.
10. Corrections write `attributes.available_date`; the detail/list view reads
    `available` or `avail`. Corrected availability can therefore still say Unstated.
11. The details template omits the source description entirely. Its fact ledger omits
    a dedicated laundry row, lease, utilities/cost breakdown, and building amenities.
    Photos are limited to five with no full-gallery navigation or broken-image state.
12. Ranking parses some amenities directly from text rather than consuming one shared
    fact representation. “Equipped gym” currently includes generic on-site gym mentions;
    equipment quality is not established. Nearby enrichment is initiated from Research,
    so listings that have been opened can have richer ranking inputs than others.

## Second-pass review and scope decisions

Reviewed again on 2026-09-06 against observation projection, scoring/persistence,
correction replay, and list reconstruction. The coverage numbers above are the earlier
read-only snapshot; they were not measured again for this plan revision.

The original first slice combined parser repairs, a new worker, gallery recovery,
refresh UX, and a projection redesign. That is too much to validate at once. Split
repair of already captured evidence from retrieval of missing evidence. Do not begin
with a generic enrichment platform.

Two additional implementation risks were confirmed in code:

- `ObservationRepo.project_listing_fields` chooses origin precedence before recency.
  Appending a better, lower-precedence observation cannot retire an old wrong source
  claim. Define which machine observations belong to the current extraction revision;
  retain historical observations for audit but exclude superseded ones from resolution.
- `_score_stage` persists enrichment updates only after a listing passes hard filters.
  A rejected listing can therefore lack stored enrichment despite the parser having
  assessed it. Persist facts independently of eligibility and scoring. UI reconstruction
  currently reruns parsers while displaying stored scores, so parser upgrades can also
  produce facts and scores from different revisions.

### Keep the implementation small

- Reuse source adapters, SQLite, the observation/correction model, and scoring code.
  Centralize the affected fact-resolution path; do not redesign every domain model.
- No Redis, Celery, event bus, workflow framework, distributed workers, or new hosting.
  No new model/provider dependency in either initial slice.
- No new preference settings or ranking weights. Parking and in-suite laundry remain
  hard requirements; preserve the configured handling of unknowns.
- Multi-unit scope requires conservative handling now, not a new offers subsystem:
  preserve ranges/raw text and flag ambiguity; only extract a scalar tied to this unit.
- Keep an all-photo viewer simple. Collect source gallery URLs/metadata first; image
  mirroring, thumbnails, visual embeddings, and image deduplication are separate work.
- Keep extraction status separate from field values. Reuse absence values where they
  fit; do not add every job state to every fact or convert failure into false/null.

## Slice 1: repair and inspect captured listing evidence

**User job:** A renter opens a listing, selects **Review extracted details**, and sees
which facts can be recovered or corrected from its saved source text, with evidence,
then applies the reviewed result without losing personal corrections or notes.

This is a dry-run preview and apply flow using saved content, with no network request.
The preview labels missing/truncated source content and directs the renter to the
original listing until live refresh is available. It must not promise lost photos or
text can be reconstructed from an incomplete snapshot.

### Included

1. Fix parking negation/cost scope, private-versus-shared laundry, and numeric floor
   false positives in adapters, enrichment and ranking detectors that interpret those
   same facts. A generic garage/washer mention is insufficient evidence.
2. Align `available_date` across corrections, facts, research and criteria. Show saved
   original text, laundry, existing availability, and evidence in the details page.
   Preserve an ambiguous date as source text instead of inventing a year.
3. Resolve an extraction revision from the saved source evidence, then apply current
   user corrections. Mark unsupported legacy heuristic positives unverified; never
   treat their stored normalized value as independent source evidence. Retain explicit
   structured source fields only when their provenance can be established.
4. Persist resolved machine facts regardless of hard-filter outcome. Commit the fact
   revision and affected profile score/breakdown together, preserving user corrections,
   research-address override, notes, shortlist, viewing state and stable listing ID.
5. Preview changed facts and eligibility before apply. Reject or regenerate a stale
   preview if source content, correction revision or profile changed. Resetting a user
   correction must reveal the latest valid machine value, not resurrect a retired one.

### Acceptance gate

- One automated workflow crosses saved source record -> real extraction -> preview ->
  apply -> persisted facts/score -> rendered details; includes a user correction made
  after preview, a failed transaction, repeat apply and a hard-filter miss.
- Known false positives from this assessment fail before the repair and pass after it.
  No negative/ambiguous parking or laundry fixture becomes a confirmed match.
- Listing facts, research missing-fact indicators and score explanations use the same
  revision. Unknowns remain reviewable according to existing policy.
- A bounded dogfood checks the saved Toronto floor-error listing and a Kijiji example,
  then equivalent Vancouver records. The operator can understand the correction and
  its evidence through the normal UI without database access.
- Go only if the repaired workflow is understandable and correct. If the source
  snapshot lacks necessary evidence, report that limitation and proceed to Slice 2;
  do not fill gaps by guessing or broaden Slice 1 into a fetcher rewrite.

## Slice 2: retrieve complete details and recover failures

**User job:** A renter selects **Update listing details** and gets the available full
source description and gallery, followed by newly extracted facts, or an actionable
fetch failure while the previous successful report remains available.

### Included

- Diagnose the recorded Craigslist missing-file error with a bounded real fetch on
  the deployed runtime. Validate an actual source detail page for Craigslist and
  Kijiji before treating either path as operational. Realtor.ca remains explicitly
  unavailable until its browser flow passes; it does not block the other adapters.
- Preserve complete listing-specific text and structured amenities, plus ordered,
  deduplicated gallery URLs. Merge field by field; empty detail metadata must not erase
  discovery content. Detect wrong-unit redirects, removed ads and challenge/error pages
  before calling retrieval successful. Do not bypass existing robots/rate-limit rules.
- Store bounded snapshots privately with source URL/ID, fetch time, content hash and
  adapter/extractor revision. Initially cap extracted text at 100 KB and gallery at
  50 URLs; expose truncation. Keep latest and previous successful evidence plus the
  existing compact history. Do not put raw live pages/contact details in Git fixtures.
- Reuse existing image presentation with a full-gallery viewer, loaded-image failures
  and a clear no-gallery state. Do not infer original photos are absent just because
  retrieval failed. Images use source URLs; unavailable URLs remain a stated limitation.
- Add one purpose-built SQLite work table for explicit detail refresh. Use a single
  local worker with existing source request limits. Web requests enqueue and return;
  network/browser calls never run inside a SQLite write transaction or page rendering.
  Use an explicit managed worker lifecycle, not a detached request task.
- Minimum job fields: listing/source identity, queued/running/succeeded/failed state,
  attempt count, claim expiry, requested/completed times, revision and sanitized outcome.
  Partial/blocked/removed are outcome reasons; stale is derived from time/version, not
  another independent state machine. Repeated clicks join the current job. Expired
  claims recover after restart; cap automatic transient retries at three. Removed or
  blocked pages do not retry indefinitely. Reuse existing network timeouts.
- Publish only a result that still matches the requested source identity/revision;
  reapply the latest corrections and current profile at commit. A late response cannot
  overwrite newer content. On success, refresh facts and scores atomically. Keep the
  previous evidence explicitly stale on failed fetch; a complete successful snapshot
  can retire machine claims it no longer supports, showing the change in history.
- Reuse Slice 1's resolver in watch/CLI and manual refresh. Only after explicit refresh
  is proven, persist discovery before fetching and enqueue automatic enrichment through
  this same path. Expose pending listings to browsing; do not filter them out before
  extracting hard-requirement evidence. Do not build a second parallel watch pipeline.

### Acceptance gate

Automated discovery -> queue -> actual adapter parser -> extraction -> SQLite -> UI
coverage with controlled HTTP fixtures, plus one real successful detail fetch per
operational source. Test process restart, repeated click, rate-limit/timeout, removed
page, incomplete metadata, source identity mismatch and correction during refresh.
Verify deployed worker/browser dependencies and local/Tailscale UI. A failed refresh
must preserve prior evidence and personal state, and a successful refresh must recover
an expected tail-of-description fact and gallery entries from a reviewed fixture.

## Validation, rollout and rollback

- Start with 30 reviewed source snapshots across both cities and working adapters,
  including failures, negations, paid/optional amenities, multi-unit ads and long text.
  This is a regression corpus, not statistical proof. For each newly supported field,
  require at least 20 labelled positive and 20 negative/ambiguous examples before
  publishing precision/recall percentages; report denominators and unsupported cases.
  Keep some examples held out from parser development. Target >=95% precision and
  >=90% recall on supported explicit facts; all known hard-requirement false positives
  are release blockers regardless of aggregate scores.
- Distinguish retrieval success, available-evidence extraction recall, false positive
  rate, gallery recovery and UI correctness. A null-rate reduction alone is not a gate.
  Report source text claims separately from independently verified property facts.
- Dates use source posting/fetch context and city timezone, preserving uncertainty.
  Cost components distinguish included, optional and unknown charges. Never call base
  rent the total cost when utilities/mandatory charges are unresolved. Do not infer
  square footage from a range, or unit floor from the unit number without evidence.
- Snapshot both city DBs with SQLite's backup API before migrations/backfill; store
  rollback artifacts in durable local storage, not /tmp. Use additive schema changes.
  Pin the deployed application revision and test rollback on a copied database.
- Preview backfill changes on copied stores, then a bounded live batch of 10 listings.
  Inspect eligibility changes and user-state preservation before expanding to shortlist
  and active candidates. No unbounded re-fetch of all 1,817 historical records.
- Identify rollback by extraction revision: restore the prior machine projection and
  re-score while retaining corrections/notes made since deployment. A whole-database
  restore after new user activity is disaster recovery, not routine rollback.
- A source outage pauses that source only. Keep incomplete coverage visible, including
  Realtor.ca. Do not claim deployment complete from unit tests or a running worker alone.

## Subsequent slices, only after the initial gates pass

1. **Broader facts:** availability, lease terms, utilities, pets, outdoor space and
   amenities, added as small field groups using the same preview/evidence path. No
   need to implement every planned field before improving current listings.
2. **Floor-plan detection and OCR:** prioritize after gallery recovery as requested.
   Extract labelled area/dimensions with provenance; inferred facts cannot independently
   establish a hard requirement. Render classification and broad visual assessments
   can wait. This does not depend on shipping general text-model integration first.
3. **Optional semantic text fallback:** introduce only when measured misses on complete
   text justify it. Use one provider-neutral interface, strict schema, source evidence,
   limits and caching. Benchmark a small model; route ambiguous cases to review rather
   than automatically adding a multi-model agent hierarchy. Treat source text as data.
4. **Comparable vicinity:** collect gym/grocery evidence consistently across eligible
   candidates. Distinguish source marketing, straight-line distance, walking routes and
   actual gym equipment; do not change the user's weight semantics silently.

Deferred: generic orchestration, offer inventory modelling, image hosting, autonomous
cross-site crawling, research synthesis, broad schema migration, and a new agent tool
suite. After the UI workflow works, expose the same operations through existing CLI/MCP
interfaces without a separate implementation.

## Other app improvements

| Area | Assessment and next improvement |
|---|---|
| Discovery | Surface source health, last successful discovery/detail fetch, and incomplete coverage in the UI. Repair current Craigslist detail errors and establish actual Realtor.ca collection before adding another source. |
| Ranking | Useful explanations and editable criteria exist. Centralize evidence interpretation and display criterion certainty alongside score. |
| Research | Address matching is improved, but excerpts are not a compiled due-diligence report. Keep external research separate from listing-detail extraction; synthesize only corroborated, correctly scoped claims later. |
| Deduplication | Canonical matching exists. Add user-visible merge review/split recovery and distinguish same building from same unit; stock photos alone are insufficient. |
| Hunt workflow | Shortlist, notes, corrections and viewing actions exist. Unify progress/actions where they overlap and preserve list filters/scroll when returning from a detail. |
| Performance | Avoid reconstructing and re-extracting every listing during filtered page reads. Use the resolved projection and indexed query fields after correctness is established. |
| Agent interface | MCP currently exposes watch/rank/list/explain/profile workflows. Add typed enqueue-enrichment/status/evidence/correction tools through the same service used by the UI, with city and stable listing identity explicit. |
| Quality assurance | Existing tests missed realistic false positives and single-image capture. Maintain redacted real-page fixtures and source/field extraction metrics; test precision as well as completeness. |

## Delivery record — 2026-09-06

Implemented the first two vertical slices: saved-content review/apply and durable
source detail refresh from the listing page, with queued discovery enabled through
`watch --queue-details`. The standalone web command manages the worker. No external
agent host or language-model service is required. Corrections take precedence, and
facts and scores publish together. Legacy scores are labelled Needs review until a
current extraction is applied; remaining historical listings are not silently backfilled.

Added a bounded explicit-text field group: availability wording (ISO dates only when
unambiguous), lease duration, included utilities and stated total monthly cost. Unknown
values remain unknown. No inferred totals or dates are presented as established facts.

Validation: 416 automated tests pass, Ruff and Mypy pass, and whitespace checks pass.
A 30-record private source audit found six hard-filter semantic blockers; all six now
have corrected parser outcomes and redacted regressions. The subsequent bounded backfill review also checks explicit car-port parking,
optional basement add-ons and contradictory shared-laundry wording. The audit records
ambiguous and unsupported text. This is not a calibrated precision/recall result.
Browser checks on copied data completed saved-content review/apply and real source
refresh, recovering 19 Craigslist photos and four Kijiji photos. Desktop and 390px
mobile detail pages were visually checked with no horizontal overflow. Automated
workflow tests cover stale previews, failures, retries and correction preservation.
The UI detector used a degraded regex fallback; no full accessibility audit is claimed.

Backups and the previous application wheel are stored locally under
`~/.local/share/nostos/backups/`. The previous application was tested against a migrated
copy before rollout. The original missing-file errors were not reproduced on fresh
source fetches, so their precise historical cause remains unconfirmed.

Still deferred: floor-plan detection/OCR, semantic-model extraction, compiled research
reports, broader automated backfill, and new agent tools. Realtor.ca market coverage
remains unverified. These are subsequent slices, not features completed by this release.


Deployment: the Mac mini runtime was rebuilt from this working tree and both persistent
city web services restarted. Both local and Tailscale roots returned HTTP 200. The
managed production worker refreshed the two inspected Toronto listings successfully
on the first attempt, with 19 and four photos visible respectively. Both six-hour
watch services now use `--queue-details`; reload did not trigger a full collection.
Fresh pre-deployment database and service backups are in
`~/.local/share/nostos/backups/20260906-135517-enrichment-deploy/`.

Bounded backfill: the final exact 10 saved listings were validated on fresh copies,
then applied live (five per city, five per source overall). Source hashes and
eligibility were checked against the reviewed manifest before publication. Eligibility
remained stable for all 10. Fingerprints of user corrections, user state, action history
and hunt progress matched before/after in both live stores. Failed-detail snapshots
were excluded from selection. No further historical backfill was performed.
