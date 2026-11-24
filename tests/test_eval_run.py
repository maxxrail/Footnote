"""The eval runner end to end on the synthetic corpus: scripted fake LLM, fake judge, no
network. Grading per category, skips, budget, summary, gate, and record -> replay."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.config import get_settings
from analyst.evals.datasets import ExpectedSection, Item
from analyst.evals.graders import JudgeResult
from analyst.evals.replay import Recording, ReplayExhausted, ReplayLLM
from analyst.evals.run import Config, gate, markdown, run_items, summarize
from analyst.llm.fake import ScriptedLLM, last_tool_results, tool_call
from analyst.retrieval import index as index_mod
from analyst.retrieval.embed import HashEmbedder
from tests.fin_corpus import UNIVERSE_CSV, SessionFactory, seed

ITEMS = [
    Item(
        id="num-rev",
        category="numeric",
        question="What was Chip Corp's revenue in FY2025?",
        tickers=["CHIP"],
        template="fy_revenue",
        expected="1500000000",
        unit="USD",
    ),
    Item(
        id="num-wrong",
        category="numeric",
        question="What was Chip Corp's FY2025 net income?",
        tickers=["CHIP"],
        template="fy_net_income",
        expected="360000000",
        unit="USD",
    ),
    Item(
        id="qual",
        category="qualitative",
        question="Why did Chip Corp's margin expand?",
        tickers=["CHIP"],
        sections=[ExpectedSection(ticker="CHIP", section="Item 7")],
        key_points=["Data center demand for accelerators outgrew operating expenses"],
    ),
    Item(
        id="refuse-tsla", category="refusal", question="What was Tesla's revenue?", tickers=["TSLA"]
    ),
    Item(
        id="num-fab",
        category="numeric",
        question="FAB revenue?",
        tickers=["FAB"],
        template="fy_revenue",
        expected="1",
        unit="USD",
    ),
]


def submit(text: str, refused: bool = False) -> dict[str, Any]:
    return tool_call("submit_answer", text=text, confidence="high", refused=refused)


def first_ref(msgs: list[dict[str, Any]]) -> int:
    content = last_tool_results(msgs)[0]["content"]
    if content.startswith("error"):  # the ablation removed the tool
        return 0
    data = json.loads(content)
    item = data["results"][0]
    return int(item["values"][0]["ref"] if "values" in item else item["ref"])


SCRIPTS: dict[str, list[Any]] = {
    "num-rev": [
        [tool_call("get_financials", ticker="CHIP", metrics=["revenue"], fiscal_years=[2025])],
        lambda m: [submit(f"Chip Corp's FY2025 revenue was $1.50B [^{first_ref(m)}].")],
    ],
    "num-wrong": [  # cites the right fact but states the wrong number twice (repair fails)
        [tool_call("get_financials", ticker="CHIP", metrics=["net_income"], fiscal_years=[2025])],
        lambda m: [submit("Net income was $400M [^1].")],
        lambda m: [submit("Net income was $400M [^1].")],
    ],
    "qual": [
        [
            tool_call(
                "search_filings",
                query="operating margin accelerators",
                ticker="CHIP",
                section="Item 7",
            )
        ],
        lambda m: [
            submit(f"Data center demand for accelerators outgrew expenses [^{first_ref(m)}].")
            if first_ref(m)
            else submit("Not in the filings: no search tool.", refused=True)
        ],
    ],
    "refuse-tsla": [
        [tool_call("resolve_company", query="Tesla")],
        lambda m: [submit("Tesla is not covered, so it is not in the filings.", refused=True)],
    ],
}


class FakeJudge:
    model = "claude-sonnet-5-5"

    def score(self, prompt: str) -> tuple[JudgeResult, object]:
        usage = type("U", (), {"input_tokens": 1000, "output_tokens": 100})()
        return JudgeResult(
            correct=5, complete=4, grounded=5, unsupported_sentences=[], rationale="matches"
        ), usage


@pytest.fixture
def env(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    raw = tmp_path / "raw"
    seed(session_factory, raw)
    index_mod.run(raw, session_factory, embedder=HashEmbedder())
    data = tmp_path / "data"
    data.mkdir()
    (data / "universe.csv").write_text(UNIVERSE_CSV, encoding="utf-8")
    (data / "prices.csv").write_text("ticker,date,close\nCHIP,2026-11-20,50\n", encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(data))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


class PricedLLM(ScriptedLLM):
    model = "claude-sonnet-5-5"  # 100 in / 20 out tokens per call = $0.0004


def llm_factory(item_id: str) -> ScriptedLLM:
    return PricedLLM(list(SCRIPTS[item_id]))


def run(env: Path, session_factory: SessionFactory, **kw: Any):
    cfg = Config(datasets=["test"], concurrency=2, **kw)
    return run_items(ITEMS, cfg, session_factory, llm_factory, lambda _id: FakeJudge()), cfg


def test_runner_grades_every_category(env: Path, session_factory: SessionFactory) -> None:
    results, cfg = run(env, session_factory)
    by = {r.id: r for r in results}
    assert by["num-rev"].passed and by["num-rev"].citation["passed"]
    assert (
        by["num-wrong"].passed is False
        and "expected 360000000" in by["num-wrong"].numeric["detail"]
    )
    assert by["num-wrong"].flags["unverified"] == ["$400M"]
    assert by["qual"].passed and by["qual"].section_hit and by["qual"].judge["correct"] == 5
    assert by["refuse-tsla"].passed and by["refuse-tsla"].refused
    assert by["num-fab"].status == "skipped" and "FAB" in (by["num-fab"].skip_reason or "")
    assert all(r.latency_s is not None for r in results if r.status == "graded")
    assert by["qual"].cost_usd == Decimal("0.0038")  # 2 agent calls ($0.0008) + the judge
    s = summarize(results, cfg)
    assert (s["graded"], s["skipped"], s["errors"]) == (4, 1, 0)
    assert s["accuracy"] == 0.75 and s["numeric_accuracy"] == 0.5
    assert s["numeric_by_template"] == {"fy_net_income": 0.0, "fy_revenue": 1.0}
    assert s["refusal_precision"] == 1.0 and s["refusal_recall"] == 1.0
    assert s["qualitative_scores"] == {"correct": 5, "complete": 4, "grounded": 5}
    md = markdown(s, results)
    assert "**num-wrong** (numeric): expected 360000000" in md
    assert "1 × not in the loaded corpus: ['FAB']" in md


def test_ablations_change_what_the_agent_gets(env: Path, session_factory: SessionFactory) -> None:
    seen: dict[str, Any] = {}

    def spy(item_id: str) -> ScriptedLLM:
        llm = llm_factory(item_id)
        seen[item_id] = llm
        return llm

    cfg = Config(datasets=["test"], concurrency=1, ablation="no-search")
    results = run_items([ITEMS[2]], cfg, session_factory, spy, lambda _id: FakeJudge())
    tools = [t["name"] for t in seen["qual"].requests[0]["tools"]]
    assert "search_filings" not in tools
    assert results[0].status == "graded" and not results[0].passed  # the tool call errored
    cfg = Config(datasets=["test"], concurrency=1, ablation="no-postcheck")
    results = run_items([ITEMS[1]], cfg, session_factory, llm_factory, lambda _id: FakeJudge())
    assert results[0].flags == {} and len(results[0].steps) == 2  # no repair turn


def test_budget_stops_new_items(env: Path, session_factory: SessionFactory) -> None:
    cfg = Config(datasets=["test"], concurrency=1, budget=Decimal("0.001"))
    results = run_items(
        [ITEMS[2], ITEMS[2].model_copy(update={"id": "qual"})],
        cfg,
        session_factory,
        llm_factory,
        lambda _id: FakeJudge(),
    )
    assert [r.status for r in results] == ["graded", "skipped"]
    assert "budget" in (results[1].skip_reason or "")


def test_record_then_replay_reproduces_the_run(env: Path, session_factory: SessionFactory) -> None:
    path = env / "rec.json"
    live, _ = run(env, session_factory, record=path)
    replayed, _ = run(env, session_factory, replay=path)
    strip = lambda rs: [(r.id, r.status, r.passed, r.answer, r.citations) for r in rs]  # noqa: E731
    assert strip(replayed) == strip(live)
    rec = Recording.load(path)
    assert [e["kind"] for e in rec.items["qual"]] == ["llm", "llm", "judge"]
    with pytest.raises(ReplayExhausted):
        ReplayLLM(rec, "nope").create(system="", messages=[], tools=[])


def test_gate(tmp_path: Path) -> None:
    t = tmp_path / "thresholds.json"
    t.write_text(json.dumps({"smoke": {"accuracy": 0.8, "citation_valid": 0.9}}), encoding="utf-8")
    assert gate({"accuracy": 0.85, "citation_valid": 0.95, "errors": 0}, "smoke", t) == []
    below = gate({"accuracy": 0.7, "citation_valid": 0.95, "errors": 1}, "smoke", t)
    assert below == ["accuracy 0.7 < 0.8", "1 items errored"]
    assert gate({}, "other", t) == ["no thresholds for dataset 'other' in " + str(t)]
    assert gate({}, "smoke", tmp_path / "none.json")[0].startswith("no thresholds file")


def test_an_account_level_api_error_stops_the_run(
    env: Path, session_factory: SessionFactory
) -> None:
    class BadRequestError(Exception):  # same name as the SDK's, as the runner matches it
        pass

    class Broke:
        model = "claude-sonnet-5-5"

        def create(self, **kw: Any) -> Any:
            raise BadRequestError("Your credit balance is too low to access the Anthropic API.")

    cfg = Config(datasets=["test"], concurrency=1)
    results = run_items(ITEMS[:3], cfg, session_factory, lambda _id: Broke(), lambda _id: None)
    assert [r.status for r in results] == ["error", "skipped", "skipped"]
    assert "stopped after an API error: BadRequestError" in (results[1].skip_reason or "")
