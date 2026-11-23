"""Print a canonical series for one company, with the filing behind every value.

uv run python -m analyst.edgar.show AAPL revenue
uv run python -m analyst.edgar.show MSFT revenue --period Q

Quarterly rows are labelled with the company's fiscal year and quarter. A derived Q4 row
(FY - Q1 - Q2 - Q3, computed in analyst.metrics.quarters) cites the four facts it used.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from analyst.db.models import Company
from analyst.db.session import session_scope
from analyst.edgar.concepts import CONCEPTS, SeriesPoint, get_series
from analyst.metrics.quarters import DerivedPoint, quarterly_series


def _label(p: SeriesPoint | DerivedPoint) -> str:
    return f"FY{p.fiscal_year}" + (f" Q{p.fiscal_quarter}" if p.fiscal_quarter else "")


def _row(p: SeriesPoint | DerivedPoint) -> tuple[str, ...]:
    period = f"{p.period_start or ''}..{p.period_end}"
    if isinstance(p, DerivedPoint):
        return (
            _label(p),
            period,
            f"{p.value:,}",
            p.unit,
            f"derived: {p.method}",
            "calc",
            p.inputs[0].accession,
            ",".join(str(i.fact_id) for i in p.inputs),
        )
    return (
        _label(p),
        period,
        f"{p.value:,}",
        p.unit,
        p.concept,
        p.form,
        p.accession,
        str(p.fact_id),
    )


def format_table(
    company: Company, canonical: str, points: Sequence[SeriesPoint | DerivedPoint]
) -> str:
    header = f"{company.ticker}  {company.name}  (CIK {company.cik})  {canonical}"
    cols = ("Fiscal", "Period", "Value", "Unit", "Concept", "Form", "Accession", "Fact id(s)")
    rows = [_row(p) for p in points]
    widths = [
        max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)
    ]
    right = {2}  # numbers right-aligned

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    out = [header, line(cols), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def show(session: Session, ticker: str, canonical: str, period: str = "FY") -> str:
    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise SystemExit(f"{ticker} is not in the database; run analyst.edgar.ingest first")
    points: Sequence[SeriesPoint | DerivedPoint]
    if period == "Q":
        points = quarterly_series(session, company.cik, canonical)
    else:
        points = get_series(session, company.cik, canonical, "FY")
    if not points:
        return f"No {period} values for {canonical} for {company.ticker} in the corpus."
    return format_table(company, canonical, points)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analyst.edgar.show")
    parser.add_argument("ticker")
    parser.add_argument("canonical", choices=sorted(CONCEPTS))
    parser.add_argument("--period", choices=["FY", "Q"], default="FY")
    args = parser.parse_args(argv)
    with session_scope() as session:
        print(show(session, args.ticker, args.canonical, args.period))
    return 0


if __name__ == "__main__":
    sys.exit(main())
