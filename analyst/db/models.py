"""ORM models for the financial data layer."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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


# -- Part 2: filing text, sections and chunks for search ----------------------------------

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5


class FilingText(Base):
    """The clean text of one filing's primary document. Every citation span points here."""

    __tablename__ = "filing_texts"

    accession: Mapped[str] = mapped_column(
        ForeignKey("filings.accession", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)
    parser_version: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))  # of the raw HTML


class FilingSection(Base):
    __tablename__ = "filing_sections"

    accession: Mapped[str] = mapped_column(
        ForeignKey("filing_texts.accession", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String(32))  # "Item 1A", "Part I, Item 2", "Cover"
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("uq_chunks_span", "accession", "char_start", "char_end", unique=True),
        Index("ix_chunks_cik_section", "cik", "section"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    accession: Mapped[str] = mapped_column(ForeignKey("filing_texts.accession", ondelete="CASCADE"))
    cik: Mapped[int] = mapped_column(ForeignKey("companies.cik"))
    section: Mapped[str] = mapped_column(String(32))
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Contextual header ("Texas Instruments Inc (TXN), 10-K filed 2025-02-14. Item 1: Business.")
    # indexed and embedded with the chunk, because chunk text says "we", not the company's
    # name. Never part of a citation: `text` alone is the cited span.
    context: Mapped[str] = mapped_column(Text, server_default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embed_model: Mapped[str | None] = mapped_column(String(128))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', context || ' ' || text)", persisted=True)
    )

    @property
    def indexed_text(self) -> str:
        """What the embedder and reranker read: header, blank line, chunk text."""
        return f"{self.context}\n\n{self.text}" if self.context else self.text
