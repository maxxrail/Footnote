"""Hybrid search, RRF, filters, rerank flag, citation resolution, idempotent indexing.

Uses a fake (hashing) embedder and small synthetic filings: no network, no model.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analyst.db.models import Chunk, Company, Filing, FilingSection, FilingText
from analyst.edgar.ingest import document_path
from analyst.retrieval import index as index_mod
from analyst.retrieval.citation import Citation, resolve, verify
from analyst.retrieval.embed import HashEmbedder
from analyst.retrieval.search import rrf, search

SessionFactory = Callable[[], AbstractContextManager[Session]]

# -- RRF (pure) ----------------------------------------------------------------------------


def test_rrf_toy_example() -> None:
    fused = rrf([[1, 2, 3], [3, 1, 4]], k=60)
    expected = {
        1: 1 / 61 + 1 / 62,
        3: 1 / 63 + 1 / 61,
        2: 1 / 62,
        4: 1 / 63,
    }
    assert [i for i, _ in fused] == [1, 3, 2, 4]
    for i, score in fused:
        assert score == pytest.approx(expected[i])


def test_rrf_rewards_agreement_over_one_top_rank() -> None:
    # 7 is 2nd in both lists; 1 is 1st in one list only → 7 wins
    fused = rrf([[1, 7, 2], [3, 7, 4]])
    assert fused[0][0] == 7


def test_rrf_counts_duplicates_once_and_handles_empty() -> None:
    assert rrf([[5, 5, 6]]) == [(5, 1 / 61), (6, 1 / 63)]
    assert rrf([[], []]) == []
    assert rrf([[], [9]]) == [(9, 1 / 61)]


# -- synthetic corpus ----------------------------------------------------------------------

FILLER = " ".join(
    f"Ordinary sentence number {i} about the company and its operations." for i in range(40)
)


def tenk_html(risk: str, mdna: str) -> str:
    return f"""<html><body>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p><p>FORM 10-K</p>
<table><tr><td>Item 1A.</td><td>Risk Factors</td><td>12</td></tr>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>30</td></tr></table>
<p>Item 1. Business</p><p>{FILLER}</p>
<p>Item 1A. Risk Factors</p><p>{risk}</p><p>{FILLER}</p>
<p>Item 7. Management's Discussion and Analysis</p><p>{mdna}</p><p>{FILLER}</p>
<p>Item 8. Financial Statements and Supplementary Data</p><p>{FILLER}</p>
</body></html>"""


def tenq_html(mdna: str) -> str:
    return f"""<html><body><p>FORM 10-Q</p>
