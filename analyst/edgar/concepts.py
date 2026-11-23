"""Canonical financial line items -> XBRL concepts, and clean per-period series.

Companies tag the same line item differently (and change tags over time: Apple used
`SalesRevenueNet` until FY2018 and `RevenueFromContractWithCustomerExcludingAssessedTax`
after). Each canonical item has an ordered list of candidate concepts. For every period,
the value from the latest filing wins; within one filing, the highest-priority candidate
wins. So a series stays continuous across tag changes and restatements under a new tag.

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
            # Not dei:EntityCommonStockSharesOutstanding: it is measured on the cover-page
            # date weeks after the period end, so it is a different data point.
            "us-gaap:CommonStockSharesOutstanding",
            instant=True,
            desc="Common shares outstanding at the balance-sheet date",
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
    # See fiscal_year_label. Matches how most issuers name fiscal years (Apple FY2024 ends
    # 2024-09-28; NVIDIA FY2025 ends 2025-01-26), not every retailer's convention.
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


def _near_fiscal_quarter_end(day: dt.date, fiscal_year_end: str) -> bool:
    """True if `day` is near the fiscal year end or 3/6/9 months after it."""
    month, dom = int(fiscal_year_end[:2]), int(fiscal_year_end[2:])
    for k in range(4):
        m = (month - 1 + 3 * k) % 12 + 1
        if _near_fiscal_year_end(day, f"{m:02d}{min(dom, 28 if m == 2 else 30):02d}"):
            return True
    return False


def fiscal_year_label(period_end: dt.date) -> int:
    """Calendar year of the period end, except a year ending in the first week of January
    (52/53-week "closest to Dec 31" calendars) belongs to the year before."""
    if period_end.month == 1 and period_end.day <= FY_END_TOLERANCE_DAYS:
        return period_end.year - 1
    return period_end.year


def _period_ok(
    f: FactRecord, concept: CanonicalConcept, period: Period, fiscal_year_end: str | None
) -> bool:
    forms = ANNUAL_FORMS if period == "FY" else QUARTERLY_FORMS
    if f.form not in forms or f.unit != concept.unit:
        return False
    if concept.instant:
        # Balance-sheet instants must sit on a fiscal year end (FY) or fiscal quarter end (Q).
        # Without a known fiscal year end we can't tell, so refuse rather than guess.
        if f.period_start is not None or not fiscal_year_end:
            return False
        if period == "FY":
            return _near_fiscal_year_end(f.period_end, fiscal_year_end)
        return _near_fiscal_quarter_end(f.period_end, fiscal_year_end)
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

    def rank(f: FactRecord) -> tuple[dt.date, str, int, int]:
        # Latest filing first, so a restatement wins even if the company switched to a
        # lower-priority tag when restating. Within one filing, the higher-priority tag wins;
        # the fact id makes the choice deterministic for exact ties.
        return (f.filed_at, f.accession, -priority[f.concept], f.fact_id)

    # FY points are keyed by fiscal year, not exact end date: filers sometimes tag the same
    # year with ends a few days apart (GS FY2008: 2008-11-28 in one 10-K, 11-30 in the next).
    best: dict[object, FactRecord] = {}
    for f in facts:
        if f.concept not in priority or not _period_ok(f, concept, period, fiscal_year_end):
            continue
        key = fiscal_year_label(f.period_end) if period == "FY" else f.period_end
        cur = best.get(key)
        if cur is None or rank(f) > rank(cur):
            best[key] = f

    return [
        SeriesPoint(
            canonical=canonical,
            period_start=f.period_start,
            period_end=f.period_end,
            fiscal_year=fiscal_year_label(f.period_end),
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
