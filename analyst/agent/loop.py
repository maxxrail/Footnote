"""The agent loop: a hand-written Anthropic tool-use loop with a hard step limit.

    answer = run(question, llm, toolbox)

- At most `MAX_STEPS` model calls, the repair turn included. Several tool calls in one
  turn are all executed and their results go back in a single user message (one
  tool_result each, errors included).
- The model ends by calling `submit_answer`. The answer then goes through the number
  post-check (`analyst.agent.postcheck`). If a number doesn't match the cited tool output,
  isn't cited, or a `[^ref]` is unknown, the model gets **one** repair turn (an error result
  for its `submit_answer` call listing the problems). After that the answer is returned with
  the problems in `unverified_numbers` / `miscited_numbers` / `uncited_numbers`, and low
  confidence, never silently passed.
- Every model call and tool call is timed and recorded in `Answer.trace`, with tokens and
  dollar cost summed in `trace.usage`.

Why not the SDK's tool runner: the loop needs a fake LLM in tests, a hard step cap, a
per-step trace and the post-check's repair turn between the model's final call and the
end of the run (ADR 0005).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from analyst.agent import postcheck
from analyst.agent.citations import AnyCitation
from analyst.agent.tools import Excerpt, SubmitAnswerInput, ToolBox, ToolError
from analyst.llm.client import LLMClient, Usage

MAX_STEPS = 10
SUMMARY_CHARS = 200
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")


class Step(BaseModel):
    tool: str
    input: dict[str, Any]
    output_summary: str
    ms: int
    error: bool = False


class LLMCall(BaseModel):
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    ms: int


class TraceUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal | None = None
    llm_calls: int = 0


class Trace(BaseModel):
    steps: list[Step] = Field(default_factory=list)
    llm_calls: list[LLMCall] = Field(default_factory=list)
    usage: TraceUsage = Field(default_factory=TraceUsage)
    ms: int = 0
    repaired: bool = False
    stop: Literal["answered", "max_steps", "model_refusal"] = "answered"


class Answer(BaseModel):
    question: str
    text: str
    citations: list[AnyCitation]
    confidence: Literal["high", "medium", "low"]
    refused: bool
    unverified_numbers: list[str] = Field(default_factory=list)
    uncited_numbers: list[str] = Field(default_factory=list)
    miscited_numbers: list[str] = Field(default_factory=list)
    trace: Trace


def _summary(output: str) -> str:
    return output if len(output) <= SUMMARY_CHARS else output[:SUMMARY_CHARS] + "…"


def renumber(text: str, refs: dict[int, AnyCitation]) -> tuple[str, list[AnyCitation]]:
    """Renumber `[^ref]` markers 1..k in order of first appearance; the citations in that order."""
    order: dict[int, int] = {}
    for m in postcheck.MARKER.finditer(text):
        r = int(m.group(1))
        if r in refs and r not in order:
            order[r] = len(order) + 1
    new = postcheck.MARKER.sub(
        lambda m: f"[^{order[int(m.group(1))]}]" if int(m.group(1)) in order else m.group(0), text
    )
    return new, [refs[r] for r in order]


def run(
    question: str,
    llm: LLMClient,
    toolbox: ToolBox,
    max_steps: int = MAX_STEPS,
    system: str = SYSTEM_PROMPT,
    context: Sequence[Excerpt] = (),
    check_numbers: bool = True,
) -> Answer:
    """Answer `question`. `context` is source material given up front (a workflow's
    filing excerpts or computed values); it gets refs and is cited like tool output."""
    started = time.perf_counter()
    toolbox.refs.clear()  # refs are per run; a reused toolbox must not carry old ones
    tools = toolbox.definitions()
    trace = Trace()
    usage = Usage()
    outputs: list[str] = []  # every tool output, for the post-check
    prompt = question
    if context:
        provided = toolbox.preload(context)
        outputs.append(provided)
        prompt = (
            f"{question}\n\nSource material (already retrieved; cite it by `ref`, and use "
            f"the tools for anything else):\n{provided}"
        )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    repair_used = False
    final: tuple[SubmitAnswerInput, postcheck.CheckResult] | None = None

    def finish(sub: SubmitAnswerInput, res: postcheck.CheckResult) -> Answer:
        text, cites = renumber(sub.text, toolbox.refs)
        trace.ms = int((time.perf_counter() - started) * 1000)
        trace.usage = TraceUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=usage.cost_usd,
            llm_calls=usage.calls,
        )
        return Answer(
            question=question,
            text=text,
            citations=cites,
            confidence=sub.confidence if res.ok else "low",
            refused=sub.refused,
            unverified_numbers=res.unverified_numbers,
            uncited_numbers=res.uncited_numbers
            + [f"[^{r}]" for r in res.unknown_refs]
            + (["(answer cites nothing)"] if res.no_citations else []),
            miscited_numbers=res.miscited_numbers,
            trace=trace,
        )

    def review(sub: SubmitAnswerInput) -> postcheck.CheckResult:
        if not check_numbers:  # eval ablation: accept the first answer as given
            return postcheck.CheckResult()
        return postcheck.check(sub.text, outputs, question, toolbox.refs, sub.refused)

    step = 0
    while step < max_steps:  # the repair turn counts toward the limit
        step += 1
        t0 = time.perf_counter()
        resp = llm.create(system=system, messages=messages, tools=tools)
        trace.llm_calls.append(
            LLMCall(
                model=resp.model,
                stop_reason=resp.stop_reason,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                ms=int((time.perf_counter() - t0) * 1000),
            )
        )
        usage.add(resp.model, resp.usage)
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            trace.stop = "model_refusal"
            sub = SubmitAnswerInput(
                text="The model declined to answer this question.", confidence="low", refused=True
            )
            return finish(sub, postcheck.CheckResult())

        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            # A plain-text ending: treat it as the answer, but ask once for submit_answer.
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            sub = SubmitAnswerInput(text=text or "(no answer)", confidence="low", refused=False)
            res = review(sub)
            if repair_used:
                return finish(sub, res)
            repair_used = True
            trace.repaired = True
            messages.append(
                {
                    "role": "user",
                    "content": "Submit your final answer with the submit_answer tool. "
                    + res.problems(),
                }
            )
            final = (sub, res)
            continue

        results: list[dict[str, Any]] = []
        submitted: tuple[str, SubmitAnswerInput, postcheck.CheckResult] | None = None
        for b in sorted(uses, key=lambda u: u.name == "submit_answer"):
            ts = time.perf_counter()
            if b.name == "submit_answer":
                try:
                    sub = SubmitAnswerInput.model_validate(b.input)
                except ValidationError as exc:
                    out, err = f"invalid submit_answer input: {exc.errors(include_url=False)}", True
                else:
                    res = review(sub)
                    submitted = (b.id, sub, res)
                    out, err = ("accepted" if res.ok else res.problems()), not res.ok
            else:
                try:
                    out, err = toolbox.run(b.name, dict(b.input)), False
                except ToolError as exc:
                    out, err = f"error: {exc}", True
                except Exception as exc:  # a bug or bad data must not crash the run
                    out, err = f"internal error in {b.name}: {type(exc).__name__}: {exc}", True
                outputs.append(out)
            trace.steps.append(
                Step(
                    tool=b.name,
                    input=dict(b.input),
                    output_summary=_summary(out),
                    ms=int((time.perf_counter() - ts) * 1000),
                    error=err,
                )
            )
            results.append(
                {"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err}
            )

        if submitted is not None:
            _, sub, res = submitted
            if res.ok or repair_used:
                return finish(sub, res)
            repair_used = True
            trace.repaired = True
            final = (sub, res)
        messages.append({"role": "user", "content": results})

    trace.stop = "max_steps"
    if final is not None:
        return finish(*final)
    sub = SubmitAnswerInput(
        text=f"No answer within {max_steps} steps.", confidence="low", refused=False
    )
    return finish(sub, postcheck.CheckResult())