<p>PART I — FINANCIAL INFORMATION</p>
<p>Item 1. Financial Statements</p><p>{FILLER}</p>
<p>Item 2. Management's Discussion and Analysis</p><p>{mdna}</p><p>{FILLER}</p>
<p>PART II — OTHER INFORMATION</p>
<p>Item 1A. Risk Factors</p><p>No material changes.</p><p>{FILLER}</p>
</body></html>"""


CORPUS = {
    # accession: (cik, ticker, form, filed, html)
    "0000000001-25-000001": (
        1,
        "CHIP",
        "10-K",
        dt.date(2025, 2, 1),
        tenk_html(
            "Export controls on advanced semiconductors to China could restrict our sales.",
            "Data center revenue grew on strong demand for accelerators.",
        ),
    ),
    "0000000001-24-000001": (
        1,
        "CHIP",
        "10-K",
        dt.date(2024, 2, 1),
        tenk_html(
            "Export controls on semiconductors were first imposed last year.",
            "Gaming revenue declined as channel inventory normalized.",
        ),
    ),
    "0000000002-25-000001": (
        2,
        "BANK",
        "10-K",
        dt.date(2025, 2, 15),
        tenk_html(
            "Deposit outflows and interest rate risk could reduce net interest income.",
            "Net interest margin compressed as deposit costs rose.",
        ),
    ),
    "0000000002-25-000002": (
        2,
        "BANK",
        "10-Q",
        dt.date(2025, 5, 1),
        tenq_html("Tariffs slowed commercial loan growth in the quarter."),
    ),
}


@pytest.fixture
def corpus(session_factory: SessionFactory, tmp_path: Path) -> Path:
    """Companies + filings rows, and their HTML on disk where Part 1 would have put it."""
    raw = tmp_path / "raw"
    with session_factory() as s:
        for cik, ticker in ((1, "CHIP"), (2, "BANK")):
            s.add(Company(cik=cik, ticker=ticker, name=f"{ticker} Corp"))
        s.flush()
        for acc, (cik, _t, form, filed, html) in CORPUS.items():
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
            path = document_path(raw, cik, acc, doc)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
    return raw


@pytest.fixture
def indexed(corpus: Path, session_factory: SessionFactory) -> SessionFactory:
    report = index_mod.run(corpus, session_factory, embedder=HashEmbedder())
    assert report.filings_parsed == len(CORPUS)
    assert report.chunks_embedded == report.chunks_written > 0
    return session_factory


def _search(sf: SessionFactory, query: str, **kw: object) -> list[Citation]:
    with sf() as s:
        return search(query, session=s, embedder=HashEmbedder(), **kw)  # type: ignore[arg-type]


# -- indexing ------------------------------------------------------------------------------


def test_index_is_idempotent(indexed: SessionFactory, corpus: Path) -> None:
    with indexed() as s:
        before = s.scalars(select(Chunk.id).order_by(Chunk.id)).all()
    again = index_mod.run(corpus, indexed, embedder=HashEmbedder())
    assert (again.filings_parsed, again.chunks_written, again.chunks_embedded) == (0, 0, 0)
    with indexed() as s:
        assert s.scalars(select(Chunk.id).order_by(Chunk.id)).all() == before


def test_changed_html_reparses_that_filing_only(indexed: SessionFactory, corpus: Path) -> None:
    acc = "0000000002-25-000002"
    path = document_path(corpus, 2, acc, f"{acc}.htm")
    path.write_text(tenq_html("Tariffs and a new topic: stablecoins."), encoding="utf-8")
    again = index_mod.run(corpus, indexed, embedder=HashEmbedder())
    assert again.filings_parsed == 1
    assert again.chunks_embedded == again.chunks_written > 0
    with indexed() as s:
        text = s.get(FilingText, acc)
        assert text is not None and "stablecoins" in text.text
        # no orphaned chunks from the old text
        for c in s.scalars(select(Chunk).where(Chunk.accession == acc)):
            assert text.text[c.char_start : c.char_end] == c.text


def test_stored_sections_and_chunks_match_text(indexed: SessionFactory) -> None:
    with indexed() as s:
        for ft in s.scalars(select(FilingText)):
            secs = s.scalars(
                select(FilingSection)
                .where(FilingSection.accession == ft.accession)
                .order_by(FilingSection.seq)
            ).all()
            assert secs[0].char_start == 0 and secs[-1].char_end == len(ft.text)
            for c in s.scalars(select(Chunk).where(Chunk.accession == ft.accession)):
                assert ft.text[c.char_start : c.char_end] == c.text
                owner = [x for x in secs if x.char_start <= c.char_start < x.char_end]
                assert owner[0].section == c.section and c.char_end <= owner[0].char_end
        assert s.scalar(select(func.count()).where(Chunk.embedding.is_(None))) == 0


# -- search --------------------------------------------------------------------------------


def test_hybrid_finds_the_right_paragraph(indexed: SessionFactory) -> None:
    hits = _search(indexed, "export controls on semiconductors to China", k=3)
    top = hits[0]
    assert (top.ticker, top.section, top.accession) == ("CHIP", "Item 1A", "0000000001-25-000001")
    assert "Export controls" in top.snippet
    assert top.section_title == "Risk Factors"
    assert top.url.endswith("0000000001-25-000001.htm")
    assert top.company == "CHIP Corp" and top.form == "10-K"


@pytest.mark.parametrize("mode", ["hybrid", "vector", "text"])
def test_filters_are_respected(indexed: SessionFactory, mode: str) -> None:
    q = "revenue risk deposit interest export tariffs"
    for hit in _search(indexed, q, cik=2, mode=mode, k=20):
        assert hit.cik == 2
    for hit in _search(indexed, q, forms=["10-Q"], mode=mode, k=20):
        assert hit.form == "10-Q"
    hits = _search(indexed, q, sections=["Item 1A"], mode=mode, k=20)
    assert hits and all(h.section == "Item 1A" for h in hits)
    for hit in _search(indexed, q, since=dt.date(2025, 2, 10), mode=mode, k=20):
        assert hit.filed_at >= dt.date(2025, 2, 10)
    combo = _search(indexed, q, cik=1, forms=["10-K"], sections=["Item 7"], mode=mode, k=20)
    assert combo and all((h.cik, h.form, h.section) == (1, "10-K", "Item 7") for h in combo)


def test_ticker_filter(indexed: SessionFactory) -> None:
    hits = _search(indexed, "tariffs", ticker="bank", k=5)
    assert hits and {h.ticker for h in hits} == {"BANK"}
    assert _search(indexed, "tariffs", ticker="NOPE") == []
    # ticker and cik that disagree: nothing, rather than silently picking one
    assert _search(indexed, "tariffs", ticker="BANK", cik=1) == []
    assert _search(indexed, "tariffs", ticker="BANK", cik=2)


def test_form_and_section_filters_ignore_case(indexed: SessionFactory) -> None:
    hits = _search(indexed, "tariffs export", forms=["10-q"], sections=["part i, item 2"], k=5)
    assert hits and all((h.form, h.section) == ("10-Q", "Part I, Item 2") for h in hits)


def test_vector_leg_can_use_the_hnsw_index(indexed: SessionFactory) -> None:
    # The test table is tiny, so Postgres would seq-scan anyway; forbid that and check the
    # query's shape lets it use HNSW (a second ORDER BY key would force a sort).
    from sqlalchemy import text

    from analyst.retrieval.search import vector_ids

    captured: list[str] = []
    with indexed() as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_sort = off"))
        real = s.scalars

        def explain(q, *a, **kw):
            sql = str(q.compile(s.get_bind(), compile_kwargs={"literal_binds": True}))
            captured.extend(r[0] for r in s.execute(text("EXPLAIN " + sql)))
            return real(q, *a, **kw)

        s.scalars = explain  # type: ignore[method-assign,assignment]
        ids = vector_ids(s, HashEmbedder().embed_query("export tariffs"), [])
    assert ids
    assert any("ix_chunks_embedding_hnsw" in line for line in captured), captured


def test_tests_cannot_fall_back_to_the_real_db() -> None:
    # no session injected -> default engine -> refused by the conftest guard
    with pytest.raises(RuntimeError, match="default DB engine"):
        search("tariffs", embedder=HashEmbedder())


def test_ten_q_sections_are_part_qualified(indexed: SessionFactory) -> None:
    hits = _search(indexed, "tariffs commercial loan growth", forms=["10-Q"], k=1)
    assert hits[0].section == "Part I, Item 2"


def test_text_leg_weights_rare_terms(indexed: SessionFactory) -> None:
    # "company" and "operations" are in every chunk (FILLER); "tariffs" is in one filing
    hits = _search(indexed, "company operations tariffs", mode="text", k=1)
    assert "Tariffs" in hits[0].snippet


def test_text_leg_idf_beats_term_frequency(indexed: SessionFactory) -> None:
    # Short chunks of a common word vs a long sentence with a rare word once. Plain
    # ts_rank scores them 0.061 vs 0.022 (length-normalised), so without IDF the common
    # word wins; with IDF ("company" is in every chunk) the rare word does.
    acc = "0000000001-25-000001"
    rare = "zeppelins and other airships were mentioned in this long sentence about things"
    with indexed() as s:
        rows = ["company"] * 8 + [rare]
        for i, t in enumerate(rows):
            s.add(
                Chunk(
                    accession=acc,
                    cik=1,
                    section="Item 9",
                    char_start=10**6 + i,
                    char_end=10**6 + i + 1,
                    text=t,
                )
            )
    hits = _search(indexed, "company zeppelins", mode="text", k=1)
    assert "zeppelins" in hits[0].snippet


def test_text_leg_matches_any_term(indexed: SessionFactory) -> None:
    # no chunk has both words: an AND query would return nothing
    hits = _search(indexed, "export tariffs", mode="text", k=10)
    assert {h.ticker for h in hits} == {"CHIP", "BANK"}


def test_text_leg_with_only_stopwords_returns_nothing(indexed: SessionFactory) -> None:
    assert _search(indexed, "the of and", mode="text") == []


def test_k_limits_results(indexed: SessionFactory) -> None:
    assert len(_search(indexed, "revenue", k=2)) == 2


class ReverseReranker:
    """Scores the fused list in reverse, so a rerank visibly changes the order."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls += 1
        return [float(i) for i in range(len(texts))]


