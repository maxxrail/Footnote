"""Run a workflow and print its output paths.

    uv run python -m analyst.workflows run comps --ticker MSFT
    uv run python -m analyst.workflows run comps --ticker MSFT --peers ORCL,CRM,ADBE
    uv run python -m analyst.workflows run tearsheet --ticker NVDA
    uv run python -m analyst.workflows run earnings --ticker JPM
    ... --out examples                 # where files go (default: output/)

comps is deterministic. tearsheet and earnings call the analyst agent (ANTHROPIC_API_KEY,
a few cents each) and the local embedding model (the `ml` extra).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from analyst.agent.ask import safe_print
from analyst.config import get_settings
from analyst.db.session import session_scope
from analyst.metrics.prices import CsvPriceProvider
from analyst.workflows.base import Context, Workflow
from analyst.workflows.comps import Comps
from analyst.workflows.earnings import Earnings
from analyst.workflows.tearsheet import Tearsheet

WORKFLOWS: dict[str, type[Workflow]] = {w.name: w for w in (Comps, Tearsheet, Earnings)}


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live only
    ap = argparse.ArgumentParser(prog="python -m analyst.workflows")
    sub = ap.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run a workflow")
    run_p.add_argument("workflow", choices=sorted(WORKFLOWS))
    run_p.add_argument("--ticker", required=True)
    run_p.add_argument("--peers", help="comps only: comma-separated peer override")
    run_p.add_argument("--out", type=Path, default=Path("output"))
    args = ap.parse_args(argv)

    settings = get_settings()
    llm = None
    if args.workflow != "comps":
        from analyst.llm.client import AnthropicLLM

        llm = AnthropicLLM()
    ctx = Context(
        session_factory=session_scope,
        prices=CsvPriceProvider(settings.prices_csv),
        universe_path=settings.universe_csv,
        out_dir=args.out,
        llm=llm,
    )
    inputs: dict[str, object] = {"ticker": args.ticker}
    if args.peers:
        inputs["peers"] = [p for p in args.peers.split(",") if p.strip()]
    result = WORKFLOWS[args.workflow]().run(inputs, ctx)
    for f in result.files:
        safe_print(str(f))
    safe_print(result.summary)
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "unknown"
    steps = ", ".join(f"{s.name} {s.ms} ms" for s in result.steps)
    safe_print(f"run {result.run_id} {result.status} | cost {cost} | {steps}")
    if result.error:
        safe_print(f"ERROR: {result.error}")
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
