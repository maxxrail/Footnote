"""Graders: numeric match, citation validity, refusal correctness, LLM judge.

All but the judge are deterministic and check the answer against the database, not
against the agent's own tool output:

- `numeric_match`: the answer's headline number (the first number of the expected unit
  kind) equals the expected value within 0.5% or the rounding it was written with, with a
  consistent sign.
- `citation_valid`: every citation resolves against the DB:
  - a filing span still hashes to its text (`verify`);
  - an XBRL fact exists with that concept, accession and value;
  - a computed value's facts exist with those values.

  Then the citations must *support* the answer. For a numeric item, a citation in the
  headline sentence contains the number: a fact or computed value equal to it, or a filing
  span whose text contains it. For other answers, every sentence with a number must find it
  in a cited span or value, and sentences without numbers go to the judge (LLM fallback).
- `refusal_correct`: refused iff the item is a "should refuse" item.
- `llm_judge`: a judge model scores correct / complete / grounded (1-5) against the gold
  key points and the cited passages, and names unsupported sentences.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from analyst.agent.citations import AnyCitation, ComputedCitation, FactCitation
from analyst.agent.loop import Answer
from analyst.agent.postcheck import MARKER, SENTENCE, Num, extract
from analyst.db.models import Fact
from analyst.evals.datasets import Item
from analyst.retrieval.citation import Citation, resolve, verify

REL_TOL = Decimal("0.005")
PASSAGE_CHARS = 3000  # of each cited passage, shown to the judge
SPLIT = SENTENCE  # the post-check's splitter: answers are graded sentence by sentence


@dataclass
class Grade:
    passed: bool
    detail: str = ""
    extra: dict[str, object] = field(default_factory=dict)


# -- numeric -----------------------------------------------------------------------------------


def _kind_ok(n: Num, unit: str) -> bool:
    if unit == "ratio":
        return n.kind == "pct"
    if unit == "x":
        return n.kind == "mult" or n.bare
    return n.kind == "amount" and not n.shares  # USD, USD/shares


def _close(n: Num, expected: Decimal, unit: str) -> bool:
    exp = expected * 100 if unit == "ratio" else expected
    if n.sign and exp != 0 and (exp > 0) != (n.sign > 0):
        return False
    if not n.sign and exp < 0 and not n.neg_context:
        return False  # a decline written as growth
    diff = abs(n.value - abs(exp))
    return diff <= max(REL_TOL * abs(exp), n.tol)


def headline(text: str, unit: str) -> Num | None:
    """The first number in the answer of the expected kind (labels like years skipped)."""
    return next((n for n in extract(text) if not n.label and _kind_ok(n, unit)), None)


def numeric_match(answer: Answer, item: Item) -> Grade:
    assert item.expected is not None and item.unit is not None
    expected = Decimal(item.expected)
    if answer.refused:
        return Grade(False, "refused")
    n = headline(answer.text, item.unit)
    if n is None:
        return Grade(False, "no number of the expected kind")
    ok = _close(n, expected, item.unit)
    return Grade(ok, f"headline {n.raw} vs expected {item.expected} {item.unit}", {"got": n.raw})


# -- citations -------------------------------------------------------------------------------


def _fact_ok(s: Session, f: FactCitation) -> bool:
    row = s.get(Fact, f.fact_id)
    return (
        row is not None
        and row.concept == f.concept
        and row.accession == f.accession
        and row.value == Decimal(f.value)
    )


def resolves(s: Session, c: AnyCitation) -> bool:
    if isinstance(c, Citation):
        return verify(c, s)
    if isinstance(c, FactCitation):
        return _fact_ok(s, c)
    return all(_fact_ok(s, f) for f in c.facts)


def _citation_values(s: Session, c: AnyCitation) -> list[Decimal]:
    """Numbers a citation vouches for: a fact's value; a computed value and its inputs."""
    if isinstance(c, FactCitation):
        return [Decimal(c.value)]
    if isinstance(c, ComputedCitation):
        vals = [Decimal(c.value)] if c.value is not None else []
        return (
            vals
            + [Decimal(i.value) for i in c.inputs if i.value]
            + [Decimal(f.value) for f in c.facts]
        )
    return []


def _supports_number(s: Session, c: AnyCitation, n: Num) -> bool:
    if isinstance(c, Citation):
        text = resolve(c, s)
        return any(abs(m.value - n.value) <= n.tol for m in extract(text) if m.kind == n.kind)
    for v in _citation_values(s, c):
        for cand in (v, v * 100):  # a ratio cited for a percentage
            if abs(n.value - abs(cand)) <= max(n.tol, REL_TOL * abs(cand)):
                return True
    return False


def sentences(text: str) -> list[tuple[str, list[int]]]:
    """(sentence, its [^n] markers) for each sentence, table row or list item."""
    out = []
    for sent in SPLIT.split(text):
        if sent.strip():
            out.append((sent.strip(), [int(m) for m in MARKER.findall(sent)]))
    return out


