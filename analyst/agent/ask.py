"""Ask the analyst agent a question.

uv run python -m analyst.agent.ask "How did NVDA's operating margin trend over 3 years?"
uv run python -m analyst.agent.ask "..." --json        # the full Answer as JSON

Prints the answer, its footnoted sources and a trace summary (steps, tokens, $ cost,
latency). Needs the Part 1 + 2 data (Postgres up, index built), the `ml` extra for the
local embedding model, and ANTHROPIC_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from analyst.agent.citations import AnyCitation, ComputedCitation, FactCitation
from analyst.agent.loop import Answer, run
from analyst.agent.tools import ToolBox
from analyst.config import get_settings
from analyst.db.session import session_scope
from analyst.llm.client import AnthropicLLM
from analyst.metrics.prices import CsvPriceProvider
from analyst.retrieval.citation import Citation


def describe(c: AnyCitation) -> str:
    if isinstance(c, Citation):
        return (
            f"{c.ticker} {c.form} filed {c.filed_at}, {c.section} ({c.section_title}), "
            f'accession {c.accession}, chars {c.char_start}-{c.char_end}: "{c.snippet[:120]}"'
        )
    if isinstance(c, FactCitation):
        return (
            f"{c.ticker} {c.canonical} = {c.value} {c.unit} ({c.concept}, {c.label}), "
            f"{c.form} accession {c.accession}, fact {c.fact_id}"
        )
    assert isinstance(c, ComputedCitation)
    value = c.value if c.value is not None else f"none ({c.note})"
    facts = ", ".join(f"{f.concept} {f.label} [{f.accession}]" for f in c.facts)
    note = f"; note: {c.note}" if c.note and c.value is not None else ""
    return f"{c.label} = {value} {c.unit}; {c.formula}; from {facts or 'no facts'}{note}"


def format_answer(a: Answer) -> str:
    lines = [a.text, ""]
    if a.citations:
        lines.append("Sources")
        lines += [f"[^{i}] {describe(c)}" for i, c in enumerate(a.citations, 1)]
        lines.append("")
    flags = []
    if a.refused:
        flags.append("REFUSED (data not in the corpus)")
    if a.unverified_numbers:
        flags.append(f"UNVERIFIED NUMBERS: {a.unverified_numbers}")
    if a.uncited_numbers:
        flags.append(f"UNCITED: {a.uncited_numbers}")
    if flags:
        lines += [*flags, ""]
    t = a.trace
    cost = f"${t.usage.cost_usd:.4f}" if t.usage.cost_usd is not None else "unknown"
    lines.append(
        f"Trace: {len(t.steps)} tool calls over {t.usage.llm_calls} model calls | "
        f"tokens in {t.usage.input_tokens:,} (+{t.usage.cache_read_tokens:,} cached) "
        f"out {t.usage.output_tokens:,} | cost {cost} | {t.ms / 1000:.1f} s | "
        f"confidence {a.confidence} | stop {t.stop}{' | repaired' if t.repaired else ''}"
    )
    lines += [f"  {s.tool}({s.input}) {s.ms} ms{' ERROR' if s.error else ''}" for s in t.steps]
    return "\n".join(lines)


def ask(question: str) -> Answer:  # pragma: no cover - live only
    settings = get_settings()
    toolbox = ToolBox(
        session_factory=session_scope,
        prices=CsvPriceProvider(settings.prices_csv),
        universe_path=settings.universe_csv,
    )
    return run(question, AnthropicLLM(), toolbox)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live only
    parser = argparse.ArgumentParser(prog="python -m analyst.agent.ask")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true", help="print the full Answer as JSON")
    args = parser.parse_args(argv)
    answer = ask(args.question)
    print(answer.model_dump_json(indent=2) if args.json else format_answer(answer))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
