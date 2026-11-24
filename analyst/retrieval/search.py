"""Hybrid search over filing chunks: vector top-50 + full-text top-50 → RRF → (rerank) → k.

    search("supply chain risk", cik=1045810, forms=["10-K"], sections=["Item 1A"], k=8)

- Vector leg: cosine distance on `chunks.embedding`. Ordered by distance alone so the
  planner can use the HNSW index; with `hnsw.iterative_scan = strict_order` a filtered
  query keeps scanning the graph until it has n rows. (With selective filters, e.g. one
  company, Postgres may pick an exact scan instead; both give the same top n.)
- Lexical leg: Postgres full text. Query lexemes are OR-ed (an AND of every word in a
  natural-language question matches almost nothing), and each term's `ts_rank` is
  weighted by its BM25 IDF over the chunk collection, so "tariffs" counts for more than
  "company". Postgres has no BM25; this is the closest cheap equivalent.
- Fusion: Reciprocal Rank Fusion, score = Σ 1 / (RRF_K + rank), rank from 1.
- Optional cross-encoder rerank (`BAAI/bge-reranker-base`) of the fused top candidates,
  behind `rerank=True` or `RERANK=true` in `.env`. It reads the first 512 tokens of each
  chunk, and after a rerank `Citation.score` is the cross-encoder logit, not the RRF score.
- Filters: `forms` and `sections` match case-insensitively ("10-k", "item 1a").
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Literal, Protocol

from sqlalchemy import ColumnElement, Select, Text, and_, func, literal, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.orm import Session

from analyst.config import get_settings
from analyst.db.models import Chunk, Company, Filing
from analyst.db.session import session_scope
from analyst.retrieval.citation import Citation, make_snippet, text_hash
from analyst.retrieval.embed import Embedder, default_embedder
from analyst.retrieval.parse import section_title

RRF_K = 60
CANDIDATES = 50
RERANK_POOL = 30
RERANK_MODEL = "BAAI/bge-reranker-base"

Mode = Literal["hybrid", "vector", "text"]


# -- fusion --------------------------------------------------------------------------------


def rrf(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. Each ranking is a list of ids, best first.

    Returns (id, score) sorted by score descending; ties keep first-seen order.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for rank, doc in enumerate(ranking, start=1):
            if doc in seen:  # a duplicate in one list only counts at its best rank
                continue
            seen.add(doc)
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


# -- rerank --------------------------------------------------------------------------------


class Reranker(Protocol):
    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model_name: str = RERANK_MODEL) -> None:
        self.name = model_name
        self._model: Any = None

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise SystemExit(
                    "sentence-transformers is not installed: run `uv sync --extra dev --extra ml`"
                ) from exc
            self._model = CrossEncoder(self.name, device="cpu", max_length=512)
        return [float(s) for s in self._model.predict([(query, t) for t in texts])]


@lru_cache
def default_reranker() -> Reranker:
    return CrossEncoderReranker()


# -- the two legs --------------------------------------------------------------------------


def _filters(
    cik: int | None,
    forms: Sequence[str] | None,
    sections: Sequence[str] | None,
    since: dt.date | None,
) -> list[ColumnElement[bool]]:
    out: list[ColumnElement[bool]] = []
    if cik is not None:
        out.append(Chunk.cik == cik)
    if forms:
        out.append(func.upper(Filing.form).in_([f.upper() for f in forms]))
    if sections:
        out.append(func.lower(Chunk.section).in_([x.lower() for x in sections]))
    if since is not None:
        out.append(Filing.filed_at >= since)
    return out


def _base(filters: list[ColumnElement[bool]]) -> Select[Any]:
    q = select(Chunk.id).join(Filing, Filing.accession == Chunk.accession)
    return q.where(and_(*filters)) if filters else q


def vector_ids(
    session: Session,
    query_vec: Sequence[float],
    filters: list[ColumnElement[bool]],
    n: int = CANDIDATES,
) -> list[int]:
    # Keep scanning the HNSW graph until n rows pass the filters, in exact distance order.
    # Order by distance only: a second sort key stops Postgres from using the HNSW index.
    session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    session.execute(text(f"SET LOCAL hnsw.ef_search = {max(n * 2, 40)}"))
    q = (
        _base(filters)
        .where(Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(list(query_vec)))
        .limit(n)
    )
    return list(session.scalars(q))


_LEXEME = re.compile(r"'((?:[^']|'')+)'")


def query_lexemes(session: Session, query: str) -> list[str]:
    """Stemmed, stopword-free lexemes of `query` (as tsquery-quoted text, deduplicated).

    Hyphenated words give a compound lexeme plus its parts ('data-cent', 'data', 'center').
    All are kept: each is cast straight to `tsquery`, so the compound stays one lexeme and
    matches the compound `to_tsvector('english')` stored for the same word."""
    tsq = session.scalar(select(func.plainto_tsquery("english", query).cast(Text)))
    seen: dict[str, None] = {}
    for m in _LEXEME.finditer(tsq or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def text_ids(
    session: Session,
    query: str,
    filters: list[ColumnElement[bool]],
    n: int = CANDIDATES,
) -> list[int]:
    lexemes = query_lexemes(session, query)
    if not lexemes:
        return []
    total = session.scalar(select(func.count(Chunk.id))) or 0
    weighted: list[tuple[Any, float]] = []
    for lex in lexemes:
        term = literal(f"'{lex}'").cast(TSQUERY)
        df = session.scalar(select(func.count(Chunk.id)).where(Chunk.tsv.op("@@")(term))) or 0
        if df == 0:
            continue
        idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
        weighted.append((term, idf))
    if not weighted:
        return []
    any_term = literal(" | ".join(f"'{lex}'" for lex in lexemes)).cast(TSQUERY)
    # ts_rank normalisation 1: divide by 1 + log(chunk length)
    rank = sum(
        (func.ts_rank(Chunk.tsv, term, 1) * idf for term, idf in weighted),
        start=literal(0.0),
    )
    q = _base(filters).where(Chunk.tsv.op("@@")(any_term)).order_by(rank.desc(), Chunk.id).limit(n)
    return list(session.scalars(q))


# -- search --------------------------------------------------------------------------------


def _citations(session: Session, ranked: Sequence[tuple[int, float]]) -> list[Citation]:
    if not ranked:
        return []
    ids = [i for i, _ in ranked]
    rows = session.execute(
        select(Chunk, Filing, Company)
        .join(Filing, Filing.accession == Chunk.accession)
        .join(Company, Company.cik == Chunk.cik)
        .where(Chunk.id.in_(ids))
    ).all()
    by_id = {c.id: (c, f, co) for c, f, co in rows}
    out = []
    for cid, score in ranked:
        c, f, co = by_id[cid]
        out.append(
            Citation(
                accession=c.accession,
                form=f.form,
                filed_at=f.filed_at,
                company=co.name,
                ticker=co.ticker,
                cik=co.cik,
                section=c.section,
                section_title=section_title(c.section),
                char_start=c.char_start,
                char_end=c.char_end,
                snippet=make_snippet(c.text),
                text_sha256=text_hash(c.text),
                url=f.url,
                chunk_id=c.id,
                score=score,
            )
        )
    return out


def chunk_citations(session: Session, chunk_ids: Sequence[int]) -> list[Citation]:
    """Citations for given chunks, in the given order (for workflows that pick chunks by
    section rather than by search)."""
    return _citations(session, [(i, 0.0) for i in chunk_ids])


def search(
    query: str,
    cik: int | None = None,
    forms: Sequence[str] | None = None,
    sections: Sequence[str] | None = None,
    since: dt.date | None = None,
    k: int = 8,
    *,
    ticker: str | None = None,
    mode: Mode = "hybrid",
    rerank: bool | None = None,
    session: Session | None = None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> list[Citation]:
    """Top-k chunks for `query` as citations, best first.

    `mode` picks one leg alone ("vector", "text") for evaluation. `rerank` defaults to
    the RERANK setting.
    """
    if session is None:
        with session_scope() as s:
            return search(
                query, cik, forms, sections, since, k,
                ticker=ticker, mode=mode, rerank=rerank, session=s,
                embedder=embedder, reranker=reranker,
            )  # fmt: skip
    if ticker is not None:
        found = session.scalar(select(Company.cik).where(Company.ticker == ticker.upper()))
        if found is None:
            return []
        if cik is not None and cik != found:
            return []
        cik = found
    filters = _filters(cik, forms, sections, since)
    rankings: list[list[int]] = []
    if mode in ("hybrid", "vector"):
        emb = embedder or default_embedder()
        rankings.append(vector_ids(session, emb.embed_query(query), filters))
    if mode in ("hybrid", "text"):
        rankings.append(text_ids(session, query, filters))
    fused = rrf(rankings)

    if rerank is None:
        rerank = get_settings().rerank
    if rerank and fused:
        pool = fused[: max(k, RERANK_POOL)]
        texts = {
            c.id: c.indexed_text
            for c in session.scalars(select(Chunk).where(Chunk.id.in_([i for i, _ in pool])))
        }
        scores = (reranker or default_reranker()).score(query, [texts[i] for i, _ in pool])
        fused = sorted(
            ((i, s) for (i, _), s in zip(pool, scores, strict=True)), key=lambda kv: -kv[1]
        )
    return _citations(session, fused[:k])