def citation_valid(
    s: Session, answer: Answer, item: Item, unsupported: Sequence[int] = ()
) -> Grade:
    """All citations resolve, and they support the answer's numbers (and, through the
    judge's `unsupported` sentence indexes, its claims)."""
    cites = answer.citations
    if answer.refused and not cites:
        return Grade(True, "refusal without citations", {"resolved": 0, "total": 0})
    bad = [i for i, c in enumerate(cites, 1) if not resolves(s, c)]
    detail = []
    if bad:
        detail.append(f"citations that don't resolve: {bad}")
    if not cites:
        return Grade(False, "no citations", {"resolved": 0, "total": 0})
    unsupported_numbers = []
    for sent, refs in sentences(answer.text):
        cited = [cites[r - 1] for r in refs if 0 < r <= len(cites)]
        for n in (m for m in extract(sent) if not m.label):
            if not any(_supports_number(s, c, n) for c in cited):
                unsupported_numbers.append(n.raw)
    if item.category == "numeric" and item.unit:
        head = headline(answer.text, item.unit)
        if head is not None and head.raw in unsupported_numbers:
            detail.append(f"headline {head.raw} not in its cited sources")
    elif unsupported_numbers:
        detail.append(f"numbers not in their cited sources: {unsupported_numbers}")
    if unsupported:
        detail.append(f"claims the judge found unsupported: sentences {list(unsupported)}")
    passed = not detail
    extra = {
        "resolved": len(cites) - len(bad),
        "total": len(cites),
        "unsupported_numbers": unsupported_numbers,
    }
    return Grade(passed, "; ".join(detail) or "all citations resolve and support the answer", extra)


# -- refusal -------------------------------------------------------------------------------


def refusal_correct(answer: Answer, item: Item) -> Grade:
    should = item.category == "refusal"
    ok = answer.refused == should
    return Grade(ok, f"refused={answer.refused}, should refuse={should}")


# -- LLM judge -----------------------------------------------------------------------------


class JudgeResult(BaseModel):
    correct: int = Field(ge=1, le=5, description="Facts agree with the key points; nothing wrong")
    complete: int = Field(ge=1, le=5, description="Covers the key points")
    grounded: int = Field(ge=1, le=5, description="Every claim is supported by its cited passage")
    unsupported_sentences: list[int] = Field(
        description="Indexes (1-based) of answer sentences not supported by their cited passages"
    )
    rationale: str


class Judge(Protocol):
    model: str

    def score(self, prompt: str) -> tuple[JudgeResult, object]:
        """The judge's scores and the raw usage object (for cost)."""
        ...


JUDGE_SYSTEM = """You grade answers from a financial analyst agent against a gold answer.
Score three things from 1 (bad) to 5 (fully meets):
- correct: the answer's facts agree with the gold key points and contain no errors.
- complete: the answer covers the gold key points (wording may differ).
- grounded: each sentence is supported by the passages it cites (shown under each sentence).
List the 1-based indexes of sentences whose cited passages do not support them. A sentence
with no citation and a factual claim is unsupported. Be strict and literal; don't reward
length."""


def judge_prompt(s: Session, item: Item, answer: Answer) -> str:
    lines = [f"Question: {item.question}", "", "Gold key points:"]
    lines += [f"- {k}" for k in item.key_points]
    lines += ["", "Answer, one sentence per line, each followed by the passages it cites:"]
    for i, (sent, refs) in enumerate(sentences(answer.text), 1):
        lines.append(f"[{i}] {sent}")
        for r in refs:
            if 0 < r <= len(answer.citations):
                lines.append(
                    f"    cites [^{r}]: {_source_text(s, answer.citations[r - 1])[:PASSAGE_CHARS]}"
                )
    return "\n".join(lines)


def _source_text(s: Session, c: AnyCitation) -> str:
    if isinstance(c, Citation):
        try:
            return " ".join(resolve(c, s).split())
        except (LookupError, ValueError):
            return "(span does not resolve)"
    if isinstance(c, FactCitation):
        return f"XBRL fact {c.concept} {c.label} = {c.value} {c.unit} ({c.form} {c.accession})"
    return f"Computed {c.label} = {c.value} {c.unit}: {c.formula}"


def llm_judge(s: Session, judge: Judge, item: Item, answer: Answer) -> tuple[JudgeResult, object]:
    return judge.score(judge_prompt(s, item, answer))


class AnthropicJudge:
    """The real judge: structured output parsed into `JudgeResult`."""

    def __init__(self, model: str | None = None) -> None:
        import anthropic

        from analyst.config import get_settings

        settings = get_settings()
        self.model = model or settings.anthropic_model
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def score(self, prompt: str) -> tuple[JudgeResult, object]:  # pragma: no cover - live
        resp = self._client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=JudgeResult,
        )
        parsed = resp.parsed_output
        if parsed is None:
            raise RuntimeError(f"judge returned no parsable output (stop {resp.stop_reason})")
        return parsed, resp.usage


def section_hit(answer: Answer, item: Item) -> bool:
    """An expected (ticker, section) is among the answer's filing citations."""
    want = {(e.ticker, e.section) for e in item.sections}
    got = {(c.ticker, c.section) for c in answer.citations if isinstance(c, Citation)}
    return bool(want & got)
