# Nostos

Nostos is a self-hosted rental-market watch that ranks listings using your own rubric.
It is open source software for personal operation, not a hosted SaaS product.

## First sitting (from install to ranked listings)

If you have not read any docs, use this path:

```bash
pip install nostos-cli
nostos init
nostos watch --yes
nostos list --limit 20
```

What this does:
- `nostos init` writes your profile (interactive prompts by default).
- `nostos watch --yes` runs discovery + scoring and writes results.
- `nostos list` prints ranked listings with listing id, score, title, URL, and rent.

### Packaged Vancouver citypack default

By default, `nostos` resolves `vancouver.yaml` from packaged citypacks, so this works
after `pip install` even outside a git checkout:

```bash
nostos init
nostos watch --yes
nostos list
```

You can still pass explicit paths if you prefer:

```bash
nostos init --citypack ./src/nostos/citypacks/vancouver.yaml --profile ./profile.yaml --force
nostos watch --citypack ./src/nostos/citypacks/vancouver.yaml --profile ./profile.yaml --db ./nostos.db --yes
nostos list --citypack ./src/nostos/citypacks/vancouver.yaml --profile ./profile.yaml --db ./nostos.db --limit 20
```

### Non-interactive init example

```bash
nostos init \
  --non-interactive \
  --city vancouver \
  --max-rent 3400 \
  --beds 2 \
  --laundry nice-to-have \
  --source craigslist \
  --source kijiji \
  --force
```

## Toronto search

Toronto is packaged alongside Vancouver. Create a separate profile so your existing
search stays available. These numbers are examples; choose your own criteria:

```bash
nostos init --non-interactive --city toronto --profile ~/.config/nostos/toronto.yaml \
  --max-rent 3200 --beds 2 --laundry nice-to-have --source craigslist --source kijiji \
  --source realtor_ca
nostos watch --profile ~/.config/nostos/toronto.yaml --yes
nostos web --profile ~/.config/nostos/toronto.yaml
```

Realtor.ca is an optional Toronto browser source. Install the browser extra and Google
Chrome before enabling it:

```bash
pip install 'nostos-cli[browser]'
```

The adapter uses a dedicated persistent Chrome context, opens a normal Chrome window,
respects Realtor.ca's `robots.txt`, and spaces navigations at five-second intervals.
Realtor.ca does not currently serve its search application to headless Chrome. The
source therefore fails with a clear source-level error when Chrome or the browser extra
is unavailable; Craigslist and Kijiji continue independently.

Commands infer the citypack from the profile's city. Vancouver keeps its existing
DB default; Toronto uses `toronto/nostos.db` beneath the data directory (or
`NOSTOS_HOME`). Explicit `--citypack` and `--db` paths still override defaults.
**Use a separate database for each city**: an explicitly shared DB is not partitioned
by city, and stored records/watermarks would mix. No existing profile is overwritten
unless `--force` is supplied.

The first Toronto scope is the City of Toronto, not the entire GTA. Neighbourhood
keywords and bounding boxes are approximate discovery labels, not verified boundaries
or commute calculations. Unknown areas remain unknown. Both adapters passed a bounded
live discovery/detail probe on 2026-09-04; Realtor.ca's rendered search response was
also probed on 2026-09-06. These checks are not a guarantee of continuing coverage.
No Toronto adapter is marked load-bearing until sustained volume has been observed.
The UI browses and edits criteria; fetching is started with `nostos watch` or the MCP
watch tool. See [the implementation review](docs/11-implementation-review.md) for
criteria limitations and the next iteration workflow.

## Safety and scraping posture

- Respects `robots.txt` by default.
- Uses conservative default rate limits.
- Uses only credentials provided by the user for sources they can legally access.
- Does not include or operate any hosted data-collection tier.

## Local web UI

For browsing listings with photos and filtering in a real browser:

```bash
nostos web                          # serves on 127.0.0.1:8421, opens browser
nostos web --port 9000              # different port
nostos web --export ~/Desktop/listings.html   # write a self-contained HTML file
```

The web UI binds to `127.0.0.1` by default. There is no app-level auth, so remote
access should use a trusted host control such as Tailscale Serve. To share a read-only
snapshot, use `--export`. Listing actions and hunt stages persist across watch runs.
Listing detail pages also support user corrections with reset, and a research workspace
for matching stored ads, nearby services, and compiled recent address research.

Building research requires an exact street address. You can set or correct its research
address in the workspace without changing the source map pin. Three focused searches
look for the address, management/reviews, and safety; accepted excerpts must explicitly
match the street and city and have a date within two years. Results explain the match
and remain source claims rather than verified facts. Unrelated evidence is excluded,
and missing evidence does not establish safety. Reports expire after 24 hours and are
invalidated when the address or relevance rules change. See
[requirements and acceptance checks](docs/12-research-relevance.md).

Compiled research is independent of any agent host. The built-in standalone provider
uses Perplexity's structured Search API when `NOSTOS_PERPLEXITY_API_KEY` is set. You can
put local service credentials in `~/.config/nostos/research.env` instead of a LaunchAgent
or shell environment:

```bash
NOSTOS_RESEARCH_PROVIDER=perplexity
NOSTOS_PERPLEXITY_API_KEY=your-key
```

The application boundary is the `ResearchProvider` protocol in
`nostos.web.research`; embedded hosts can pass their own implementation to `create_app`.
This keeps Nostos usable as a standalone app and lets Codex, Claude, or another agent
adapter supply structured findings without becoming a package dependency.

Hard filters and ranking weights are editable in the browser at `/profile`. Preview
shows entrants, exits, and match counts before a guarded save. Saving writes the
profile YAML and immediately re-scores stored listings from their latest records,
without a network fetch. Listings that miss current criteria remain visible as saved
research with the reason shown.

The same revision-safe workflow is available to people and agents through the CLI,
and the MCP tools wrap these commands:

```bash
nostos profile-get --profile ~/.config/nostos/toronto.yaml
nostos profile-preview --profile ~/.config/nostos/toronto.yaml \
  --patch-json '{"hard":{"rent":{"max":3400,"currency":"CAD"}}}'
nostos profile-apply --profile ~/.config/nostos/toronto.yaml \
  --patch-json '{"hard":{"rent":{"max":3400,"currency":"CAD"}}}' \
  --expected-revision REVISION_FROM_PREVIEW
nostos profile-history --profile ~/.config/nostos/toronto.yaml
nostos profile-undo REVISION_ID --profile ~/.config/nostos/toronto.yaml \
  --expected-revision CURRENT_REVISION
```

## Development

```bash
uv sync --all-groups
uv run nostos --help
uv run ruff check .
uv run mypy --strict src tests
uv run pytest
```

## Contributing

Citypacks (a new metro) and source adapters (a new site) are the main contribution
surfaces. See [CONTRIBUTING.md](CONTRIBUTING.md) for the citypack schema, the `Source`
protocol, the fixture and conformance requirements, and the checks every PR must pass.

## License

Apache-2.0.
