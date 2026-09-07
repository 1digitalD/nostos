# Agent assistance: reviewed delivery plan

Reviewed 2026-09-06 against the current checkout. This is a proposed implementation
plan; no agent feature, installer, credential connection, or live model evaluation
has been implemented by this review. This plan controls sequencing and acceptance
where it differs from [the research comparison](16-agent-enrichment-research.md).

## Product decision

A renter opens a saved listing, asks Nostos to interpret unclear details, reviews
the supporting text and proposed facts, and applies useful changes to see an
updated match and score. The same job must work from the browser, CLI, and agent
interface through one library service.

Use PydanticAI slim for API execution, starting with one structured call and one
optional repair. Retain the existing scheduler, per-city stores, parsers, and
deterministic scoring. Add a tool loop only when the building-research slice needs
it. Local Codex and Claude Code connections remain required adjoining slices for
the requested subscription experience, not a promise satisfied by an API-only demo.

Default to user-requested interpretation and reviewed publication. Ordinary
discovery, browsing, correction, and ranking continue when AI is disconnected.

## Review findings and resolutions

| Gap in the first proposal | Required resolution |
|---|---|
| `enrich/review.py` calls enrichers inside `BEGIN IMMEDIATE`; a model call could hold the write lock | Run inference outside transactions. Persist proposals, then perform short local preview/apply transactions. |
| Parser publication replaces the active extraction snapshot; adding a semantic origin alone cannot preserve accepted AI facts | Persist claim/evidence associations and review decisions; integrate them into the shared resolver used by watch, browsing, corrections, and rescore. |
| Existing refresh jobs only support queued/running/succeeded/failed and immediately apply source facts | Use a small purpose-specific interpretation job/result record with review and cancellation semantics. Do not overload source refresh or generalize it into a workflow engine. |
| Process-local estimated budgets cannot enforce a cap shared by both city services | Persist atomic reservations and usage by connection across those services. Count repairs, retries, and uncertain billing outcomes. |
| The web UI has no application-level owner authentication; the footer says “Data stays on this host” | Protect credential and paid-action routes, reject cross-site requests, and disclose the actual remote model/search destination. |
| A 95% precision target permits an agent that returns almost nothing | Measure useful recovery/recall against the existing parser and report hard-filter errors separately. |

Code anchors: `enrich/review.py:261`, `watch/runner.py:666`,
`store/repo.py:306`, `rank/rescore.py:80`, `enrich/refresh.py:223`,
`enrich/budget.py:30`, `web/templates/base.html:35` (line numbers at review time).

## Setup and everyday use

### Browser journey

Preserve the existing listings/criteria layout and visual system. Add an “AI
assistance” settings entry and an “Interpret unclear details” action beside
saved-content review. Explain how it differs from “Update listing details,” which
fetches the source. Do not combine the actions into an invisible paid fetch.

1. **Choose a connection.** Offer “Use Codex on this computer,” “Use Claude Code
   on this computer,” and “Use an API key,” plus “Continue without AI.” Show only
   working choices as available. Recommend an already detected supported local
   connection; otherwise guide API setup with provider links and an explanation
   that API charges are separate from a chat subscription. Framework names and
   model lists belong under Advanced.
2. **Connect and test.** Native subscriptions use the provider's own sign-in.
   API keys use a masked field and local OS credential storage. “Test connection”
   runs a tiny disclosed synthetic task under a small request limit, after the
   user accepts its displayed API allowance or subscription usage; it verifies
   actual schema output from the service host, not just credential presence.
   Show the service computer, account label where supported, usable capability,
   and whether it consumes subscription allowance or API spend.
3. **Set usage preferences.** Start with “Only when I ask.” For API usage require
   a clearly labelled spending allowance before a paid request. For subscription
   usage explain shared plan limits. State that selected listing text goes to the
   chosen provider; personal notes/contact details are excluded by default.
   Setup does not initiate collection or bulk enrichment.
4. **Get a first result.** Return to the originating listing. If there is no usable
   saved text, explain how to update or add it. An optional labelled example uses
   the same interpretation/review workflow without polluting the live shortlist.
   Do not require a search-provider account for saved-text interpretation.

Connection errors offer one relevant recovery action: finish sign-in, replace
key, install the supported runtime, start the service, wait for a limit reset, or
continue without AI. Never expose stack traces as the primary message. Connection
tests, result status, and setup choices must work on mobile and with keyboard and
screen-reader navigation; no drag-only controls or color-only statuses.

### Review journey

Show “3 suggested changes,” an Old → Suggested comparison, supporting quotes,
and a plain explanation of eligibility/score impact. Allow accepting selected
changes, editing a fact, or rejecting suggestions. Unsupported/contradictory facts
stay unknown or conflicting; never present model self-confidence as “verified.”
Accepting a machine suggestion retains its machine provenance. Editing a fact
creates an explicit user correction. Existing user corrections remain authoritative.

