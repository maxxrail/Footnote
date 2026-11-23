# analyst-agent

An AI financial analyst for public companies. Ask a question in plain English and get an
answer where **every number is computed deterministically and every claim is cited** to a
specific SEC filing passage or XBRL fact. It also runs banker workflows end to end: public
comps to a formula-driven Excel sheet, one-page tearsheets, and earnings briefs. A real eval
harness measures how accurate it is.

> Status: Part 1 of 6 done (v0.1.0, data layer). See [PROGRESS.md](PROGRESS.md).

## What's inside (built in 6 parts)
1. **Data layer**: SEC EDGAR + XBRL ingestion into Postgres, normalized and traceable
2. **Retrieval**: section-aware hybrid search over 10-K/10-Q text with span-level citations
3. **Agent**: tool-using LLM analyst; math only in tested Python; number post-check; honest refusals
4. **Deliverables**: comps → Excel (live formulas), tearsheet PDF, earnings brief
5. **Evals**: auto-generated golden set from XBRL ground truth, citation validity, ablations, CI gate
6. **Product**: FastAPI + React UI with clickable citations, Docker, deployed demo, cost/latency tracing

## Building it with Claude Code
Open this folder in Claude Code and paste the prompt at the bottom of
`docs/parts/PART-N.md` for the current part (see `PROGRESS.md`). Claude Code reads
`CLAUDE.md` for the rules and builds one part per session.

## Part 1: Financial data layer

Pulls companies, filings and XBRL facts from SEC EDGAR into Postgres. Every value keeps its
provenance: concept, period, unit, filing accession and a fact id.

- **SEC client** (`analyst/edgar/client.py`): sends `SEC_USER_AGENT` on every request,
  stays under 8 req/s, retries 429/5xx with backoff, and caches every response under
  `data/raw/`, so re-runs cost no SEC requests.
- **Normalizer** (`analyst/edgar/concepts.py`): companies tag the same line item
  differently and change tags over time. Thirteen canonical items (`revenue`, `net_income`,
  `eps_diluted`, `total_assets`, `cash`, `total_debt`, `operating_cash_flow`, `capex`, …)
  map to ordered candidate tags. `get_series(session, cik, "revenue", "FY")` returns one
  value per period, from annual 10-K facts with ~365-day durations. When a value was
  restated, the latest filing wins. Each point carries its fact id and accession.
- **Quarters**: `--period Q` labels rows with the company's own fiscal year and quarter.
  Q4 isn't in any 10-Q, so for flow items like revenue it is derived as FY − (Q1+Q2+Q3) in
  `analyst/metrics/quarters.py`, with all four source facts cited. It is never derived for EPS.
- **Ingest** is idempotent: upserts write only rows whose values changed. It also saves
  the last 3 10-K and 4 10-Q documents to `data/raw/filings/` for Part 2.

```powershell
copy .env.example .env                 # set SEC_USER_AGENT="Your Name you@example.com"
docker compose up -d db                # Postgres on localhost:5433
uv sync --extra dev
uv run alembic upgrade head
uv run python -m analyst.edgar.ingest --universe data/universe.csv   # ~6 min cold, 0 requests warm
uv run python -m analyst.edgar.ingest --tickers AAPL MSFT            # or a few tickers
uv run python -m analyst.edgar.show AAPL revenue                     # FY table with accessions
uv run python -m analyst.edgar.show MSFT revenue --period Q
uv run pytest -rs                      # offline: recorded fixtures, no network
```

Re-fetch new filings with `--refresh`. Design notes: [ADR 0001](docs/adr/0001-idempotent-cached-ingest.md)
(caching and idempotency) and [ADR 0002](docs/adr/0002-per-period-concept-fallback.md)
(concept fallback).

## Quick start (fills in as parts land)
```powershell
copy .env.example .env      # add SEC_USER_AGENT and ANTHROPIC_API_KEY
docker compose up -d
uv sync --extra dev
uv run alembic upgrade head
uv run python -m analyst.edgar.ingest --universe data/universe.csv
```
