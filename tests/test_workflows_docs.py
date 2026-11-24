"""Tearsheet and earnings brief on fixture data, with a scripted fake LLM (no network).

CHIP has three 10-Ks (with text) and, for the brief, a FY2025 Q3 10-Q with MD&A and a risk
section that adds one paragraph the 10-K doesn't have.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.db.models import Fact, Filing
from analyst.edgar.concepts import SeriesPoint
from analyst.llm.fake import ScriptedLLM, tool_call
from analyst.metrics.prices import CsvPriceProvider
from analyst.retrieval import index as index_mod
from analyst.retrieval.citation import verify
from analyst.retrieval.embed import HashEmbedder
from analyst.workflows.base import Context
from analyst.workflows.earnings import Earnings, new_paragraphs
from analyst.workflows.report import Footnotes, md_inline_to_rl, page_count
from analyst.workflows.tearsheet import Tearsheet, split_warning
from tests.fin_corpus import FILLER, M, SessionFactory, seed, seed_peer, tenq_html

NEW_RISK = (
    "New tariffs on imported wafers could raise our manufacturing costs and reduce our gross "
    "margin in future quarters."
)
MDNA_Q3 = "Revenue grew on accelerator demand from data center customers in the quarter."


def provided(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The source-material items the workflow gave the agent (first user message)."""
    text = messages[0]["content"]
    return json.loads(text[text.index("{") :])["results"]


def submit(text: str) -> dict[str, Any]:
    return tool_call("submit_answer", text=text, confidence="high", refused=False)


@pytest.fixture
def ctx(session_factory: SessionFactory, tmp_path: Path) -> Context:
    raw = tmp_path / "raw"
    q3 = tenq_html(MDNA_Q3, f"<p>{FILLER}</p><p>{NEW_RISK}</p>")
    seed(session_factory, raw, q3_html=q3)
    seed_peer(session_factory)
    with session_factory() as s:  # prior-year Q3 and operating income, for the brief
        acc = "0000000001-24-000013"
        s.add(Filing(accession=acc, cik=1, form="10-Q", filed_at=dt.date(2024, 11, 4), url="u"))
        s.flush()
        rows = [
            (9001, "Revenues", 300 * M, dt.date(2024, 7, 1), dt.date(2024, 9, 30), acc),
            (9002, "OperatingIncomeLoss", 90 * M, dt.date(2024, 7, 1), dt.date(2024, 9, 30), acc),
            (
                9003,
                "OperatingIncomeLoss",
                120 * M,
                dt.date(2025, 7, 1),
                dt.date(2025, 9, 30),
                "0000000001-25-000013",
            ),
        ]
        for fid, concept, value, start, end, a in rows:
            s.add(
                Fact(
                    id=fid,
                    cik=1,
                    concept=f"us-gaap:{concept}",
                    unit="USD",
                    value=value,
                    period_start=start,
                    period_end=end,
                    form="10-Q",
                    accession=a,
                )
            )
    index_mod.run(raw, session_factory, embedder=HashEmbedder())
    (tmp_path / "universe.csv").write_text(
        "ticker,name,peer_group\nCHIP,Chip Corp,semis\nFAB,Fab Inc,semis\n", encoding="utf-8"
    )
    (tmp_path / "prices.csv").write_text(
        "ticker,date,close\nCHIP,2026-11-20,50\nFAB,2026-11-20,30\n", encoding="utf-8"
    )
    return Context(
        session_factory=session_factory,
        prices=CsvPriceProvider(tmp_path / "prices.csv"),
        universe_path=tmp_path / "universe.csv",
        out_dir=tmp_path / "out",
        embedder=HashEmbedder(),
        today=dt.date(2026, 11, 24),
    )


# -- tearsheet ---------------------------------------------------------------------------------


