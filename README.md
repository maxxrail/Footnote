# analyst-agent

An AI financial analyst for public companies. Ask a question in plain English and get an
answer where **every number is computed deterministically and every claim is cited** to a
specific SEC filing passage or XBRL fact. It also runs banker workflows end to end: public
comps to a formula-driven Excel sheet, one-page tearsheets, and earnings briefs. A real eval
harness measures how accurate it is.

> Status: under construction. See [PROGRESS.md](PROGRESS.md).

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

## Quick start (fills in as parts land)
```powershell
copy .env.example .env      # add SEC_USER_AGENT and ANTHROPIC_API_KEY
docker compose up -d
uv sync
uv run alembic upgrade head
uv run python -m analyst.edgar.ingest --universe data/universe.csv
```
