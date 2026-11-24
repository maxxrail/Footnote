"""A small synthetic company universe with XBRL-style facts and filing text, for agent tests.

CHIP (cik 1, Dec year end) has three fiscal years of facts, three 10-Q quarters of FY2025
revenue (so Q4 is derived) and a 10-K whose MD&A explains the margin change. BANK
(cik 2) lost money in FY2025, so its P/E must be refused. Values are in USD.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from analyst.db.models import Company, Fact, Filing
from analyst.edgar.ingest import document_path

SessionFactory = Callable[[], AbstractContextManager[Session]]

M = 1_000_000
FILLER = " ".join(["The company designs products and sells them worldwide."] * 40)

# (fiscal year) -> canonical concept -> value
CHIP_FY: dict[int, dict[str, Decimal | int]] = {
    2023: {
        "Revenues": 1000 * M,
        "GrossProfit": 600 * M,
        "OperatingIncomeLoss": 200 * M,
        "NetIncomeLoss": 150 * M,
        "EarningsPerShareDiluted": Decimal("1.50"),
        "DepreciationDepletionAndAmortization": 50 * M,
        "LongTermDebt": 300 * M,
        "CashAndCashEquivalentsAtCarryingValue": 100 * M,
        "CommonStockSharesOutstanding": 100 * M,
    },
    2024: {
        "Revenues": 1200 * M,
        "GrossProfit": 750 * M,
        "OperatingIncomeLoss": 300 * M,
        "NetIncomeLoss": 240 * M,
        "EarningsPerShareDiluted": Decimal("2.40"),
        "DepreciationDepletionAndAmortization": 60 * M,
        "LongTermDebt": 300 * M,
        "CashAndCashEquivalentsAtCarryingValue": 200 * M,
        "CommonStockSharesOutstanding": 100 * M,
    },
    2025: {
        "Revenues": 1500 * M,
        "GrossProfit": 960 * M,
        "OperatingIncomeLoss": 450 * M,
        "NetIncomeLoss": 360 * M,
        "EarningsPerShareDiluted": Decimal("3.60"),
        "DepreciationDepletionAndAmortization": 70 * M,
        "LongTermDebt": 250 * M,
        "CashAndCashEquivalentsAtCarryingValue": 400 * M,
        "CommonStockSharesOutstanding": 100 * M,
    },
}
CHIP_Q2025_REVENUE = [340 * M, 360 * M, 380 * M]  # Q1-Q3; Q4 derived = 420M
INSTANT = {
    "LongTermDebt",
    "CashAndCashEquivalentsAtCarryingValue",
    "CommonStockSharesOutstanding",
}
UNITS = {"EarningsPerShareDiluted": "USD/shares", "CommonStockSharesOutstanding": "shares"}

BANK_FY2025: dict[str, Decimal | int] = {
    "Revenues": 800 * M,
    "NetIncomeLoss": -50 * M,
    "EarningsPerShareDiluted": Decimal("-0.50"),
}

MDNA_2025 = (
    "Operating margin expanded to 30 percent because data center demand for our accelerators "
    "outgrew operating expenses, and gross margin benefited from a richer product mix."
)
UNIVERSE_CSV = (
    "ticker,name,peer_group\nCHIP,Chip Corp,semis\nFAB,Fab Inc,semis\nBANK,Bank Co,banks\n"
)


def tenk_html(mdna: str) -> str:
    return f"""<html><body><p>FORM 10-K</p>
