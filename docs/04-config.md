# 04 · Configuration

Two files, two owners, two lifetimes. Conflating them is the mistake the old codebase
made: its `CityConfig` bundled search criteria with city facts, so changing your budget
meant forking a citypack and two people in one city could not share the city data.

| | Citypack | Profile |
|---|---|---|
| **Owner** | The project | The user |
| **Scope** | One per metro | One per hunt |
| **Lifetime** | Stable for years | Changes weekly during a hunt |
| **Ships in repo** | Yes — contribution surface | No, never |
| **Holds** | Where and how to search | What you want and how much |

`SearchContext = citypack + profile`, resolved once and threaded through every stage.

## Citypack

`src/nostos/citypacks/vancouver.yaml` — shipped, community-contributable.

```yaml
name: vancouver
locale:   { language: en-CA, timezone: America/Vancouver,
            currency: CAD, area_unit: sqft }

areas:                          # the neighbourhood vocabulary
  - key: kits_beach
    label: Kitsilano
    keywords: [kitsilano, "kits point", "4th & york"]
    bbox: [49.262, -123.190, 49.278, -123.145]

sources:
  craigslist:
    enabled: true
    load_bearing: true          # zero results here means something broke
    base_url: https://vancouver.craigslist.org
    areas: [van, nvn, bby]
  kijiji:
    enabled: true
    load_bearing: false
    regions: [{ path: vancouver, id: c37l1700287 }]

address:
  directional: { w: west, e: east, n: north, s: south,
                 nw: northwest, ne: northeast }
  strip_tokens: [vancouver, burnaby, "north vancouver"]
  region_tokens: [bc, "british columbia"]
```

### Rules

- **`areas[].key` is the neighbourhood vocabulary.** The model validates `area_key`
  against this list. Never an enum in code. This is the specific bug — `listing.py:152`
  raising `ValueError` on any nb outside a hardcoded tuple — that made the old codebase
  single-city.
- **Adapter sections are optional.** A city with no REW coverage must be addable without
  writing stub keys for an adapter it will never call.
- **`enabled` and `load_bearing` are separate flags** and need different handling.
  `enabled` = worth running here. `load_bearing` = zero results means an outage, not a
  quiet market. The old `REQUIRED_SOURCES = {craigslist, kijiji, rew, zumper}` frozenset
  asserted four sources define a healthy run *everywhere* — point it at Austin and REW
  (BC-only) and Kijiji (Canada-only) return zero forever, the health gate never passes,
  and the watermark never advances. It does not degrade; it stops.

## Profile

`profiles/mine.yaml` — the user's, never committed.

```yaml
city: vancouver

hard:                           # fail these and the listing is out
  rent:  { max: 3600, currency: CAD }
  beds:  { eq: 2 }
  baths: { min: 1, max: 2 }
  area:  { min: 750, unit: sqft }
  exclude: [basement, furnished_only]

weights:                        # sign and magnitude are yours
  laundry.in_suite:   6
  parking.available:  5
  pets.allowed:      +8         # flip the sign to avoid instead
  floor.low:          0         # switched off
  area.over_minimum: { per_100_sqft: 4, cap: 12 }
  rent.headroom:     { per_100: 1, cap: 15 }

area_key_weights:               # profile-owned neighborhood bonuses/penalties
  downtown_van:       +15
  burnaby_brentwood:  +11
  kits_beach:         +8
  n_van_lonsdale:     +1
  west_van:           +1

proximity:                      # [R3]
  - { category: grocery, within_min: 8,  weight: 5 }
  - { category: transit, within_min: 10, weight: 4 }

avoid_areas:                    # [R3] — generic, not a shipped judgment
  - { bbox: [49.270, -123.075, 49.295, -123.020], weight: -8 }

confidence:
  unverified_penalty: 0         # opt in if you want it

sources:  { craigslist: on, kijiji: on }
notify:   [ "ntfy://myhost/nostos", "mailto://..." ]
schedule: "0 */6 * * *"
```

### Rules

- **Hard filters and soft weights are separate sections.** The old `score.py` mixes them.
- **Every weight's sign is the user's.** Pets is the canonical example: the old rubric
  hardcodes `-10` for pet-friendly, which is backwards for most renters. In this shape
  it is just a sign someone picks.
- **`avoid_areas` is user-authored**, taking a bbox, radius or address pattern. The old
  code ships a hardcoded penalty against one named low-income neighbourhood with a
  lat/lng bounding box and postal prefixes. As a personal preference that is ordinary;
  as a product default compiled into everyone's copy it is not.
- **`area_key_weights` is profile-authored** and keyed by `citypack.areas[].key`. Empty
  map means no neighbourhood term in ranking.
- **Shipped profiles**: a neutral `balanced.yaml`, plus `example-vancouver.yaml`
  carrying the original tuned rubric as a worked example of what a real profile looks
  like — without it being anyone's default.

## The wizard

`config/wizard.py` is **an R2 feature, not deferred polish.**

Weights in YAML is a bad interface for the one thing that differentiates the product. If
the rubric is hard to express, everyone runs the default profile and the tool is a worse
Zillow alert.

The wizard asks plain-language questions — *"in-suite laundry: deal-breaker, nice to
have, or don't care?"* — and writes the weights. Where an agent host is available, the
MCP surface does this conversationally and better; the CLI wizard is the path for
everyone else.

## Credentials

Never in either file. The config references a credential **name**; the value lives in
the OS keychain via `keyring`, with an env-var fallback for headless boxes.
`nostos auth <source>` stores it once.

Nothing credentialed is in the default install. Credentialed sources arrive in R4 behind
an explicit opt-in with the account risk stated on first run.
