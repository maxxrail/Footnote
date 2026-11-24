"""Hand-computed cases for the deterministic finance math (analyst.metrics)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.metrics import finance as fin
from analyst.metrics.computed import Computed, Operand
from analyst.metrics.prices import CsvPriceProvider, PriceQuote
from analyst.metrics.stats import peer_stat, peer_summary, quantile

D = Decimal


def op(value: str | int, label: str = "x", fact: int | None = 1, **kw: object) -> Operand:
    return Operand(label, D(str(value)), (fact,) if fact is not None else (), **kw)  # type: ignore[arg-type]


# -- Computed / Operand --------------------------------------------------------------------


def test_fact_ids_deduplicated_in_order() -> None:
    c = Computed("m", D(1), "f", (op(1, fact=3), op(2, fact=1), op(3, fact=3)))
    assert c.fact_ids == (3, 1)


def test_as_operand_carries_lineage_and_refuses_empty() -> None:
    c = Computed("market_cap", D(100), "price * shares", (op(1, fact=7), op(2, fact=None)), "USD")
    o = c.as_operand("AAPL market cap")
    assert (o.label, o.value, o.fact_ids, o.source, o.detail) == (
        "AAPL market cap",
        D(100),
        (7,),
        "computed",
        "price * shares",
    )
    assert c.as_operand().label == "market_cap"
    with pytest.raises(ValueError, match="no value"):
        Computed("pe", None, "f", note="negative EPS").as_operand()


# -- growth --------------------------------------------------------------------------------


def test_yoy_growth_hand_computed() -> None:
    # Apple FY2026 vs FY2025 net sales ($M): 478,834 / 416,161 - 1 = 15.06%
    g = fin.growth(op(478_834, fact=1), op(416_161, fact=2))
    assert g.value is not None
    assert round(g.value, 6) == D("0.150598")
    assert g.fact_ids == (1, 2)
    assert g.formula == "(current - prior) / prior"


def test_growth_can_be_negative() -> None:
    g = fin.growth(op(90), op(100))
    assert g.value == D("-0.1")


def test_growth_refuses_non_positive_base_and_missing() -> None:
    assert fin.growth(op(10), op(-5)).value is None
    assert fin.growth(op(10), op(-5)).note == "prior value is not positive"
    assert fin.growth(op(10), op(0)).value is None
    m = fin.growth(None, op(5))
    assert (m.value, m.note, len(m.inputs)) == (None, "missing input", 1)


def test_cagr_hand_computed() -> None:
    # 100 -> 121 over 2 years = 10% a year exactly
    c = fin.cagr(op(100), op(121), 2)
    assert c.value is not None and round(c.value, 12) == D("0.1")
    # 391,035 -> 478,834 over 2 years: sqrt(1.224523) - 1 = 10.658%
    c2 = fin.cagr(op(391_035), op(478_834), 2)
    assert c2.value is not None and round(c2.value, 5) == D("0.10658")
    assert c2.formula == "(end / start) ^ (1 / 2) - 1"


def test_cagr_refuses() -> None:
    assert fin.cagr(op(-1), op(10), 3).value is None
    assert fin.cagr(op(10), op(0), 3).note == "start or end value is not positive"
    assert fin.cagr(None, op(10), 3).note == "missing input"
    with pytest.raises(ValueError):
        fin.cagr(op(1), op(2), 0)


# -- margins -------------------------------------------------------------------------------


def test_margin_hand_computed() -> None:
    m = fin.margin(op(30, "operating income"), op(120, "revenue"), "operating_margin")
    assert m.value == D("0.25")
    assert m.metric == "operating_margin"
    assert m.formula == "operating income / revenue"


def test_margin_with_a_loss_is_negative_and_zero_revenue_refused() -> None:
    assert fin.margin(op(-12), op(100)).value == D("-0.12")
    assert fin.margin(op(5), op(0)).value is None
    assert fin.margin(None, op(5)).formula == "numerator / revenue"
    assert fin.margin(op(5), None).note == "missing input"


# -- LTM -----------------------------------------------------------------------------------


def q(start: str, end: str, value: int, fact: int) -> Operand:
    return op(
        value,
        f"Q ending {end}",
        fact=fact,
        period_start=dt.date.fromisoformat(start),
        period_end=dt.date.fromisoformat(end),
    )


MSFT_QS = [  # Microsoft revenue ($M), FY2025 Q2..FY2026 Q1
    q("2024-10-01", "2024-12-31", 69_632, 1),
    q("2025-01-01", "2025-03-31", 70_066, 2),
    q("2025-04-01", "2025-06-30", 76_441, 3),
    q("2025-07-01", "2025-09-30", 77_673, 4),
]


def test_ltm_sums_last_four_contiguous_quarters() -> None:
    older = q("2024-07-01", "2024-09-30", 65_585, 9)
    r = fin.ltm([*reversed(MSFT_QS), older])  # order doesn't matter; oldest dropped
    assert r.value == D(69_632 + 70_066 + 76_441 + 77_673)
    assert r.fact_ids == (1, 2, 3, 4)
    assert r.unit == "USD"


def test_ltm_refuses_too_few_gaps_and_wrong_span() -> None:
    assert fin.ltm(MSFT_QS[:3]).note == "need 4 quarters, have 3"
    gap = [*MSFT_QS[:2], q("2025-05-01", "2025-06-30", 1, 5), MSFT_QS[3]]
    assert fin.ltm(gap).note == "quarters are not contiguous"
    no_period = [*MSFT_QS[:3], op(5, fact=6, period_end=dt.date(2025, 12, 31))]
    assert fin.ltm(no_period).note == "quarter without a period"
    # contiguous 60-day "quarters" span ~240 days: not a year
    short = [
        q("2025-01-01", "2025-03-01", 1, 1),
        q("2025-03-02", "2025-05-01", 1, 2),
        q("2025-05-02", "2025-07-01", 1, 3),
        q("2025-07-02", "2025-09-01", 1, 4),
    ]
    assert "not a year" in fin.ltm(short).note


# -- valuation -----------------------------------------------------------------------------


def test_market_cap_ev_and_multiples_hand_computed() -> None:
    price, shares = op("200", "price", fact=None, source="price"), op(1_000, "shares", fact=1)
    mc = fin.market_cap(price, shares)
    assert mc.value == D(200_000)
    ev = fin.enterprise_value(
        mc.as_operand(), op(50_000, "debt", fact=2), op(30_000, "cash", fact=3)
    )
    assert ev.value == D(220_000)
    assert ev.fact_ids == (1, 2, 3)  # lineage flows through market cap
    e = fin.ebitda(op(40_000, "operating income", fact=4), op(4_000, "D&A", fact=5))
    assert e.value == D(44_000)
    assert fin.ev_to_ebitda(ev.as_operand(), e.as_operand()).value == D(5)
    assert fin.ev_to_revenue(ev.as_operand(), op(110_000, "revenue")).value == D(2)
    nd = fin.net_debt_to_ebitda(op(50_000), op(30_000), e.as_operand())
    assert nd.value is not None and round(nd.value, 6) == D("0.454545")
    assert fin.pe(price, op(8, "EPS")).value == D(25)


def test_negative_earnings_pe_is_none() -> None:
    r = fin.pe(op(50, "price"), op("-1.25", "diluted EPS"))
    assert r.value is None
    assert r.note == "diluted EPS is not positive"
    assert fin.pe(op(50), op(0)).value is None


def test_valuation_refusals() -> None:
    assert fin.market_cap(None, op(5)).note == "missing input"
    assert fin.market_cap(op(0), op(5)).value is None
    assert fin.enterprise_value(op(1), None, op(1)).value is None
    assert fin.ebitda(op(1), None).value is None
    assert fin.ev_to_ebitda(op(100), op(-5)).value is None
    assert fin.multiple(None, op(1), "m", "f").note == "missing input"
    assert fin.net_debt_to_ebitda(op(1), op(1), op(0)).note == "EBITDA is not positive"
    assert fin.net_debt_to_ebitda(op(1), None, op(1)).value is None
    # net cash gives a negative ratio, which is meaningful
    assert fin.net_debt_to_ebitda(op(10), op(30), op(10)).value == D(-2)


# -- peer stats ----------------------------------------------------------------------------


def comp(v: str | None, label: str) -> Computed:
    return Computed(
        "pe",
        D(v) if v is not None else None,
        "price / EPS",
        (op(1, fact=len(label)),),
        "x",
        label=label,
    )


def test_quantiles_match_excel_quartile_inc() -> None:
    v = [D(x) for x in (1, 2, 3, 4)]
    # Excel: QUARTILE.INC({1,2,3,4},1)=1.75, ,2)=2.5, ,3)=3.25
    assert (quantile(v, D("0.25")), quantile(v, D("0.5")), quantile(v, D("0.75"))) == (
        D("1.75"),
        D("2.5"),
        D("3.25"),
    )
    assert quantile([D(7)], D("0.5")) == D(7)
    assert quantile(v, D(1)) == D(4)
    with pytest.raises(ValueError):
        quantile([], D("0.5"))
    with pytest.raises(ValueError):
        quantile(v, D("1.5"))


def test_peer_median_skips_missing_and_says_so() -> None:
    items = [comp("30", "AAPL"), comp("20", "MSFT"), comp(None, "INTC"), comp("40", "NVDA")]
    med = peer_stat(items, "median")
    assert med.value == D(30)
    assert med.note == "excluded (no value): INTC"
    assert med.metric == "median(pe)"
    assert len(med.inputs) == 3
    s = peer_summary(items)
    assert s["mean"].value == D(30)
    assert (s["min"].value, s["max"].value) == (D(20), D(40))
    assert (s["q1"].value, s["q3"].value) == (D(25), D(35))
    assert s["q1"].formula == "25th percentile (QUARTILE.INC)"


def test_peer_stat_edge_cases() -> None:
    assert peer_stat([comp(None, "X")], "median").value is None
    assert peer_stat([], "median", "pe").note == "no values"
    with pytest.raises(ValueError, match="unknown stat"):
        peer_stat([comp("1", "A")], "mode")


# -- prices --------------------------------------------------------------------------------


def test_csv_price_provider(tmp_path: Path) -> None:
    p = tmp_path / "prices.csv"
    p.write_text(
        "ticker,date,close\naapl,2026-11-20,250.10\nAAPL,2026-11-21,251.00\nMSFT,2026-11-21,500\n",
        encoding="utf-8",
    )
    prices = CsvPriceProvider(p)
    latest = prices.latest("AAPL")
    assert latest == PriceQuote("AAPL", dt.date(2026, 11, 21), D("251.00"), p.as_posix())
    assert prices.latest("aapl", dt.date(2026, 11, 20)).close == D("250.10")  # type: ignore[union-attr]
    assert prices.latest("AAPL", dt.date(2026, 1, 1)) is None
    assert prices.latest("NVDA") is None
    o = latest.as_operand()
    assert (o.source, o.fact_ids, o.period_end) == ("price", (), dt.date(2026, 11, 21))
    assert "AAPL,2026-11-21,251.00" in o.detail


def test_csv_price_provider_missing_file_and_bad_rows(tmp_path: Path) -> None:
    assert CsvPriceProvider(tmp_path / "none.csv").latest("AAPL") is None
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker,date,close\nAAPL,not-a-date,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad price row"):
        CsvPriceProvider(bad).latest("AAPL")
    neg = tmp_path / "neg.csv"
    neg.write_text("ticker,date,close\nAAPL,2026-01-01,-3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive"):
        CsvPriceProvider(neg).latest("AAPL")
