# Research reports and floor-plan evidence

## Slice 1 — compiled research

A renter opens Research for a saved address and reads a cited building report,
including what remains unknown, to decide what to investigate before a viewing.

The existing Research entry point remains. Compile short claims into building,
management, services, groceries, gyms, parks and safety sections. Keep building,
other-unit and nearby scopes distinct. Show source dates, original excerpts and
citations. Do not infer safety from silence or treat advertising as verification.
Search uses the address and city, never rent or the target unit's commercial terms.
Use the existing bounded provider interface; no agent-host dependency or required
language-model synthesis. A user can exclude an incorrect source and restore it.

Gate: real-address report through the UI, automated request/compiler/store/render
coverage, stale and irrelevant sources excluded, incorrect-source correction works,
provider failure preserves earlier evidence, and no changes to listing/ranking facts.
Validate this slice before beginning floor-plan implementation.

## Slice 2 — floor-plan evidence

A renter examines a listing's images, identifies a floor plan, and sees its printed
dimensions and area alongside the original image before accepting corrections.

Start with bounded image analysis and local OCR. Clearly distinguish a candidate
from a confirmed floor plan and printed labels from verified unit facts. No inferred
measurements, room counts from geometry, layout scores or silent ranking changes.
Allow the user to select/correct which image is the floor plan and refresh analysis.
Unavailable OCR, unreadable images and listings without plans need useful outcomes.

Gate: listing UI through retrieval/OCR/persistence/result rendering, realistic image
fixture and failure cases, bounded manual image verification, desktop/mobile check.
No broad historical image scan, image hosting service or generic agent orchestration.

## Research gate

The report/compiler/store/render and source exclusion/restoration workflow passes.
The captured 15 Mercer Street evidence yields building identity, management and
transit claims; 55 Bremner Boulevard has much sparser accepted evidence. Other-unit
terms, incomplete fragments, marketing and ambiguous aggregate counts are withheld.
These are extractive reports from search excerpts, not full-page verification.
Desktop/mobile browser checks show the report and citations without overflow.

## Delivery and validation — 2026-09-06

Both slices are implemented. Research adds bounded parallel search, an extractive
report, dated citations, scoped claims, verification questions and persistent source
exclusion/restoration. It does not independently verify full source pages. Floor-plan
scanning uses Pillow plus local Tesseract or macOS Vision, with bounded image fetches,
maintained image-host restrictions, and content fingerprints. Printed area and
measurements remain separate from listing facts; unassigned OCR dimensions are not
attached to rooms based on neighboring text alone.

437 tests pass, including real macOS Vision OCR, request/service/store/render workflows,
stale selection, failed scans, source feedback, private-network rejection and unchanged
listing facts. Ruff and Mypy pass. The first eight images in a real 19-photo Craigslist
gallery were scanned in about seven seconds; no candidate was found. A clearly labelled
synthetic plan verified the positive browser workflow, including printed 812 sq ft,
dimensions, confirmation and correction persistence. No synthetic data was added to
production. Desktop/mobile screenshots showed no horizontal overflow. The UI detector
ran in degraded regex mode, so a full accessibility audit is not claimed.

The Mac mini runtime includes the floorplans extra and the packaged Swift helper; the
installed runtime successfully ran Vision OCR. Both persistent city services were
restarted and local/Tailscale listing pages returned HTTP 200, with report and scan
controls present. Production scanning succeeded. Facts, scores, corrections and personal
state fingerprints were unchanged in both cities. Pre-deployment database snapshots
and the previous application wheel are under
`~/.local/share/nostos/backups/20260906-143735-reports-floorplans/`.

Deferred: full-page corroboration, inferred floor-plan geometry, spatial room-label
association, layout scoring, automatic fact application, bulk historical image scans
and hosted semantic/vision models. Sparse evidence and unsupported image hosts remain
visible limitations rather than invented findings.

### Independent UI finish review

Disposition: **ACCEPT for this bounded finish review.** No release-blocking findings.
This was a screenshot/source review; closed disclosures were inspected in templates,
and unavailable/error states were covered by tests rather than additional captures.

| Surface | Verdict | Basis |
|---|---|---|
| Research desktop | PASS | Clear report hierarchy, dates, scope labels, gaps, and no overflow |
| Research mobile | PASS | Sections and rail stack cleanly at 390px |
| Source provenance controls | PASS, capture-limited | Exclude/restore and cited-evidence paths are present; disclosures are closed |
| Floor plan desktop | PASS | Original image, printed OCR readings, decision state, and saved note are visible |
| Floor plan mobile | PASS | Image, measurements, textarea, and actions remain usable and wrap correctly |
| Real eight-photo OCR run | PASS per supplied evidence | Correctly produces no candidate |
| Contrast/detector assurance | INCONCLUSIVE | Degraded regex mode prevents a full contrast claim |
