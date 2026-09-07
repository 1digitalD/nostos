# Saved-evidence pilot: implementation and validation

## User job and entry point

A renter opens a saved listing, sees which requirements are supported or unresolved,
and corrects a fact to update the assessment without editing a database.

Start at the normal listing detail page. The brief links to saved evidence,
extraction review, source refresh where supported, fact correction, and criteria.
The same read-only assessment is available as `nostos decision LISTING_ID --json`
and the MCP `decision` tool. Existing profile, database, and citypack options apply.
No AI connection, new account, paid request, or scheduler is required for this pilot.

## What changed

- Typed requirement checks provide reasons and evidence without parsing human
  explanation strings. Processing eligibility remains separate from evidence certainty.
- Explicit false laundry is a failure, optional furnishing is not furnished-only,
  and mismatched currency/period or contradictory evidence cannot support a positive brief.
- Unknown basement/furnishing remain visible rather than being treated as established facts.
- Source capture age, incomplete extraction, exclusion, and dismissal constrain the brief.
  The 24-hour age guard is a presentation policy, not the refresh schedule or proof of availability.
- Narrow saved-text guards flag conflicting bedroom counts and room/shared-unit scope,
  including the sampled bedroom-plus-office and private-room cases. They retain quotes,
  do not invent replacement facts, and respect explicit per-field user corrections.
- Basement, furnishing, and entire-unit corrections use the existing correction and reset path.
- Parser fixes recover saved parking/area evidence and reject zero-area placeholders.
  Existing saved extraction revisions require re-review; no live data was bulk rewritten.

## Why this is deliberately not the full agent feature

The [20-listing evaluation](18-decision-evaluation.md) found useful saved evidence,
but also over-precise and conflicting claims. This pilot is an evidence assessment
and correction workflow—not semantic understanding, verified advice, or a viewing recommendation.
Pattern guards are incomplete, especially for unusual negation, area bounds, and multi-unit ads.
The remaining model-assisted interpretation proposal is still in
[the delivery plan](17-agent-delivery-plan.md).

Deferred, not silently completed:

- Reviewed model proposals with quote validation, accepted/rejected state, and safe undo.
- API-key onboarding, official local Codex/Claude Code connections, budget controls,
  provider disclosures, and paid-action authentication.
- General research tool loops, unattended model enrichment, and agent-based ranking.
- Realtor.ca access provisioning/licensing and collection automation.
- Promotion to the persistent live services. This branch is a pilot; local browser
  validation used an isolated fixture store, not the user's listings.

## Acceptance and next decision

Validation on 2026-09-06:

- Full suite: **481 passed** (baseline before this work: 437 passed, one UTC-date fixture failure).
- Strict mypy: **113 source/test files passed**. Ruff and diff whitespace checks passed.
- Independent stronger-model review: **GO for this bounded pilot** after three concrete
  integration blockers were fixed; 97 focused tests passed in that final review.
- Chromium desktop (1440px) and mobile (390px) checks exercised correction submission,
  conflict badge/brief agreement, and horizontal-overflow checks. Two bounded screenshot
  rounds preserved the incumbent UI under Impeccable guidance.
- The sample preview uses a temporary fixture database on port 8439. It is not a
  persistent deployment and contains no live user listings.

Required gates: detail-to-correction-to-reset persistence; conflict-to-correction-to-
re-extraction preservation; explicit negative/unknown/unit-mismatch cases; read-only
CLI/MCP queries; full regression suite and static checks; bounded desktop/mobile check.

Go: the evidence/clarification workflow is useful without a model connection.
No-go: do not market a generalized agent brain or recommend viewings from unreviewed
parser claims. The next adjoining slice is one user-requested saved-text interpretation,
with quoted proposals and selective acceptance—not background autonomy.

Before expanding, dogfood the normal detail/review/correction journey on representative
saved listings and confirm that the explanations improve the renter's decision. Evaluate
the model slice against the same redacted failure cases plus independently reviewed labels;
report useful recovery and harmful claims separately, not only aggregate accuracy.
