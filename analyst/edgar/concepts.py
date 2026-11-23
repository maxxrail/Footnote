"""Canonical financial line items -> XBRL concepts, and clean per-period series.

Companies tag the same line item differently (and change tags over time: Apple used
`SalesRevenueNet` until FY2018 and `RevenueFromContractWithCustomerExcludingAssessedTax`
after). Each canonical item has an ordered list of candidate concepts. For every period,
the highest-priority candidate that has a value for that period wins, so a series stays
continuous across tag changes.

Selection rules (see `select_series`):
- FY: facts from annual forms (10-K, 10-K/A). Duration items need a 350-380 day period.
  Instant items (balance sheet) need a period end within 7 days of the company's fiscal
  year end (52/53-week years drift by a few days).
- Q: facts from 10-Q (and 10-Q/A). Duration items need a 80-100 day period, so only
  discretely reported quarters are returned: year-to-date cash-flow figures are skipped,
  and Q4 is never derived here, because derived numbers are arithmetic and belong in
  `analyst/metrics/` (Part 3).
- Restatements: the same period is reported again in later filings; the latest filing
  (by filed date, then accession) wins, and its fact id and accession are returned.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.db.models import Company, Fact, Filing

Period = Literal["FY", "Q"]


@dataclass(frozen=True)
class CanonicalConcept:
    name: str
    candidates: tuple[str, ...]  # "taxonomy:Concept", highest priority first
    unit: str
    instant: bool = False  # balance-sheet item measured at a point in time
    description: str = ""


def _c(
    name: str, unit: str, *candidates: str, instant: bool = False, desc: str = ""
) -> CanonicalConcept:
    return CanonicalConcept(name, tuple(candidates), unit, instant, desc)


CONCEPTS: dict[str, CanonicalConcept] = {
    c.name: c
    for c in [
        _c(
            "revenue",
            "USD",
            "us-gaap:Revenues",
            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap:SalesRevenueNet",
            "us-gaap:RevenuesNetOfInterestExpense",  # banks
            "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
            "us-gaap:SalesRevenueGoodsNet",
            desc="Total revenue",
        ),
        _c("gross_profit", "USD", "us-gaap:GrossProfit", desc="Gross profit"),
        _c("operating_income", "USD", "us-gaap:OperatingIncomeLoss", desc="Operating income"),
        _c(
            "net_income",
            "USD",
            "us-gaap:NetIncomeLoss",
            "us-gaap:ProfitLoss",
            "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
            desc="Net income attributable to the parent",
        ),
        _c("eps_diluted", "USD/shares", "us-gaap:EarningsPerShareDiluted", desc="Diluted EPS"),
        _c("total_assets", "USD", "us-gaap:Assets", instant=True, desc="Total assets"),
        _c(
            "total_liabilities",
            "USD",
            "us-gaap:Liabilities",
            instant=True,
            desc="Total liabilities",
        ),
        _c(
            "cash",
            "USD",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndDueFromBanks",
            "us-gaap:Cash",
            instant=True,
            desc="Cash and cash equivalents",
        ),
        _c(
            "total_debt",
            "USD",
            "us-gaap:LongTermDebt",
            "us-gaap:DebtInstrumentCarryingAmount",
            "us-gaap:LongTermDebtNoncurrent",
            "us-gaap:DebtLongtermAndShorttermCombinedAmount",
            instant=True,
            desc="Total debt (long-term debt incl. current portion where reported)",
        ),
        _c(
            "shares_outstanding",
            "shares",
            "us-gaap:CommonStockSharesOutstanding",
            "dei:EntityCommonStockSharesOutstanding",
            instant=True,
            desc="Common shares outstanding",
        ),
        _c(
            "operating_cash_flow",
            "USD",
            "us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            desc="Cash from operations",
        ),
        _c(
            "capex",
            "USD",
            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
            "us-gaap:PaymentsToAcquireProductiveAssets",
            desc="Capital expenditures (reported as a positive payment)",
        ),
        _c(
            "d_and_a",
            "USD",
            "us-gaap:DepreciationDepletionAndAmortization",
            "us-gaap:DepreciationAmortizationAndAccretionNet",
            "us-gaap:DepreciationAndAmortization",
            "us-gaap:Depreciation",
            desc="Depreciation and amortization",
        ),
    ]
}

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
FY_DAYS = (350, 380)
Q_DAYS = (80, 100)
FY_END_TOLERANCE_DAYS = 7


@dataclass(frozen=True)
class FactRecord:
    """The columns of a fact that selection needs, plus its filing's filed date."""

    fact_id: int
    concept: str
    unit: str
    value: Decimal
    period_start: dt.date | None
    period_end: dt.date
    form: str
    accession: str
    filed_at: dt.date


