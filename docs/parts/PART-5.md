# Part 5 — Evals (golden set, numeric + citation graders, CI gate)

## Goal
Prove the agent is accurate, with numbers, and stop regressions automatically.

## Why it matters for Rogo
Their own postings ask people to "design and run the evaluation and testing layer" for
agentic surfaces. A candidate who shows a real eval harness with honest scores stands out
far more than one with a slick demo.

## Build
1. **Golden set generator** `analyst/evals/build_gold.py`: auto-generate ~150 numeric
   questions **from XBRL ground truth** (templates × companies × years: "What was {co}'s
   FY{y} diluted EPS?", "{co} revenue growth FY{y-1}→FY{y}?", "EV/Revenue for {co}?").
   Expected answers come from `analyst.metrics`, never the LLM. Plus a hand-written set
   of ~30 qualitative questions with expected (ticker, section) and key points, and ~15
   "should refuse" questions. Store in `analyst/evals/datasets/*.jsonl` with a version tag.
2. **Graders** `analyst/evals/graders.py`:
   - `numeric_match`: extract the headline number, compare with tolerance (0.5% or rounding).
   - `citation_valid`: every citation resolves (Part 2 resolver) and the cited span/fact
     actually contains the number or supports the claim (string/number match first,
     LLM-judge fallback).
   - `refusal_correct`: refused iff it should.
   - `llm_judge` for qualitative: rubric (correct, complete, grounded) scored 1–5 by a
     judge model with the gold key points; report judge agreement on 20 items Maxx labels
     by hand (`datasets/human_labels.jsonl`).
3. **Runner** `uv run python -m analyst.evals.run --dataset all --concurrency 4`:
   writes `evals/results/{timestamp}/results.jsonl` + `summary.md` with accuracy per
   category, citation validity, refusal precision/recall, p50/p95 latency, $ per query,
   and a list of failures with traces.
4. **Ablations**: run with (a) no filing search, (b) no number post-check, (c) vector-only
   retrieval — table in README showing what each component buys.
5. **Eval seed** `uv run python -m analyst.evals.seed`: loads a fixed eval corpus from
   committed fixtures (no live SEC calls) so `.github/workflows/evals.yml` can run on a
   fresh database. That workflow already exists; make its commands work, and support
   `--gate`, `--markdown`, `--out`, `--budget` and `--model` in the runner.
6. **CI**: GitHub Actions workflow — unit tests on every push; a small **smoke eval**
   (20 items, fake-LLM replay of recorded responses so no key is needed) that fails the
   build if accuracy drops below the committed baseline.

## Acceptance checks
- [ ] Full eval run completed live; `summary.md` committed under `evals/baseline/`.
- [ ] README "Results" section: headline numbers + ablation table + 3 honest failure
      examples and what you'd do about them.
- [ ] CI green on GitHub.

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 5 exactly as specified in
> docs/parts/PART-5.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand (including the 20 human labels).
