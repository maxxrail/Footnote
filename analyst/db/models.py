"""ORM models for the financial data layer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(Text)
    sic: Mapped[str | None] = mapped_column(String(8))
    sic_description: Mapped[str | None] = mapped_column(Text)
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))  # MMDD, e.g. "0927"


class Filing(Base):
    __tablename__ = "filings"

    accession: Mapped[str] = mapped_column(String(20), primary_key=True)  # 0000320193-24-000123
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik"), index=True)
    form: Mapped[str] = mapped_column(String(32))
    filed_at: Mapped[dt.date] = mapped_column(Date)
    period_of_report: Mapped[dt.date | None] = mapped_column(Date)
    primary_doc: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)


class Fact(Base):
    """One XBRL fact as reported in one filing (the same number restated later is another row)."""

    __tablename__ = "facts"
    __table_args__ = (
        # Instant facts (balance sheet) have no period_start. NULLS NOT DISTINCT (Postgres 15+)
        # makes the key still unique for them, so re-ingesting cannot duplicate them.
        Index(
            "uq_facts_key",
            "cik",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_facts_cik_concept", "cik", "concept"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik"))
    concept: Mapped[str] = mapped_column(String(255))  # "us-gaap:Revenues", "dei:..."
    label: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(64))  # "USD", "USD/shares", "shares"
    value: Mapped[Decimal] = mapped_column(Numeric)
    period_start: Mapped[dt.date | None] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    # fy/fp are those of the *filing* that reported the fact, not of the fact's period.
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    form: Mapped[str] = mapped_column(String(32))
    accession: Mapped[str] = mapped_column(ForeignKey("filings.accession"), index=True)
    frame: Mapped[str | None] = mapped_column(String(16))