@dataclass(frozen=True)
class SeriesPoint:
    canonical: str
    period_start: dt.date | None
    period_end: dt.date
    # Calendar year of period_end. Matches how most issuers name fiscal years (Apple FY2024
    # ends 2024-09-28; NVIDIA FY2025 ends 2025-01-26), not every retailer's convention.
    fiscal_year: int
    value: Decimal
    unit: str
    concept: str
    fact_id: int
    accession: str
    form: str
    filed_at: dt.date


def _near_fiscal_year_end(day: dt.date, fiscal_year_end: str) -> bool:
    """True if `day` is within the tolerance of MMDD, wrapping around new year."""
    month, dom = int(fiscal_year_end[:2]), int(fiscal_year_end[2:])
    for year in (day.year - 1, day.year, day.year + 1):
        try:
            anchor = dt.date(year, month, dom)
        except ValueError:  # 0229 in a non-leap year
            anchor = dt.date(year, month, 28)
        if abs((day - anchor).days) <= FY_END_TOLERANCE_DAYS:
            return True
    return False


def _period_ok(
    f: FactRecord, concept: CanonicalConcept, period: Period, fiscal_year_end: str | None
) -> bool:
    forms = ANNUAL_FORMS if period == "FY" else QUARTERLY_FORMS
    if f.form not in forms or f.unit != concept.unit:
        return False
    if concept.instant:
        if f.period_start is not None:
            return False
        if period == "FY" and fiscal_year_end:
            return _near_fiscal_year_end(f.period_end, fiscal_year_end)
        return True
    if f.period_start is None:
        return False
    lo, hi = FY_DAYS if period == "FY" else Q_DAYS
    return lo <= (f.period_end - f.period_start).days <= hi


def select_series(
    facts: Iterable[FactRecord],
    canonical: str,
    period: Period = "FY",
    fiscal_year_end: str | None = None,
) -> list[SeriesPoint]:
    """Pure selection: filter by period type, de-dupe restatements, apply candidate fallback."""
    concept = CONCEPTS[canonical]
    priority = {name: i for i, name in enumerate(concept.candidates)}

    # Per (concept, period): latest filing wins.
    latest: dict[tuple[str, dt.date | None, dt.date], FactRecord] = {}
    for f in facts:
        if f.concept not in priority or not _period_ok(f, concept, period, fiscal_year_end):
            continue
        key = (f.concept, f.period_start, f.period_end)
        cur = latest.get(key)
        if cur is None or (f.filed_at, f.accession) > (cur.filed_at, cur.accession):
            latest[key] = f

    # Per period end: highest-priority candidate wins.
    best: dict[dt.date, FactRecord] = {}
    for f in latest.values():
        cur = best.get(f.period_end)
        if cur is None or priority[f.concept] < priority[cur.concept]:
            best[f.period_end] = f

    return [
        SeriesPoint(
            canonical=canonical,
            period_start=f.period_start,
            period_end=f.period_end,
            fiscal_year=f.period_end.year,
            value=f.value,
            unit=f.unit,
            concept=f.concept,
            fact_id=f.fact_id,
            accession=f.accession,
            form=f.form,
            filed_at=f.filed_at,
        )
        for f in sorted(best.values(), key=lambda f: f.period_end)
    ]


def load_fact_records(session: Session, cik: int, concepts: Sequence[str]) -> list[FactRecord]:
    rows = session.execute(
        select(
            Fact.id,
            Fact.concept,
            Fact.unit,
            Fact.value,
            Fact.period_start,
            Fact.period_end,
            Fact.form,
            Fact.accession,
            Filing.filed_at,
        )
        .join(Filing, Filing.accession == Fact.accession)
        .where(Fact.cik == cik, Fact.concept.in_(list(concepts)))
    ).all()
    return [FactRecord(*row) for row in rows]


def get_series(
    session: Session, cik: int, canonical: str, period: Period = "FY"
) -> list[SeriesPoint]:
    """Clean series for one company and canonical item, each point traceable to a fact."""
    if canonical not in CONCEPTS:
        raise KeyError(f"unknown canonical concept {canonical!r}; known: {sorted(CONCEPTS)}")
    if period not in ("FY", "Q"):
        raise ValueError("period must be 'FY' or 'Q'")
    fye = session.scalar(select(Company.fiscal_year_end).where(Company.cik == cik))
    records = load_fact_records(session, cik, CONCEPTS[canonical].candidates)
    return select_series(records, canonical, period, fye)
