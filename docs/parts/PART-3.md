# Part 3 — The analyst agent (tools, deterministic math, cited answers)

## Goal
A tool-using agent that answers analyst questions ("How did NVDA's operating margin trend
over the last 3 years, and what did management attribute it to?") by combining structured
facts, deterministic calculations and filing search — and returns an answer where every
number and claim carries a citation.

## Why it matters for Rogo
This is the product: "an agent that does the work of a world-class analyst, reliably."
Reliability in finance = correct numbers + verifiable sources + honest refusals.

## Build
1. **Metrics library** `analyst/metrics/` (pure functions, 100% unit tested): growth
   (YoY, CAGR), margins, LTM from quarters, EV = mkt cap + debt − cash, EV/Revenue,
   EV/EBITDA, P/E, net debt/EBITDA, median/mean/quartiles for peer sets. Every result is a
   `Computed(value, formula, inputs=[fact_ids...])` so its lineage is traceable.
   Market price: a `PriceProvider` protocol; default reads `data/prices.csv`
   (ticker, date, close) that the user can refresh; document this limitation honestly.
2. **Tools** `analyst/agent/tools.py` (JSON-schema defined, Pydantic validated):
   - `resolve_company(query)` → cik/ticker/name (fuzzy)
   - `get_financials(ticker, metrics[], periods, period_type)` → values + fact citations
   - `compute(metric, ticker(s), period)` → `Computed` with lineage
   - `search_filings(query, ticker?, form?, section?, since?)` → snippets + citations
   - `list_peers(ticker)` → peer group from `universe.csv`
3. **Agent loop** `analyst/agent/loop.py`: Anthropic tool-use loop, max 10 steps, parallel
   tool calls allowed, per-run token + cost accounting, full trace object
   (`steps: [{tool, input, output_summary, ms}]`). System prompt in
   `analyst/agent/prompts/system.md` instructs: use tools for every number, never compute
   in prose, cite with `[^n]` markers, say "not in the filings" when unsupported.
4. **Structured answer**: final answer is parsed into
   `Answer(text, citations: list[Citation | FactCitation | ComputedCitation], confidence,
   refused: bool, trace)`. A **post-check** scans `text` for numbers; every number must
   match (within rounding) a tool output in the trace, otherwise the answer is flagged
   `unverified_numbers=[...]` and the loop gets one repair turn.
5. CLI: `uv run python -m analyst.agent.ask "..."` prints the answer, footnoted sources and
   the trace summary (steps, tokens, $ cost, latency).

## Tests
- Metrics: hand-computed cases incl. negative earnings (P/E → None), missing data, LTM.
- Agent with a **scripted fake LLM** that emits predetermined tool calls: assert tools run,
  citations flow through, the number post-check catches an invented number.

## Acceptance checks
- [ ] `pytest`, `ruff` clean; metrics coverage 100% (`pytest --cov=analyst/metrics`).
- [ ] Live (`-m live`): 5 sample questions in `docs/sample_questions.md` answered with
      citations; paste outputs (trimmed) into PROGRESS.md.
- [ ] Asking about a company **not** in the universe, or a fact not in filings, yields a
      refusal, not a guess.
- [ ] README "Part 3" section with an architecture diagram (Mermaid).

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 3 exactly as specified in
> docs/parts/PART-3.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand.
