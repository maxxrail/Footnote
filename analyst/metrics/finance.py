"""Deterministic finance math: growth, margins, LTM, valuation multiples.

Pure functions over `Operand`s, exact `Decimal` arithmetic. Ratios are fractions
(0.124, not 12.4); presenting them as percentages is the caller's job. Each function
returns a `Computed` whose `value` is None (with a `note`) when the result would be
meaningless: a non-positive base for growth, negative earnings for a P/E, a gap in the
quarters for LTM.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence
from decimal import Decimal, localcontext

from analyst.metrics.computed import Computed, Operand, Unit, missing

ZERO = Decimal(0)
ONE = Decimal(1)
LTM_DAYS = (350, 380)  # four quarters of a 52/53-week year
MAX_QUARTER_GAP_DAYS = 7


def _none(
    metric: str, formula: str, inputs: Sequence[Operand], note: str, unit: Unit = "ratio"
) -> Computed:
    return Computed(metric, None, formula, tuple(inputs), unit, note=note)


# -- growth --------------------------------------------------------------------------------


def growth(current: Operand | None, prior: Operand | None, metric: str = "growth") -> Computed:
    """Period-over-period growth: (current - prior) / prior."""
    formula = "(current - prior) / prior"
    if current is None or prior is None:
        return missing(metric, formula, current, prior)
    if prior.value <= ZERO:
        return _none(metric, formula, (current, prior), "prior value is not positive")
    return Computed(metric, (current.value - prior.value) / prior.value, formula, (current, prior))


def cagr(start: Operand | None, end: Operand | None, years: int, metric: str = "cagr") -> Computed:
    """Compound annual growth: (end / start) ^ (1 / years) - 1."""
    if years <= 0:
        raise ValueError("years must be positive")
    formula = f"(end / start) ^ (1 / {years}) - 1"
    if start is None or end is None:
        return missing(metric, formula, start, end)
    if start.value <= ZERO or end.value <= ZERO:
        return _none(metric, formula, (start, end), "start or end value is not positive")
    with localcontext() as ctx:
        ctx.prec = 28
        value = (end.value / start.value) ** (ONE / Decimal(years)) - ONE
    return Computed(metric, value, formula, (start, end))


# -- margins -------------------------------------------------------------------------------


def margin(numerator: Operand | None, revenue: Operand | None, metric: str = "margin") -> Computed:
    """numerator / revenue (gross, operating, net margin...)."""
    formula = f"{numerator.label if numerator else 'numerator'} / revenue"
    if numerator is None or revenue is None:
        return missing(metric, formula, numerator, revenue)
    if revenue.value <= ZERO:
        return _none(metric, formula, (numerator, revenue), "revenue is not positive")
    return Computed(metric, numerator.value / revenue.value, formula, (numerator, revenue))


# -- LTM -----------------------------------------------------------------------------------


def ltm(quarters: Sequence[Operand], metric: str = "ltm") -> Computed:
    """Last twelve months: the sum of the last four quarters, which must be contiguous
    and span a year. Each operand needs its period start and end."""
    formula = "Q(t-3) + Q(t-2) + Q(t-1) + Q(t)"
    ordered = sorted(quarters, key=lambda q: q.period_end or dt.date.min)[-4:]
    if len(ordered) < 4:
        return _none(metric, formula, ordered, f"need 4 quarters, have {len(ordered)}", "USD")
    if any(q.period_start is None or q.period_end is None for q in ordered):
        return _none(metric, formula, ordered, "quarter without a period", "USD")
    for prev, nxt in itertools.pairwise(ordered):
        gap = (nxt.period_start - prev.period_end).days  # type: ignore[operator]
        if not 0 < gap <= MAX_QUARTER_GAP_DAYS:
            return _none(metric, formula, ordered, "quarters are not contiguous", "USD")
    span = (ordered[-1].period_end - ordered[0].period_start).days  # type: ignore[operator]
    if not LTM_DAYS[0] <= span <= LTM_DAYS[1]:
        return _none(metric, formula, ordered, f"quarters span {span} days, not a year", "USD")
    return Computed(metric, sum((q.value for q in ordered), ZERO), formula, tuple(ordered), "USD")


# -- valuation -----------------------------------------------------------------------------


def market_cap(price: Operand | None, shares: Operand | None) -> Computed:
    formula = "price * shares outstanding"
    if price is None or shares is None:
        return missing("market_cap", formula, price, shares, unit="USD")
    if price.value <= ZERO or shares.value <= ZERO:
        return _none("market_cap", formula, (price, shares), "price or shares not positive", "USD")
    return Computed("market_cap", price.value * shares.value, formula, (price, shares), "USD")


def enterprise_value(
    mkt_cap: Operand | None, debt: Operand | None, cash: Operand | None
) -> Computed:
    """EV = market cap + debt - cash."""
    formula = "market cap + total debt - cash"
    if mkt_cap is None or debt is None or cash is None:
        return missing("enterprise_value", formula, mkt_cap, debt, cash, unit="USD")
    value = mkt_cap.value + debt.value - cash.value
    return Computed("enterprise_value", value, formula, (mkt_cap, debt, cash), "USD")


def ebitda(operating_income: Operand | None, d_and_a: Operand | None) -> Computed:
    """EBITDA = operating income + depreciation & amortization."""
    formula = "operating income + D&A"
    if operating_income is None or d_and_a is None:
        return missing("ebitda", formula, operating_income, d_and_a, unit="USD")
    value = operating_income.value + d_and_a.value
    return Computed("ebitda", value, formula, (operating_income, d_and_a), "USD")


def multiple(
    numerator: Operand | None, denominator: Operand | None, metric: str, formula: str
) -> Computed:
    """numerator / denominator as an "x" multiple; None if the denominator isn't positive
    (a negative EBITDA or earnings multiple is not meaningful)."""
    if numerator is None or denominator is None:
        return missing(metric, formula, numerator, denominator, unit="x")
    if denominator.value <= ZERO:
        return _none(
            metric, formula, (numerator, denominator), f"{denominator.label} is not positive", "x"
        )
    return Computed(
        metric, numerator.value / denominator.value, formula, (numerator, denominator), "x"
    )


def ev_to_revenue(ev: Operand | None, revenue: Operand | None) -> Computed:
    return multiple(ev, revenue, "ev_to_revenue", "enterprise value / revenue")


def ev_to_ebitda(ev: Operand | None, ebitda_: Operand | None) -> Computed:
    return multiple(ev, ebitda_, "ev_to_ebitda", "enterprise value / EBITDA")


def pe(price: Operand | None, eps: Operand | None) -> Computed:
    """Price / diluted EPS. None for zero or negative earnings."""
    return multiple(price, eps, "pe", "price / diluted EPS")


def net_debt_to_ebitda(
    debt: Operand | None, cash: Operand | None, ebitda_: Operand | None
) -> Computed:
    """(debt - cash) / EBITDA. None if EBITDA is not positive. Can be negative (net cash)."""
    formula = "(total debt - cash) / EBITDA"
    if debt is None or cash is None or ebitda_ is None:
        return missing("net_debt_to_ebitda", formula, debt, cash, ebitda_, unit="x")
    if ebitda_.value <= ZERO:
        return _none(
            "net_debt_to_ebitda", formula, (debt, cash, ebitda_), "EBITDA is not positive", "x"
        )
    value = (debt.value - cash.value) / ebitda_.value
    return Computed("net_debt_to_ebitda", value, formula, (debt, cash, ebitda_), "x")
