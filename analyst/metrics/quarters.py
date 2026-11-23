"""Fiscal Q4 for flow items: Q4 = FY - (Q1 + Q2 + Q3).

Companies don't file a 10-Q for Q4; the fourth quarter is only inside the 10-K's full
year. For additive items (revenue, net income, ...), Q4 is derived here, deterministically
and only when all three reported quarters exist and tile the fiscal year without gaps.
The derived point cites all four facts it was computed from. Per-share items (EPS) are
never derived, because quarterly share counts differ.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from analyst.db.models import Company
from analyst.edgar.concepts import CONCEPTS, Q_DAYS, SeriesPoint, fiscal_period, get_series

Q4_METHOD = "FY - (Q1 + Q2 + Q3)"
_START_TOLERANCE = dt.timedelta(days=7)  # 52/53-week calendars drift a few days
_MAX_GAP_DAYS = 7  # between one quarter's end and the next quarter's start


@dataclass(frozen=True)
class DerivedPoint:
    canonical: str
    period_start: dt.date
    period_end: dt.date
    fiscal_year: int
    fiscal_quarter: int
    value: Decimal
    unit: str
    method: str
    inputs: tuple[SeriesPoint, ...]  # the FY point first, then Q1-Q3


def q4_value(full_year: Decimal, quarters: Sequence[Decimal]) -> Decimal:
    """FY minus the first three quarters. Exact Decimal arithmetic."""
    if len(quarters) != 3:
        raise ValueError(f"need exactly 3 quarters, got {len(quarters)}")
    return full_year - sum(quarters, Decimal(0))


def derive_q4(
    fy: SeriesPoint, quarters: Sequence[SeriesPoint], fiscal_year_end: str | None
) -> DerivedPoint | None:
    """Q4 for one fiscal year, or None if the inputs don't support it (refuse, don't guess)."""
    concept = CONCEPTS[fy.canonical]
    if concept.instant or not concept.additive or fy.period_start is None:
        return None
    inside = sorted(
        (
            q
            for q in quarters
            if q.canonical == fy.canonical
            and q.unit == fy.unit
            and q.period_start is not None
            and q.period_start >= fy.period_start - _START_TOLERANCE
            and q.period_end < fy.period_end
        ),
        key=lambda q: q.period_end,
    )
    if len(inside) != 3:
        return None
    q1, q2, q3 = inside
    assert q1.period_start is not None
    if abs((q1.period_start - fy.period_start).days) > _START_TOLERANCE.days:
        return None
    for prev, nxt in ((q1, q2), (q2, q3)):
        assert nxt.period_start is not None
        if not 0 < (nxt.period_start - prev.period_end).days <= _MAX_GAP_DAYS:
            return None
    lo, hi = Q_DAYS
    q4_start = q3.period_end + dt.timedelta(days=1)
    if not lo <= (fy.period_end - q4_start).days <= hi:
        return None
    fiscal_year, _ = fiscal_period(fy.period_end, fiscal_year_end)
    return DerivedPoint(
        canonical=fy.canonical,
        period_start=q4_start,
        period_end=fy.period_end,
        fiscal_year=fiscal_year,
        fiscal_quarter=4,
        value=q4_value(fy.value, [q1.value, q2.value, q3.value]),
        unit=fy.unit,
        method=Q4_METHOD,
        inputs=(fy, q1, q2, q3),
    )


def with_derived_q4(
    fy_points: Sequence[SeriesPoint],
    q_points: Sequence[SeriesPoint],
    fiscal_year_end: str | None,
) -> list[SeriesPoint | DerivedPoint]:
    """Reported quarters plus a derived Q4 for each fiscal year that has no reported Q4."""
    reported_ends = {q.period_end for q in q_points}
    out: list[SeriesPoint | DerivedPoint] = list(q_points)
    for fy in fy_points:
        if fy.period_end in reported_ends:
            continue
        d = derive_q4(fy, q_points, fiscal_year_end)
        if d is not None:
            out.append(d)
    return sorted(out, key=lambda p: p.period_end)


def quarterly_series(
    session: Session, cik: int, canonical: str
) -> list[SeriesPoint | DerivedPoint]:
    """Quarterly series for a company: reported 10-Q values plus derived Q4s."""
    fye = session.get(Company, cik)
    fiscal_year_end = fye.fiscal_year_end if fye else None
    q_points = get_series(session, cik, canonical, "Q")
    if CONCEPTS[canonical].instant:
        return list(q_points)  # Q4 balances are reported in the 10-K
    return with_derived_q4(get_series(session, cik, canonical, "FY"), q_points, fiscal_year_end)
