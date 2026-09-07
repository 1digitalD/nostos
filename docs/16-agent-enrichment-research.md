# Agent-assisted enrichment and research for Nostos

Research date: 2026-09-06. Recommendation, not an implementation or benchmark.

Second-pass review: [the reviewed delivery plan](17-agent-delivery-plan.md) is the
implementation reference for sequencing, setup, publication, and acceptance gates.
It keeps subscription support in scope while staging each connection, explicitly
addresses database locking and accepted-fact survival, and strengthens the quality
gate with recall and beginner usability tests. This document retains the research
and framework comparison.

## Recommendation

Add an optional, bounded reasoning capability for interpreting listing evidence
and researching unresolved questions. Start with PydanticAI's slim Python package
for API-key execution. Offer an official local Codex runtime adapter for subscription
users, and an unmodified Claude Code runtime connection with Anthropic-owned login.
Keep these execution backends behind the same typed task/result contract.

Nostos should retain ownership of saved evidence, jobs, fact resolution, user
corrections, eligibility, and numerical ranking. Model-derived facts can influence
ranking after validation and publication. A separate narrative comparison can
explain tradeoffs without silently changing the rubric or its scores.

This is valuable for ambiguous prose, unit-versus-building context, extracting
terms from user-supplied documents/images, and deciding which source to consult
next. A fixed extraction task often needs one structured model call; a research
task benefits from a short tool loop. Neither requires multiple agents or a
continuously running conversation.

## Fit with current code

Verified in this checkout:

- Python 3.11+, FastAPI, and Pydantic are already core dependencies
  (`pyproject.toml`).
- `enrich/review.py` has saved-source snapshots, preview/apply, stale-input
  guards, extraction revisions, and preservation of user corrections.
- `enrich/refresh.py` has a durable SQLite queue and claim/retry behavior, but
  its table and implementation are specifically for source-detail refresh.
  Reuse the operational pattern; do not pretend arbitrary research jobs already
  fit the table unchanged.
- `web/research.py:ResearchProvider` provides bounded structured search. A model
  key alone does not automatically supply its current Perplexity search service.
- `model/listing.py:Origin` has user, source, detail, text-rule, geo, and vision
  origins; there is no semantic-model origin yet. Model claims need explicit
  provenance rather than impersonating source fields.
- `enrich/budget.py` reserves estimated spend and requires a cap and confirmation
  callback. It is not a durable, cross-worker daily usage ledger or an actual
  provider billing reconciler.
- Web reconstruction can rerun text rules when a current extraction is missing.
  A paid agent must not be added indiscriminately to that read path.

The existing pieces are a useful foundation, but do not yet provide the proposed
agent workflow, arbitrary document ingestion, agent credential onboarding, or
model-quality evaluations.

## Comparable public implementations

The following were inspected through public code/documentation during this
research, not executed. They are examples of implementation patterns; sustained
production operation and Canadian coverage were not established.