def tearsheet_llm() -> ScriptedLLM:
    def business(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ref = provided(msgs)[0]["ref"]
        return [submit(f"Chip Corp designs products and sells them worldwide [^{ref}].")]

    def risks(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = provided(msgs)
        ref = next(i["ref"] for i in items if "Export controls" in i["excerpt"])
        return [submit(f"- Export controls could restrict sales [^{ref}].")]

    return ScriptedLLM([business, risks])


def test_tearsheet_writes_pdf_markdown_and_chart(ctx: Context) -> None:
    ctx.llm = tearsheet_llm()
    res = Tearsheet().run({"ticker": "chip"}, ctx)
    assert res.status == "succeeded", res.error
    pdf, md_path, png = res.files
    assert (pdf.suffix, md_path.suffix, png.suffix) == (".pdf", ".md", ".png")
    assert pdf.read_bytes().startswith(b"%PDF") and png.stat().st_size > 1000
    assert "fits on one page" in res.summary
    assert page_count(pdf) == 2  # profile, then sources
    md = md_path.read_text(encoding="utf-8")
    assert "# Chip Corp (CHIP)" in md
    assert (
        "| Revenue [^" in md
        and "1,500" in md
        and "| FY2025 |" in md.replace(" FY2025 |", " FY2025 |")
    )
    assert "| Operating margin [^" in md and "30.0%" in md  # 450 / 1,500
    assert "Chip Corp designs products and sells them worldwide [^1]." in md
    # footnotes in reading order: business [^1], 9 table rows [^2]-[^10], then risks
    assert "| Revenue [^2] |" in md
    assert "- Export controls could restrict sales [^11]." in md
    assert re.search(r"\| Valuation \[\^12\] \|", md)
    valuation_note = md.split("[^12]: ", 1)[1].splitlines()[0]
    assert "net income FY2025" in valuation_note and "us-gaap:NetIncomeLoss" in valuation_note
    assert "| EV/EBITDA | 9.3x | 6.2x |" in md  # CHIP vs peer median (FAB)
    sources = md.split("## Sources", 1)[1]
    assert "[^1]: CHIP 10-K filed 2026-02-15, Item 1 (Business)" in sources
    assert "us-gaap:Revenues" in sources and "0000000001-26-000001" in sources
    assert [s.kind for s in res.steps].count("agent") == 2
    assert "_Number check" not in md
    assert res.cost_usd is None  # the fake model has no price


def test_tearsheet_agent_citations_resolve(ctx: Context, session_factory: SessionFactory) -> None:
    ctx.llm = tearsheet_llm()
    state: dict[str, Any] = {"inputs": Tearsheet.Inputs.model_validate({"ticker": "CHIP"})}
    for step in Tearsheet().steps(state["inputs"]):
        state = step.fn(state, ctx)
    with session_factory() as s:
        for key in ("business", "risks"):
            answer = state[key]
            assert answer.citations and answer.unverified_numbers == []
            assert all(verify(c, s) for c in answer.citations)


def test_tearsheet_without_llm_says_skipped(ctx: Context) -> None:
    res = Tearsheet().run({"ticker": "CHIP"}, ctx)
    md = res.files[1].read_text(encoding="utf-8")
    assert md.count("_Agent step skipped (no LLM)._") == 2


def test_tearsheet_flags_an_unverified_number(ctx: Context) -> None:
    bad = lambda msgs: [submit(f"Revenue was $9 billion [^{provided(msgs)[0]['ref']}].")]  # noqa: E731
    ok = lambda msgs: [submit(f"- Export controls [^{provided(msgs)[0]['ref']}].")]  # noqa: E731
    ctx.llm = ScriptedLLM([bad, bad, ok])
    res = Tearsheet().run({"ticker": "CHIP"}, ctx)
    md = res.files[1].read_text(encoding="utf-8")
    assert "_Number check: unverified numbers ['$9 billion']._" in md


def test_tearsheet_unknown_ticker_fails(ctx: Context) -> None:
    res = Tearsheet().run({"ticker": "TSLA"}, ctx)
    assert res.status == "failed" and "not in the covered universe" in (res.error or "")


def sp(fy: int, value: str, canonical: str) -> SeriesPoint:
    unit = "USD/shares" if canonical == "eps_diluted" else "USD"
    return SeriesPoint(
        canonical,
        None,
        dt.date(fy, 1, 28),
        fy,
        Decimal(value),
        unit,
        "c",
        fy,
        "a",
        "10-K",
        dt.date(fy, 3, 1),
    )


def test_split_warning_flags_mixed_per_share_bases() -> None:
    # NVIDIA: FY2022 EPS 3.85 (pre 10:1 split), FY2023 0.17 (restated after it)
    ni = {
        2022: sp(2022, "9752e6", "net_income"),
        2023: sp(2023, "4368e6", "net_income"),
        2024: sp(2024, "29760e6", "net_income"),
    }
    eps = {
        2022: sp(2022, "3.85", "eps_diluted"),
        2023: sp(2023, "0.17", "eps_diluted"),
        2024: sp(2024, "1.19", "eps_diluted"),
    }
    w = split_warning(eps, ni)
    assert "FY2022 to FY2023" in w and "not split-adjusted" in w
    # only the displayed years count: a jump before the table isn't reported, one inside is
    eps[2021] = sp(2021, "40", "eps_diluted")
    ni[2021] = sp(2021, "9000e6", "net_income")
    assert "FY2021" not in split_warning(eps, ni, [2022, 2023, 2024])
    assert "FY2022 to FY2023" in split_warning(eps, ni, [2022, 2023, 2024])
    eps_ok = {2023: sp(2023, "0.17", "eps_diluted"), 2024: sp(2024, "1.19", "eps_diluted")}
    assert split_warning(eps_ok, ni) == ""
    assert split_warning({2022: sp(2022, "-1", "eps_diluted")}, ni) == ""


def test_pdf_markup_merges_adjacent_markers_and_escapes() -> None:
    assert md_inline_to_rl("a [^4][^5] <b> & **x**") == (
        "a <super>4,5</super> &lt;b&gt; &amp; <b>x</b>"
    )


def test_footnotes_renumber_answers_into_one_list() -> None:
    notes = Footnotes()
    notes.add("table row")
    assert notes.markdown() == "[^1]: table row"


# -- earnings brief ----------------------------------------------------------------------------


def earnings_llm() -> ScriptedLLM:
    def mdna(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = provided(msgs)
        growth = next(
            i["ref"] for i in items if i.get("label", "").startswith("CHIP Revenue growth")
        )
        text = next(i["ref"] for i in items if "accelerator demand" in i.get("excerpt", ""))
        return [submit(f"- Revenue grew 26.7% [^{growth}] on accelerator demand [^{text}].")]

    def risks(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ref = provided(msgs)[0]["ref"]
        return [submit(f"- The 10-Q adds a risk from new wafer tariffs [^{ref}].")]

    return ScriptedLLM([mdna, risks])


def test_earnings_brief(ctx: Context) -> None:
    ctx.llm = earnings_llm()
    res = Earnings().run({"ticker": "CHIP"}, ctx)
    assert res.status == "succeeded", res.error
    (path,) = res.files
    assert path.name == "earnings_CHIP_FY2025Q3.md"
    md = path.read_text(encoding="utf-8")
    assert "compared with FY2024 Q3" in md
    assert re.search(r"\| Revenue \[\^\d+\] \| 380 \| 300 \| \+26\.7% \|", md)
    assert re.search(r"\| Operating income \[\^\d+\] \| 120 \| 90 \| \+33\.3% \|", md)
    assert re.search(r"\| Operating margin \[\^\d+\] \| 31\.6% \| 30\.0% \| \+1\.6 pp \|", md)
    assert "| Gross profit" in md and "n/a (missing input)" in md
    assert "Revenue grew 26.7%" in md and "accelerator demand" in md
    assert "1 paragraph(s) in the 10-Q's Part II, Item 1A have no close match" in md
    assert "wafer tariffs" in md
    assert "_Number check" not in md  # both agent sections passed the post-check
    assert "revenue +26.7% vs FY2024 Q3; risk factors changed" in res.summary


def test_earnings_unchanged_risks_cite_the_10q_without_an_agent(
    ctx: Context, session_factory: SessionFactory, tmp_path: Path
) -> None:
    # Replace the Q3 10-Q text with a "no material changes" risk section and re-index.
    raw = tmp_path / "raw"
    from analyst.edgar.ingest import document_path

    acc = "0000000001-25-000013"
    html = tenq_html(
        MDNA_Q3,
        "<p>There have been no material changes to the risk factors "
        "disclosed in our Annual Report on Form 10-K for fiscal 2024.</p>",
    )
    document_path(raw, 1, acc, f"{acc}.htm").write_text(html, encoding="utf-8")
    index_mod.run(raw, session_factory, embedder=HashEmbedder())
    ctx.llm = ScriptedLLM([earnings_llm().turns[0]])  # only the MD&A step calls the model
    res = Earnings().run({"ticker": "CHIP"}, ctx)
    md = res.files[0].read_text(encoding="utf-8")
    assert "No new risk-factor paragraphs" in md
    assert "There have been no material changes to the risk factors" in md
    assert "risk factors unchanged" in res.summary


def test_new_paragraphs_ignores_near_copies() -> None:
    annual = [("Our supply chain depends on a small number of foundries in one region.", 0, 1)]
    edited = ("Our supply chain depends on a small number of foundries in one region!", 0, 1)
    fresh = (NEW_RISK, 0, 1)
    assert new_paragraphs([edited, fresh], annual) == [fresh]


def test_a_10q_paragraph_cut_from_a_longer_10k_paragraph_is_not_new() -> None:
    # 10-Q text is often cut at page breaks: a piece of a 10-K paragraph is not a change.
    long_10k = (
        "Threat actors use social engineering and AI-assisted techniques to induce our employees "
        "to grant access, and they also exploit vulnerabilities in third-party software we use."
    )
    piece = (
        "Threat actors use social engineering and AI-assisted techniques to induce our "
        "employees to grant access,",
        0,
        1,
    )
    assert new_paragraphs([piece], [(long_10k, 0, 1)], annual_text=long_10k) == []
    assert new_paragraphs([(NEW_RISK, 0, 1)], [(long_10k, 0, 1)], annual_text=long_10k) != []


def test_quarter_comes_from_the_10q_period_when_its_revenue_is_not_tagged(
    ctx: Context, session_factory: SessionFactory
) -> None:
    """Citigroup-style: the latest 10-Q reports no revenue fact. The brief must still be
    about that 10-Q's quarter (from its period of report), never an older quarter."""
    from sqlalchemy import delete, update

    with session_factory() as s:
        s.execute(
            delete(Fact).where(
                Fact.accession == "0000000001-25-000013", Fact.concept == "us-gaap:Revenues"
            )
        )
        s.execute(
            update(Filing)
            .where(Filing.accession == "0000000001-25-000013")
            .values(period_of_report=dt.date(2025, 9, 30))
        )
    ctx.llm = ScriptedLLM(
        [lambda msgs: [submit("- Accelerator demand [^1].")], earnings_llm().turns[1]]
    )
    res = Earnings().run({"ticker": "CHIP"}, ctx)
    assert res.status == "succeeded", res.error
    md = res.files[0].read_text(encoding="utf-8")
    assert "FY2025 Q3 earnings brief" in md and "compared with FY2024 Q3" in md
    assert re.search(r"\| Revenue \[\^\d+\] \| n/a \| 300 \|", md)


def test_quarter_unknown_is_refused(ctx: Context, session_factory: SessionFactory) -> None:
    from sqlalchemy import delete

    with session_factory() as s:
        s.execute(
            delete(Fact).where(
                Fact.accession == "0000000001-25-000013", Fact.concept == "us-gaap:Revenues"
            )
        )
    res = Earnings().run({"ticker": "CHIP"}, ctx)
    assert res.status == "failed" and "can't tell which quarter" in (res.error or "")


def test_earnings_footnotes_cite_full_xbrl_facts(ctx: Context) -> None:
    ctx.llm = earnings_llm()
    md = Earnings().run({"ticker": "CHIP"}, ctx).files[0].read_text(encoding="utf-8")
    first = md.split("[^1]: ", 1)[1].splitlines()[0]
    assert "us-gaap:Revenues 2025-07-01..2025-09-30 = 380000000 USD" in first
    assert "(10-Q 0000000001-25-000013, fact" in first
    margin = next(
        ln for ln in md.splitlines() if ln.startswith("[^") and "Operating margin =" in ln
    )
    assert "us-gaap:OperatingIncomeLoss" in margin and "us-gaap:Revenues" in margin
