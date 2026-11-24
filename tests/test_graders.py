"""Graders against the synthetic CHIP corpus: numeric match, citation validity, refusal,
and the judge prompt (with a fake judge). No network, no LLM."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.agent.citations import FactCitation
from analyst.agent.loop import Answer, Trace
from analyst.agent.tools import ToolBox
from analyst.evals.datasets import ExpectedSection, Item
from analyst.evals.graders import (
    JudgeResult,
    citation_valid,
    headline,
    judge_prompt,
    llm_judge,
    numeric_match,
    refusal_correct,
    resolves,
    section_hit,
)
from analyst.metrics.prices import CsvPriceProvider
from analyst.retrieval import index as index_mod
from analyst.retrieval.citation import Citation
from analyst.retrieval.embed import HashEmbedder
from tests.fin_corpus import UNIVERSE_CSV, SessionFactory, seed


@pytest.fixture
def box(session_factory: SessionFactory, tmp_path: Path) -> ToolBox:
    raw = tmp_path / "raw"
    seed(session_factory, raw)
    index_mod.run(raw, session_factory, embedder=HashEmbedder())
    (tmp_path / "u.csv").write_text(UNIVERSE_CSV, encoding="utf-8")
    (tmp_path / "p.csv").write_text("ticker,date,close\nCHIP,2026-11-20,50\n", encoding="utf-8")
    return ToolBox(
        session_factory, CsvPriceProvider(tmp_path / "p.csv"), tmp_path / "u.csv", HashEmbedder()
    )


def answer(text: str, citations: list, refused: bool = False) -> Answer:
    return Answer(
        question="q",
        text=text,
        citations=citations,
        confidence="high",
        refused=refused,
        trace=Trace(),
    )


def num_item(expected: str, unit: str) -> Item:
    return Item(id="n", category="numeric", question="q", expected=expected, unit=unit)


def fact(box: ToolBox, metric: str = "revenue", year: int = 2025) -> FactCitation:
    out = json.loads(
        box.run("get_financials", {"ticker": "CHIP", "metrics": [metric], "fiscal_years": [year]})
    )
    c = box.refs[out["results"][0]["values"][0]["ref"]]
    assert isinstance(c, FactCitation)
    return c


# -- numeric_match -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected", "unit", "ok"),
    [
        ("Revenue was $1.50B in FY2025 [^1].", "1500000000", "USD", True),
        ("Revenue was $1.5 billion [^1].", "1523000000", "USD", True),  # written rounding
        ("Revenue was $1.6 billion [^1].", "1500000000", "USD", False),
        (
            "For the year ended December 31, 2025, revenue was $1,500 million.",
            "1500000000",
            "USD",
            True,
        ),
        ("Revenue grew 25.0% from $1.2B to $1.5B.", "0.25", "ratio", True),  # % before $
        ("Revenue went from $1.2B to $1.5B, up 25.0%.", "0.25", "ratio", True),  # $ first
        ("Revenue grew 26%.", "0.25", "ratio", False),
        ("Revenue fell 5.7% a year.", "-0.0571", "ratio", True),
        ("Revenue grew 5.7% a year.", "-0.0571", "ratio", False),  # wrong direction
        ("The CAGR was -5.7%.", "-0.0571", "ratio", True),
        ("Diluted EPS was $3.60.", "3.6", "USD/shares", True),
        ("EV/Revenue is 3.2x.", "3.2333", "x", True),
        ("EV/Revenue is about 3.", "3.2333", "x", True),  # bare number, rounded to the unit
        ("No figure here.", "1", "USD", False),
    ],
)
def test_numeric_match(text: str, expected: str, unit: str, ok: bool) -> None:
    assert numeric_match(answer(text, []), num_item(expected, unit)).passed is ok


def test_numeric_match_within_half_a_percent_even_if_written_precisely() -> None:
    # 0.5% tolerance: $1,503M against 1,500M passes, $1,510M doesn't
    item = num_item("1500000000", "USD")
    assert numeric_match(answer("Revenue was $1,503 million.", []), item).passed
    assert not numeric_match(answer("Revenue was $1,510 million.", []), item).passed


def test_refused_answer_fails_numeric() -> None:
    g = numeric_match(answer("Not in the filings.", [], refused=True), num_item("1", "USD"))
    assert not g.passed and g.detail == "refused"


def test_headline_skips_labels() -> None:
    n = headline("In FY2025 (ended 2025-12-31, per the 10-K Item 7) revenue was $1.5B.", "USD")
    assert n is not None and n.raw == "$1.5B"


# -- citation_valid ----------------------------------------------------------------------------


def test_valid_fact_citation(box: ToolBox, session_factory: SessionFactory) -> None:
    c = fact(box)
    a = answer("Revenue was $1.50B [^1].", [c])
    with session_factory() as s:
        g = citation_valid(s, a, num_item("1500000000", "USD"))
    assert g.passed, g.detail
    assert g.extra["resolved"] == g.extra["total"] == 1


def test_tampered_fact_does_not_resolve(box: ToolBox, session_factory: SessionFactory) -> None:
    c = fact(box).model_copy(update={"value": "1600000000"})
    with session_factory() as s:
        assert not resolves(s, c)
        g = citation_valid(
            s, answer("Revenue was $1.60B [^1].", [c]), num_item("1500000000", "USD")
        )
    assert not g.passed and "don't resolve" in g.detail


def test_headline_cited_to_the_wrong_fact_fails(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    wrong = fact(box, "operating_income")  # 450M, not revenue
    with session_factory() as s:
        g = citation_valid(
            s, answer("Revenue was $1.50B [^1].", [wrong]), num_item("1500000000", "USD")
        )
    assert not g.passed and "not in its cited sources" in g.detail


def test_computed_citation_supports_a_percentage(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    out = json.loads(box.run("compute", {"metric": "growth", "tickers": ["CHIP"]}))
    c = box.refs[out["results"][0]["ref"]]
    with session_factory() as s:
        g = citation_valid(s, answer("Revenue grew 25.0% [^1].", [c]), num_item("0.25", "ratio"))
    assert g.passed, g.detail


def test_filing_span_supports_text_and_numbers(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    out = json.loads(
        box.run(
            "search_filings",
            {"query": "operating margin accelerators", "ticker": "CHIP", "section": "Item 7"},
        )
    )
    c = box.refs[out["results"][0]["ref"]]
    assert isinstance(c, Citation)
    item = Item(
        id="q",
        category="qualitative",
        question="q",
        sections=[ExpectedSection(ticker="CHIP", section="Item 7")],
    )
    with session_factory() as s:
        ok = citation_valid(s, answer("Operating margin expanded to 30 percent [^1].", [c]), item)
        bad = citation_valid(s, answer("Operating margin expanded to 45 percent [^1].", [c]), item)
        judged = citation_valid(s, answer("Demand was strong [^1].", [c]), item, unsupported=[1])
    assert ok.passed, ok.detail
    assert not bad.passed and "45 percent" in bad.detail
    assert not judged.passed and "judge found unsupported" in judged.detail
    assert section_hit(answer("x [^1]", [c]), item)


def test_no_citations_fails_but_a_bare_refusal_passes(session_factory: SessionFactory) -> None:
    item = Item(id="r", category="refusal", question="q")
    with session_factory() as s:
        assert citation_valid(s, answer("Not in the filings.", [], refused=True), item).passed
        assert not citation_valid(s, answer("Revenue was big.", []), num_item("1", "USD")).passed


# -- refusal -----------------------------------------------------------------------------------


def test_refusal_correct() -> None:
    should = Item(id="r", category="refusal", question="q")
    shouldnt = num_item("1", "USD")
    assert refusal_correct(answer("No.", [], refused=True), should).passed
    assert not refusal_correct(answer("It was $5.", []), should).passed
    assert not refusal_correct(answer("Not in the filings.", [], refused=True), shouldnt).passed
    assert refusal_correct(answer("It was $1.", []), shouldnt).passed


# -- judge -------------------------------------------------------------------------------------


class FakeJudge:
    model = "fake-judge"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def score(self, prompt: str) -> tuple[JudgeResult, object]:
        self.prompts.append(prompt)
        return JudgeResult(
            correct=4, complete=3, grounded=5, unsupported_sentences=[], rationale="ok"
        ), None


def test_judge_sees_key_points_sentences_and_cited_text(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    c = fact(box)
    item = Item(
        id="q",
        category="qualitative",
        question="How big is CHIP?",
        key_points=["Revenue was $1.5 billion in FY2025"],
    )
    a = answer("CHIP is a chip company. Revenue was $1.50B [^1].", [c])
    judge = FakeJudge()
    with session_factory() as s:
        result, _ = llm_judge(s, judge, item, a)
        prompt = judge_prompt(s, item, a)
    assert (result.correct, result.complete, result.grounded) == (4, 3, 5)
    assert "- Revenue was $1.5 billion in FY2025" in prompt
    assert "[1] CHIP is a chip company." in prompt and "[2] Revenue was $1.50B [^1]." in prompt
    assert "cites [^1]: XBRL fact us-gaap:Revenues" in prompt and "= 1500000000 USD" in prompt
    assert Decimal(c.value) == 1_500_000_000
