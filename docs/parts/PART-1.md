# Part 1 — Financial data layer (SEC EDGAR + XBRL)

## Goal
Pull real financial data for a list of public companies from SEC EDGAR into Postgres, in a
clean, normalized shape the agent can trust. This is the foundation: Rogo's value depends on
"structured, clean" financial data, and so does ours.

## Why it matters for Rogo
Their Financial Data team's job is sourcing and normalizing structured and unstructured
financial data for the AI. Showing you can build a reliable, cached, idempotent ingestion
pipeline with provenance on every number is the first signal.

## Data sources (all free, no key — but `User-Agent` header is mandatory)
- Ticker → CIK map: `https://www.sec.gov/files/company_tickers.json`
- Company filings index: `https://data.sec.gov/submissions/CIK##########.json`
- XBRL facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- Filing documents: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}`

## Build
1. **Project setup**: `pyproject.toml` (uv), `ruff`, `pytest`, `mypy`, `.env.example`
   (`DATABASE_URL`, `SEC_USER_AGENT`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`),
   `docker-compose.yml` with a `db` service on `pgvector/pgvector:pg16` (user/password/db
   `analyst`, matching `.github/workflows/ci.yml`). Dev tools go in a `dev` optional-deps
   group so `uv sync --extra dev` works; configure mypy in `pyproject.toml`.
   `analyst/__init__.py` with `__version__`, and `analyst/config.py` with pydantic-settings.
   Run `uv run pre-commit install`. `.gitignore`, CI and templates already exist; keep them.
2. **SEC client** `analyst/edgar/client.py`: httpx client with the User-Agent, a token-bucket
   limiter (≤ 8 req/s), retries with backoff on 429/5xx, and an on-disk cache keyed by URL
   under `data/raw/`.
3. **DB models + Alembic migration** (`analyst/db/`):
   - `companies(cik PK, ticker, name, sic, sic_description, fiscal_year_end)`
   - `filings(accession PK, cik FK, form, filed_at, period_of_report, primary_doc, url)`
   - `facts(id, cik FK, concept, label, unit, value NUMERIC, period_start, period_end,
      fiscal_year, fiscal_period, form, accession FK, frame)` with a unique index on
      `(cik, concept, unit, period_start, period_end, accession)`.
4. **Concept normalizer** `analyst/edgar/concepts.py`: companies tag the same line item
   differently. Map a canonical set → ordered list of candidate us-gaap tags, e.g.
   - `revenue`: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`
   - `gross_profit`, `operating_income`, `net_income`, `eps_diluted`, `total_assets`,
     `total_liabilities`, `cash`, `total_debt`, `shares_outstanding`, `operating_cash_flow`,
     `capex`, `d_and_a`
   Provide `get_series(cik, canonical, period="FY"|"Q")` returning values **with the source
   fact id and accession** for each point. Pick annual values from 10-K facts with ~365-day
   durations; de-duplicate restated values by keeping the latest filing.
5. **Ingestion CLI** `uv run python -m analyst.edgar.ingest --tickers AAPL MSFT NVDA ...`
   and `--universe data/universe.csv`. Idempotent (upserts). Download the primary document
   HTML for the last 3 10-Ks and last 4 10-Qs per company into `data/raw/filings/` (Part 2
   will parse them). Ship a `data/universe.csv` of ~25 companies in 3 peer groups
   (e.g. mega-cap software, semiconductors, US banks).
6. **Quick check CLI** `uv run python -m analyst.edgar.show AAPL revenue` prints a small
   table of FY values with accession numbers.

## Tests (no network)
- Record trimmed fixture JSON for 2 companies into `tests/fixtures/`.
- Unit tests: CIK padding, rate limiter, cache hit, concept fallback order, FY-vs-quarter
  duration filter, restatement de-dupe, upsert idempotency (ingest twice → same row counts).

## Acceptance checks
- [ ] `docker compose up -d` then `uv run alembic upgrade head` succeeds.
- [ ] `uv run pytest -q` passes; `uv run ruff check .` clean.
- [ ] Live ingest of `data/universe.csv` finishes; a second run makes 0 new SEC requests
      (all cache hits) and changes 0 rows.
- [ ] `show AAPL revenue` returns ≥ 5 fiscal years, each with an accession number, and the
      latest value matches the 10-K (spot check one by hand and note it in PROGRESS.md).
- [ ] README has a "Part 1" section: what it does, how to run it.

## Out of scope
Parsing filing text, embeddings, the LLM. Do not touch them yet.

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 1 exactly as specified in
> docs/parts/PART-1.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand.
