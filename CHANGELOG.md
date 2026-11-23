# Changelog

All notable changes are listed here, one version per part.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-11-23: Part 1, financial data layer

### Added
- Project setup: `uv`, ruff, mypy, pytest, pre-commit, `.env.example`, `docker-compose.yml`
  (Postgres 16 + pgvector on host port 5433), pydantic-settings config.
- `analyst/edgar/client.py`: SEC client with mandatory User-Agent, 8 req/s token bucket,
  retries with backoff on 429/5xx (honours `Retry-After`), on-disk cache under `data/raw/`.
- `analyst/db/`: `companies`, `filings`, `facts` models and the first Alembic migration.
  The unique fact key is `NULLS NOT DISTINCT`, so instant facts cannot be duplicated.
- `analyst/edgar/concepts.py`: 13 canonical line items mapped to ordered us-gaap/dei
  candidates. `get_series` returns FY or Q values with fact id + accession, applies
  per-period fallback across tags, and keeps the latest filing on restatements.
- `python -m analyst.edgar.ingest`: idempotent ingest (`--tickers`, `--universe`,
  `--refresh`, `--no-docs`). Downloads the last 3 10-K and 4 10-Q primary documents.
- `python -m analyst.edgar.show TICKER ITEM [--period Q]`.
- `data/universe.csv`: 25 companies in 3 peer groups (tech/software, semis, US banks).
- Recorded AAPL/MSFT fixtures, 57 offline tests, `scripts/mutation_check.py`,
  `scripts/record_fixtures.py`, ADRs 0001-0002.

### Added
- Project plan: six part specs, CLAUDE.md, Claude Code commands and reviewer agent,
  CI, eval workflow, templates.
