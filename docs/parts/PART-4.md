# Part 4 — Banker deliverables (comps → Excel, tearsheet, earnings brief)

## Goal
Go beyond chat: run end-to-end workflows that produce the files bankers actually use.

## Why it matters for Rogo
Rogo sells "end-to-end financial workflows" via purpose-built agents (refreshing public
comps, reviewing models, reading memos). Producing a correct, formula-driven Excel comps
sheet is the most recognizable junior-banker task there is.

## Build
1. **Workflow framework** `analyst/workflows/base.py`: a `Workflow` has typed inputs, a
   sequence of steps (deterministic or agent-driven), and typed outputs (files + summary).
   Runs are stored in `workflow_runs(id, type, inputs JSON, status, outputs JSON, trace,
   cost_usd, created_at)`.
2. **Public comps** `workflows/comps.py`: input = target ticker (+ optional peer override).
   Steps: resolve peers → pull LTM revenue, EBITDA (op income + D&A), net income, debt,
   cash, shares, price → compute EV and multiples → stats rows (median, mean, 25th/75th).
   Output: `comps_{ticker}_{date}.xlsx` via openpyxl with
   - an **Inputs** sheet holding raw values (each with a cell comment carrying the
     accession / fact citation) and
   - a **Comps** sheet where multiples and stats are **live Excel formulas** referencing
     Inputs (not pasted values), banker formatting (`0.0x`, `$#,##0`, blue inputs / black
     formulas, frozen header), and an NM rule for negative/missing denominators.
   - a **Sources** sheet listing every filing used with URLs.
3. **Tearsheet** `workflows/tearsheet.py`: one-page company profile as **PDF** (and
   Markdown): business description (from Item 1, summarized by the agent with citations),
   5-year financial summary table, margin chart (matplotlib PNG), top 5 risk factors
   (Item 1A, cited), valuation snapshot from comps.
4. **Earnings brief** `workflows/earnings.py`: for the latest 10-Q vs prior year quarter —
   key metric deltas (deterministic), MD&A drivers (agent, cited), "what changed" in risk
   factors (diff of Item 1A vs prior 10-K, agent-summarized).
5. CLI: `uv run python -m analyst.workflows run comps --ticker MSFT` → prints output paths.

## Tests
- Comps on fixture data: open the xlsx with openpyxl, assert formulas exist in multiple
  cells, assert values computed by evaluating formulas (use `formulas` or LibreOffice
  headless recalculation if available; otherwise compare against `analyst.metrics`).
- NM handling, peer override, missing-metric peer excluded from stats with a note.

## Acceptance checks
- [ ] `pytest`, `ruff` clean.
- [ ] Live: generate comps for 3 targets (one per peer group), a tearsheet and an earnings
      brief; commit them to `examples/` so reviewers can open them without running anything.
- [ ] Open one comps sheet in Excel (Maxx does this by hand) — formulas recalc, no #REF/#DIV/0.
- [ ] README "Part 4" section with screenshots placeholders.

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 4 exactly as specified in
> docs/parts/PART-4.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand.
