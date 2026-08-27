# 07 · Porting from apartment-hunt

Source of truth: `1digitald/apartment-hunt` at `1137c9c`.

## The rule

> **If it is a pure function over listing data, port it with its tests.
> If it touches a file, a database, a browser, or a schedule, rewrite it.**

The parsing heuristics took months of watching wrong output to get right. The plumbing
is what is wrong with the old design. Roughly 1,300 lines come across; roughly 3,200 are
deleted.

## Carry — with tests, tests first

| From | To | Lines | Tests | Notes |
|---|---|---|---|---|
| `io_util/address.py` | `normalize/address.py` | 379 | 52 | Nearly as-is. BC token lists become citypack data. |
| `match/dedupe.py` | `normalize/dedupe.py` | 161 | 5 | Zero regex, pure logic. Straight port. |
| `match/criteria.py` → `recover_missing_attributes` | `enrich/text.py` | ~150 | in test_scout | Port the detectors and the `contentEvidence` approach. Drop the module constants. |
| `listing.py` → record shape + nb classifier | `model/listing.py` | ~200 | 25 | Port the shape; add provenance; make the taxonomy citypack data. |
| `track/score.py` → detector halves only | `rank/rules.py` | ~400 of 1,077 | — | Laundry with negation, floor-from-unit-number, walk-score parsing, den/solarium, walkable/sparse phrases. **Functions come, numbers stay behind.** |
| `adapters/craigslist.py` → parse functions | `sources/craigslist.py` | ~120 of 664 | in test_scout | `parse_cl_rss`, `cl_posted_iso`. Fetch/orchestration rewritten. |
| `adapters/kijiji.py` → parse functions | `sources/kijiji.py` | ~100 of 250 | in test_scout | JSON-LD parse. Replace hand-rolled with extruct where equivalent. |

## Delete — rewritten or dissolved

| File(s) | Lines | Why |
|---|---|---|
| `scout.py` orchestration | 480 | Replaced by `watch/runner.py` |
| `io_util/state.py` | 260 | Seven-store bookkeeping; replaced by one store |
| `track/enrich_all.py`, `enrich_db.py`, `rank.py`, `rescore.py`, `sync_extras.py`, `report.py`, `soft_mark_dead.py` | ~950 | The entire second pipeline |
| `tracker/sync.py` | 130 | Store-to-store sync that no longer exists |
| `track/*_extractor.py` (6 files) + `cl_rescrape.py` | 1,537 | **Folds into `Source.fetch_detail()`.** This is the big one — six detail scrapers against sites that already have discovery scrapers. |
| `config.py` + `citypacks/*.yaml` | ~430 | Superseded by the citypack/profile split. Reread for the field inventory, then rewrite. |
| All seven data stores | — | See `03-data-model.md` |

## Known bugs — do not port these forward

Verified on a clean checkout (installed deps, ran the suite: 226 tests, 6 failed):

| Bug | Location | Fix in rebuild |
|---|---|---|
| Hardcoded venv path breaks 5 tests | `tracker/server.py:34` | `sys.executable` |
| Hardcoded volume path breaks 1 test | `io_util/state.py:29` | package root / platformdirs |
| `lxml` used but undeclared | `adapters/kijiji.py:54` | selectolax, declared |
| Three different `PRICE_MIN` | `criteria.py:19` = 1800, `score.py:93` = 2500, `facebook_graphql.py:45` = 180000¢ | One profile value |
| `nb` enum blocks multi-city | `listing.py:152` | Citypack-validated `area_key` |
| Global required-source set | `io_util/state.py:67`, gated at `scout.py:371` | Per-citypack `load_bearing` |
| Auth fails open | `tracker/server.py:78-88` | Fail closed off-loopback |
| Absolute paths (6 more) | `state.py:41`, `reconcile.py:1`, `fb_extractor.py:33`, `facebook.py:29`, `fb_login.py:35`, `watchdog.sh:8-9` | platformdirs + env |

## Do not migrate the data

The old repo carries a live hunt: 161 enriched listings with contact history and viewing
state. **Do not write a migration.** Leave the old tool running on its own machine until
Nostos reaches parity, then switch. The advantage of rebuilding is not having to keep
the old thing working in lockstep — do not spend it.

Archive the old repo private as the reference for heuristics not yet ported.

## Privacy blocker on the old repo

Before `apartment-hunt` is ever made public: `tracker/contact_details.db` (161 rows with
`contact_phone` / `contact_name` / `contact_email`) and `site/listings_enriched.json`
(43 unique phone numbers) carry scraped third-party contact data across 15 and 19 commits
respectively. `.gitignore` covers `tracker.db` but not `tracker/contact_details.db`.

Deleting the files is insufficient — it needs `git filter-repo`. **Nostos starting as a
fresh repo sidesteps this entirely: never import the data.**

## Edge cases the ported tests encode

Sample of what would be lost by rewriting the heuristics from scratch — none of these
are things anyone writes on day one:

- Craigslist IDs are case-sensitive base62
- "basement storage" and "underground parking" must **not** trip the basement filter
- Marketing copy — "minutes from Yaletown" — must not set the neighbourhood
- A price straddling a dedupe bucket boundary must not collapse two distinct listings
- Directional abbreviations must not expand inside words
- Unit-number regex must not match street numbers
- REW neighbourhood must not accept a broad city name
- Rebuild must preserve existing values when new data is empty
