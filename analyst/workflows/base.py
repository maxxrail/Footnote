"""Workflow framework: typed inputs, a sequence of steps, typed outputs, a stored run.

A `Workflow` subclass declares `name`, an `Inputs` Pydantic model and `steps(inputs)`, a
list of `Step(name, kind, fn)`. Each step takes and returns the run's `state` dict.
Deterministic steps do lookups and math. Agent steps call the analyst agent and add its
cost. `run()` times every step, stores the run in `workflow_runs` (inputs, status,
outputs, trace, cost) and returns `Result(files, summary, ...)`.
"""

from __future__ import annotations

import datetime as dt
import time
import traceback
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from analyst.agent.loop import Answer
from analyst.agent.loop import run as run_agent
from analyst.agent.tools import Excerpt, ToolBox
from analyst.db.models import WorkflowRun
from analyst.llm.client import LLMClient
from analyst.metrics.prices import PriceProvider
from analyst.retrieval.embed import Embedder

SessionFactory = Callable[[], AbstractContextManager[Session]]
StepKind = Literal["deterministic", "agent"]
State = dict[str, Any]


@dataclass
class Context:
    """What every workflow needs: the DB, prices, the universe, where to write, the LLM."""

    session_factory: SessionFactory
    prices: PriceProvider
    universe_path: Path
    out_dir: Path
    llm: LLMClient | None = None  # agent steps need it; deterministic workflows don't
    embedder: Embedder | None = None
    today: dt.date = field(default_factory=dt.date.today)

    def toolbox(self) -> ToolBox:
        return ToolBox(self.session_factory, self.prices, self.universe_path, self.embedder)

    def ask(self, question: str, state: State, context: Sequence[Excerpt] = ()) -> Answer:
        """Run the analyst agent (an agent step). Its cost and trace go into the run."""
        if self.llm is None:
            raise RuntimeError("this step needs an LLM; pass --no-agent to skip agent steps")
        box = self.toolbox()
        answer = run_agent(question, self.llm, box, context=context)
        state.setdefault("_answers", []).append(answer)
        return answer


@dataclass(frozen=True)
class Step:
    name: str
    kind: StepKind
    fn: Callable[[State, Context], State]


class StepRecord(BaseModel):
    name: str
    kind: StepKind
    ms: int
    cost_usd: Decimal | None = None
    error: str | None = None


class Result(BaseModel):
    run_id: int
    status: Literal["succeeded", "failed"]
    files: list[Path]
    summary: str
    steps: list[StepRecord]
    cost_usd: Decimal | None
    error: str | None = None


class Workflow:
    name: ClassVar[str]
    Inputs: ClassVar[type[BaseModel]]

    def steps(self, inputs: Any) -> list[Step]:  # pragma: no cover - abstract
        raise NotImplementedError

    def outputs(self, state: State) -> tuple[list[Path], str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, inputs: BaseModel | dict[str, Any], ctx: Context) -> Result:
        args = inputs if isinstance(inputs, BaseModel) else self.Inputs.model_validate(inputs)
        with ctx.session_factory() as s:
            row = WorkflowRun(type=self.name, inputs=args.model_dump(mode="json"), status="running")
            s.add(row)
            s.flush()
            run_id = row.id
        state: State = {"inputs": args}
        records: list[StepRecord] = []
        cost: Decimal | None = Decimal(0)
        error = None
        for step in self.steps(args):
            t0 = time.perf_counter()
            before = len(state.get("_answers", []))
            try:
                state = step.fn(state, ctx)
            except Exception as exc:
                error = f"{step.name}: {type(exc).__name__}: {exc}"
                records.append(
                    StepRecord(
                        name=step.name,
                        kind=step.kind,
                        ms=int((time.perf_counter() - t0) * 1000),
                        error=traceback.format_exc(limit=3),
                    )
                )
                break
            step_cost = _answers_cost(state.get("_answers", [])[before:])
            if step_cost is None or cost is None:
                cost = None
            else:
                cost += step_cost
            records.append(
                StepRecord(
                    name=step.name,
                    kind=step.kind,
                    ms=int((time.perf_counter() - t0) * 1000),
                    cost_usd=step_cost,
                )
            )
        files, summary = ([], error or "") if error else self.outputs(state)
        result = Result(
            run_id=run_id,
            status="failed" if error else "succeeded",
            files=files,
            summary=summary,
            steps=records,
            cost_usd=cost,
            error=error,
        )
        with ctx.session_factory() as s:
            stored = s.get(WorkflowRun, run_id)
            assert stored is not None
            stored.status = result.status
            stored.outputs = {"files": [str(f) for f in files], "summary": summary}
            stored.trace = {
                "steps": [r.model_dump(mode="json") for r in records],
                "agent_runs": [a.trace.model_dump(mode="json") for a in state.get("_answers", [])],
            }
            stored.cost_usd = cost
        return result


def _answers_cost(answers: Sequence[Answer]) -> Decimal | None:
    total: Decimal | None = Decimal(0)
    for a in answers:
        c = a.trace.usage.cost_usd
        total = None if c is None or total is None else total + c
    return total
