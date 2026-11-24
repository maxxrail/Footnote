# Changelog

All notable changes are listed here, one version per part.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] - 2026-11-24: Part 4, banker deliverables

### Added
- `analyst/workflows/base.py`: `Workflow` with typed inputs, deterministic or agent `Step`s and
  typed outputs. Runs are stored in `workflow_runs` (inputs, status, outputs, trace, cost_usd,
  created_at); migration `dca8d10fc542`.
- `analyst/workflows/comps.py`: public comps to `comps_{TICKER}_{date}.xlsx`.
  - Inputs sheet: blue values, each with a citation comment.
  - Comps sheet: live formulas, NM rule, peer median/mean/quartiles, `0.0x` and `$#,##0,,` formats, frozen header.
  - Sources sheet. Peer override with `--peers` (ADR 0006).
- `analyst/workflows/tearsheet.py`: one-page PDF + Markdown.
  - Agent-written business summary (Item 1) and top 5 risks (Item 1A), cited.
  - 5-year XBRL table, matplotlib margin chart, valuation snapshot from comps.
  - Stock-split warning for per-share values.
- `analyst/workflows/earnings.py`: earnings brief.
  - Deterministic YoY deltas and margin changes in pp.
  - MD&A drivers (agent, cited).
  - Risk-factor paragraph diff of the 10-Q against the prior 10-K.
- `python -m analyst.workflows run {comps,tearsheet,earnings} --ticker T [--peers ...] [--out DIR]`.
- `loop.run(context=...)`: a workflow can hand the agent source material (filing excerpts,
  computed deltas) as citable refs that the post-check accepts.
- `fin.difference`; the post-check reads "pp", "percentage points", "bps" and "basis points".
- `examples/`: comps for MSFT, NVDA and JPM, an NVDA tearsheet and an MSFT earnings brief.
- `scripts/mutation_check_part4.py`: 18 guarantees, each broken on purpose and caught.

### Changed
- Market cap uses the newer of the balance-sheet and cover-page share counts
  (`dei:EntityCommonStockSharesOutstanding`). This fills Citigroup, PNC and Texas Instruments;
  the same 400-day refusal and 100-day note apply.
- `.gitattributes`: `*.pdf`, `*.xlsx` and `*.png` are binary, so no line-ending conversion
  (it would corrupt the PDF).

## [0.3.0] - 2026-11-24: Part 3, the analyst agent

### Added
- `analyst/metrics/`: `Operand` / `Computed(value, formula, inputs)` with fact lineage;
  growth, CAGR, margins, LTM (contiguous quarters only), market cap, EV, EBITDA, EV/Revenue,
  EV/EBITDA, P/E (None for non-positive EPS), net debt/EBITDA, peer median/mean/quartiles
  (QUARTILE.INC). Exact `Decimal` math, 100% test coverage.
- `analyst/metrics/prices.py`: `PriceProvider` protocol and `CsvPriceProvider` over
  `data/prices.csv`. `scripts/fetch_prices.py` (`analyst/metrics/yahoo.py`) refreshes it with
  completed daily closes from Yahoo Finance (525 rows, 25 tickers, to 2026-11-23).
- Market metrics refuse a balance-sheet input more than 400 days older than the latest balance
  sheet (Citigroup's FY2010 share count), and note one older than 100 days.
- `analyst/llm/`: `LLMClient` protocol, `AnthropicLLM` (model from `ANTHROPIC_MODEL`, effort
  from `AGENT_EFFORT`, cached system prompt, server-side refusal fallback), per-run token and
  dollar accounting, and `ScriptedLLM`, a fake that replays tool calls for tests.
- `analyst/agent/tools.py`: `resolve_company`, `get_financials`, `compute`, `search_filings`,
  `list_peers`, `submit_answer`; JSON-schema definitions from Pydantic models. Every returned
  item has a run-wide `ref` mapped to a `FactCitation`, `ComputedCitation` or filing `Citation`.
- `analyst/agent/loop.py`: hand-written tool-use loop (max 10 steps, parallel tool calls in
  one result message, trace of every call with ms, tokens and cost) returning `Answer`.
- `analyst/agent/postcheck.py`: number post-check against the cited ref's typed values
  (kind: %, x, dollars, shares; sign; written rounding; scale words; uncited, miscited and unknown refs flagged;
  a non-refused answer must cite something) with one repair turn (ADR 0005).
- `python -m analyst.agent.ask "..."` CLI; `docs/sample_questions.md`; live test
  `tests/test_agent_live.py` (`-m live`).
- `scripts/mutation_check_part3.py`: 48 guarantees, each broken on purpose and caught.

### Changed
- Tests marked `live` skip the no-network and no-default-DB guards (they use the real API
  and read the real DB); all other tests keep both guards.

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
