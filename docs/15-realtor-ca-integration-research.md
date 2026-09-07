# REALTOR.ca integration research

Date: 2026-09-06

## Executive conclusion

The best production path for Nostos is not to automate REALTOR.ca's consumer
search UI indefinitely. REALTOR.ca's official distribution mechanism is CREA's
REALTOR.ca DDF®, which is designed for REALTORS® and broker owners to share
listing data with third-party websites and member websites. Nostos should first
confirm whether the operator is eligible for DDF access and obtain written
permission for the intended personal, non-commercial use. If eligible, build a
small DDF-backed source adapter with explicit attribution, retention, and
removal rules.

If DDF access is unavailable, retain the current browser adapter only as a
best-effort, manually supervised source. Do not make it load-bearing: it uses a
visible persistent Chrome session, waits five seconds between navigations, and
depends on a browser response from the search application. It is inherently
fragile and may conflict with site terms or access controls unless the user has
clear permission.

## Current implementation

Nostos has a `realtor_ca` source in `src/nostos/sources/realtor_ca.py`.

- Discovery opens a `https://www.realtor.ca/map#...` URL and waits for the
  browser response named `AsyncPropertySearch_Post`.
- It parses the response's `Results` payload, with a rendered-card fallback.
- It filters parsed results to addresses containing Toronto.
- Detail fetching opens each listing in Chrome and extracts address, price,
  beds, baths, square footage, description/features, maintenance company,
  parking, and image URLs.
- It requires Chrome plus the browser extra, respects `robots.txt`, and spaces
  navigations by five seconds (12 requests/minute is the declared capability).
- Tests and fixtures exist, but the repo assessment records no stored
  REALTOR.ca listings and the latest live run failed waiting for the browser
  search response. This is adapter support, not proven market coverage.

The current adapter also returns the discovery record when detail fetching
fails. That should be corrected before production automation so discovery
success, detail success, and listing availability cannot be confused.

## Refresh and algorithm cadence

There are three different cadences:

1. **Discovery / ranking automation:** the installed Toronto and Vancouver
   LaunchAgents both specify `StartInterval=21600` (six hours) and run
   `nostos watch --yes --queue-details`. The profile's default cron string
   (`0 */6 * * *`) is schedule metadata; the application does not install or
   manage a scheduler itself.
2. **Known-listing source refresh:** the watch runner uses a 24-hour freshness
   window for seen source IDs. This prevents ordinary rediscovery/detail work
   more often than daily in the relevant path; it is not a complete price/removal
   monitoring policy.
3. **Detail enrichment:** with `--queue-details`, discovery persists first and
   queues detail jobs. The web worker polls the durable queue every two seconds
   when idle. Transient failures allow three total attempts, with retry delays
   of 2 and 4 seconds (plus polling/queue time); a worker claim expires after
   five minutes. The standalone `watch` path can also fetch details
   synchronously.

Ranking and the built-in text enrichment are deterministic. Watch scores its
synchronous records; queued records are published and scored by the detail
worker after success. Web reconstruction uses the current extraction revision
when available and otherwise reruns text rules; it does not necessarily persist
a new ranking score on every page view. There is no scheduled LLM enrichment in
the watch path. Address research has its own 24-hour report expiry, separate
from listing enrichment and ranking.

## Automation status verified on 2026-09-06

Automation is partially built:

- In-repo: watch CLI, deterministic enrichment/ranking, durable detail-refresh
  SQLite queue, web worker, and `--queue-details` are implemented.
- On the machine: both LaunchAgent plist files exist and are configured for
  six-hour execution.
- Current runtime state: `launchctl print` reports both
  `com.danish.nostos.toronto.watch` and `com.danish.nostos.vancouver.watch` as
  `state = not running`, `active count = 0`, `runs = 0`.
  Correction: a periodic LaunchAgent is normally idle between runs, and the run
  counter can reset on reload. This snapshot does not prove automation is broken
  or that there were no earlier runs. Logs and database run history would be
  required to establish end-to-end scheduler health. No fresh database count was
  taken for this research; the zero REALTOR.ca count above is the earlier repo
  assessment's snapshot.
- Missing: scheduler installation/health management inside Nostos, a dedicated
  source-health alert for REALTOR.ca browser failure, and a proven sustained
  REALTOR.ca run with stored records.

## Integration choices

### A. CREA REALTOR.ca DDF® feed — recommended production option

Use this only after confirming eligibility, permissions, licensing, and the
allowed display/use case with CREA or the relevant board/broker. Use the
official technical documentation and policy as the contract, preserve required
attribution, and implement removal/update handling and rate limits from that
contract. This removes the dependence on the consumer UI and gives Nostos a
stable source boundary.

### B. Licensed MLS/board data provider — second choice

If the user has a brokerage/board relationship, use the provider's authorized
RETS/RESO or proprietary feed. This can be operationally stronger than browser
automation, but access, fields, redistribution, and cost are provider-specific.
The adapter must keep credentials outside profiles and preserve source
provenance.

### C. Current Chrome adapter — fallback / probe only

Keep it behind an explicit opt-in, low-volume mode. Add a browser health state,
bounded discovery volume, backoff, fixture replay, and a clear “source
unavailable” result. It should not be the sole source for alerts or a claim of
complete REALTOR.ca coverage.

### D. User-supplied listing URLs — useful complement

For a personal workflow, support importing a user-provided REALTOR.ca URL and
refreshing only that saved listing. This has lower coverage but much lower
operational and compliance risk, and fits Nostos's evidence-first model.

## Recommended next slice

1. Contact CREA/member support and document eligibility, permitted use,
   attribution, retention, and feed/API details.
2. Add an explicit source-health dashboard and make the current adapter report
   browser-search failure separately from zero results.
3. Build one DDF or authorized-feed adapter through discovery → persistence →
   detail → deterministic enrichment → ranking, with removal and correction
   paths tested end to end.
4. Keep six-hour discovery as the starting cadence; use daily detail refresh for
   active saved listings, and add jitter/backoff rather than increasing request
   concurrency. Re-rank on every successful source/detail update and on profile
   changes, without fetching the network.
5. Only promote REALTOR.ca to load-bearing after several days of successful
   runs, nonzero volume, stable identifiers, and verified update/removal behavior.

## Primary sources

- CREA REALTOR.ca DDF® overview: https://www.crea.ca/technology/realtor-ca-for-realtors/realtor-ca-tools/realtor-ca-ddf/
- CREA DDF® policy/rules: https://support.crea.ca/DDF#/discussion/32/realtor-ca-ddf-policy-and-rules
- CREA DDF® technical documentation index: https://support.crea.ca/DDF#/categories
- REALTOR.ca robots policy: https://www.realtor.ca/robots.txt
- Nostos source adapter: `src/nostos/sources/realtor_ca.py`
- Nostos watch runner and refresh worker: `src/nostos/watch/runner.py`, `src/nostos/enrich/refresh.py`
