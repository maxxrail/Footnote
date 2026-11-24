"""Comps workflow on fixture data: live formulas, recalculated and compared with analyst.metrics.

Fixture (USD): CHIP (target) and FAB in "semis", BANK in "banks".
- CHIP: price $50, 100M shares; debt 250, cash 400, revenue LTM 1,500, operating income 450,
  D&A 70, net income 360 ($M).
- FAB: price $30, 40M cover-page shares; debt 100, cash 50, revenue 800, operating income 160,
  D&A 40, net income 120 ($M).
- BANK: price $20 but no share count, so it is NM throughout; a net loss.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import formulas
import openpyxl
import pytest
from sqlalchemy import select

from analyst.agent.tools import ToolError
from analyst.db.models import WorkflowRun
from analyst.metrics.prices import CsvPriceProvider
from analyst.workflows.base import Context
from analyst.workflows.comps import Comps
from tests.fin_corpus import SessionFactory, seed, seed_peer

UNIVERSE = (
    "ticker,name,peer_group\nCHIP,Chip Corp,semis\nFAB,Fab Inc,semis\n"
    "BANK,Bank Co,semis\nNODATA,No Data Inc,semis\nOTHER,Other,banks\n"
)


@pytest.fixture
def ctx(session_factory: SessionFactory, tmp_path: Path) -> Context:
    seed(session_factory)
    seed_peer(session_factory)
    (tmp_path / "universe.csv").write_text(UNIVERSE, encoding="utf-8")
    (tmp_path / "prices.csv").write_text(
        "ticker,date,close\nCHIP,2026-11-20,50\nFAB,2026-11-20,30\nBANK,2026-11-20,20\n",
        encoding="utf-8",
    )
    return Context(
        session_factory=session_factory,
        prices=CsvPriceProvider(tmp_path / "prices.csv"),
        universe_path=tmp_path / "universe.csv",
        out_dir=tmp_path / "out",
        today=dt.date(2026, 11, 24),
    )


def recalc(path: Path) -> dict[str, Any]:
    """Evaluate every formula with the `formulas` package: {"Comps!H2": value}."""
    sol = formulas.ExcelModel().loads(str(path)).finish().calculate()
    out = {}
    for k, v in sol.items():
        if "!" not in k or ":" in k:
            continue
        sheet, cell = k.split("]", 1)[1].split("!")
        val = v.value[0][0] if hasattr(v, "value") else v
        out[f"{sheet.strip(chr(39)).title()}!{cell}"] = val
    return out


def run(ctx: Context, **inputs: Any):
    res = Comps().run({"ticker": "CHIP", **inputs}, ctx)
    assert res.status == "succeeded", res.error
    return res


def test_comps_writes_three_sheets_with_live_formulas(ctx: Context) -> None:
    res = run(ctx)
    (path,) = res.files
    assert path.name == "comps_CHIP_2026-11-24.xlsx"
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Comps", "Inputs", "Sources"]
    comps = wb["Comps"]
    # every value on Comps is a formula referencing Inputs, not a pasted number
    for col in "CDEFGHIJ":
        v = comps[f"{col}2"].value
        assert isinstance(v, str) and v.startswith("="), (col, v)
    assert "Inputs!" in comps["C2"].value
    assert comps["H2"].number_format == '0.0"x"'
    assert comps["C2"].number_format == "$#,##0,,"
    assert comps.freeze_panes == "C2"
    inputs = wb["Inputs"]
    assert inputs["C2"].font.color.rgb == "000000FF"  # blue input
    assert getattr(comps["C2"].font.color, "rgb", None) != "000000FF"  # black formula
    assert (
        "data/prices.csv" in inputs["C2"].comment.text or "prices.csv" in inputs["C2"].comment.text
    )
    assert "us-gaap:Revenues" in inputs["G2"].comment.text  # revenue cites its facts
    sources = wb["Sources"]
    urls = [sources.cell(row=r, column=6).value for r in range(2, sources.max_row + 1)]
    assert any(u and "0000000001-26-000001" in u for u in urls)


def test_formulas_recalculate_to_the_metrics_values(ctx: Context) -> None:
    (path,) = run(ctx).files
    v = recalc(path)
    D = Decimal
    # CHIP: mcap 5,000M; EV 5,000 + 250 - 400 = 4,850M; EBITDA 450 + 70 = 520M
    assert v["Comps!C2"] == pytest.approx(5_000e6)
    assert v["Comps!D2"] == pytest.approx(4_850e6)
    assert v["Comps!F2"] == pytest.approx(520e6)
    assert v["Comps!H2"] == pytest.approx(float(D(4850) / D(1500)))  # EV/Revenue 3.23x
    assert v["Comps!I2"] == pytest.approx(float(D(4850) / D(520)))  # EV/EBITDA 9.33x
    assert v["Comps!J2"] == pytest.approx(float(D(5000) / D(360)))  # P/E 13.9x
    # FAB: mcap 1,200M; EV 1,250M; EBITDA 200M
    assert v["Comps!H3"] == pytest.approx(1250 / 800)
    assert v["Comps!I3"] == pytest.approx(1250 / 200)
    assert v["Comps!J3"] == pytest.approx(1200 / 120)


def test_formulas_match_analyst_metrics_for_every_cell(ctx: Context) -> None:
    """The workbook's formulas and the Python check values agree on every company."""
    wf = Comps()
    state: dict[str, Any] = {"inputs": wf.Inputs.model_validate({"ticker": "CHIP"})}
    for step in wf.steps(state["inputs"]):
        state = step.fn(state, ctx)
    v = recalc(state["path"])
    cols = {"market_cap": "C", "enterprise_value": "D", "ebitda": "F",
            "ev_to_revenue": "H", "ev_to_ebitda": "I", "pe": "J"}  # fmt: skip
    for r, row in enumerate(state["rows"], 2):
        for key, col in cols.items():
            expected = row.check[key].value
            got = v[f"Comps!{col}{r}"]
            if expected is None:
                assert got == "NM", (row.ticker, key, got)
            else:
                assert got == pytest.approx(float(expected)), (row.ticker, key)


