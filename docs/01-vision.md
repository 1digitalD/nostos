# 01 · Vision

## Thesis

> Rank listings on attributes no portal has a field for, weighted the way you want.
> The watch keeps it current; the rubric is why you'd use it.

Nostos is not a search engine. It is a standing watch over a rental market, ordered by
a rubric its user wrote, that knows more about each listing than the listing says.

## What it competes with

Zillow, Padmapper, Zumper and Rentals.ca all email you when a new match appears. Search
plus notify is solved and is not worth rebuilding.

Three things they do not do:

1. **Rank by your weighting.** You get date order or their relevance model. Never
   "in-suite laundry matters twice as much to me as a balcony."
2. **Enrich past the ad.** No real walking time to a grocery store you would use, no
   square footage read off the floor plan, no flag that a laundry claim is unverified.
3. **Span the informal market.** Craigslist, Kijiji and Marketplace carry a large share
   of private-landlord stock, and no aggregator covers them together.

The differentiator is the **rubric and the enrichment**. The scraping is cost of entry
and a permanent maintenance tax — it is not the moat.

Critically, the rubric and the enrichment are **one value proposition, not two**.
Ranking over standard fields is a better Zillow sort. Enrichment with nothing weighting
it is trivia. The product is the pair.

## Who it is for

**The household operator.** One person runs the search, often for two people. Their
criteria are elaborate enough that portal filters fail them — the original user's were
2BR, 1–2 bath, ≥750 sqft, ≤$3,600, unfurnished, floor ≤12, no basement, five named
neighbourhoods, pet-averse, walkability-weighted. Zillow's filter set handles about half.

Secondary and likely first on GitHub: **the tinkerer**, who self-hosts by preference and
whose actual pain is lower but whose willingness to run it is high.

## Validation status

From a product diagnostic run 2026-08-25:

- **Demand evidence is behavioural but N=2**, both inside one household. The tool was
  built and then iterated under live pressure (three rubric revisions in one evening),
  and a second person who did not build it operated the interface unassisted. That is a
  real usability signal and a real "would be upset if it vanished" signal — for two people.
- **The status quo is the good kind**: multiple tabs refreshed daily across four sites.
  A repeated manual workaround, not "nothing exists."
- **Unproven, and the gap that matters**: that anyone outside that household values a
  rubric enough to install something.

**Open assignment:** watch one person who is *currently* hunting, for twenty minutes,
without helping or demoing. Watch for whether they articulate a preference no filter on
the site can express. That is the wedge firing in the wild, and it is the only evidence
that would show this generalises.

## Business shape

**Open source, optional donations.** Not a startup.

Apartment hunting is acute for six to eight weeks, then stops for one to three years.
Churn is 100% by design and no retention is achievable. That is fatal to a business and
irrelevant to an open-source tool.

Two consequences that shape the build:

- **No hosted tier.** Self-hosted, each user scrapes with their own credentials and
  carries their own risk. Hosted, the project would be scraping commercially at scale —
  a materially different legal position, and the thing that has actually killed
  comparable products. Ruled out by construction.
- **Install-to-first-value must be minutes.** A six-week motivation window means anyone
  facing an evening of setup never finishes. This makes the setup wizard and
  `pip install` higher priority than any additional source.

## Scope boundaries

Deliberately **not** building:

| Not building | Why |
|---|---|
| Auto-contacting or auto-applying to landlords | The obvious next ask, and the line where a research tool becomes a spam engine. Gets users' accounts banned. Draft a message for a human to send; never send one. |
| A hosted tier | See above. Settled by the donation model. |
| Deal scoring / price prediction | Needs comparable-sales data we do not have, and fails in ways users cannot check. |
| A mobile app | The notification sink plus a responsive page is the mobile story. |
| Anything landlord-side | Posting, screening, managing. Different product entirely. |
