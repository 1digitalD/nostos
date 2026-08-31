# Contributing to Nostos

Nostos is a self-hosted rental-market watch that ranks listings using a rubric the user
writes. The two contributions that matter most are **citypacks** (make it work in a new
metro) and **source adapters** (make it work against a new site). Both are designed so
you can add one without touching the core.

Read [`docs/README.md`](docs/README.md) before you start — it carries the non-negotiables
this document restates at the end. The reading order table there tells you which design
doc covers what.

---

## The gate every PR must pass

```bash
uv run ruff check . && uv run mypy --strict src tests && uv run pytest
```

All three must be clean. `mypy --strict` covers `tests/` as well as `src/`, so test
helpers need annotations too. CI runs the same three commands on Python 3.11, 3.12 and
3.13.

Getting set up:

```bash
uv sync --all-groups
uv run nostos --help
```

Keep a PR scoped to one thing. Do not refactor adjacent code opportunistically — an
unrelated cleanup in an adapter PR makes the adapter harder to review and harder to
revert.

---

## Adding a citypack

A citypack is city facts: where to search and how this metro's addresses read. It ships
in the repo and is the main contribution surface. It is **not** search criteria — what
someone wants and can pay lives in their profile, which is never committed. The full
schema and the reasoning behind the split are in
[`docs/04-config.md`](docs/04-config.md).

Add `src/nostos/citypacks/<name>.yaml`:

```yaml
name: portland
locale:   { language: en-US, timezone: America/Los_Angeles,
            currency: USD, area_unit: sqft }

areas:                          # the neighbourhood vocabulary
  - key: pearl_district
    label: Pearl District
    keywords: [pearl, "pearl district", "nw 13th"]
    bbox: [45.522, -122.688, 45.536, -122.673]

sources:
  craigslist:
    enabled: true
    load_bearing: true
    base_url: https://portland.craigslist.org
    areas: [mlt, clc, wsc]

address:
  directional: { w: west, e: east, n: north, s: south,
                 nw: northwest, ne: northeast }
  strip_tokens: [portland, beaverton]
  region_tokens: [or, oregon]
```

### `areas[].key` is the neighbourhood vocabulary

This is the rule that matters most. `area_key` on a listing is a **validated string**,
checked against the `areas[].key` values in the citypack — never an enum in code. A
hardcoded neighbourhood tuple in the model is the specific bug that made the predecessor
codebase single-city: any neighbourhood outside the tuple raised `ValueError`.

So: if your city needs a neighbourhood, add it to `areas`. Do not add it to a Python
enum, and do not widen a validator. The `keywords` list is what the pipeline matches
listing text against to assign the key, so include the forms locals actually type,
including abbreviations and cross-streets.

`area_key_weights` in a user's profile references these keys — that is the whole point
of the indirection. Renaming a key is a breaking change for anyone whose profile uses it.

### Adapter sections are optional

Only list the sources that are actually worth running in your metro. A city with no
Kijiji coverage should have no `kijiji` key at all — do not write a stub section for an
adapter your city will never call. An adapter absent from `sources` is simply not run
there.

### `enabled` and `load_bearing` are different flags

They look similar and need different handling:

| Flag | Means | Zero results means |
|---|---|---|
| `enabled` | This source is worth running in this metro | — |
| `load_bearing` | This source returning nothing is an outage | Something broke; the run alerts |

`enabled: false` turns a source off here. `load_bearing: true` says a healthy run in this
metro *must* see listings from it, so zero is a signal rather than a quiet market. A
source failing never fails the run: each reports `ok` / `degraded` / `failed` with a
count, a `load_bearing` source at zero alerts, and a non-load-bearing one at zero is a
line in the report.

Set `load_bearing: true` only for a source you have actually watched return steady
volume in that metro. A global "these four sources define a healthy run" set is exactly
what this replaces — point that at a city where two of the four have no coverage and the
health gate never passes and the watermark never advances.

---

## Adding a source adapter

An adapter is one class satisfying the `Source` protocol in
[`src/nostos/sources/base.py`](src/nostos/sources/base.py). Discovery and detail live on
the **same class** — the predecessor split them across two module trees against the same
sites, which meant two sets of selectors and twice the breakage. See
[`docs/02-architecture.md`](docs/02-architecture.md) for the contract in context.

```python
class Source(Protocol):
    name: str
    capabilities: Capabilities   # credentials? detail? browser? rate_limit

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]: ...
    def fetch_detail(self, rec: SourceRecord) -> SourceRecord: ...
    def check_liveness(self, rec: SourceRecord) -> Liveness: ...
    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing: ...
```

### `to_listing` must be pure