| Project | Actual setup | Lesson for Nostos |
|---|---|---|
| [property-search-agent](https://github.com/jis478/property-search-agent) | Australian Domain.com.au rentals; LangGraph, allowlisted Playwright MCP tools, FastAPI streaming, Google Maps walking-distance enrichment | Closest workflow analogue. Separate browser discovery from deterministic geographic calculations. Its error paths can discard listings or disable a filter; Nostos should preserve explicit unknowns. |
| [awesome-llm-apps real-estate team](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/advanced_ai_agents/multi_agent_apps/agent_teams/ai_real_estate_agent_team) | Streamlit, Agno/Gemini, Firecrawl extraction from US portals including Realtor.com; subsequent analysis/valuation calls | Borrow structured extraction and progress. Market analysis without retrieved market evidence is speculative. Realtor.com is not REALTOR.ca. |
| [AI-Real-Estate-Search-Agent](https://github.com/findsaddam/AI-Real-Estate-Search-Agent) | Indian property sales, Firecrawl/Pydantic extraction and Agno analysis | Illustrates extract → analyse; provider configuration inconsistencies in the inspected code make it an example to learn from, not a turnkey dependency. |

None establishes reliable REALTOR.ca integration. The common useful pattern is
retrieval → typed extraction → analysis, even when the UI describes several agents.

### REALTOR.ca-specific acquisition candidates

- [Apify igolaizola/realtor-canada-scraper-ppe](https://apify.com/igolaizola/realtor-canada-scraper-ppe)
  advertises buy, rent, and sold records with listing details and media.
- [Apify fatihtahta/realtor-canada-scraper](https://apify.com/fatihtahta/realtor-canada-scraper)
  advertises structured REALTOR.ca property/contact/media extraction; rental
  coverage was not verified.
- [Apify MCP](https://github.com/apify/apify-mcp-server) exposes Actors to agents.
  For fixed scheduled discovery, a direct Actor/API adapter is simpler than
  paying a model to choose and invoke the same scraper each time.
- [Firecrawl Browser](https://docs.firecrawl.dev/features/browser) and
  [Firecrawl MCP](https://docs.firecrawl.dev/mcp-server) provide managed browsing
  and agent integration. REALTOR.ca compatibility was not demonstrated.

These are vendor claims and documented interfaces, not live-verified results.
Before adopting an Actor, test a small Toronto rental query for rental-versus-sale
accuracy, unit identity, pagination, freshness, detail/media completeness, cost,
and update/removal handling. Check the terms applicable to the source and Actor;
an MCP integration alone supplies neither data rights nor coverage guarantees.
Do not purchase or upload private criteria as part of this research.

The official [CREA DDF route](https://www.crea.ca/technology/realtor-ca-for-realtors/realtor-ca-tools/realtor-ca-ddf/)
remains an alternative conditional on access. For a personal trial without feed
access, a bounded Actor comparison is a concrete next experiment; an agent
framework does not resolve the underlying source-access problem.

## Framework comparison

| Option | Fit | Recommendation |
|---|---|---|
| PydanticAI slim | Python, typed outputs, dependencies, tools, multiple providers, run limits | Default embedded API-key implementation |
| Official Codex SDK/runtime | Complete local agent, managed ChatGPT login, sessions/events; current docs include a stable Python SDK | Optional local subscription execution backend |
| Claude Agent SDK / unmodified Claude Code | Complete tool loop; Python/TypeScript APIs and CLI structured execution | API-key SDK integration, or distinct user-owned native runtime connection |
| OpenAI Agents SDK | Lightweight Python agent/tool loop, structured workflows, sessions, tracing, other providers | Credible alternative if OpenAI-native tools become the dominant requirement |
| LangGraph | Explicit stateful graphs, persistence, interrupt/resume | Reconsider when a demonstrated workflow needs multi-stage checkpointing; unnecessary for the first bounded job |
| Pi agent core | Small TypeScript agent runtime with context transformation, tools, events | Interesting if adopting a TypeScript runtime; introduces another language/process to the current Python application |

[Instructor](https://github.com/567-labs/instructor) is an even narrower option
for extraction-only jobs with Pydantic response validation. It becomes less
compelling as an additional dependency if PydanticAI already supplies the
structured output and tools needed for the next research slice.
[OpenCode's server](https://opencode.ai/docs/server/) offers an HTTP interface to
a full agent harness. It is useful as an optional external client/runtime, but
is not necessary to embed the first reasoning task. Third-party runtime login
claims must be checked against the provider's current official authentication
rules; this report uses official Codex and Anthropic guidance below.

PydanticAI exposes typed outputs and validation, provider-specific model adapters,
request/tool/token limits, and optional integrations. Use `pydantic-ai-slim` with
only selected provider extras; hosted tracing is not required. Schema validity
does not establish factual accuracy. These capabilities are documented in
[agents][1], [output][2], and [installation][3]. LangGraph's own description
emphasizes long-running stateful orchestration [4]. Pi documents its separate
agent core and message/context boundary [5]. OpenAI's SDK documentation describes
its lightweight primitives [6]. These are architectural comparisons, not measured
memory/latency rankings.

Do not nest a full Codex/Claude agent inside a PydanticAI tool loop for ordinary
extraction. Either PydanticAI owns the model/tool iteration for an API-key job,
or the official runtime owns it for a local-runtime job. Nostos owns the outer
job lifecycle in both cases.

## API keys and subscriptions

| User connection | Supported integration direction | Qualification |
|---|---|---|
| OpenAI API key | Direct provider through PydanticAI, or official SDK | API usage billed separately from ChatGPT subscription |
| Anthropic API key | Direct provider through PydanticAI or Claude Agent SDK | User owns and pays for API usage |
| ChatGPT/Codex subscription | Official local Codex runtime; managed browser/device login | Subject to plan limits; official auth docs recommend API keys for automation |
| Claude subscription | User signs into the unmodified Claude Code binary through Anthropic's own flow | Do not build a generic Claude.ai-login/token proxy in Nostos; ordinary individual usage limits apply |

OpenAI explicitly documents ChatGPT and API-key authentication, managed token
refresh, and separate API billing [7]. The current SDK docs include Python
`openai-codex` as well as TypeScript [8]. App-server exposes managed login,
account state, and rate-limit events for custom clients [9]. Prefer the SDK/local
stdio route for the initial job adapter; avoid basing the first release on
experimental remote transports or externally managed tokens.

Anthropic's current policy expressly distinguishes running its unmodified binary
from offering your own Claude.ai sign-in or routing user subscription credentials.
It permits platforms to run unmodified Claude Code with users authenticating and
paying directly, subject to the stated terms, while forbidding developers from
collecting/storing/intermediating Claude.ai credentials [10]. The product must
preserve the binary's native authentication methods. Its headless docs describe
`claude -p` and structured output; importantly, `--bare` does not use subscription
OAuth/keychain login and instead requires API/provider credentials [11].

Therefore a consistent task UI is feasible, but the connections are not
interchangeable API keys. Login, available models/tools, limits, and capability
checks differ. Show connected/disconnected/limit-reached states and explicit
reconnect actions. Do not automatically switch a subscription job to a paid API
or a different data destination without a configured user choice. Account sign-in
must happen on the host where the runtime executes; another desktop session's
login does not guarantee the service has credentials.

API-key mode also needs a search choice: use an already configured search service,
or a supported provider-native search tool. Display any additional requirement
and cost. A model credential neither grants access to REALTOR.ca nor guarantees
that a web page can be retrieved.

## Proposed task design

1. Save the source evidence with its URL/document identity, capture time, content
   hash, and unit/building scope. Extract available text/OCR locally first.
2. Dispatch only when unresolved facts justify a model call, the user requests
   research, or genuinely new evidence arrives.
3. Supply a compact context packet: target listing/unit, existing sourced facts,
   user corrections, unresolved questions, and relevant preference definitions.
   Preferences identify the question; they must not bias the extracted answer.
4. Allow a small set of tools such as reading saved evidence, searching for the
   exact address, and retrieving an allowed public source. Documents are data,
   not authority to change criteria or run arbitrary commands.
5. Return structured candidate facts and unresolved questions. Each claim carries
   evidence references, quote/page or image-region references, entity scope,
   uncertainty, source time, and model/prompt version.
6. Validate references and field semantics; preserve conflicts and unknown values.
   Review proposed changes and their ranking effect in the existing UI. Apply
   through the current publication/correction rules, then compute the score.

Example: “Parking available for $150; tenant pays hydro; building laundry”
can yield parking available with a separate $150 optional charge, hydro amount
unknown, and shared building laundry. It must not become included parking,
known all-in rent, or in-suite laundry. If two units are advertised together,
retain ambiguity until the fact can be tied to the selected unit.

For ranking, the model may extract contextual features, explain comparisons,
or propose a preference patch for preview. Numerical rank remains a function of
published facts and a fixed profile revision. Subjective features such as “quiet”
or “good layout” need their own definitions and evaluation before affecting rank.

## Execution, cost, and context

- Reuse the current scheduler and per-city SQLite storage. Add only the job state
  needed for the next named use case; no second scheduling platform is necessary.
- Cache semantic extraction by evidence hash, relevant context, model, prompt,
  and schema revision. Identical evidence should not trigger another paid call
  on every six-hour discovery or page view.
- Separate building research from unit facts; building reports can be reused
  across listings at that address without borrowing another unit's amenities.
- Start extraction with one call and at most one repair attempt; research with a
  proposed maximum of six model turns and five retrieval calls. These are initial
  design limits to evaluate, not framework defaults or measured optima.
- Record tokens, latency, provider usage, search charges when available, failure
  reasons, and actual published output. Enforce concurrent budget reservations;
  a process-local cost counter is insufficient for daily spend caps.
- Keep durable context and traces local by default, and send only the selected
  evidence to the configured model. Hosted tracing is optional and may contain
  listing/user content. Explicit remote inference still sends that selected data.
- Resume from durable task/result state after failure. Revalidate evidence and
  profile/correction revisions before publishing a result completed later.

## First functional slice and acceptance gate

User job: a renter opens a saved listing with ambiguous source text, selects
“Interpret listing details,” reviews cited proposed facts and the match/score
effect, and applies or rejects the changes to make a better viewing decision.

Start with saved text and one API provider. Reuse the listing detail entry point,
preview/apply, and corrections. Demonstrate login/key failure, no supported facts,
contradictory evidence, correction during execution, budget exhaustion, and a
retry without double publication. The adjoining slices are local-runtime parity,
PDF/image import, and bounded building research; do not claim these are shipped
with the first text interpretation slice.

Proposed go/no-go gate: a reviewed set of 30–50 realistic tricky excerpts; at least
95% precision on proposed populated fields and at least 80% recall on labelled
answerable target fields, with recovery beyond the parser baseline; zero unsupported
hard-filter passes in the test set;
no overwritten user corrections; evidence attached to every accepted claim;
unchanged-input cache reuse; and an automated UI → worker/provider boundary →
SQLite → preview → apply → ranking test. Use fixtures for repeatable E2E and a
bounded live provider run for actual model quality/cost. Compare against today's
parser baseline and have the operator inspect 5–10 listings. If errors or cost
fail the gate, keep model suggestions review-only and refine the narrow task.
Passing this small pilot set is not a guarantee of production accuracy. The
reviewed delivery plan adds transaction/publication, failure, and usability gates.

## Sources

[1]: https://pydantic.dev/docs/ai/core-concepts/agent/
[2]: https://pydantic.dev/docs/ai/core-concepts/output/
[3]: https://pydantic.dev/docs/ai/overview/install/
[4]: https://docs.langchain.com/oss/python/langgraph/overview
[5]: https://github.com/badlogic/pi-mono/tree/main/packages/agent
[6]: https://github.com/openai/openai-agents-python
[7]: https://developers.openai.com/codex/auth/
[8]: https://developers.openai.com/codex/sdk/
[9]: https://developers.openai.com/codex/app-server/
[10]: https://code.claude.com/docs/en/legal-and-compliance
[11]: https://code.claude.com/docs/en/headless
