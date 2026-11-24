"""The eval seed loads the committed corpus into an empty DB, offline and idempotently."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analyst.db.models import Chunk, Company, FilingText
from analyst.edgar.concepts import get_series
from analyst.evals.seed import seed
from analyst.retrieval.citation import Citation, verify
from analyst.retrieval.embed import HashEmbedder
from analyst.retrieval.search import search

SessionFactory = Callable[[], AbstractContextManager[Session]]


def test_seed_loads_corpus_offline_and_is_idempotent(session_factory: SessionFactory) -> None:
    first = seed(session_factory, HashEmbedder())
    assert first.filings_stored == 8 and first.chunks > 500 and first.embedded == first.chunks
    with session_factory() as s:
        tickers = sorted(s.scalars(select(Company.ticker)))
        assert tickers == ["AAPL", "JPM", "MSFT", "NVDA"]
        assert s.scalar(select(func.count()).select_from(FilingText)) == 8
        aapl = s.scalar(select(Company).where(Company.ticker == "AAPL"))
        assert aapl is not None
        rev = {p.fiscal_year: p.value for p in get_series(s, aapl.cik, "revenue", "FY")}
        assert rev[2025] == 416_161_000_000  # matches the full corpus
        hits: list[Citation] = search(
            "iPhone net sales", ticker="AAPL", k=3, session=s, embedder=HashEmbedder()
        )
        assert hits and all(verify(h, s) for h in hits)
    again = seed(session_factory, HashEmbedder())
    assert (again.facts, again.filings, again.filings_stored, again.chunks, again.embedded) == (
        0,
        0,
        0,
        0,
        0,
    )
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Chunk)) == first.chunks