def test_nm_rule_for_missing_and_negative_denominators(ctx: Context) -> None:
    (path,) = run(ctx).files
    v = recalc(path)
    # BANK (row 4): no share count -> market cap, EV and every multiple NM, no #DIV/0!
    assert [v[f"Comps!{c}4"] for c in "CDHIJ"] == ["NM"] * 5
    # NODATA (row 5): no data at all
    assert v["Comps!H5"] == "NM"
    assert not any(isinstance(x, str) and x.startswith("#") for x in v.values())
    notes = openpyxl.load_workbook(path)["Comps"]["K4"].value
    assert "P/E NM" in notes


def test_negative_earnings_pe_is_nm(ctx: Context, session_factory: SessionFactory) -> None:
    from analyst.db.models import Fact

    with session_factory() as s:  # give BANK shares, so only its P/E is NM (net loss)
        s.add(
            Fact(
                id=8000, cik=2, concept="dei:EntityCommonStockSharesOutstanding", unit="shares",
                value=Decimal(10_000_000), period_start=None, period_end=dt.date(2026, 2, 1),
                form="10-K", accession="0000000002-26-000001",
            )
        )  # fmt: skip
    (path,) = run(ctx).files
    v = recalc(path)
    assert v["Comps!C4"] == pytest.approx(200e6)
    assert v["Comps!J4"] == "NM"  # net income -50M
    assert "P/E NM" in openpyxl.load_workbook(path)["Comps"]["K4"].value


def test_peer_stats_exclude_target_and_nm_peers(ctx: Context) -> None:
    (path,) = run(ctx).files
    wb = openpyxl.load_workbook(path)
    comps = wb["Comps"]
    labels = {comps.cell(row=r, column=2).value: r for r in range(1, comps.max_row + 1)}
    med, n = labels["Median"], labels["n (peers with a value)"]
    assert comps[f"H{med}"].value == '=IFERROR(MEDIAN(H3:H5),"NM")'  # peers only, not row 2
    v = recalc(path)
    # only FAB has values; BANK and NODATA are NM and skipped
    assert v[f"Comps!H{med}"] == pytest.approx(1250 / 800)
    assert v[f"Comps!H{n}"] == 1
    assert v[f"Comps!J{labels['75th percentile']}"] == pytest.approx(10.0)


def test_peer_override(ctx: Context) -> None:
    run(ctx, peers=["FAB"])
    wb = openpyxl.load_workbook(ctx.out_dir / "comps_CHIP_2026-11-24.xlsx")
    col_a = [wb["Comps"].cell(row=r, column=1).value for r in range(2, wb["Comps"].max_row + 1)]
    assert col_a[:2] == ["CHIP", "FAB"]
    assert "BANK" not in col_a and "NODATA" not in col_a  # the universe group is overridden


