"""Q4 derivation (analyst/metrics/quarters.py): exact arithmetic, refuse on bad inputs."""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

import pytest

from analyst.edgar.concepts import SeriesPoint
from analyst.metrics.quarters import Q4_METHOD, derive_q4, q4_value, with_derived_q4

D = dt.date
_ids = itertools.count(100)


def pt(start, end, value, canonical="revenue", unit="USD", fq=None) -> SeriesPoint:
    return SeriesPoint(
        canonical,
        start,
        end,
        end.year,
        Decimal(value),
        unit,
        "us-gaap:X",
        next(_ids),
        "0000000001-25-000001",
        "10-Q",
        end,
        fq,
    )


# Microsoft FY2025 (July-June), real reported values in $M.
FY = pt(D(2024, 7, 1), D(2025, 6, 30), 281724)
Q1 = pt(D(2024, 7, 1), D(2024, 9, 30), 65585)
Q2 = pt(D(2024, 10, 1), D(2024, 12, 31), 69632)
Q3 = pt(D(2025, 1, 1), D(2025, 3, 31), 70066)


def test_q4_value_is_exact():
    assert q4_value(Decimal("10.10"), [Decimal("1.01"), Decimal("2.02"), Decimal("3.03")]) == (
        Decimal("4.04")
    )
    with pytest.raises(ValueError):
        q4_value(Decimal(1), [Decimal(1)])


def test_derive_q4_msft_fy2025():
    d = derive_q4(FY, [Q3, Q1, Q2], "0630")
    assert d is not None
    assert d.value == 76441  # Microsoft's reported Q4 FY2025 revenue, $76.4B
    assert (d.period_start, d.period_end) == (D(2025, 4, 1), D(2025, 6, 30))
    assert (d.fiscal_year, d.fiscal_quarter, d.method) == (2025, 4, Q4_METHOD)
    assert [i.fact_id for i in d.inputs] == [FY.fact_id, Q1.fact_id, Q2.fact_id, Q3.fact_id]


def test_refuses_when_a_quarter_is_missing():
    assert derive_q4(FY, [Q1, Q3], "0630") is None


def test_refuses_when_quarters_leave_a_gap():
    q2_late = pt(D(2024, 10, 20), D(2024, 12, 31), 69632)
    assert derive_q4(FY, [Q1, q2_late, Q3], "0630") is None


def test_ignores_quarters_from_another_year():
    prior_q4 = pt(D(2024, 4, 1), D(2024, 6, 30), 64727)
    d = derive_q4(FY, [prior_q4, Q1, Q2, Q3], "0630")
    assert d is not None and d.value == 76441


def test_refuses_per_share_items():
    eps = [pt(FY.period_start, FY.period_end, "13.64", "eps_diluted", "USD/shares")]
    qs = [
        pt(q.period_start, q.period_end, "3.30", "eps_diluted", "USD/shares") for q in (Q1, Q2, Q3)
    ]
    assert derive_q4(eps[0], qs, "0630") is None


def test_refuses_unit_mismatch():
    q3_eur = pt(Q3.period_start, Q3.period_end, 70066, unit="EUR")
    assert derive_q4(FY, [Q1, Q2, q3_eur], "0630") is None


def test_with_derived_q4_adds_only_missing_q4s():
    out = with_derived_q4([FY], [Q1, Q2, Q3], "0630")
    assert [p.period_end for p in out] == [
        Q1.period_end,
        Q2.period_end,
        Q3.period_end,
        FY.period_end,
    ]
    reported_q4 = pt(D(2025, 4, 1), D(2025, 6, 30), 76441)
    out = with_derived_q4([FY], [Q1, Q2, Q3, reported_q4], "0630")
    assert out[-1] is reported_q4  # a reported value is never replaced by a derived one
