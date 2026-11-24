"""The retrieval gold set is well-formed, and recall@k is computed correctly."""

from __future__ import annotations

import csv
import datetime as dt

import pytest

from analyst.retrieval.citation import Citation
from analyst.retrieval.evaluate import GOLD, GoldQuery, is_hit, load_gold, recall_at_k
from analyst.retrieval.parse import ITEMS_10K, ITEMS_10Q, section_code
from tests.conftest import FIXTURES

VALID_SECTIONS = {section_code(None, i) for i in ITEMS_10K} | {
    section_code(p, i) for p, items in ITEMS_10Q.items() for i in items
}


@pytest.mark.parametrize(
    ("name", "size"),
    [("retrieval_gold.yaml", 15), ("retrieval_holdout.yaml", 10), ("retrieval_holdout2.yaml", 10)],
)
def test_gold_set_is_well_formed(name: str, size: int) -> None:
    gold = load_gold(FIXTURES / name)
    assert GOLD == FIXTURES / "retrieval_gold.yaml"
    assert len(gold) == size
    assert len({g.id for g in gold}) == size
    universe = FIXTURES.parent.parent / "data" / "universe.csv"
    with universe.open(encoding="utf-8") as fh:
        tickers = {row["ticker"] for row in csv.DictReader(fh)}
    for g in gold:
        assert g.ticker in tickers, g.id
        assert g.sections and set(g.sections) <= VALID_SECTIONS, g.id
        assert len(g.query.split()) >= 4


def cite(ticker: str, section: str) -> Citation:
    return Citation(
        accession="0000000001-25-000001",
        form="10-K",
        filed_at=dt.date(2025, 1, 1),
        company=ticker,
        ticker=ticker,
        cik=1,
        section=section,
        section_title=section,
        char_start=0,
        char_end=1,
        snippet="x",
        text_sha256="0" * 64,
        url="u",
    )


def test_recall_at_k() -> None:
    gold = [
        GoldQuery("a", "q a", "AAPL", ("Item 1A",)),
        GoldQuery("b", "q b", "MSFT", ("Item 7", "Part I, Item 2")),
        GoldQuery("c", "q c", "NVDA", ("Item 1A",)),
    ]
    results = {
        "a": [cite("MSFT", "Item 1A"), cite("AAPL", "Item 1A")],  # hit at rank 2
        "b": [cite("MSFT", "Item 1A")] * 5 + [cite("MSFT", "Part I, Item 2")],  # rank 6
        # c: no results at all
    }
    assert recall_at_k(results, gold, k=1) == 0
    assert recall_at_k(results, gold, k=2) == 1 / 3
    assert recall_at_k(results, gold, k=6) == 2 / 3
    # right section, wrong company is not a hit
    assert not is_hit(gold[0], [cite("MSFT", "Item 1A")], 5)
    assert recall_at_k({}, [], 5) == 0.0
