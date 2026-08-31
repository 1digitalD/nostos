# 10 · Live smoke run

Every test in this repo passes against fixtures. Until the pipeline has touched a live
site, nothing proves the parsers match what Craigslist and Kijiji serve *today*, or that
the robots.txt and rate-limit layer in `sources/http.py` behaves against a real host.

This is the runbook for that check. It has to be run somewhere with real outbound
network access — a sandboxed CI or agent container whose egress policy blocks the target
hosts cannot perform it, and a green fixture suite is not a substitute.

Run this **before** tagging a release.

---

## 0 · Check robots.txt first

This decides whether the rest is even possible, and it is the fastest thing to get wrong
silently — a fully robots-blocked source currently looks a lot like a quiet market.

```bash
uv run python scripts/capture_fixtures.py robots https://vancouver.craigslist.org
uv run python scripts/capture_fixtures.py robots https://www.kijiji.ca \
  --probe /b-apartments-condos/vancouver/apartments/k0c37l1700287
```

This prints the live robots.txt and an ALLOW/BLOCK verdict for each URL the adapter
would fetch, using the same `Protego` parser and User-Agent the real run uses.

**What to look for.** Craigslist discovery fetches two URLs that differ only by a
`format=rss` query parameter:

```
/search/van/apa?format=rss&hasImage=1&...     ← RSS
/search/van/apa?hasImage=1&...                ← HTML fallback
```

Both are under `/search`. If robots.txt disallows `/search`, **both are blocked** and the
RSS→HTML fallback cannot help — discovery returns nothing. If you see BLOCK on both
lines, the craigslist adapter cannot function against this host as written, and that is
the finding; do not work around it by loosening the robots check.

Record the verdicts. They are the answer to an open question in the source layer.

---

## 1 · One city, end to end

```bash
uv sync --all-groups
export NOSTOS_HOME="$(mktemp -d)"          # keeps the smoke run out of your real config

uv run nostos init \
  --non-interactive --city vancouver \
  --max-rent 3200 --beds 2 --baths-min 1 --min-area 700 \
  --laundry nice-to-have --parking nice-to-have --pets dont-care \
  --source craigslist --source kijiji --force

uv run nostos watch --yes
uv run nostos list --limit 20
uv run nostos explain <listing_id>
```

Confirm, and write down what you actually saw:

- [ ] Listings return, from **each** enabled source. Note the per-source counts printed by
      `watch` (`source=… status=… count=…`).
- [ ] Fields are populated, not empty — rent, beds, baths, area, address. A source that
      returns records whose fields are all `not_stated` is a parser that has drifted, not
      a working source.
- [ ] Scores compute and differ between listings.
- [ ] `nostos explain <id>` renders a readable breakdown naming rules, points and
      evidence.
- [ ] Cross-source dedupe collapses a unit posted on both sites. Check the store
      directly, since the CLI does not surface it:

      ```bash
      sqlite3 "$NOSTOS_HOME/nostos.db" \
        "SELECT signature, COUNT(*) c, GROUP_CONCAT(source) FROM listing_source
         GROUP BY signature HAVING c > 1;"
      ```

      Rows here are units seen on more than one site. Each such signature should map to
      **one** `listing_id`:

      ```bash
      sqlite3 "$NOSTOS_HOME/nostos.db" \
        "SELECT signature, COUNT(DISTINCT listing_id) FROM listing_source
         GROUP BY signature HAVING COUNT(*) > 1;"
      ```

      A count above 1 in the second query means duplicates were stored separately.

---

## 2 · Run it a second time

```bash
uv run nostos watch --yes
```

- [ ] Only genuinely new listings are reported.
- [ ] Detail pages for already-seen listings are **not** re-fetched. Confirm from the
      wire, not from the summary — run with request logging on:

      ```bash
      uv run python -c "
      import logging; logging.basicConfig(level=logging.DEBUG)
      from nostos.cli import app; app()
      " watch --yes 2>&1 | grep -c 'GET'
      ```

      Compare the request count against run 1. It should drop to roughly the discovery
      URLs alone.
- [ ] A quiet second run does **not** produce a spurious
      `load-bearing source returned zero listings` alert. If it does on a run where the
      market simply had nothing new, that is a false positive, not an outage.

---

## 3 · The run row

```bash
sqlite3 -json "$NOSTOS_HOME/nostos.db" \
  "SELECT id, started_at, finished_at, sources_json, counts_json FROM run ORDER BY started_at;"
```

- [ ] Per-source counts, `status`, `records_seen`, baseline band and watermark are all
      present.
- [ ] The watermark advanced on run 1 and held on a quiet run 2.
- [ ] A `load_bearing` source at zero produced an alert. Per
      `docs/02-architecture.md:181` this **alerts**; it does not raise and does not fail
      the run — `nostos watch` exits 0. If you want a non-zero exit for automation, that
      is a change to propose against that documented decision, not a bug to patch around.

---

## 4 · Record real fixtures

The fixtures in `tests/fixtures/` were authored by hand to match the parsers. That means
they cannot detect a parser drifting from the site — which is the one thing fixtures
exist to catch. Replace them with recorded pages while you have network access:

```bash
uv run python scripts/capture_fixtures.py craigslist \
  --base-url https://vancouver.craigslist.org --area van --max-price 3200 \
  --out tests/fixtures/craigslist
```

The script fetches through the project's own `SourceHttpClient`, so robots.txt and the
rate limit apply, and it redacts emails, phone numbers, `tel:` and `mailto:` links, reply
links and JSON-LD contact fields before writing. Listing URLs are preserved: a craigslist
post id is ten digits, the same shape as a phone number, so URLs are masked before the
text patterns run — otherwise every detail URL in the fixture would be rewritten into a
fake phone number.

**Redaction is mechanical and incomplete.** It does not remove personal names — no
pattern identifies those — and a phone number sitting in some other URL's query string
survives masking. Read every captured file before committing: visible text, JSON-LD
blocks, `meta` tags and inline scripts. Scraped contact data must never enter this
repository.

Then re-run the suite against the real pages:

```bash
uv run ruff check . && uv run mypy --strict src tests && uv run pytest
```

Failures here are the real prize — each one is a place a parser had drifted from the site
and the hand-written fixture was hiding it. Fix the parser. Do not widen a selector or
relax an assertion to make a failure disappear.

---

## What to report

For each of the four sections: what you ran, what you saw, and what broke. A parser that
returns records with empty fields is a failure even though nothing raised — "it ran" is
not the same as "it worked".
