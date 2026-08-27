# 08 · Decision log

Decisions taken in the 2026-08-25 design session. Each records what was decided, why,
and what it costs. **If you want to reopen one, say so — do not silently work around it.**

---

### D1 · Rebuild, don't refactor — but harvest the heuristics
**Decision.** New repository. Port the parsing heuristics and their tests; rewrite
everything that touches a file, a database, a browser, or a schedule.
**Why.** The two structural problems — duplicate scraping stacks and seven data stores —
require touching nearly every file anyway, and refactoring in place means keeping a live
6-hourly cron working at every step. A fresh repo also dissolves the committed-PII
problem: never import the data.
**Cost.** Two codebases until parity. Mitigated by the old tool running untouched until R2.
**Reversible?** Not cheaply after R1.

---

### D2 · Citypack and profile are separate objects
**Decision.** City facts (shipped, contributable) split from search preferences (the
user's). `SearchContext = citypack + profile`.
**Why.** The old `CityConfig` bundled them, so changing a budget meant forking a citypack
and two people in one city could not share city data.
**Cost.** Two loaders, two schemas.

---

### D3 · Every field carries provenance; absence is typed
**Decision.** `Field[T] = Observed[T] | Absence`, with an origin precedence ladder.
**Why.** It is how conflicts resolve, how ranking discounts weak values, and how the UI
explains itself. The old ten `unverified_*` booleans reach for this and get it wrong —
`laundry_unverified: -3` fires on units where the question doesn't apply.
**Cost.** ~20 accessor sites in ranking change shape. Detector logic is untouched.
**Not contained** to sourcing/enrichment — deliberately. That containment is the feature.

---

### D4 · Store raw records alongside canonical listings
**Decision.** `SourceRecord` persisted immutably; normalization is a pure function.
**Why.** A parser bug found six weeks in is fixed by replaying storage, not re-scraping
listings that are gone. You cannot retrofit data you never stored.
**Cost.** Storage. Mitigated by payloads expiring first under the retention rule.

---

### D5 · `score` is keyed by `profile_id` from day one
**Decision.** Even though R1 uses exactly one profile.
**Why.** Costs nothing now; without it R6's two-rubric mode is a data migration.

---

### D6 · Ranking ships in R1, merged with text enrichment
**Decision.** Not a separate later release.
**Why.** The wedge is "your criteria over non-standard attributes" — one value
proposition, not two. Ranking over standard fields is a better Zillow sort; enrichment
nobody weights is trivia. The cheap half of enrichment is free regex over listing text.
**Superseded.** An earlier draft had ranking as R2 and enrichment as R3.

---

### D7 · The UI is R2, not R4
**Decision.** Moved up two releases.
**Why.** You cannot run a six-week hunt across a hundred listings from a terminal, and
the UI is where the rubric becomes legible — which is the wedge.

---

### D8 · Open source with optional donations; no hosted tier
**Decision.** Apache-2.0, public, donations via sponsorship. Not a business.
**Why.** Apartment hunting is acute for 6–8 weeks then stops for 1–3 years. Churn is 100%
by design; no retention is achievable. Fatal to a business, irrelevant to OSS.
**Consequence that matters most.** No hosted tier means no commercial scraping at scale —
which removes the single largest legal exposure in the project by construction.

---

### D9 · Single-user MVP; per-person rubrics deferred to R6
**Decision.** The wedge is defining your own ranking criteria over non-standard
attributes. Multi-person weighting is an extension.
**Why.** User's call. An earlier draft over-read "household operator is the user" into
"multi-person preference is the differentiator" — those are different claims.

---

### D10 · Models at the edges only
**Decision.** Perception in (photos R5, fallback text extraction), authoring out
(adapters and citypacks drafted for human review, R4). Never orchestrating a run.
**Why.** A scheduled job whose output changes because the model felt different is a
ranking you cannot reproduce — and explaining the ranking is the product. The old
`scout.py` already replaced an agent loop with a deterministic pipeline, correctly.
**Note.** The old repo contains *zero* LLM providers. OpenClaw supplies infrastructure
(persistent browser, scheduler, message relay), not intelligence. All three have plain
replacements, and swapping them is what makes the tool host-agnostic.

---

### D11 · The agent surface is a thin MCP wrapper
**Decision.** Ship `nostos-mcp` wrapping the CLI. All capability stays in the library.
**Why.** Claude Code, Codex and OpenClaw all speak MCP — one server reaches all three.
And conversation is the best available interface for rubric authoring, which is the
hardest UX problem in the product.
**Constraint.** The moment the MCP server can do something the CLI cannot, there are two
products and the non-agent user has the worse one.

---

### D12 · Two sources in R1, structurally different
**Decision.** One HTML scrape (Craigslist) and one JSON-LD (Kijiji or Zumper).
**Why.** Building the vertical slice against one source lets its shape — RSS prefilter,
path-segment areas, base62 IDs — leak into the `Source` protocol, discovered at source two.
**Cost.** ~30% more work in R1.

---

### D13 · Source relevance is a per-city fact
**Decision.** `enabled` and `load_bearing` flags per source per citypack. No global
required-source set.
**Why.** Craigslist dominates Vancouver rentals; Zillow may dominate Austin. The old
`REQUIRED_SOURCES` frozenset asserts four sources define a healthy run everywhere —
point it at Austin and the health gate never passes and the watermark never advances.

---

### D14 · No ORM
**Decision.** Plain SQL behind repo classes; numbered forward-only migrations.
**Why.** The store is the only thing here that can lose data. It gets the most
conservative machinery available, and the schema stays readable.

---

### D15 · Name and licence
**Decision.** `nostos` (Greek νόστος — the homecoming, the return home after a long
journey; the root of "nostalgia"). Apache-2.0, public repository.
**Why the meaning fits.** The homecoming is what the whole hunt is *for*. The name is
apt without being literal — and being oblique is exactly what keeps the search space clear.
**Naming conventions.**
- Repository and command: `nostos`
- Python distribution: `nostos-cli` (`nostos` on PyPI is taken by an unrelated
  git batch-update tool, v1.7.2 — a real package, not a squat)
- Agent surface: `nostos-mcp`

**Candidates rejected, and the pattern behind them.** Five alternatives were checked
against PyPI, npm and in-domain brand collision:

| Candidate | Verdict |
|---|---|
| `roost` | Free on PyPI, but Roostify (mortgage software) plus two "Roost" companies in property and financial software |
| `nivas` / `nivaas` | Free on PyPI and npm, but 6+ Indian housing platforms — Rent Nivas and Happy Nivaas are rental-listing products directly |
| `nuhaus` | Free on PyPI and npm, but 6+ real-estate and design firms, including NUHAUS Homes in Richmond BC — the same metro |
| `alohomora` | Taken on PyPI and npm, *and* Warner Bros IP. Also poor CLI ergonomics, and naming a scraper after an unlocking charm works against the project's stated scraping posture |
| `perch` · `kestrel` · `vigil` · `vantage` · `winnow` | All taken on PyPI |

**The pattern, recorded so nobody relitigates it:** every name that *means* "home" in any
language is already a housing company. Haus, nivas, roost, casa, domus, hearth — that is
the obvious naming move and thousands of realtors and proptech firms made it first. The
names still available are the ones that do not obviously mean home. If this ever gets
reopened, search obliquely — waiting, watching, choosing, arriving — not house-words.

**On the licence.** Apache-2.0 over MIT for the explicit patent grant and §5's clear
contribution terms; community citypacks and adapters are expected, and it is better
settled in the licence than in a CLA later. Over AGPL because AGPL deters contributors
and would not prevent the concern that motivates it.
**Status.** Confirmed 2026-08-25.
