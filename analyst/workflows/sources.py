"""Finding filings and sections, and turning spans of them into verifiable citations.

Workflows pick their source text by position (the latest 10-K's Item 1, a 10-Q's MD&A)
rather than by search, so they need citations for chunks and for arbitrary spans. Every
citation here resolves with `analyst.retrieval.citation.verify`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analyst.agent.tools import Excerpt
from analyst.db.models import Chunk, Company, Filing, FilingSection, FilingText
from analyst.retrieval.citation import Citation, make_snippet, resolve_span, text_hash
from analyst.retrieval.parse import section_title
from analyst.retrieval.search import chunk_citations

EXCERPT_CHARS = 1500


def latest_filing(s: Session, cik: int, forms: Sequence[str], offset: int = 0) -> Filing | None:
    """The latest filing of these forms that has stored text (`offset` 1 = the one before)."""
    return s.scalars(
        select(Filing)
        .join(FilingText, FilingText.accession == Filing.accession)
        .where(Filing.cik == cik, Filing.form.in_(list(forms)))
        .order_by(Filing.filed_at.desc(), Filing.accession.desc())
        .offset(offset)
        .limit(1)
    ).first()


def latest_filing_with_section(
    s: Session,
    cik: int,
    forms: Sequence[str],
    section: str,
    filed_on_or_before: dt.date | None = None,
) -> Filing | None:
    """The latest filing of these forms that has `section` detected (skips, e.g., a 10-K/A
    that only amends Part III and has no Item 1A)."""
    q = (
        select(Filing)
        .join(FilingSection, FilingSection.accession == Filing.accession)
        .where(Filing.cik == cik, Filing.form.in_(list(forms)), FilingSection.section == section)
    )
    if filed_on_or_before is not None:
        q = q.where(Filing.filed_at <= filed_on_or_before)
    return s.scalars(q.order_by(Filing.filed_at.desc(), Filing.accession.desc()).limit(1)).first()


def section_span(s: Session, accession: str, section: str) -> tuple[int, int] | None:
    """The longest stored span labelled `section` in a filing (a section can be split)."""
    rows = s.execute(
        select(FilingSection.char_start, FilingSection.char_end).where(
            FilingSection.accession == accession, FilingSection.section == section
        )
    ).all()
    if not rows:
        return None
    start, end = max(rows, key=lambda r: r[1] - r[0])
    return int(start), int(end)


def section_text(s: Session, accession: str, section: str) -> tuple[str, int] | None:
    """(text, char_start) of a section, or None if the filing has no such section."""
    span = section_span(s, accession, section)
    if span is None:
        return None
    return resolve_span(s, accession, *span), span[0]


def span_citation(s: Session, accession: str, start: int, end: int, section: str) -> Citation:
    filing, company = s.execute(
        select(Filing, Company)
        .join(Company, Company.cik == Filing.cik)
        .where(Filing.accession == accession)
    ).one()
    text = resolve_span(s, accession, start, end)
    return Citation(
        accession=accession,
        form=filing.form,
        filed_at=filing.filed_at,
        company=company.name,
        ticker=company.ticker,
        cik=company.cik,
        section=section,
        section_title=section_title(section),
        char_start=start,
        char_end=end,
        snippet=make_snippet(text),
        text_sha256=text_hash(text),
        url=filing.url,
    )


def section_chunks(s: Session, accession: str, section: str, limit: int) -> list[Citation]:
    """The first `limit` chunks of a section, in document order, as citations."""
    ids = list(
        s.scalars(
            select(Chunk.id)
            .where(Chunk.accession == accession, Chunk.section == section)
            .order_by(Chunk.char_start)
            .limit(limit)
        )
    )
    return chunk_citations(s, ids)


def to_excerpt(s: Session, c: Citation, chars: int = EXCERPT_CHARS) -> Excerpt:
    text = resolve_span(s, c.accession, c.char_start, c.char_end)
    return Excerpt(
        c,
        {
            "company": c.company,
            "ticker": c.ticker,
            "form": c.form,
            "filed_at": c.filed_at.isoformat(),
            "section": f"{c.section} {c.section_title}",
            "excerpt": text[:chars],
        },
    )


def text_length(s: Session, accession: str) -> int:
    return int(
        s.scalar(select(func.length(FilingText.text)).where(FilingText.accession == accession)) or 0
    )