Persist progress so leaving/reloading the page rejoins the same job. Offer Cancel
while running. Distinguish “No supported changes” from failure, and “Ready to
review” from already applied. Retry and repeated Apply cannot double-publish.
Provide Undo for an applied suggestion batch, preserving any newer corrections;
when state has changed, explain the affected facts and recompute locally rather
than restoring a stale whole-listing snapshot. Rejection remains remembered for
the same evidence/model revision; changed evidence can generate a new proposal.

### Technical users and installation

Expose the same connect/status, interpret/status/cancel, preview/apply/undo library
operations through CLI and MCP, with JSON output and stable listing references.
Read-only actions never invoke a model. Environment/key references are supported
for unattended API installations; scripts do not require browser form automation.
These are proposed operations, not existing commands.

For an existing deployment, the browser flow requires no terminal work. For a
fresh nontechnical installation, first certify the current macOS target with a
versioned launcher/install path that checks dependencies and storage, starts the
service, opens the UI, and offers visible start/stop/update and redacted diagnostics.
Automatic startup is an explicit choice. A clean-machine check is a release gate;
`pip install` instructions alone do not satisfy nontechnical setup. Keep this a
thin launcher around the web app; no Electron rewrite or cross-platform installer
matrix. Signing/distribution and unattended credential access must be validated
before claiming frictionless installation. Other hosts retain documented technical
setup until individually tested. Preserve configured data locations on upgrade.

## Minimum implementation contract

### Evidence and publication

Store a bounded interpretation result with immutable input snapshot references,
entity/unit scope, per-field value or abstention, quotes/offsets, provider/model,
prompt/schema version, usage, and accepted/rejected decisions. JSON columns and
existing evidence storage are sufficient initially; no vector store or knowledge
graph. Do not rely on `Observed.detail` alone: current projection reconstruction
does not preserve that metadata.

Use a distinct semantic origin below explicit source facts and user corrections;
conflicting higher-precedence claims require explicit correction/review. Support
absence, field units, advertised ranges, optional fees, and uncertain dates rather
than forcing them into unsupported scalar facts. Scope the first slice to current
fact fields; show extra costs or ranges as cited findings until the schema can
represent them correctly. Do not silently alter personal ranking preferences.

An accepted claim remains usable across byte-equivalent evidence rediscovery,
rescore, and restart. New relevant evidence marks affected claims stale and
recomputes eligibility consistently, retaining the old evidence for inspection.
Unchanged supporting evidence may retain its accepted claims. Never leave a
positive score based on retired facts. Cover watch, refresh, review, corrections,
and rank with one resolution rule; do not implement independent UI merging.

Separate the inference cache from the review token. Cache by semantic input,
listing/unit identity, exact supplied context, provider/model, and prompt/schema
revision. Use explicit task/field definitions instead of the entire mutable user
profile where possible. A profile change normally recomputes the preview/score
without another paid call. Source/correction/profile changes must still be checked
at apply time. Do not reuse results across cities or infer shared unit amenities
from a common building address.

### Jobs, budgets, and failures

Use ordinary job states (queued, running, ready to review, applied, cancelled,
failed), with stale input or connection/budget problems expressed as actionable
outcomes. Keep the last usable listing visible throughout. Do not introduce a
state-machine framework merely to encode these states.

Reuse the existing web service lifecycle, with a bounded AI worker lane separate
from source-detail work. Persist one purpose-specific job/result record per request
in its city store. Deduplicate identical active requests. Start with a host-wide
concurrency limit of one model job, using a small shared SQLite connection-usage
ledger for reservations/limits across city workers. That ledger contains operational
references and usage, not a combined listing store. No new broker or service fleet.

Set a job deadline and request/output limits covering the initial call and repair.
Use provider retry guidance for transient failures; authentication, exhausted
allowance, invalid configuration, and rejected evidence require distinct outcomes.
Keep every retry within the original approved budget. API spend estimates use
the provider's billing currency, separate from CAD rental prices. Show Nostos's
tracked usage as an estimate for this app, not the user's entire provider bill.

Cancellation/restart invalidates the worker's publication token so late results
cannot apply. A timeout may still be billed: retain an uncertain reservation and
do not immediately submit a duplicate request. Resume from saved output if it
exists. Exactly-once publication is required; exactly-once provider billing is
not a guarantee. Make uncertain outcomes visible with an explicit retry action.

Persist cost records and bounded evidence locally. Exclude credentials and private
notes from diagnostics; provide result/history cleanup that preserves references
needed by currently accepted claims. Disable hosted tracing by default. Runtime
transcripts and crash logs also need retention controls, not just application logs.

