"""Agent tools and loop, with a scripted fake LLM (no network, no model).

The fake emits predetermined tool calls. The tests check that the tools really run against
the (synthetic) DB, that citations flow from tool output into the answer, and that the
number post-check catches an invented number and gives the model exactly one repair turn.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.agent.ask import format_answer
from analyst.agent.citations import ComputedCitation, FactCitation
from analyst.agent.loop import Answer, run
from analyst.agent.tools import ToolBox, ToolError, display, tool_definitions
from analyst.llm.client import Usage, cost_usd
from analyst.llm.fake import ScriptedLLM, last_tool_results, text, tool_call
from analyst.metrics.prices import CsvPriceProvider
from analyst.retrieval import index as index_mod
from analyst.retrieval.citation import Citation, verify
from analyst.retrieval.embed import HashEmbedder
from tests.fin_corpus import UNIVERSE_CSV, SessionFactory, seed

M = Decimal(1_000_000)


@pytest.fixture
def box(session_factory: SessionFactory, tmp_path: Path) -> ToolBox:
    raw = tmp_path / "raw"
    seed(session_factory, raw)
    index_mod.run(raw, session_factory, embedder=HashEmbedder())
    (tmp_path / "universe.csv").write_text(UNIVERSE_CSV, encoding="utf-8")
    (tmp_path / "prices.csv").write_text(
        "ticker,date,close\nCHIP,2026-11-20,50\nBANK,2026-11-20,20\n", encoding="utf-8"
    )
    return ToolBox(
        session_factory=session_factory,
        prices=CsvPriceProvider(tmp_path / "prices.csv"),
        universe_path=tmp_path / "universe.csv",
        embedder=HashEmbedder(),
    )


def call(box: ToolBox, name: str, **args: Any) -> dict[str, Any]:
    return json.loads(box.run(name, args))


def compute(box: ToolBox, metric: str, *tickers: str, **kw: Any) -> dict[str, Any]:
    out = call(box, "compute", metric=metric, tickers=list(tickers or ["CHIP"]), **kw)
    return out["results"][0]


# -- tool definitions ----------------------------------------------------------------------


def test_tool_definitions_are_valid_json_schema_and_stable() -> None:
    defs = tool_definitions()
    assert [d["name"] for d in defs] == [
        "resolve_company",
        "get_financials",
        "compute",
        "search_filings",
        "list_peers",
        "submit_answer",
    ]
    for d in defs:
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"]["additionalProperties"] is False
    assert json.dumps(defs) == json.dumps(tool_definitions())  # cache-stable


def test_invalid_input_and_unknown_tool_raise_tool_error(box: ToolBox) -> None:
    with pytest.raises(ToolError, match="invalid input"):
        box.run("get_financials", {"ticker": "CHIP"})
    with pytest.raises(ToolError, match="invalid input"):
        box.run("compute", {"metric": "made_up", "tickers": ["CHIP"]})
    with pytest.raises(ToolError, match="unknown tool"):
        box.run("delete_everything", {})


# -- resolve_company / list_peers ----------------------------------------------------------


def test_resolve_company(box: ToolBox) -> None:
    assert call(box, "resolve_company", query="chip")["matches"][0]["ticker"] == "CHIP"
    by_name = call(box, "resolve_company", query="Chip Corp")["matches"][0]
    assert (by_name["ticker"], by_name["cik"], by_name["peer_group"]) == ("CHIP", 1, "semis")
    assert call(box, "resolve_company", query="Chp Corp")["matches"][0]["ticker"] == "CHIP"
    assert call(box, "resolve_company", query="FAB")["matches"][0]["cik"] is None  # no data


def test_resolve_company_outside_universe_says_refuse(box: ToolBox) -> None:
    out = call(box, "resolve_company", query="Tesla")
    assert out["found"] is False
    assert "not in the covered universe" in out["message"] and "refuse" in out["message"]


def test_list_peers(box: ToolBox) -> None:
    assert call(box, "list_peers", ticker="chip") == {
        "ticker": "CHIP",
        "peer_group": "semis",
        "peers": [{"ticker": "FAB", "name": "Fab Inc"}],
    }
    with pytest.raises(ToolError, match="not in the covered universe"):
        call(box, "list_peers", ticker="TSLA")


# -- get_financials ------------------------------------------------------------------------


def test_get_financials_returns_facts_with_citations(box: ToolBox) -> None:
    out = call(box, "get_financials", ticker="CHIP", metrics=["revenue", "operating_income"])
    rev = out["results"][0]["values"]
    assert [v["period"] for v in rev] == ["FY2023", "FY2024", "FY2025"]
    assert rev[-1]["value"] == "1500000000" and rev[-1]["display"] == "$1.50B"
    cite = box.refs[rev[-1]["ref"]]
    assert isinstance(cite, FactCitation)
    assert (cite.concept, cite.accession, cite.form, cite.value) == (
        "us-gaap:Revenues",
        "0000000001-26-000001",
        "10-K",
        "1500000000",
    )


def test_get_financials_fiscal_years_and_quarters_with_derived_q4(box: ToolBox) -> None:
    fy = call(box, "get_financials", ticker="CHIP", metrics=["revenue"], fiscal_years=[2024])
    assert [v["period"] for v in fy["results"][0]["values"]] == ["FY2024"]
    q = call(box, "get_financials", ticker="CHIP", metrics=["revenue"], period_type="Q", periods=4)
    vals = q["results"][0]["values"]
    assert [v["period"] for v in vals] == ["FY2025 Q1", "FY2025 Q2", "FY2025 Q3", "FY2025 Q4"]
    assert vals[-1]["value"] == "420000000"
    assert vals[-1]["source"] == "derived: FY - (Q1 + Q2 + Q3)"
    q4 = box.refs[vals[-1]["ref"]]
    assert isinstance(q4, ComputedCitation)
    assert len(q4.facts) == 4  # the FY fact and Q1-Q3


def test_get_financials_missing_item_says_not_in_filings(box: ToolBox) -> None:
    out = call(box, "get_financials", ticker="BANK", metrics=["gross_profit"])
    assert out["results"][0]["values"] == []
    assert "do not estimate" in out["results"][0]["note"]


def test_get_financials_refuses_unknown_items_and_companies(box: ToolBox) -> None:
    with pytest.raises(ToolError, match="unknown line items"):
        call(box, "get_financials", ticker="CHIP", metrics=["vibes"])
    with pytest.raises(ToolError, match="not in the covered universe"):
        call(box, "get_financials", ticker="TSLA", metrics=["revenue"])
    with pytest.raises(ToolError, match="no data loaded"):
        call(box, "get_financials", ticker="FAB", metrics=["revenue"])


# -- compute -------------------------------------------------------------------------------


def test_compute_growth_cagr_margins(box: ToolBox) -> None:
    g = compute(box, "growth")
    assert (g["value"], g["display"], g["label"]) == (
        "0.25",
        "25.0%",
        "CHIP revenue growth FY2024-FY2025",
    )
    assert [i["label"] for i in g["inputs"]] == ["CHIP revenue FY2025", "CHIP revenue FY2024"]
    c = compute(box, "cagr", years=2)
    assert Decimal(c["value"]).quantize(Decimal("1e-8")) == Decimal("0.22474487")
    assert compute(box, "operating_margin")["value"] == "0.3"
    assert compute(box, "operating_margin", fiscal_year=2023)["value"] == "0.2"
    assert compute(box, "gross_margin")["value"] == "0.64"
    assert compute(box, "net_margin")["value"] == "0.24"
    assert compute(box, "ebitda")["value"] == "520000000"
    assert compute(box, "ebitda_margin")["display"] == "34.7%"
    cite = box.refs[g["ref"]]
    assert isinstance(cite, ComputedCitation)
    assert [f.label for f in cite.facts] == ["2025-01-01..2025-12-31", "2024-01-01..2024-12-31"]


def test_compute_ltm_uses_derived_q4(box: ToolBox) -> None:
    r = compute(box, "ltm")
    assert r["value"] == "1500000000"
    assert r["label"] == "CHIP revenue LTM"
    cite = box.refs[r["ref"]]
    assert isinstance(cite, ComputedCitation)
    # Q1-Q3 facts plus the FY fact behind the derived Q4 (Q1-Q3 counted once)
    assert [f.form for f in cite.facts] == ["10-Q", "10-Q", "10-Q", "10-K"]


def test_compute_valuation(box: ToolBox) -> None:
    assert compute(box, "market_cap")["value"] == "5000000000"
    ev = compute(box, "enterprise_value")
    assert ev["value"] == "4850000000"  # 5,000 + 250 debt - 400 cash ($M)
    assert compute(box, "ev_to_revenue")["label"] == "CHIP EV / revenue (LTM to 2025-12-31)"
    assert Decimal(compute(box, "ev_to_revenue")["value"]).quantize(Decimal("1e-6")) == Decimal(
        "3.233333"
    )
    e = compute(box, "ev_to_ebitda")
    assert e["label"] == "CHIP EV / EBITDA (FY2025)"  # no quarterly D&A: FY used
    assert e["display"] == "9.3x"
    pe = compute(box, "pe")
    assert pe["display"] == "13.9x"
    nd = compute(box, "net_debt_to_ebitda")
    assert Decimal(nd["value"]).quantize(Decimal("1e-6")) == Decimal("-0.288462")
    price_input = next(i for i in compute(box, "market_cap")["inputs"] if "close" in i["label"])
    assert price_input == {"label": "CHIP close 2026-11-20", "value": "50", "display": "$50.00"}


def test_compute_refuses_negative_pe_and_missing_price(box: ToolBox, tmp_path: Path) -> None:
    pe = compute(box, "pe", "BANK")
    assert pe["value"] is None and pe["display"] is None
    assert "not positive" in pe["note"]
    box.prices = CsvPriceProvider(tmp_path / "no_prices.csv")
    mc = compute(box, "market_cap")
    assert mc["value"] is None
    assert "no price for CHIP in the price file" in mc["note"]


def test_compute_peer_set_adds_stats_and_names_exclusions(box: ToolBox) -> None:
    out = call(box, "compute", metric="operating_margin", tickers=["CHIP", "BANK"])
    assert [r["value"] for r in out["results"]] == ["0.3", None]  # BANK has no operating income
    med = out["peer_stats"][0]
    assert (med["label"], med["value"]) == ("median(operating_margin)", "0.3")
    assert "BANK" in med["note"]


def test_compute_refuses_unknown_item_and_missing_years(box: ToolBox) -> None:
    with pytest.raises(ToolError, match="unknown item"):
        call(box, "compute", metric="growth", tickers=["CHIP"], item="vibes")
    r = compute(box, "growth", fiscal_year=2023)
    assert r["value"] is None and r["note"] == "missing input"
    assert compute(box, "growth", "BANK", item="gross_profit")["note"] == "no gross_profit reported"
    assert compute(box, "gross_margin", "BANK")["value"] is None


# -- search_filings ------------------------------------------------------------------------


def test_search_filings_returns_verifiable_citations(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    out = call(
        box,
        "search_filings",
        query="operating margin accelerators",
        ticker="CHIP",
        section="Item 7",
    )
    assert out["results"], out
    top = out["results"][0]
    assert top["ticker"] == "CHIP" and top["section"].startswith("Item 7")
    assert "accelerators" in top["excerpt"]
    cite = box.refs[top["ref"]]
    assert isinstance(cite, Citation)
    with session_factory() as s:
        assert verify(cite, s)


def test_search_filings_with_no_hits_says_so(box: ToolBox) -> None:
    out = call(box, "search_filings", query="operating margin", ticker="CHIP", form="10-Q")
    assert out["results"] == [] and "Do not make the claim" in out["note"]


def test_display_formats() -> None:
    assert display(Decimal("-2500000"), "USD") == "-$2.5M"
    assert display(Decimal("999"), "USD") == "$999"
    assert display(Decimal("100000000"), "shares") == "100.0M shares"
    assert display(Decimal("3"), "other") == "3"
    assert display(None, "USD") is None


# -- the loop with a scripted LLM ----------------------------------------------------------


def cite_first(tool_name: str, template: str):
    """A scripted turn that reads the last tool output and cites its first ref."""

    def turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        res = last_tool_results(messages)
        data = json.loads(res[0]["content"])
        item = data["results"][0]
        ref = item["values"][-1]["ref"] if "values" in item else item["ref"]
        return [
            tool_call(
                "submit_answer", text=template.format(ref=ref), confidence="high", refused=False
            )
        ]

    return turn


def test_agent_runs_tools_and_citations_flow_into_the_answer(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [text("Looking up the company."), tool_call("resolve_company", query="Chip Corp")],
            [
                tool_call("get_financials", ticker="CHIP", metrics=["revenue"], periods=1),
                tool_call("compute", metric="operating_margin", tickers=["CHIP"]),
                tool_call(
                    "search_filings", query="operating margin", ticker="CHIP", section="Item 7"
                ),
            ],
            lambda msgs: [
                tool_call(
                    "submit_answer",
                    text=(
                        "Management credited data center demand [^3]. "
                        "CHIP's FY2025 revenue was $1.50B [^1] with a 30.0% operating margin [^2]."
                    ),
                    confidence="high",
                    refused=False,
                )
            ],
        ]
    )
    a = run("How did CHIP's operating margin look in FY2025 and why?", llm, box)
    assert a.text == (
        "Management credited data center demand [^1]. "
        "CHIP's FY2025 revenue was $1.50B [^2] with a 30.0% operating margin [^3]."
    )  # renumbered in order of first appearance
    assert [type(c).__name__ for c in a.citations] == [
        "Citation",
        "FactCitation",
        "ComputedCitation",
    ]
    assert (a.refused, a.confidence, a.unverified_numbers, a.uncited_numbers) == (
        False,
        "high",
        [],
        [],
    )
    assert [s.tool for s in a.trace.steps] == [
        "resolve_company",
        "get_financials",
        "compute",
        "search_filings",
        "submit_answer",
    ]
    # the three parallel calls went back as three tool_results in ONE user message
    third = llm.requests[2]["messages"][-1]
    assert third["role"] == "user" and len(third["content"]) == 3
    assert {r["type"] for r in third["content"]} == {"tool_result"}
    assert a.trace.usage.llm_calls == 3 and a.trace.usage.input_tokens == 300
    assert a.trace.usage.cost_usd is None  # the fake model has no price
    assert all(s.ms >= 0 for s in a.trace.steps)
    assert llm.requests[0]["tools"] == tool_definitions()
    assert "Never answer from memory" in llm.requests[0]["system"]


def test_invented_number_gets_one_repair_turn(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [tool_call("compute", metric="growth", tickers=["CHIP"])],
            cite_first("compute", "Revenue grew 31% [^{ref}]."),  # invented: tool said 25%
            cite_first("compute", "Revenue grew 25% [^{ref}]."),
        ]
    )
    # second scripted turn reads the compute output; the repair turn reads the error result
    llm.turns[2] = lambda msgs: [
        tool_call("submit_answer", text="Revenue grew 25% [^1].", confidence="high", refused=False)
    ]
    a = run("How fast did CHIP grow?", llm, box)
    repair_msg = llm.requests[2]["messages"][-1]["content"][0]
    assert repair_msg["is_error"] is True and "31%" in repair_msg["content"]
    assert a.trace.repaired is True
    assert (a.text, a.unverified_numbers, a.confidence) == ("Revenue grew 25% [^1].", [], "high")


def test_number_still_invented_after_repair_is_flagged(box: ToolBox) -> None:
    bad = lambda msgs: [  # noqa: E731
        tool_call("submit_answer", text="Revenue grew 31% [^1].", confidence="high", refused=False)
    ]
    llm = ScriptedLLM([[tool_call("compute", metric="growth", tickers=["CHIP"])], bad, bad])
    a = run("How fast did CHIP grow?", llm, box)
    assert a.unverified_numbers == ["31%"]
    assert a.confidence == "low"  # never reported as confident with an unverified number
    assert len(llm.requests) == 3  # exactly one repair turn, then stop
    assert "UNVERIFIED NUMBERS: ['31%']" in format_answer(a)


def test_uncited_number_is_flagged_after_repair(box: ToolBox) -> None:
    uncited = lambda msgs: [  # noqa: E731
        tool_call("submit_answer", text="Revenue grew 25%.", confidence="high", refused=False)
    ]
    llm = ScriptedLLM([[tool_call("compute", metric="growth", tickers=["CHIP"])], uncited, uncited])
    a = run("q", llm, box)
    assert a.uncited_numbers == ["25%", "(answer cites nothing)"]
    assert a.unverified_numbers == []


def test_refusal_for_company_outside_universe(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [tool_call("resolve_company", query="Tesla")],
            [
                tool_call(
                    "submit_answer",
                    text="Tesla is not in the covered universe, so it is not in the filings.",
                    confidence="high",
                    refused=True,
                )
            ],
        ]
    )
    a = run("What was Tesla's FY2025 revenue?", llm, box)
    assert a.refused is True and a.citations == [] and a.unverified_numbers == []
    assert '"found": false' in a.trace.steps[0].output_summary
    assert "REFUSED" in format_answer(a)


def test_tool_errors_go_back_to_the_model(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [tool_call("get_financials", ticker="TSLA", metrics=["revenue"])],
            [
                tool_call(
                    "submit_answer", text="Not in the filings.", confidence="high", refused=True
                )
            ],
        ]
    )
    a = run("TSLA revenue?", llm, box)
    err = llm.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "not in the covered universe" in err["content"]
    assert a.trace.steps[0].error is True


def test_plain_text_ending_is_asked_once_for_submit_answer(box: ToolBox) -> None:
    llm = ScriptedLLM([[text("CHIP grew a lot.")], [text("CHIP grew a lot.")]])
    a = run("q", llm, box)
    assert "submit_answer" in llm.requests[1]["messages"][-1]["content"]
    assert (a.text, a.confidence, a.trace.repaired) == ("CHIP grew a lot.", "low", True)


def test_invalid_submit_answer_is_reported(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [tool_call("submit_answer", text="x")],  # missing fields
            [
                tool_call(
                    "submit_answer", text="Not in the filings.", confidence="low", refused=True
                )
            ],
        ]
    )
    a = run("q", llm, box)
    assert "invalid submit_answer" in llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert a.refused


def test_max_steps(box: ToolBox) -> None:
    llm = ScriptedLLM([[tool_call("list_peers", ticker="CHIP")]] * 10)
    a = run("q", llm, box)
    assert a.trace.stop == "max_steps" and len(llm.requests) == 10
    assert a.text == "No answer within 10 steps."


def test_model_refusal_stop_reason(box: ToolBox) -> None:
    class Refuses(ScriptedLLM):
        def create(self, **kw: Any):
            m = super().create(**kw)
            return m.model_copy(update={"stop_reason": "refusal"})

    a = run("q", Refuses([[text("no")]]), box)
    assert a.refused and a.trace.stop == "model_refusal"


def test_answer_round_trips_as_json(box: ToolBox) -> None:
    llm = ScriptedLLM(
        [
            [tool_call("compute", metric="growth", tickers=["CHIP"])],
            cite_first("compute", "Revenue grew 25% [^{ref}]."),
        ]
    )
    a = run("q", llm, box)
    again = Answer.model_validate_json(a.model_dump_json())
    assert isinstance(again.citations[0], ComputedCitation)
    assert again.citations[0].facts[0].concept == "us-gaap:Revenues"


# -- cost accounting -----------------------------------------------------------------------


def test_cost_accounting() -> None:
    # Sonnet 5.5: $2 in, $10 out, $2.50 cache write, $0.10 cache read per million tokens
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 100_000, 200_000, 1_000_000) == Decimal("3.6")
    assert cost_usd("unknown-model", 1, 1) is None
    u = Usage()

    class U:
        input_tokens, output_tokens = 1000, 500
        cache_creation_input_tokens, cache_read_input_tokens = 0, None

    u.add("claude-sonnet-5-5", U())
    u.add("claude-sonnet-5-5", U())
    assert (u.calls, u.input_tokens, u.cost_usd) == (2, 2000, Decimal("0.014"))
    u.add("mystery", U())
    assert u.cost_usd is None


# -- review fixes --------------------------------------------------------------------------


def test_invented_number_caught_after_get_financials(box: ToolBox) -> None:
    """A real trace has dates (2025-12-31), accessions and ref ids: none may verify '31%'."""
    bad = lambda msgs: [  # noqa: E731
        tool_call("submit_answer", text="Revenue grew 31% [^4].", confidence="high", refused=False)
    ]
    llm = ScriptedLLM(
        [
            [
                tool_call("get_financials", ticker="CHIP", metrics=["revenue"]),
                tool_call("compute", metric="growth", tickers=["CHIP"]),
            ],
            bad,
            bad,
        ]
    )
    a = run("How fast did CHIP grow?", llm, box)
    assert a.unverified_numbers == ["31%"] and a.confidence == "low"


def test_repair_turn_counts_toward_the_step_limit(box: ToolBox) -> None:
    bad = lambda msgs: [  # noqa: E731
        tool_call("submit_answer", text="Revenue grew 31% [^1].", confidence="high", refused=False)
    ]
    turns: list[Any] = [[tool_call("compute", metric="growth", tickers=["CHIP"])]] * 9 + [bad, bad]
    llm = ScriptedLLM(turns)
    a = run("q", llm, box)
    assert len(llm.requests) == 10  # the 10th call submitted; no 11th call for the repair
    assert a.trace.stop == "max_steps" and a.unverified_numbers == ["31%"]


def test_unexpected_tool_exception_goes_back_as_an_error(box: ToolBox, tmp_path: Path) -> None:
    bad_prices = tmp_path / "bad.csv"
    bad_prices.write_text("ticker,date,close\nCHIP,yesterday,1\n", encoding="utf-8")
    box.prices = CsvPriceProvider(bad_prices)
    llm = ScriptedLLM(
        [
            [tool_call("compute", metric="pe", tickers=["CHIP"])],
            [
                tool_call(
                    "submit_answer", text="Not in the filings.", confidence="low", refused=True
                )
            ],
        ]
    )
    a = run("q", llm, box)
    err = llm.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] and "internal error in compute: ValueError" in err["content"]
    assert a.trace.steps[0].error


def test_refs_are_reset_per_run(box: ToolBox) -> None:
    call(box, "get_financials", ticker="CHIP", metrics=["revenue"])
    assert box.refs
    llm = ScriptedLLM(
        [
            [
                tool_call(
                    "submit_answer",
                    text="Revenue was $1.50B [^1].",
                    confidence="high",
                    refused=False,
                )
            ]
        ]
        * 2
    )
    a = run("q", llm, box)
    assert a.uncited_numbers == ["[^1]", "(answer cites nothing)"]  # old ref 1 not known


def test_ebitda_never_mixes_ltm_windows(box: ToolBox, session_factory: SessionFactory) -> None:
    """Operating income LTM ends 2025-12-31 but D&A's last four quarters end 2025-03-31:
    EBITDA must fall back to the same fiscal year for both, not mix the windows."""
    import datetime as dt

    from sqlalchemy import delete

    from analyst.agent.tools import _Data, _flow
    from analyst.db.models import Company, Fact, Filing

    def q(year: int, n: int) -> tuple[dt.date, dt.date]:
        starts = [(1, 1), (4, 1), (7, 1), (10, 1)]
        ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
        return dt.date(year, *starts[n - 1]), dt.date(year, *ends[n - 1])

    with session_factory() as s:
        fid = 1000
        for year, n in [(2024, 1), (2024, 2), (2024, 3), (2025, 1), (2025, 2), (2025, 3)]:
            acc = f"0000000001-{year % 100}-0009{n}{year % 10}"
            start, end = q(year, n)
            s.add(Filing(accession=acc, cik=1, form="10-Q", filed_at=end, url="u"))
            s.flush()
            items = [("OperatingIncomeLoss", 100 * M)]
            if year == 2024 or n == 1:
                items.append(("DepreciationDepletionAndAmortization", 15 * M))
            for concept, value in items:
                fid += 1
                s.add(
                    Fact(
                        id=fid, cik=1, concept=f"us-gaap:{concept}", unit="USD", value=value,
                        period_start=start, period_end=end, form="10-Q", accession=acc,
                    )
                )  # fmt: skip
        # D&A has no FY2025 fact, so its own latest window is the LTM to 2025-03-31
        s.execute(
            delete(Fact).where(
                Fact.concept == "us-gaap:DepreciationDepletionAndAmortization",
                Fact.period_end == dt.date(2025, 12, 31),
            )
        )
    with session_factory() as s:
        assert _flow(_Data(s, s.get(Company, 1)), "d_and_a")[1] == "LTM to 2025-03-31"  # type: ignore[arg-type]
        assert _flow(_Data(s, s.get(Company, 1)), "operating_income")[1] == "LTM to 2025-12-31"  # type: ignore[arg-type]
    e = compute(box, "ev_to_ebitda")
    # windows differ -> same fiscal year for both legs -> FY2025 D&A is missing -> no value
    assert e["label"] == "CHIP EV / EBITDA (FY2025)"
    assert e["value"] is None


def test_market_metric_notes(box: ToolBox) -> None:
    ev = compute(box, "enterprise_value", fiscal_year=2023)
    assert "fiscal_year 2023 ignored" in ev["note"]
    assert compute(box, "pe", "BANK")["label"] == "BANK P/E (FY2025 diluted EPS)"


def test_balance_sheet_dates_that_differ_are_noted(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    import datetime as dt

    from analyst.db.models import Fact, Filing

    with session_factory() as s:
        s.add(
            Filing(
                accession="0000000001-26-000099",
                cik=1,
                form="10-Q",
                filed_at=dt.date(2026, 5, 1),
                url="u",
            )
        )
        s.flush()
        s.add(
            Fact(
                id=5000, cik=1, concept="us-gaap:CashAndCashEquivalentsAtCarryingValue", unit="USD",
                value=500 * M, period_start=None, period_end=dt.date(2026, 3, 31), form="10-Q",
                accession="0000000001-26-000099",
            )
        )  # fmt: skip
    ev = compute(box, "enterprise_value")
    assert "different dates" in ev["note"] and "cash at 2026-03-31" in ev["note"]


def test_stale_ltm_loses_to_a_newer_fiscal_year(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    """FY2026 revenue is filed but no FY2026 quarters are: the 2025 LTM is older, so
    EV / revenue must use FY2026, not call the stale window 'LTM'."""
    import datetime as dt

    from analyst.db.models import Fact, Filing

    with session_factory() as s:
        s.add(
            Filing(
                accession="0000000001-27-000001",
                cik=1,
                form="10-K",
                filed_at=dt.date(2027, 2, 15),
                url="u",
            )
        )
        s.flush()
        s.add(
            Fact(
                id=6000, cik=1, concept="us-gaap:Revenues", unit="USD", value=2000 * M,
                period_start=dt.date(2026, 1, 1), period_end=dt.date(2026, 12, 31), form="10-K",
                accession="0000000001-27-000001",
            )
        )  # fmt: skip
    r = compute(box, "ev_to_revenue")
    assert r["label"] == "CHIP EV / revenue (FY2026)"


def test_market_cap_has_no_note_about_debt_or_cash_dates(
    box: ToolBox, session_factory: SessionFactory
) -> None:
    import datetime as dt

    from analyst.db.models import Fact, Filing

    with session_factory() as s:
        s.add(
            Filing(
                accession="0000000001-26-000098",
                cik=1,
                form="10-Q",
                filed_at=dt.date(2026, 5, 1),
                url="u",
            )
        )
        s.flush()
        s.add(
            Fact(
                id=5001, cik=1, concept="us-gaap:CashAndCashEquivalentsAtCarryingValue", unit="USD",
                value=500 * M, period_start=None, period_end=dt.date(2026, 3, 31), form="10-Q",
                accession="0000000001-26-000098",
            )
        )  # fmt: skip
    assert "different dates" not in compute(box, "market_cap")["note"]
    assert "different dates" in compute(box, "net_debt_to_ebitda")["note"]
