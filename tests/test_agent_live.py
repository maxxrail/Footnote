"""Live acceptance run for Part 3: real model, real corpus. Skipped unless `-m live`.

    uv run pytest -m live tests/test_agent_live.py -s

Needs Postgres with the Part 1 + 2 data, the `ml` extra and ANTHROPIC_API_KEY. Costs a
little money (a few cents per question). Reads the DB only. Each answer is printed and
appended to `evals/results/part3_live.md` (gitignored).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from analyst.agent.ask import ask, format_answer, safe_print
from analyst.agent.loop import Answer
from analyst.db.session import session_scope
from analyst.retrieval.citation import Citation, verify

pytestmark = pytest.mark.live

DOC = Path(__file__).parents[1] / "docs" / "sample_questions.md"
OUT = Path(__file__).parents[1] / "evals" / "results" / "part3_live.md"


def _questions() -> tuple[list[str], list[str]]:
    text = DOC.read_text(encoding="utf-8")
    samples = re.findall(r"^\d+\. (.+)$", text, re.M)
    refusals = re.findall(r"^- R\d \([^)]*\): (.+)$", text, re.M)
    return samples, refusals


SAMPLES, REFUSALS = _questions()


def _record(a: Answer) -> None:
    out = format_answer(a)
    safe_print(f"\n=== {a.question}\n{out}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(f"## {a.question}\n\n```\n{out}\n```\n\n")


def test_doc_has_five_samples_and_refusals() -> None:
    assert len(SAMPLES) == 5 and len(REFUSALS) >= 2


@pytest.mark.parametrize("question", SAMPLES)
def test_sample_question_is_answered_with_citations(question: str) -> None:
    a = ask(question)
    _record(a)
    assert not a.refused, a.text
    assert a.citations, "no citations"
    assert a.unverified_numbers == [], a.unverified_numbers
    assert a.uncited_numbers == [], a.uncited_numbers
    with session_scope() as s:
        for c in a.citations:
            if isinstance(c, Citation):
                assert verify(c, s), c


@pytest.mark.parametrize("question", REFUSALS)
def test_refusal(question: str) -> None:
    a = ask(question)
    _record(a)
    assert a.refused, a.text
    assert a.unverified_numbers == []
