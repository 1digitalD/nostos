# 06 · Roadmap

Six releases, each useful on its own. A release is done when its **ship gate** passes.

## Subsystems by release

| Subsystem | R1 watch+rank | R2 triage | R3 vicinity | R4 anywhere | R5 photos | R6 two rubrics |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Model & store | ● | ◐ | ◐ | | | ◐ |
| Sources | ● | | | ◐ | | |
| Schedule & alerts | ● | | | | | ◐ |
| Ranking | ● | ◐ | ◐ | | | ◐ |
| Enrichment chain | ● | | ◐ | | ◐ | |
| Interface | | ● | | | | ◐ |
| Onboarding tools | | | | ● | | |
| Vision | | | | | ● | |

● comes online · ◐ extended

---

## R1 · Watch and rank

> "Point it at your city, say what you actually care about, and every few hours it tells
> you what's new — in your order, with its reasons."

**Scope.** Two sources (one HTML scrape, one JSON-LD — a single source lets its shape
leak into the protocol). Canonical model + store. Text enrichment. Rubric with the setup
wizard. Scheduled watch. One notification sink. CLI only.

**Why ranking is in R1 and not later:** the wedge is your criteria *plus* non-standard
attributes, and those are one value proposition, not two. Ranking over price and beds is
a better Zillow sort; enrichment nobody weights is trivia. The cheap half of enrichment —
laundry, den, floor-from-unit-number, parking — is pure regex over listing text with no
API and no cost, so it ships here.

**Ship gate.** Someone who isn't you installs it, answers the wizard, and within the same
sitting receives a ranked new-listings message for their own city that they can explain
without reading code.

---

## R2 · Triage and track

> "Yes, no, maybe. Then contacted, replied, viewing booked."

Browse, sort, filter, shortlist, exclude, contact pipeline, notes, viewing + ICS export,
manual adds. Rank explanation and per-field provenance surfaced in place.

**Second, not fourth.** You cannot run a six-week hunt across a hundred listings from a
terminal, and this is where the rubric becomes legible — which is the wedge.

**Ship gate.** A full cycle — spotted, shortlisted, contacted, viewing booked, marked
seen — survives the listing going dead halfway through with every note intact.

---

## R3 · Knows the neighbourhood

> "How far is it really to a grocery store you'd actually use."

Named proximity categories with real walking distances, weighted like any other rule.
`avoid_areas` as a user rule. OpenStreetMap keyless default; Google as an opt-in upgrade.
Cost estimate, confirmation and cap on anything paid.

**Ship gate.** Two listings identical on paper rank differently because one is four
minutes from a supermarket and the other is nineteen — with no API key configured.

---

## R4 · Anywhere, any site

> "Add your market and find out which sites are actually worth watching there."

Coverage probe reporting real counts and distinguishing **blocked** from **empty** —
opposite signals, different fixes. Citypack generator and validator. Per-market
`enabled` / `load_bearing`. Credentialed sources via keychain. Adapter scaffold with a
fixture-based conformance harness.

Also where agent-assisted authoring pays off: a new adapter or citypack drafted from a
fixture and a schema, reviewed by a human, committed as deterministic code.

**Ship gate.** Someone adds a city you have never run and a source you never wrote, and
touches nothing in the core to do it.

---

## R5 · Reads the photos

> "Pulls the square footage off the floor plan."

Floor-plan detection and OCR, in-suite laundry, render-vs-real, condition and finish,
room-count sanity against claimed beds. Vision behind a provider protocol with prompts
and schemas versioned in the repo. Lowest confidence tier, labelled as inferred
everywhere. Same estimate-and-cap guard as geo. Extraction schema closed to unit
attributes only.

**Late on purpose:** the only genuinely new subsystem, the only one costing money per
listing, and far cheaper once the chain, confidence tiers and budget guards exist.

**Ship gate.** Floor-plan square footage recovered on listings that never stated it,
visibly marked as inferred, and never on its own enough to promote a listing.

---

## R6 · Two people, two rubrics

> "You weight it your way, they weight it theirs, and the tool shows you where you disagree."

Two weight sets against one hunt. Each listing ranked under both, sortable by the gap.
Alerts respecting whose deal-breakers are whose.

**Deferred by decision** — but `score` is keyed by `profile_id` from R1 precisely so this
is a feature and not a migration. Use one profile until then.

**Ship gate.** Two people run one hunt, neither adopts the other's weights, and the
disagreement list is short enough to talk through.