def test_rerank_flag(indexed: SessionFactory) -> None:
    rr = ReverseReranker()
    with indexed() as s:
        plain = search("export controls", k=3, session=s, embedder=HashEmbedder(), reranker=rr)
        assert rr.calls == 0  # off by default (RERANK=false)
        reranked = search(
            "export controls", k=3, rerank=True, session=s, embedder=HashEmbedder(), reranker=rr
        )
    assert rr.calls == 1
    assert [h.chunk_id for h in reranked] != [h.chunk_id for h in plain]


# -- citations -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["vector", "text"])
def test_company_name_found_through_the_chunk_header(indexed: SessionFactory, mode: str) -> None:
    # No chunk's text names its company ("CHIP Corp" is only in the header), but a query
    # naming the company must still find that company's chunks, in each leg on its own.
    for name in ("CHIP", "BANK"):
        hits = _search(indexed, f"{name} {name} Corp", mode=mode, k=1)
        assert hits and hits[0].ticker == name, (name, hits)
    with indexed() as s:
        texts = s.scalars(select(Chunk.text)).all()
    assert not any("CHIP" in t for t in texts)


def test_chunk_header_is_indexed_but_never_cited(indexed: SessionFactory) -> None:
    with indexed() as s:
        c = s.scalars(
            select(Chunk)
            .where(Chunk.accession == "0000000001-24-000001", Chunk.section == "Item 1A")
            .limit(1)
        ).one()
        assert c.context == "CHIP Corp (CHIP), 10-K filed 2024-02-01. Item 1A: Risk Factors."
        ft = s.get(FilingText, c.accession)
        assert ft is not None and ft.text[c.char_start : c.char_end] == c.text
        assert c.context not in ft.text
        assert c.indexed_text == f"{c.context}\n\n{c.text}"