### Connection boundary

Keep a small capability contract: connection status, supported input/output types,
run/cancel, structured result, usage/limit status. Do not introduce a plugin registry
or universal model router. Each backend passes the same workflow acceptance tests;
unsupported modalities remain visibly unavailable. Pin tested dependency/runtime
versions and rerun adapter tests on upgrade.

Use OS-managed credentials, never YAML/source files/browser local storage for keys.
If the service cannot access the user's keychain, explain the host/session issue;
technical users can explicitly configure supported environment credentials. Never
silently fall back to plaintext or forward a subscription credential to a model API.
Do not automatically switch billing mode, provider, or data destination.

Protect new credential, connection-test, and paid-job routes with a minimal owner
session and same-origin/CSRF checks, also covering the existing paid research
action. A locally provisioned owner credential/session is sufficient for the
single-operator scope; no multi-user account platform is needed. Trusted private
HTTPS access is still necessary for remote use; being on the same network is not
an owner identity. Reject untrusted Host/Origin values and avoid logging secrets.

For Codex/Claude, use a dedicated task workspace and enforce tool/filesystem/network
restrictions through supported runtime controls. Do not inherit unrelated project
hooks, plugins, MCP servers, or broad shell access into listing interpretation.
Reject unsupported configurations instead of trusting prompt instructions as a
security boundary. The Claude subscription mode cannot simply use `--bare`, which
the inspected docs say bypasses subscription credentials; this needs a specific
integration test. Source text is untrusted input even in a user-supplied document.

## Delivery order and acceptance

| Slice | Visible result and gate |
|---|---|
| 1. Interpret saved text | One API provider, browser connection test, capped job, evidence preview, selected apply/reject/undo, deterministic re-score. Internal pilot only. |
| 2. Connect subscriptions | Same workflow through official local Codex, then unmodified Claude Code, with native login, limits, cancellation, and enforced permissions. Validate the chosen runtime under the actual service account. |
| 3. Complete setup | Remaining OpenAI/Anthropic API connection, beginner installation path, mobile/keyboard recovery, restart/upgrade checks. Only now describe the API-or-subscription experience as supported. |
| 4. Interpret supplied documents | Paste text first, then bounded PDF/image input with type/size limits and cited page/image evidence, uncertain OCR and unreadable-file handling. Reuse review/publication. |
| 5. Research a building | Small tool loop with configured search, exact-address matching, source freshness, bounded retrieval, and cited report. Reuse building evidence, preserving unit scope. |
| 6. Opt-in automation | Enable capped enrichment on new/changed evidence and selected listings after quality is established. Default to queued suggestions; automatic application is a separate evaluated policy. Show last success/backlog/failure without noisy unchanged notifications. |

Each slice must be usable and validated before the next. Source acquisition work,
including a REALTOR.ca Actor trial, is an independent dependency: no agent runtime
can compensate for missing source access. Do not restrict interpretation only to
listings already passing hard filters; unknown or incorrectly parsed listings are
often the ones that need it. Later automation prioritizes shortlisted and unresolved
listings without discarding other saved research.

For slice 1, label 30–50 realistic excerpts including ambiguous/negated amenities,
multiple units, fee/range/date uncertainty, prompt-injection text, and missing data.
Evaluate against the current parser, separately for hard-filter fields. Proposed
gates: at least 95% precision on supported populated fields, at least 80% recall
on labelled answerable target fields, and measurable recovery of parser-missed facts;
zero unsupported hard-filter passes or overwritten corrections in this test set.
These pilot thresholds are not a statistical reliability guarantee. Report the
denominators, abstentions, latency, and cost per useful recovered fact for each
backend; a schema-valid or all-abstain result cannot pass.

E2E covers connection → worker/provider boundary → persisted proposal → selective
review → apply → score → undo, plus slow-provider concurrent correction, double
click, changed evidence, cancellation, restart, uncertain provider completion,
budget limits across cities, and accepted-fact survival after refresh/rescore.
Use deterministic provider fixtures and a bounded live sample. Require an operator
to inspect 5–10 actual listing outcomes before expanding automation.

Usability gate: one technical and one nontechnical tester independently connect,
interpret, inspect evidence, correct/reject a suggestion, and recover from a failed
connection. Target a first result within five minutes once Nostos/runtime and the
chosen account are available; separately report installation/sign-up time. The
nontechnical tester must not need a terminal or unexplained model/job identifiers.

Deferred: multiple collaborating agents, autonomous preference changes, subjective
layout ranking, general browser autonomy, automatic provider fallback, shared chat
memory, a new scheduler, hosted tracing infrastructure, and a plugin marketplace.
Core provenance, usable onboarding, cancellation, spending controls, and correction
paths are required work and cannot be cut to meet the first-slice date.
