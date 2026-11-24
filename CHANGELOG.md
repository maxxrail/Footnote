# Changelog

All notable changes are listed here, one version per part.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-11-23: Part 2, filing search with citations

### Added
- `analyst/retrieval/parse.py`: filing HTML → clean text (inline XBRL header, hidden blocks,
  running headers/footers and page numbers dropped; tables as `cell | cell` rows) and
  10-K Item / 10-Q Part+Item detection that skips the table of contents (ADR 0003).
- `analyst/retrieval/chunk.py`: ~800-token chunks with ~100-token overlap that never cross a
  section; every chunk keeps its exact `char_start:char_end` span.
- DB: `filing_texts`, `filing_sections`, `chunks` (pgvector `vector(384)` with HNSW, generated
  `tsvector` with GIN). Migration `183713853069`.
- `analyst/retrieval/embed.py`: `Embedder` protocol, local `BAAI/bge-small-en-v1.5` default
  (install with `uv sync --extra ml`), embeds only pending chunks.
- `python -m analyst.retrieval.index`: idempotent indexer (re-parses only when the HTML or
  `PARSER_VERSION` changes).
- `analyst/retrieval/search.py`: `search(query, cik, forms, sections, since, k)`, vector top-50 +
  Postgres full-text top-50 fused with RRF (k=60), optional `BAAI/bge-reranker-base` rerank
  (`RERANK=true` or `--rerank`). Returns `Citation`s with a hash of the cited text;
  `resolve()` returns the exact span and `verify()` checks it. Form/section filters ignore case.
- `python -m analyst.retrieval.cli "supply chain risk" --ticker NVDA`.
- Contextual chunk headers (`chunks.context`: company, ticker, form, filing date, section),
  indexed in full-text and embedded with the chunk, never cited. Migration `5e1f0c2a9b7d`,
  `PARSER_VERSION = 2`.
- `tests/fixtures/retrieval_gold.yaml` (15 queries), `tests/fixtures/retrieval_holdout.yaml`
  (10 held-out queries) and `python -m analyst.retrieval.evaluate [--gold PATH]` (recall@k per
  retriever).
- `scripts/section_survey.py`, `scripts/mutation_check_part2.py` (37 mutations), ADRs 0003-0004.

### Known gaps
- Retrieval eval (recall@5, open / filtered): "hybrid ≥ both" holds on the held-out set (hybrid
  10/10, vector 10/10, full-text 8/10) but not on the gold set (hybrid 14/15 vs vector 15/15,
  miss `orcl-capex`). Hybrid + rerank: 15/15 and 10/10. 450-token chunks tie with 800. See ADR 0004.
- WFC/USB Risk Factors and MD&A live in Exhibit 13, which isn't downloaded.

### Fixed
- Test DB isolation: test tables are always created in the throwaway schema, the fixture
  refuses to truncate if any table resolves to `public`, and code under test can't fall back
  to the default engine.

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
  per-period fallback across tags, and keeps the latest filing on restatements (even when the
  restatement uses a different tag).
- `python -m analyst.edgar.ingest`: idempotent ingest (`--tickers`, `--universe`,
  `--refresh`, `--no-docs`). Downloads the last 3 10-K and 4 10-Q primary documents.
- `python -m analyst.edgar.show TICKER ITEM [--period Q]`. Quarterly rows are labelled with the
  company's fiscal year and quarter (e.g. `FY2026 Q1`).
- `analyst/metrics/quarters.py`: derived Q4 = FY − (Q1+Q2+Q3) for additive flow items. It cites
  all four input facts and refuses for EPS or when a quarter is missing.

### Fixed
- CI and evals workflows failed to parse (`hashFiles()` in a job-level `if:`). They now use gate jobs.
- `data/universe.csv`: 25 companies in 3 peer groups (tech/software, semis, US banks).
- Recorded AAPL/MSFT fixtures, 72 offline tests (sockets blocked except loopback), `scripts/mutation_check.py`,
  `scripts/record_fixtures.py`, ADRs 0001-0002.

### Added
- Project plan: six part specs, CLAUDE.md, Claude Code commands and reviewer agent,
  CI, eval workflow, templates.