def test_company_label_strips_sec_state_suffixes() -> None:
    assert index_mod.company_label("BANK OF AMERICA CORP /DE/") == "BANK OF AMERICA CORP"
    assert index_mod.company_label("US BANCORP \\DE\\") == "US BANCORP"
    assert index_mod.company_label("WELLS FARGO & COMPANY/MN") == "WELLS FARGO & COMPANY"
    assert index_mod.company_label("Apple Inc.") == "Apple Inc."


def test_citation_resolves_to_exact_chunk_text(indexed: SessionFactory) -> None:
    hits = _search(indexed, "net interest margin deposit costs", k=5)
    assert hits
    with indexed() as s:
        for h in hits:
            chunk = s.get(Chunk, h.chunk_id)
            assert chunk is not None
            assert resolve(h, s) == chunk.text
            assert verify(h, s)


def test_tampered_citation_fails_verification(indexed: SessionFactory) -> None:
    h = _search(indexed, "export controls", k=1)[0]
    with indexed() as s:
        assert not verify(h.model_copy(update={"char_start": h.char_start + 5}), s)
        assert not verify(h.model_copy(update={"accession": "0000000009-25-000009"}), s)
        assert not verify(h.model_copy(update={"char_end": 10**9}), s)
        # end moved but still inside the document: the snippet still matches, the hash doesn't
        assert not verify(h.model_copy(update={"char_end": h.char_end + 40}), s)
        assert not verify(h.model_copy(update={"char_end": h.char_end - 1}), s)
        assert verify(h, s)
        with pytest.raises(ValueError, match="past end"):
            resolve(h.model_copy(update={"char_end": 10**9}), s)
        with pytest.raises(LookupError):
            resolve(h.model_copy(update={"accession": "0000000009-25-000009"}), s)