`to_listing` takes a `SourceRecord` and a `SearchContext` and returns a `Listing`. It
must not fetch, must not mutate the record it was handed, and must return an equal
`Listing` every time it is called with the same inputs. The conformance suite asserts all
three: it dumps the record before and after the call and compares, and it calls
`to_listing` twice and compares the results.

That purity is what makes a parser testable offline. All network access belongs in
`discover` and `fetch_detail`, and should go through `build_fetch_text` in
[`src/nostos/sources/http.py`](src/nostos/sources/http.py), which supplies the shared
robots.txt check and per-source token bucket. Take your fetch callable as a constructor
argument so tests can inject a fixture reader:

```python
def __init__(self, *, fetcher: FetchText | None = None) -> None:
    self._fetcher = fetcher or build_fetch_text(
        self.capabilities, user_agent=..., timeout=20.0
    )
```

Declare honest `Capabilities`. `rate_limit_per_minute` should be conservative — Nostos is
a personal tool pointed at sites that did not ask for the traffic.

### Every field carries provenance

Never store a bare value on a `Listing`. Each field is an `Observed[T]` carrying origin,
confidence, evidence and `observed_at`, or a typed `Absence`. And absence is typed:
`NOT_STATED` (the ad did not say) and `NOT_APPLICABLE` (the question does not apply here)
are different facts and must not be collapsed to `None`. See
[`docs/03-data-model.md`](docs/03-data-model.md) — most bugs in this codebase are
provenance or precedence bugs.

### A PR needs recorded fixtures and a passing conformance run

This is not negotiable, and the reason is practical: **reviewers cannot be expected to
hold credentials for your market, or to be able to reach your site at all.** A source
test that needs the network is a broken test. Your adapter has to be reviewable and
maintainable by someone who has never loaded the site.

So a source adapter PR contains:

1. **Recorded fixtures** under `tests/fixtures/<source>/` — real saved pages from the
   site (a search/index page and at least one detail page), not hand-written HTML shaped
   to match your selectors. A fixture you authored to fit the parser cannot detect the
   parser drifting from the site, which is the one thing fixtures exist to catch.
2. **Parser tests** over those fixtures as pure functions.
3. **A conformance case** in `tests/conformance/test_source_conformance.py`, wiring your
   source with a fixture-backed fetcher and calling `assert_source_conforms`. It checks
   the protocol shape, that records carry your source name, `fetch_detail` behaviour
   consistent with your declared capabilities, that `to_listing` is pure, and that
   `listing.raw_ref` round-trips to the record.

```bash
uv run pytest tests/conformance/
```

### Strip contact data from fixtures before committing

Recorded pages routinely contain landlord names, phone numbers, email addresses and
reply links. **Remove them before you commit.** Scraped contact data must never enter
this repository. Redact in place — keep the surrounding markup so the fixture still
exercises your selectors — and check the whole file, including JSON-LD blocks, `meta`
tags and inline scripts, not just the visible text.

Nostos does not contact landlords. Auto-contacting or auto-applying is the line where a
research tool becomes a spam engine, and it is permanently out of scope
([`docs/01-vision.md`](docs/01-vision.md)); the fixture rule is the same principle at the
repository level.

---

## The non-negotiables

From [`docs/README.md`](docs/README.md). Violating any of these is a defect regardless of
whether tests pass:

- **The pipeline is deterministic. No model call in the scheduled run path.** Models
  operate at the edges only — perception in (photos, fallback extraction) and authoring
  out (drafting adapters and citypacks for human review). An adapter that calls a model
  to parse a page during `nostos watch` will be rejected. Using a model to *write* an
  adapter that a human then reviews and commits as deterministic code is the intended
  workflow.
- **Every field carries provenance.** Never store a bare value.
- **Absence is typed.** `NOT_STATED` and `NOT_APPLICABLE` are different facts.
- **The citypack owns the neighbourhood vocabulary.** `area_key` is a validated string,
  never an enum.
- **All capability lives in the library.** The CLI, MCP server, UI and scheduler are
  callers. None may do anything the library cannot — so add the capability to the
  library and have the caller call it.
- **Nothing paid runs without consent.** Estimate, ask, cap. The free provider is the
  default for every paid capability.
- **Listing facts are replaceable; user state is not.** Archival never touches a listing
  someone shortlisted, contacted or viewed.
- **No credentials in config files.** Ever. Not in a citypack, not in a profile, not in a
  fixture, not in a test. Credentials live in the OS keychain and are referenced by name.
  A citypack is a public, committed file.

If a decision in [`docs/08-decisions.md`](docs/08-decisions.md) looks wrong, say so in
your PR or an issue — those decisions have recorded rationale and cost. Do not silently
work around one.