<p>Item 1. Business</p><p>{FILLER}</p>
<p>Item 1A. Risk Factors</p><p>Export controls could restrict our sales.</p><p>{FILLER}</p>
<p>Item 7. Management's Discussion and Analysis</p><p>{mdna}</p><p>{FILLER}</p>
<p>Item 8. Financial Statements and Supplementary Data</p><p>{FILLER}</p>
</body></html>"""


def seed(session_factory: SessionFactory, raw: Path | None = None) -> dict[str, int]:
    """Insert companies, filings and facts. Returns {label: fact id} for key facts.
    If `raw` is given, the 10-K HTML is also written where the indexer looks for it."""
    ids: dict[str, int] = {}
    fid = itertools.count(1)
    with session_factory() as s:
        s.add(Company(cik=1, ticker="CHIP", name="Chip Corp", fiscal_year_end="1231"))
        s.add(Company(cik=2, ticker="BANK", name="Bank Co", fiscal_year_end="1231"))
        s.flush()

        def filing(cik: int, acc: str, form: str, filed: dt.date, html: str | None = None) -> None:
            doc = f"{acc}.htm"
            s.add(
                Filing(
                    accession=acc,
                    cik=cik,
                    form=form,
                    filed_at=filed,
                    primary_doc=doc,
                    url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}",
                )
            )
            if raw is not None and html is not None:
                path = document_path(raw, cik, acc, doc)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(html, encoding="utf-8")

        def fact(
            cik: int,
            concept: str,
            value: Decimal | int,
            start: dt.date | None,
            end: dt.date,
            acc: str,
            form: str,
            key: str | None = None,
        ) -> None:
            i = next(fid)
            s.add(
                Fact(
                    id=i,
                    cik=cik,
                    concept=f"us-gaap:{concept}",
                    unit=UNITS.get(concept, "USD"),
                    value=Decimal(value),
                    period_start=start,
                    period_end=end,
                    form=form,
                    accession=acc,
                )
            )
            if key:
                ids[key] = i

        for fy, items in CHIP_FY.items():
            acc = f"0000000001-{(fy + 1) % 100:02d}-000001"
            filing(
                1,
                acc,
                "10-K",
                dt.date(fy + 1, 2, 15),
                tenk_html(MDNA_2025 if fy == 2025 else "Revenue grew."),
            )
            s.flush()
            for concept, value in items.items():
                start = None if concept in INSTANT else dt.date(fy, 1, 1)
                fact(
                    1,
                    concept,
                    value,
                    start,
                    dt.date(fy, 12, 31),
                    acc,
                    "10-K",
                    f"CHIP {concept} FY{fy}",
                )
        quarter_ends = [
            (dt.date(2025, 1, 1), dt.date(2025, 3, 31)),
            (dt.date(2025, 4, 1), dt.date(2025, 6, 30)),
            (dt.date(2025, 7, 1), dt.date(2025, 9, 30)),
        ]
        for n, ((start, end), value) in enumerate(
            zip(quarter_ends, CHIP_Q2025_REVENUE, strict=True), 1
        ):
            acc = f"0000000001-25-00001{n}"
            filing(1, acc, "10-Q", end + dt.timedelta(days=35))
            s.flush()
            fact(1, "Revenues", value, start, end, acc, "10-Q", f"CHIP Revenues FY2025 Q{n}")

        filing(
            2,
            "0000000002-26-000001",
            "10-K",
            dt.date(2026, 2, 20),
            tenk_html("Credit losses rose."),
        )
        s.flush()
        for concept, value in BANK_FY2025.items():
            fact(
                2,
                concept,
                value,
                dt.date(2025, 1, 1),
                dt.date(2025, 12, 31),
                "0000000002-26-000001",
                "10-K",
                f"BANK {concept} FY2025",
            )
    return ids


# FAB (cik 3): a second semis peer with FY2025 facts and a cover-page share count, for comps.
FAB_FY2025: dict[str, Decimal | int] = {
    "Revenues": 800 * M,
    "OperatingIncomeLoss": 160 * M,
    "DepreciationDepletionAndAmortization": 40 * M,
    "NetIncomeLoss": 120 * M,
    "LongTermDebt": 100 * M,
    "CashAndCashEquivalentsAtCarryingValue": 50 * M,
    "Assets": 2000 * M,
}


def seed_peer(session_factory: SessionFactory) -> None:
    with session_factory() as s:
        s.add(Company(cik=3, ticker="FAB", name="Fab Inc", fiscal_year_end="1231"))
        s.flush()
        acc = "0000000003-26-000001"
        s.add(
            Filing(
                accession=acc,
                cik=3,
                form="10-K",
                filed_at=dt.date(2026, 2, 20),
                url=f"https://www.sec.gov/Archives/edgar/data/3/{acc}/{acc}.htm",
            )
        )
        s.flush()
        fid = 900
        for concept, value in FAB_FY2025.items():
            fid += 1
            instant = concept in INSTANT or concept == "Assets"
            s.add(
                Fact(
                    id=fid,
                    cik=3,
                    concept=f"us-gaap:{concept}",
                    unit="USD",
                    value=Decimal(value),
                    period_start=None if instant else dt.date(2025, 1, 1),
                    period_end=dt.date(2025, 12, 31),
                    form="10-K",
                    accession=acc,
                )
            )
        s.add(
            Fact(
                id=fid + 1,
                cik=3,
                concept="dei:EntityCommonStockSharesOutstanding",
                unit="shares",
                value=Decimal(40 * M),
                period_start=None,
                period_end=dt.date(2026, 2, 10),
                form="10-K",
                accession=acc,
            )
        )
