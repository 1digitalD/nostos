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

The web UI binds to `127.0.0.1` only. There is no auth — it is a self-hosted single-user tool. To share a snapshot with someone on another machine, use `--export` and send the file. Action buttons (`Star` / `Dismiss` / `Contacted` / `Note`) write to a `listing_action` table so preferences persist across `nostos watch` runs.

Hard filters and ranking weights are editable in the browser at `/profile`. Saving writes the profile YAML and immediately re-scores every stored listing from its latest source record — no network fetch — so the ranked list reflects the change on the next page load; listings that now fail a hard filter drop out of the list. The same re-score is available from the CLI with `nostos rank` after editing the YAML by hand.

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
