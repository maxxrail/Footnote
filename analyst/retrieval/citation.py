"""A citation into a filing's stored text, and the resolver that verifies it.

`resolve(citation)` returns exactly `filing_texts.text[char_start:char_end]`, so any
citation the agent emits can be checked later (Part 5's citation grader uses it).
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analyst.db.models import FilingText
from analyst.db.session import session_scope

ELLIPSIS = "…"


class Citation(BaseModel):
    accession: str = Field(description="SEC accession number, e.g. 0000320193-25-000079")
    form: str
    filed_at: dt.date
    company: str
    ticker: str
    cik: int
    section: str = Field(description='"Item 1A", "Part I, Item 2", or "Cover"')
    section_title: str
    char_start: int = Field(description="offset into filing_texts.text (0-based, inclusive)")
    char_end: int = Field(description="offset into filing_texts.text (exclusive)")
    snippet: str = Field(description="start of the cited text, possibly cut with an ellipsis")
    url: str = Field(description="the filing's primary document on sec.gov")
    chunk_id: int | None = None
    score: float | None = None


def make_snippet(text: str, limit: int = 300) -> str:
    """The first `limit` chars of `text`, cut at a word boundary, ellipsis if cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + ELLIPSIS


def resolve_span(session: Session, accession: str, char_start: int, char_end: int) -> str:
    if char_start < 0 or char_end < char_start:
        raise ValueError(f"bad span {char_start}:{char_end}")
    # Postgres substr is 1-based and counts characters (code points), like Python slicing.
    row = session.execute(
        select(
            func.substr(FilingText.text, char_start + 1, char_end - char_start),
            func.length(FilingText.text),
        ).where(FilingText.accession == accession)
    ).one_or_none()
    if row is None:
        raise LookupError(f"no stored text for filing {accession}")
    text, length = row
    if char_end > length:
        raise ValueError(f"span {char_start}:{char_end} past end of filing text ({length})")
    return str(text)


def resolve(citation: Citation, session: Session | None = None) -> str:
    """The exact cited text: `filing_texts.text[char_start:char_end]`."""
    if session is not None:
        return resolve_span(session, citation.accession, citation.char_start, citation.char_end)
    with session_scope() as s:
        return resolve_span(s, citation.accession, citation.char_start, citation.char_end)


def verify(citation: Citation, session: Session | None = None) -> bool:
    """True if the span exists and its text starts with the citation's snippet."""
    try:
        text = resolve(citation, session)
    except (LookupError, ValueError):
        return False
    return text.startswith(citation.snippet.removesuffix(ELLIPSIS))