def test_unknown_target_or_peer_fails_cleanly(ctx: Context) -> None:
    res = Comps().run({"ticker": "TSLA"}, ctx)
    assert res.status == "failed" and "TSLA is not in the covered universe" in (res.error or "")
    res = Comps().run({"ticker": "CHIP", "peers": ["ZZZ"]}, ctx)
    assert res.status == "failed" and "ZZZ" in (res.error or "")
    assert isinstance(ToolError("x"), Exception)


def test_run_is_stored(ctx: Context, session_factory: SessionFactory) -> None:
    res = run(ctx)
    with session_factory() as s:
        row = s.scalar(select(WorkflowRun).where(WorkflowRun.id == res.run_id))
        assert row is not None
        assert (row.type, row.status, row.inputs["ticker"]) == ("comps", "succeeded", "CHIP")
        assert row.outputs is not None and row.outputs["files"][0].endswith(".xlsx")
        assert [s["name"] for s in row.trace["steps"]] == [  # type: ignore[index]
            "resolve peers",
            "gather inputs",
            "compute check values",
            "write xlsx",
        ]
        assert row.cost_usd == 0
    assert "EV/Revenue 3.2x vs peer median 1.6x" in res.summary


def test_every_function_is_one_excel_knows(ctx: Context) -> None:
    """openpyxl writes formulas as text; a function Excel stores with an `_xlfn.` prefix
    (QUARTILE.INC, ...) would show #NAME? in Excel, hidden by IFERROR. Every function used
    must be in openpyxl's list of plain Excel functions."""
    import re

    from openpyxl.utils import FORMULAE

    (path,) = run(ctx).files
    wb = openpyxl.load_workbook(path)
    used = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    used |= set(re.findall(r"([A-Z][A-Z0-9.]*)\(", c.value))
    assert used and used <= FORMULAE, used - FORMULAE


def test_every_input_value_has_a_citation_comment(ctx: Context) -> None:
    (path,) = run(ctx).files
    ws = openpyxl.load_workbook(path)["Inputs"]
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=10):
        for c in row:
            if c.value is not None:
                assert c.comment is not None and c.comment.text, c.coordinate


def test_duplicate_peers_count_once(ctx: Context) -> None:
    (path,) = run(ctx, peers=["FAB", "FAB", "fab"]).files
    v = recalc(path)
    comps = openpyxl.load_workbook(path)["Comps"]
    col_a = [comps.cell(row=r, column=1).value for r in range(2, comps.max_row + 1)]
    assert col_a.count("FAB") == 1
    labels = {comps.cell(row=r, column=2).value: r for r in range(1, comps.max_row + 1)}
    assert v[f"Comps!H{labels['n (peers with a value)']}"] == 1


def test_no_peers_and_all_nm_columns_show_nm(ctx: Context) -> None:
    (path,) = run(ctx, peers=[]).files  # no peers at all
    comps = openpyxl.load_workbook(path)["Comps"]
    labels = {comps.cell(row=r, column=2).value: r for r in range(1, comps.max_row + 1)}
    assert comps[f"H{labels['Median']}"].value == "NM"
    (path,) = run(ctx, peers=["BANK"]).files  # one peer, all NM
    v = recalc(path)
    comps = openpyxl.load_workbook(path)["Comps"]
    labels = {comps.cell(row=r, column=2).value: r for r in range(1, comps.max_row + 1)}
    for stat in ("Median", "Mean", "25th percentile", "75th percentile"):
        assert v[f"Comps!H{labels[stat]}"] == "NM", stat
    assert v[f"Comps!H{labels['n (peers with a value)']}"] == 0


def test_mixed_bases_are_noted_on_the_comps_sheet(ctx: Context) -> None:
    (path,) = run(ctx).files
    notes = openpyxl.load_workbook(path)["Comps"]["K2"].value  # CHIP: revenue LTM, EBITDA FY
    assert "bases differ: revenue LTM to 2025-12-31, EBITDA FY2025" in notes


def test_failed_run_is_stored_as_failed(ctx: Context, session_factory: SessionFactory) -> None:
    res = Comps().run({"ticker": "TSLA"}, ctx)
    with session_factory() as s:
        row = s.get(WorkflowRun, res.run_id)
        assert row is not None and row.status == "failed"
        assert row.trace is not None and row.trace["steps"][-1]["error"]
