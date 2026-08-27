# 05 · Tech stack

Verified against current releases as of August 2026. `Rel` is the release a dependency
first appears in — do not add it earlier.

## Runtime

| Package | Role | Why this one | Rel |
|---|---|---|---|
| python ≥3.11 | Runtime | 3.10 reaches EOL Oct 2026; `StrEnum` and better generics | R1 |
| pydantic v2 | Model, validation, serialization | Discriminated unions express `Observed \| Absence` directly; one model serves the store, the API and the MCP tool schemas | R1 |
| typer + rich | CLI | Type hints become the CLI; rich renders the ranked table and score breakdown legibly | R1 |
| httpx | HTTP | Async, HTTP/2, one client for sync and async paths | R1 |
| hishel | HTTP caching | RFC-compliant cache over httpx — cuts repeat detail fetches between runs | R1 |
| selectolax | HTML parsing | Markedly faster than lxml, and drops the undeclared-lxml bug the old repo has | R1 |
| extruct | JSON-LD / microdata | Kijiji and Zumper both expose `ItemList`; stop hand-rolling that parse | R1 |
| protego | robots.txt | The posture the README promises has to be enforced somewhere | R1 |
| apprise | Notifications | 110+ services behind one URL scheme. This *is* the sink abstraction, already written | R1 |
| platformdirs | Paths | Config, data and cache in the right place per OS — kills every hardcoded absolute path | R1 |
| sqlite3 (stdlib) | Store | No ORM. Plain SQL behind repo classes; migrations are numbered files | R1 |
| structlog | Logging | Structured run logs the observability rollup can read back | R1 |
| mcp (python-sdk) | Agent surface | One server reaches Claude Code, Codex and OpenClaw — all three speak MCP | R1 |
| playwright | Browser sources | Only where a source needs a browser; persistent context replaces the CDP dependency | R1 |
| FastAPI + uvicorn | UI backend | Shares pydantic models with the core — no DTO duplication — and gives OpenAPI free | R2 |
| htmx + vanilla CSS | UI frontend | No npm, no build step. `pip install` stays the entire install story | R2 |
| geopy (Nominatim) | Geocoding | Keyless default via OpenStreetMap | R3 |
| openrouteservice | Walk times | Real walking isochrones from OSM, free tier, self-hostable. Replaces straight-line distance | R3 |
| keyring | Credentials | OS keychain; config references a name, never a value | R4 |
| HomeHarvest | US sources | Zillow, Realtor.com, Redfin with a `for_rent` type — US coverage without writing three adapters | R4 |
| curl_cffi | TLS fingerprint | **contrib only** — see note below | R4 |
| pytesseract | Floor-plan OCR | Cheap non-model first pass before spending on vision | R5 |

### On curl_cffi

It impersonates browser TLS/JA3 fingerprints and would fix the failure documented in the
old Facebook adapter (`aiohttp gets 1357054`). But shipping fingerprint impersonation in
a public tool's default path changes what the project looks like.

**Rules:** out of the base install; used only in credentialed opt-in adapters where the
user is authenticated as themselves; everything else respects `robots.txt`.

## Development

| Tool | Role | Note |
|---|---|---|
| uv | Env + dependencies | Lockfile, fast installs; replaces the hand-built venv in the old README |
| pytest + pytest-asyncio | Tests | 226 ported tests are the spec — ported before the code they cover |
| respx | HTTP mocking | Source tests run offline against recorded fixtures |
| ruff | Lint + format | One tool, replaces flake8 + black + isort |
| mypy --strict | Types | Pydantic makes this cheap; the `Field[T]` union is where it pays |
| GitHub Actions | CI | Matrix 3.11 / 3.12 / 3.13 — would have caught every path failure in the old repo |

## Repository

- **Name**: repository and command `nostos`; PyPI distribution `nostos-cli` (the bare `nostos` is taken by an unrelated package). Agent surface `nostos-mcp`.
- **Public**, **Apache-2.0**. Apache over MIT for the explicit patent grant and §5's
  clear contribution terms — community adapters and citypacks are expected, and it is
  better to have that settled in the licence than in a CLA later. Over AGPL because AGPL
  deters contributors and would not prevent the thing you would worry about anyway.
- **README must state the scraping posture**: robots.txt respected by default,
  conservative rate limits, credentials are the user's own, no hosted tier. That is both
  honest and what keeps the project from reading as an evasion toolkit.

## Adding a dependency

Justify it against: does it replace code we would otherwise write and maintain? Is it
maintained? Does it work on all three CI Python versions? Does it pull a build
toolchain a user would need (that is a hard no for anything in the R1/R2 install path)?
