# Nostos

Nostos is a self-hosted rental-market watch that ranks listings using your own rubric.
It is open source software for personal operation, not a hosted SaaS product.

## Safety and scraping posture

- Respects `robots.txt` by default.
- Uses conservative default rate limits.
- Uses only credentials provided by the user for sources they can legally access.
- Does not include or operate any hosted data-collection tier.

## Project status

This repository currently contains the T1 skeleton:

- Python CLI package (`nostos-cli`) with command entrypoint `nostos`
- Strict lint/type/test tooling (`ruff`, `mypy --strict`, `pytest`)
- CI matrix for Python 3.11, 3.12, and 3.13

## Development

```bash
uv sync --all-groups
uv run nostos --help
uv run ruff check .
uv run mypy --strict src tests
uv run pytest
```

## License

Apache-2.0.
