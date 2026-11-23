from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

import pytest

from analyst.edgar.concepts import CONCEPTS, FactRecord, _near_fiscal_year_end, select_series

D = dt.date
_ids = itertools.count(1)
FILED = D(2024, 11, 1)


def fact(
    concept,
    value,
    start,
    end,
    *,
    form="10-K",
    accn="0000000001-24-000001",
    filed=FILED,
    unit="USD",
) -> FactRecord:
    return FactRecord(next(_ids), concept, unit, Decimal(value), start, end, form, accn, filed)


REV = "us-gaap:Revenues"
RFC = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
SRN = "us-gaap:SalesRevenueNet"


def test_every_canonical_concept_from_the_spec_exists():
    expected = {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "eps_diluted",
        "total_assets",
        "total_liabilities",
        "cash",
        "total_debt",
        "shares_outstanding",
        "operating_cash_flow",
        "capex",
        "d_and_a",
    }
    assert expected <= set(CONCEPTS)


def test_revenue_candidate_order_matches_spec():
    assert CONCEPTS["revenue"].candidates[:3] == (REV, RFC, SRN)


def test_fallback_prefers_earlier_candidate_for_same_period():
    facts = [
        fact(SRN, 100, D(2023, 1, 1), D(2023, 12, 31)),
        fact(RFC, 200, D(2023, 1, 1), D(2023, 12, 31)),
        fact(REV, 300, D(2023, 1, 1), D(2023, 12, 31)),
    ]
    [p] = select_series(facts, "revenue")
    assert (p.concept, p.value) == (REV, 300)


def test_fallback_fills_periods_missing_from_preferred_tag():
    # Apple-style tag change: old years only under SalesRevenueNet, new under RFC.
    facts = [
        fact(SRN, 100, D(2016, 1, 1), D(2016, 12, 31)),
        fact(SRN, 110, D(2017, 1, 1), D(2017, 12, 31)),
        fact(RFC, 120, D(2018, 1, 1), D(2018, 12, 31)),
        fact(RFC, 111, D(2017, 1, 1), D(2017, 12, 31)),  # comparative under new tag
    ]
    pts = select_series(facts, "revenue")
    assert [(p.fiscal_year, p.concept, p.value) for p in pts] == [
        (2016, SRN, 100),
        (2017, RFC, 111),
        (2018, RFC, 120),
    ]


def test_fy_keeps_only_annual_durations_from_10k():
    facts = [
        fact(REV, 400, D(2023, 1, 1), D(2023, 12, 31)),  # 364 days, 10-K: keep
        fact(REV, 100, D(2023, 10, 1), D(2023, 12, 31)),  # quarter inside a 10-K: drop
        fact(REV, 300, D(2023, 1, 1), D(2023, 9, 30), form="10-Q"),  # 9-month YTD: drop
        fact(REV, 401, D(2022, 1, 1), D(2022, 12, 31), form="10-Q"),  # wrong form: drop
    ]
    pts = select_series(facts, "revenue", "FY")
    assert [p.value for p in pts] == [400]


def test_q_keeps_only_quarterly_durations_from_10q():
    facts = [
        fact(REV, 100, D(2024, 1, 1), D(2024, 3, 31), form="10-Q"),  # 90 days: keep
        fact(REV, 210, D(2024, 1, 1), D(2024, 6, 30), form="10-Q"),  # 6-month YTD: drop
        fact(REV, 110, D(2024, 4, 1), D(2024, 6, 30), form="10-Q"),  # keep
        fact(REV, 400, D(2023, 1, 1), D(2023, 12, 31), form="10-Q"),  # annual: drop
        fact(REV, 99, D(2023, 10, 1), D(2023, 12, 31), form="10-K"),  # 10-K: drop
    ]
    pts = select_series(facts, "revenue", "Q")
    assert [p.value for p in pts] == [100, 110]


def test_restatement_keeps_latest_filing():
    old = fact(
        REV, 100, D(2022, 1, 1), D(2022, 12, 31), accn="0000000001-23-000001", filed=D(2023, 2, 1)
    )
    new = fact(
        REV, 105, D(2022, 1, 1), D(2022, 12, 31), accn="0000000001-24-000001", filed=D(2024, 2, 1)
    )
    for order in ([old, new], [new, old]):
        [p] = select_series(order, "revenue")
        assert (p.value, p.accession, p.fact_id) == (105, new.accession, new.fact_id)


def test_restatement_same_day_tiebreak_on_accession():
    a = fact(REV, 1, D(2022, 1, 1), D(2022, 12, 31), accn="0000000001-24-000001")
    b = fact(REV, 2, D(2022, 1, 1), D(2022, 12, 31), accn="0000000001-24-000002")
    assert select_series([b, a], "revenue")[0].value == 2


def test_unit_must_match():
    facts = [fact(REV, 1, D(2023, 1, 1), D(2023, 12, 31), unit="EUR")]
    assert select_series(facts, "revenue") == []


def test_instant_fy_needs_fiscal_year_end():
    assets = "us-gaap:Assets"
    facts = [
        fact(assets, 10, None, D(2024, 9, 28)),  # FY end (Apple, 52/53-week year)
        fact(assets, 9, None, D(2023, 9, 30)),  # prior FY end
        fact(assets, 8, None, D(2024, 3, 30)),  # mid-year instant in a 10-K: drop
        fact(assets, 7, D(2023, 9, 30), D(2024, 9, 28)),  # a duration is not an instant
    ]
    pts = select_series(facts, "total_assets", "FY", fiscal_year_end="0928")
    assert [(p.period_end, p.value) for p in pts] == [(D(2023, 9, 30), 9), (D(2024, 9, 28), 10)]


def test_point_carries_provenance():
    f = fact(REV, 5, D(2023, 1, 1), D(2023, 12, 31), accn="0000320193-24-000123")
    [p] = select_series([f], "revenue")
    assert (p.fact_id, p.accession, p.concept, p.form) == (f.fact_id, f.accession, REV, "10-K")


@pytest.mark.parametrize(
    ("day", "fye", "ok"),
    [
        (D(2024, 12, 31), "1231", True),
        (D(2025, 1, 2), "1231", True),  # wraps the new year
        (D(2024, 12, 20), "1231", False),
        (D(2025, 1, 26), "0126", True),
        (D(2024, 2, 29), "0228", True),
        (D(2023, 2, 28), "0229", True),  # non-leap fallback
    ],
)
def test_near_fiscal_year_end(day, fye, ok):
    assert _near_fiscal_year_end(day, fye) is ok
